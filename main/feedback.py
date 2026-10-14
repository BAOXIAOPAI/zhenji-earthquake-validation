"""Structured-feedback normalisation and condition application (O1, §4.1).

Extracted from `review.py` so that `prepare_agent` can apply a user's explicit
change to a child run's observation copies BEFORE `mcp_state.initial.json` and the
binding are written — without importing `review` (which imports `prepare_agent`).

Two responsibilities, deliberately separate:

  normalize_change   turn the user's structured change (or a concrete free-text
                     instruction) into a validated, applicable change object.
  apply_conditions   apply it to the child's own observation copies: update the
                     participating set, the target band, the method goal and the
                     diagnostic grid; re-judge the metadata-only input conditions;
                     mark waveform-dependent evidence stale.

`precompute=False` is the default for a real-Agent child: the guard applies the
*conditions* and invalidates derived evidence, but does NOT run the onset or the
stability grid. Those are the agent's own tool calls (book §5.2). `precompute=True`
is for the deterministic reference flow, which has no tools and must recompute.

Every application writes a `condition_application` event with
`decision_actor=deterministic_guard` and an explicit before/after, so "the
condition was applied" is checkable and is never mistaken for an agent decision.
"""
from __future__ import annotations

import copy
import json

import core
import evidence_state
import execution_conditions as EC

CHANGE_KINDS = ("exclude", "include", "band", "stability_bands", "method_goal")


def records_of(state):
    return state.get("records") or state.get("acquired") or []


def resolve_record(state, change):
    """Find the observation a change names, by candidate id or station code."""
    records = records_of(state)
    candidate_id = change.get("candidate_id")
    if candidate_id:
        record = next((r for r in records if r.get("candidate_id") == candidate_id), None)
        if record is not None:
            return record
        raise ValueError(f"候选 {candidate_id} 不在本轮的观测里")
    name = (change.get("station") or "").strip().upper()
    if not name:
        raise ValueError("排除／恢复反馈需要一个台站或候选编号")
    matches = [r for r in records if (r.get("station") or "").upper() == name]
    if not matches:
        raise ValueError(f"本轮观测里没有台站 {name}")
    if len(matches) > 1:
        raise ValueError(f"台站 {name} 有多条观测，请指定候选编号")
    return matches[0]


def normalize_change(state, change, instruction=None):
    """Normalise the feedback into a condition we can actually apply.

    A structured change is preferred; a free-text instruction is only accepted
    when it names something concrete in this run. Anything else raises with the
    reason, so the interface can tell the user rather than guess.
    """
    change = dict(change or {})
    kind = change.get("kind")
    if not kind and instruction:
        text = instruction.strip()
        station = next((r.get("station") for r in records_of(state)
                        if r.get("station") and r["station"].upper() in text.upper()),
                       None)
        import re
        numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
        if station and any(w in text for w in ("排除", "去掉", "不用", "剔除", "exclude")):
            kind, change = "exclude", {"kind": "exclude", "station": station,
                                       "reason": text}
        elif station and any(w in text for w in ("恢复", "加回", "include", "restore")):
            kind, change = "include", {"kind": "include", "station": station}
        elif any(w in text for w in ("垂直", "只看垂直", "vertical", "Z 分量", "Z分量")):
            # C 批次 T1 的反馈形态：改问垂直目标。goal 变化走结转重判，不需要
            # 重新派生波形，因此与旧的 vertical_only（物理删分量）是两件事。
            kind, change = "method_goal", {"kind": "method_goal",
                                           "method_goal": "vertical_onset"}
        elif any(w in text for w in ("三分量", "三通道", "three-component",
                                     "three_component")):
            kind, change = "method_goal", {"kind": "method_goal",
                                           "method_goal":
                                               "three_component_pick"}
        elif len(numbers) >= 2:
            # The target band and the onset diagnostic grid are two different
            # conditions (review-4 B-presentation). Free text naming the grid
            # ("把到时网格收紧到 3–12") must not silently rewrite the target band;
            # naming both is ambiguous and asks for a choice instead.
            gridish = any(w in text for w in ("网格", "到时"))
            bandish = any(w in text for w in ("频带", "band", "Band", "目标"))
            if gridish and bandish:
                raise ValueError(
                    "这句话同时提到目标频带和到时网格；它们是两个独立的条件，"
                    "请只说其中一个，或用页面上的专门按钮")
            if gridish:
                kind, change = "stability_bands", {
                    "kind": "stability_bands",
                    "target_band_hz": [numbers[0], numbers[1]]}
            else:
                kind, change = "band", {"kind": "band",
                                        "target_band_hz": [numbers[0], numbers[1]]}
        else:
            raise ValueError(
                "无法从这句话确定要改的条件；请明确指定要排除的台站，"
                "或给出新的目标频带（如 2–8 Hz），或把到时网格收紧到 3–12 Hz")
    if kind in ("exclude", "include"):
        record = resolve_record(state, change)
        change["candidate_id"] = record["candidate_id"]
        change.setdefault("station", record.get("station"))
        change["reason"] = change.get("reason") or (
            f"用户要求{'排除' if kind == 'exclude' else '恢复'}该观测")
    elif kind == "band":
        band = change.get("target_band_hz") or []
        if len(band) != 2 or not 0 < float(band[0]) < float(band[1]):
            raise ValueError("目标频带需要两个数，且 0 < 下限 < 上限")
        change["target_band_hz"] = EC.normalize_band(band)
    elif kind == "stability_bands":
        # The recommended action of an unstable check, made operable: re-run the
        # onset grid over a declared subset of its bands. This is a *narrower* check
        # and the result says so (`full_grid` becomes false).
        bands = change.get("bands_hz")
        declared = [list(b) for b in core.stability_grid_bands()]
        if bands is None:
            band = change.get("target_band_hz") or []
            if len(band) != 2 or not 0 < float(band[0]) < float(band[1]):
                raise ValueError("收紧到时网格需要一个目标频带（如 3–12 Hz）")
            bands = core.stability_bands_within(band)
            if bands == declared:
                raise ValueError(
                    f"{float(band[0]):g}–{float(band[1]):g} Hz 没有把到时网格收窄："
                    f"网格频带本来就是 {declared}；请改用更窄的频带，"
                    "或直接接受完整网格的不稳定结论")
            change["target_band_hz"] = EC.normalize_band(band)
        # O1: the one validator decides what a legal grid is.
        change["bands_hz"] = EC.normalize_diagnostic_bands(bands, allowed=declared,
                                                           field="bands_hz")
    elif kind == "method_goal":
        goal = change.get("method_goal")
        if goal not in ("vertical_onset", "three_component_pick"):
            raise ValueError(
                f"不支持的方法目标：{goal}（可用：vertical_onset、"
                "three_component_pick）")
        change["method_goal"] = goal
    elif kind == "vertical_only":
        raise ValueError(
            "只看垂直分量需要重新派生仅垂直分量的观测，本版尚未实现；"
            "可以先排除受影响的台站，或改用新的目标频带重算")
    else:
        raise ValueError(f"不支持的反馈类型：{kind}")
    return change


def condition_patch(change: dict) -> dict:
    """Which condition fields this change rewrites (for `from_task`)."""
    if not change:
        return {}
    kind = change.get("kind")
    if kind == "band":
        return {"target_band_hz": list(change["target_band_hz"])}
    if kind == "stability_bands":
        patch = {"diagnostic_bands_hz": [list(b) for b in change["bands_hz"]]}
        # Only the stability grid was named: the user's target band is NOT rewritten
        # by a diagnostic-grid change, even though the free-text parser derived the
        # grid from a band. That derivation is disclosed, not silently promoted.
        return patch
    if kind == "method_goal":
        return {"method_goal": change["method_goal"]}
    if kind == "exclude":
        return {"excluded_candidate_ids": [change["candidate_id"]]}
    if kind == "include":
        return {"excluded_candidate_ids": []}
    return {}


def requested_after(state, change, *, base=None, scope=None) -> dict:
    """The child's requested conditions after the change is applied.

    Acquisition permission for a reuse-based feedback turn is DENIED unless the
    user opened it with an explicit structured permission (`acquisition_allowed` /
    `allow_new_acquisition`). A feedback turn exists because the previous
    observations are being reused; letting it silently keep fetching is what let a
    "don't fetch more" instruction be scored compliant while new requests ran.
    """
    records = records_of(state)
    parent_requested = base or state.get("requested_conditions") or {}
    patch = condition_patch(change)
    excluded = list(parent_requested.get("excluded_candidate_ids") or [])
    if change.get("kind") == "exclude":
        excluded = sorted(set(excluded) | {change["candidate_id"]})
    elif change.get("kind") == "include":
        excluded = sorted(set(excluded) - {change["candidate_id"]})
    diagnostic = patch.get("diagnostic_bands_hz")
    if diagnostic is None:
        diagnostic = parent_requested.get("diagnostic_bands_hz")
    permission = change.get("acquisition_allowed")
    if permission is None:
        permission = change.get("allow_new_acquisition")
    if permission is None:
        # Default for the reuse path: no new fetches. An explicit boolean (either
        # the structured field or the declared change) is the only way to open it.
        permission = False
    return EC.build(
        question_id=parent_requested.get("question_id") or "Q_propagating",
        scope=scope if scope is not None else parent_requested.get("scope"),
        method_goal=patch.get("method_goal") or parent_requested.get("method_goal")
        or "three_component_pick",
        target_band_hz=patch.get("target_band_hz")
        or parent_requested.get("target_band_hz") or [1.0, 12.0],
        onset_method_band_hz=parent_requested.get("onset_method_band_hz") or [1.0, 8.0],
        diagnostic_bands_hz=diagnostic,
        acquisition_allowed=bool(permission),
        excluded_candidate_ids=excluded,
        max_unique_candidates_episode=(parent_requested.get(
            "max_unique_candidates_episode") or 6),
        requested_changes=[c for c in (parent_requested.get("requested_changes") or [])
                           if not _superseded(c, change)] + [change],
    )


def _superseded(prior, change):
    """A later change to the same field replaces the earlier one for that field."""
    a, b = prior.get("kind"), change.get("kind")
    if a == b:
        return True
    if {a, b} <= {"exclude", "include"}:
        return prior.get("candidate_id") == change.get("candidate_id")
    return False


def _analysis_waveform(folder, candidate_id):
    """The waveform the parent actually analysed: its derived variant, else the raw.

    Reading `raw/` unconditionally would silently undo a disclosed demo injection on
    the first feedback round: the child would recompute from the untouched download
    while still claiming the injection applied.
    """
    for kind in ("derived", "raw"):
        path = folder / kind / candidate_id / "waveform.mseed"
        if path.exists():
            return path
    return None


def apply_conditions(change, state, folder, *, requested_before=None,
                     requested_after=None, precompute=False, actor="deterministic_guard",
                     scope=None):
    """Apply the change to this run's observation copies.

    Returns `(recomputed_count, detail)`. No network access: the parent's files
    were copied into this run, so any recomputation is local.
    """
    kind = change["kind"]
    records = records_of(state)
    before = requested_before or state.get("requested_conditions") or {}
    after = requested_after or requested_after_conditions(state, change, base=before,
                                                          scope=scope)
    detail = {"applied": kind, "actor": actor,
              "requested_before": {"condition_id": before.get("condition_id")},
              "requested_after": {"condition_id": after.get("condition_id")}}
    recomputed = 0

    if kind in ("exclude", "include"):
        target = change["candidate_id"]
        record = next((r for r in records if r.get("candidate_id") == target), None)
        if record is None:
            raise ValueError(f"候选 {target} 不在本轮的观测里")
        want_excluded = kind == "exclude"
        if bool(record.get("excluded")) == want_excluded:
            detail.update({"already_applied": True,
                           "candidate_id": target,
                           "note": "该对象已处于目标状态，未重复动作"})
        else:
            record["excluded"] = want_excluded
            record["excluded_by"] = actor
            record["exclusion_reason"] = (change.get("reason")
                                          if want_excluded else None)
            detail.update({"candidate_id": target, "station": record.get("station"),
                           "reason": change.get("reason")})
            exclusions = [e for e in (state.get("exclusions") or [])
                          if e.get("candidate_id") != target]
            if want_excluded:
                exclusions.append({"candidate_id": target,
                                   "station": record.get("station"),
                                   "reason": change.get("reason") or "用户要求排除该观测",
                                   "source": "user_feedback", "actor": actor})
            state["exclusions"] = exclusions

    elif kind == "stability_bands":
        bands = [list(b) for b in change["bands_hz"]]
        per_station = []
        for record in records:
            before = record.get("stability")
            record["stability_bands_hz"] = list(bands)
            per_station.append({"station": record.get("station"),
                                "candidate_id": record.get("candidate_id"),
                                "excluded": bool(record.get("excluded")),
                                "before": (before or {}).get("spread_s"),
                                "state_before": (before or {}).get("stability"),
                                "after": None, "state_after": None})
        detail["bands_hz"] = bands
        detail["note"] = ("到时网格已按当前条件更新；旧网格下算出的稳定性证据标记为失效，"
                          "由后续工具调用按当前网格重算")
        if precompute:
            recomputed = _recompute(state, records, folder, onset=False)
        for row in per_station:
            record = next(r for r in records
                          if r.get("candidate_id") == row["candidate_id"])
            row["after"] = (record.get("stability") or {}).get("spread_s")
            row["state_after"] = (record.get("stability") or {}).get("stability")
        detail["stations"] = per_station

    elif kind == "band":
        band = list(change["target_band_hz"])
        per_station = []
        for record in records:
            before = record.get("band_support")
            record["target_band_hz"] = list(band)
            per_station.append({"station": record.get("station"),
                                "candidate_id": record.get("candidate_id"),
                                "excluded": bool(record.get("excluded")),
                                "before": before, "after": None})
        _rejudge_metadata(records, band)
        detail["target_band_hz"] = band
        detail["note"] = "目标频带已更新；元数据层面的适用性已按新频带重判"
        if precompute:
            recomputed = _recompute(state, records, folder, onset=False)
            for row in per_station:
                record = next(r for r in records
                              if r.get("candidate_id") == row["candidate_id"])
                row["after"] = record.get("band_support")
        else:
            for row in per_station:
                record = next(r for r in records
                              if r.get("candidate_id") == row["candidate_id"])
                row["after"] = record.get("band_support")
        detail["stations"] = per_station

    elif kind == "method_goal":
        goal = change["method_goal"]
        for record in records:
            record["method_applicability_goal"] = goal
        detail["method_goal"] = goal
        detail["note"] = ("方法目标已改；逐站输入条件按新目标重判，"
                          "波形与频带判定结转不变")
        if precompute:
            recomputed = _recompute(state, records, folder, onset=False)

    # Whatever changed, the aggregate summary depends on the participating set and
    # the current facts, so it is never inherited.
    state["requested_conditions"] = after
    stale = evidence_state.invalidate(state, after)
    detail["stale_evidence"] = stale
    detail["recomputed"] = recomputed
    event = {"actor": actor, "change": change, "before": before, "after": after,
             "recomputed": recomputed, "stale_evidence": stale,
             "source": "feedback_guard"}
    state.setdefault("condition_applications", []).append(event)
    return recomputed, detail


def requested_after_conditions(state, change, *, base=None, scope=None):
    return requested_after(state, change, base=base, scope=scope)


def _rejudge_metadata(records, band):
    """Recompute band-support-dependent input conditions from measured metadata.

    This is a local, metadata-only re-judgement the guard is explicitly allowed to
    do (§4.1). No waveform diagnostic is run here.
    """
    for record in records:
        record["band_support"] = core.band_support(record.get("sampling_rate_hz"),
                                                   list(band))
        support = record.get("band_support") or {}
        record["method_applicability"] = {
            "three_component_pick": bool(record.get("three_component_ok")
                                         and record.get("vertical_ok")
                                         and support.get("supported")),
            "vertical_onset": bool(record.get("vertical_ok")
                                   and support.get("supported"))}


def _recompute(state, records, folder, *, onset=True, stability=True):
    """Actually re-run the local waveform diagnostics for the current conditions.

    Used by the deterministic reference flow (`precompute=True`) and by the M arm,
    both of which have no tools and must produce complete evidence. The agent arms
    leave this to their own tool calls.
    """
    requested = state.get("requested_conditions") or {}
    count = 0
    for record in records:
        candidate = {"candidate_id": record["candidate_id"],
                     "network": record.get("network"),
                     "station": record.get("station"),
                     "family": record.get("family") or "HH?"}
        wave = _analysis_waveform(folder, record["candidate_id"])
        meta = folder / "raw" / record["candidate_id"] / "metadata.xml"
        if wave is not None and meta.exists():
            core.reanalyse(candidate, state, record, wave, meta)
            count += 1
        elif record.get("sampling_rate_hz"):
            _rejudge_metadata([record],
                              record.get("target_band_hz")
                              or requested.get("target_band_hz") or [1.0, 12.0])
        if stability:
            record["stability_computed"] = record.get("stability") is not None
        record["onset_computed"] = record.get("onset_utc") is not None
    return count


def precompute_stale(state, folder):
    """Deterministic full recomputation for a flow that has no tools (R, M)."""
    records = records_of(state)
    state.setdefault("records", records)
    return _recompute(state, records, folder)


def recompute_after_change(state, folder, change, *, requested_before=None,
                           requested_after=None, scope=None,
                           actor="deterministic_guard"):
    """Convenience: apply the conditions and precompute, for the reference flows."""
    return apply_conditions(change, state, folder, requested_before=requested_before,
                            requested_after=requested_after, precompute=True,
                            actor=actor, scope=scope)


# Backwards-compatible aliases used by review.py and the historical runner.
normalize = normalize_change
apply_change = apply_conditions