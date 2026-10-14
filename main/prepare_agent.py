"""Prepare one run's state for the adaptive arm. No model calls, no reference data.

Writes two files under `main/runlog/<run_id>/`:

  mcp_state.json   what the MCP tools may serve. Anonymous: the opaque task id,
                   the user's VERBATIM question, the physical window/region/target
                   band, and the discovered candidates. No case name, no
                   event/quiet label, no injection marker.

  evaluator.json   evaluator-side labels and the injection switch. Never served
                   to the model. The MCP server reads it only to decide whether to
                   derive the injected variant; validation reads it to score.

The split exists because the previous version put `kind`, `split` and
`inject_first` into the same file the MCP server served from, which told the
reasoner the answer (evaluation P0-4).

Usage:
  prepare_agent.py <case_id> [run_id] [question...]
    case_id   demo case used for its window/region (a demo entry point only)
    run_id    optional; a fresh one is allocated when omitted
    question  optional; the user's question. Defaults to the case's preset.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402


class PreparationError(RuntimeError):
    """A preparation failure already saved under its own run id."""

    def __init__(self, run_id, task_id, detail):
        self.run_id, self.task_id, self.detail = run_id, task_id, detail
        super().__init__(detail)


def model_state(run_id, frozen, discovery, *, analysis_level="full") -> dict:
    """The anonymous state the MCP tools serve from.

    Every model-visible task field is copied from `task.model_view` rather than
    hand-listed, so adding a requested condition (O1: `question_id`, `scope`, the
    diagnostic grid, `acquisition_allowed`) cannot be dropped by this one entry
    point while the prompt or the validator still sees it.
    """
    view = taskmod.model_view(frozen)
    state = {k: view.get(k) for k in taskmod.MODEL_VISIBLE_TASK_FIELDS}
    state.update({
        "run_id": run_id,
        "task_id": frozen["task_id"],
        "method_band_hz": list(core.METHOD_BAND),
        "method_applicability_goal": view.get("method_goal"),
        # O1: the request itself, frozen and hashable, and the thin-split switch.
        "requested_conditions": (frozen.get("requested_conditions")
                                 or taskmod.requested_conditions_for(frozen)),
        "analysis_level": analysis_level,
        "exclusions": [],
        "attempted_candidate_ids_episode": [],
        "condition_applications": [],
        "stability_checks": {},
        "derived_evidence_checks": {},
        # The discovery summary the interface reads. The full `eligible` list is
        # kept because the MCP tools and the bridge's candidate allow-list both
        # need the station facts; nothing here is an answer label.
        "discovery": {"n_eligible": discovery["n_eligible"],
                      "eligible": discovery["eligible"]},
        "acquired": [], "requests": 0, "http_attempts": 0,
        "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
    })
    return state


def evaluator_state(frozen):
    """Labels the evaluator keeps; the model must never receive these."""
    return {"task_id": frozen["task_id"],
            "demo_case_ref": frozen.get("demo_case_ref"),
            "acceptance": frozen.get("acceptance"),
            "kind": (frozen.get("acceptance") or {}).get("kind"),
            "split": (frozen.get("acceptance") or {}).get("split"),
            "inject_first": bool((frozen.get("acceptance") or {}).get("injected")),
            # C 批次 T1：固定台站的注入（同一派生副本给三组）。只在 evaluator
            # 侧，与 inject_first 一样绝不经 MCP 提供给模型。
            "inject_station": (frozen.get("acceptance") or {}).get("inject_station"),
            "note": "evaluator-side only; never served through an MCP tool"}


def execution_state(run_id, frozen, discovery, *, acquired=None,
                    parent_run_id=None, extra=None, analysis_level="full") -> dict:
    """The complete execution state for one run, built from its frozen task.

    One constructor for every entry point (deterministic, feedback, AGH), so a
    state can never be missing the window/region/band/method_goal/discovery a
    later turn needs to build a prompt. The third review found the deterministic
    and feedback writers emitting a trimmed state, which made a follow-up fail
    with a missing-band error.
    """
    state = model_state(run_id, frozen, discovery, analysis_level=analysis_level)
    state["acquired"] = list(acquired or [])
    if parent_run_id:
        state["parent_run_id"] = parent_run_id
    state.update(extra or {})
    return state


def resolve_scope(frozen, discovery) -> dict:
    """Resolve the task's declared scope against the real candidate list.

    The declaration travels in the frozen task; the candidate ids only exist after
    discovery, so they are resolved here and recorded next to the declaration. A
    station-list request keeps `explicit_candidates` even when nothing matched —
    the request is what it is, and an empty scope is reported as such rather than
    quietly widened to the whole network (book §5.3).
    """
    import execution_conditions as EC  # noqa: PLC0415
    declared = frozen.get("scope")
    region = frozen.get("region") or {}
    if declared and (declared.get("scope_kind") or declared.get("kind")):
        base = EC.normalize_scope(declared)
    elif region.get("kind") == "stations":
        base = EC.normalize_scope(
            {"scope_kind": "explicit_candidates",
             "stations": [str(s) for s in (region.get("stations") or [])]})
    else:
        base = EC.normalize_scope({"scope_kind": "all_allowed"})
    names = {s.upper() for s in (base.get("stations") or [])}
    eligible = (discovery or {}).get("eligible") or []
    def named(c):
        station = (c.get("station") or "").upper()
        return station in names or f"{c.get('network', '').upper()}.{station}" in names

    ids = [c["candidate_id"] for c in eligible if named(c)]
    resolved = {"scope_kind": base["scope_kind"],
                "stations": list(base.get("stations") or []),
                "candidate_ids": ids}
    if base["scope_kind"] == "explicit_candidates":
        matched = {name for name in names for c in eligible
                   if name in ((c.get("station") or "").upper(),
                               f"{c.get('network', '').upper()}.{(c.get('station') or '').upper()}")}
        resolved["unmatched_stations"] = sorted(names - matched)
    return resolved


def write_state(folder, state) -> None:
    """Write the live execution state in place (the initial snapshot stays put)."""
    (folder / "mcp_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str))


def parent_discovery(parent, frozen) -> dict:
    """Reuse the parent's station metadata, re-scored against the child's band.

    Station discovery is metadata the parent already fetched. Reusing it keeps the
     feedback turn at zero new requests; re-scoring `band_support` against the
    child's target band is what keeps the *physical* spec of the run (and not just
    the task id) consistent with the frozen child task.
    """
    path = parent / "discovery.json"
    if not path.exists():
        raise ValueError("上一轮没有台站发现记录，无法在不新增请求的前提下重算")
    discovery = json.loads(path.read_text())
    band = [float(b) for b in (frozen.get("target_band_hz")
                               or core.DEFAULT_TARGET_BAND)]
    discovery["target_band_hz"] = band
    discovery["method_band_hz"] = list(core.METHOD_BAND)
    for key in ("candidates", "eligible"):
        for candidate in discovery.get(key) or []:
            rate = (candidate.get("band_support") or {}).get("sampling_rate_hz")
            candidate["band_support"] = core.band_support(rate, band)
    return discovery


def prepare(case_id, run_id=None, question=None, target_band=None,
            arm="adaptive_agent_open", method_goal=None,
            operational_standard=None, analysis_level="full") -> dict:
    case = core.case_by_id(case_id)
    frozen = taskmod.task_from_case(case, user_question=question,
                                    target_band_hz=target_band,
                                    method_goal=method_goal,
                                    operational_standard=operational_standard)
    taskmod.freeze(frozen)
    return _finish(case, frozen, run_id, arm, case_id,
                   analysis_level=analysis_level)


def prepare_task(frozen, run_id=None, arm="adaptive_agent_open",
                 analysis_level="full") -> dict:
    """Prepare a session for a task the user wrote, not a preset demo case.

    The task must already be frozen (and therefore already be write-once), so the
    prompt, the tool state and the run record all refer to the same digest. There
    is no demo case reference: this request did not come from one.
    """
    case = taskmod.case_for_task(frozen)
    return _finish(case, frozen, run_id, arm, None, analysis_level=analysis_level)


def _finish(case, frozen, run_id, arm, case_ref, *, analysis_level="full") -> dict:
    run_id = run_id or runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm=arm,
                             extra={"demo_case_ref": case_ref})
    try:
        discovery = core.discover(case, frozen)
    except Exception as exc:  # preserve the run even if discovery never completes
        if isinstance(exc, core.fdsn.OfflineMaterialMissing):
            detail = "离线缓存缺少该任务的台站资料，请查看已有记录或启用联网后重试。模型尚未调用。"
        elif isinstance(exc, HTTPError) and exc.code == 404:
            detail = "公开数据源未找到该台站在所选时窗的通道资料，请检查台站代码与时窗。模型尚未调用。"
        elif isinstance(exc, HTTPError):
            detail = f"公开台站数据源返回 HTTP {exc.code}，请稍后重试。模型尚未调用。"
        elif isinstance(exc, (URLError, TimeoutError)):
            detail = "无法连接公开台站数据源，请检查网络后重试。模型尚未调用。"
        else:
            detail = f"台站资料准备失败（{type(exc).__name__}）。模型尚未调用。"
        runlog.record_failure(run_id, "discovery_failure", detail,
                              error_type=type(exc).__name__)
        runlog.write_cost(run_id, {"known": True, "cost_upper_cny": 0,
                                   "reason": "model_not_started"})
        runlog.outcome(run_id, "failed", failure_kind="discovery_failure",
                       failure_detail=detail, stations_acquired=0,
                       model_started=False, model_cost_known=True)
        raise PreparationError(run_id, frozen["task_id"], detail) from exc
    runlog.write_once(folder / "discovery.json", discovery)
    # The declared scope resolved against the real candidates, recorded beside the
    # declaration. The condition object in the state is then rebuilt from the same
    # frozen fields plus the resolution, so the run's `requested_conditions` is
    # exactly what the request means for this candidate list.
    scope = resolve_scope(frozen, discovery)
    requested = _requested_with_scope(frozen, scope)
    frozen = {**frozen, "requested_conditions": requested}
    runlog.write_once(folder / "scope_resolution.json", scope)
    initial = model_state(run_id, frozen, discovery, analysis_level=analysis_level)
    # The live state mutates during the run; the initial snapshot is write-once so
    # the prompt and the first tools can be tied to what the model actually saw.
    runlog.write_once(folder / "mcp_state.initial.json", initial)
    runlog.write_once(folder / "mcp_state.json", initial)
    runlog.write_once(folder / "evaluator.json",
                      evaluator_state({**frozen, "acceptance":
                                       {**(frozen.get("acceptance") or {}),
                                        "injected": bool(case.get("injected")),
                                        "inject_station": case.get("inject_station")}}))
    return {"run_id": run_id, "task_id": frozen["task_id"],
            "n_eligible": discovery["n_eligible"],
            "user_question": frozen["user_question"],
            "scope": scope, "condition_id": requested.get("condition_id"),
            "analysis_level": analysis_level}


def _requested_with_scope(frozen, scope):
    """Rebuild the requested condition object with the resolved scope."""
    import execution_conditions as EC  # noqa: PLC0415
    base = frozen.get("requested_conditions") or {}
    return EC.build(
        question_id=frozen.get("question_id") or base.get("question_id")
        or _classify(frozen.get("user_question")),
        scope=scope,
        method_goal=frozen.get("method_goal") or "three_component_pick",
        target_band_hz=frozen.get("target_band_hz") or [1.0, 12.0],
        onset_method_band_hz=frozen.get("method_band_hz") or [1.0, 8.0],
        diagnostic_bands_hz=(frozen.get("diagnostic_bands_hz")
                             or base.get("diagnostic_bands_hz")),
        acquisition_allowed=frozen.get("acquisition_allowed", True),
        excluded_candidate_ids=frozen.get("excluded_candidate_ids"),
        max_unique_candidates_episode=frozen.get("max_unique_candidates_episode", 6),
        requested_changes=base.get("requested_changes"))


def _classify(text):
    import execution_conditions  # noqa: PLC0415,F401
    return core.classify_question(text)


def rejudge_carried_records(folder, frozen, acquired) -> list:
    """Re-judge carried records against the CHILD's frozen conditions (A3).

    `band_support`, `method_applicability` and every other target-band-dependent
    field on a carried record were computed under the PARENT's conditions. When
    the child's frozen band differs, the raw waveforms stay reusable (they are
    copied, never re-downloaded) but the derived judgements do not transfer: a
    20 Hz station that cannot carry 1–12 Hz may carry 3–8 Hz, and trusting the
    stale False delivered `pick_not_applicable` on supported data.

    With the stored waveform on disk the full `core.reanalyse` path re-runs (one
    place, one pass). Without files (a synthetic or partially stored record) the
    two band-dependent fields are recomputed directly from the measured sample
    rate — `band_support` is a pure function of rate and band. A record with
    neither files nor a measured rate keeps nothing: its band fields are cleared
    to "not decidable" rather than trusted.
    """
    band = [float(b) for b in (frozen.get("target_band_hz") or core.DEFAULT_TARGET_BAND)]
    rejudged = []
    state = {"run_id": folder.name, "task_id": frozen["task_id"], "task": frozen}
    for record in acquired:
        old_band = record.get("target_band_hz")
        support_band = (record.get("band_support") or {}).get("target_band_hz")
        if (old_band is not None and support_band is not None
                and core._same_band(old_band, band) and core._same_band(support_band, band)):
            continue                      # already judged under these conditions
        record["target_band_hz"] = list(band)
        wave = None
        for kind in ("derived", "raw"):
            candidate_path = folder / kind / record["candidate_id"] / "waveform.mseed"
            if candidate_path.exists():
                wave = candidate_path
                break
        meta = folder / "raw" / record["candidate_id"] / "metadata.xml"
        if (wave is not None and meta.exists()
                and record.get("status") in ("analysed", "no_vertical")):
            candidate = {"candidate_id": record["candidate_id"],
                         "network": record.get("network"),
                         "station": record.get("station"),
                         "family": record.get("family") or "HH?"}
            core.reanalyse(candidate, state, record, wave, meta)
        elif record.get("sampling_rate_hz"):
            record["band_support"] = core.band_support(record["sampling_rate_hz"], band)
            support = record["band_support"]
            record["method_applicability"] = {
                "three_component_pick": bool(record.get("three_component_ok")
                                             and record.get("vertical_ok")
                                             and support["supported"]),
                "vertical_onset": bool(record.get("vertical_ok")
                                       and support["supported"])}
        else:
            # Nothing to re-judge from: the old band judgement must not be reused.
            record["band_support"] = None
            record["method_applicability"] = None
        rejudged.append(record["candidate_id"])
    return rejudged


def _episode_attempts(parent_state) -> list:
    """Independent candidate attempts accumulated across the episode.

    The episode id is the first-round root run. The union starts from every
    ancestor's `attempted_candidate_ids_episode` plus this round's records, so a
    third feedback round cannot reset the count by inheriting only its parent's
    *new* attempts (book §6.2).
    """
    attempted = set(parent_state.get("attempted_candidate_ids_episode") or [])
    attempted |= {r["candidate_id"] for r in (parent_state.get("acquired") or [])}
    attempted |= set(parent_state.get("attempted_candidate_ids") or [])
    return sorted(attempted)


def prepare_followup(parent_run_id, instruction, *,
                     arm="adaptive_agent_followup", target_band_hz=None,
                     method_goal=None, change=None, precompute=False,
                     analysis_level="full") -> dict:
    """A second session that carries the previous observations forward.

    The user has seen the first answer and now asks for something else — typically
    "exclude that bad channel and tell me what changes". The guide requires this to
    reuse the waveforms already obtained rather than fetching again, so the stored
    raw and derived data are copied into the new run directory and the new state
    starts with the same observations.

    It is a *new* run with its own id: the first run's record is never rewritten.
    A free-input parent has no preset case, so nothing here requires one.

    `target_band_hz` carries a changed target band into the new frozen task;
    `method_goal` carries a changed method goal (C 批次 T1：同一波形的坏 E
    副本先按三分量目标、再按垂直目标问)。goal 变化不需要重新派生波形：
    结转记录的 `method_applicability` 本来就按两个目标分别判定
    （`rejudge_carried_records`），判决层按子任务的目标选择。父任务的
    记录一概不动。
    """
    import shutil

    parent = runlog.run_dir(parent_run_id)
    if not (parent / "task.json").exists():
        raise ValueError(f"unknown parent run: {parent_run_id}")
    parent_task = json.loads((parent / "task.json").read_text())
    parent_state = json.loads((parent / "mcp_state.json").read_text())
    parent_eval = (json.loads((parent / "evaluator.json").read_text())
                   if (parent / "evaluator.json").exists() else
                   evaluator_state(parent_task))
    if not parent_state.get("acquired"):
        raise ValueError("the parent run acquired nothing to carry forward")

    import execution_conditions as EC  # noqa: PLC0415
    import feedback  # noqa: PLC0415

    parent_requested = (parent_state.get("requested_conditions")
                        or taskmod.requested_conditions_for(parent_task))
    # The user's explicit change is normalised ONCE, here, so the child's task, its
    # state, its binding and the validator all carry the same condition object.
    change_obj = None
    if change or instruction:
        change_obj = feedback.normalize_change(parent_state, change, instruction)
    if change_obj is None and target_band_hz:
        change_obj = feedback.normalize_change(
            parent_state, {"kind": "band", "target_band_hz": target_band_hz})
    if change_obj is None and method_goal:
        change_obj = feedback.normalize_change(
            parent_state, {"kind": "method_goal", "method_goal": method_goal})

    # The physical spec comes from the CHILD's conditions, not from a copy of the
    # parent's state. The review's P0-2 probe froze the child at 3–8 Hz while the
    # child's MCP state still served the parent's 1–12 Hz.
    probe = {"target_band_hz": (parent_requested.get("target_band_hz")
                                if change_obj is None
                                else feedback.requested_after(
                                    parent_state, change_obj,
                                    base=parent_requested).get("target_band_hz")),
             "method_goal": (parent_task.get("method_goal")
                             or "three_component_pick")}
    discovery = parent_discovery(parent, probe)
    # The parent's declared scope is inherited unless the change alters it: a
    # feedback that excludes an object runs in the SAME scope, and silently
    # widening it to the whole network would answer a different question.
    child_scope = resolve_scope({**parent_task, "scope": parent_task.get("scope")},
                                discovery)
    if change_obj is None:
        # A reuse-based follow-up with no structured change still may not silently
        # keep fetching: the default is no new acquisition, and only an explicit
        # permission on the change object can open it.
        child_requested = _requested_with_scope(
            {**parent_task, "question_id": parent_requested.get("question_id"),
             "target_band_hz": parent_requested.get("target_band_hz"),
             "method_goal": parent_requested.get("method_goal"),
             "diagnostic_bands_hz": parent_requested.get("diagnostic_bands_hz"),
             "method_band_hz": parent_requested.get("onset_method_band_hz"),
             "acquisition_allowed": False,
             "excluded_candidate_ids": parent_requested.get("excluded_candidate_ids")},
            child_scope)
    else:
        child_requested = feedback.requested_after(
            parent_state, change_obj, base=parent_requested, scope=child_scope)

    frozen = taskmod.make_task(
        user_question=parent_task["user_question"],
        window_start=parent_task["window_start"],
        window_end=parent_task["window_end"],
        region=parent_task.get("region"),
        target_band_hz=child_requested["target_band_hz"],
        method_goal=child_requested["method_goal"],
        # 公开操作标准随反馈结转：子任务的判据与父任务一致。
        operational_standard=parent_task.get("operational_standard"),
        demo_case_ref=parent_task.get("demo_case_ref"),
        # O1: the change's condition fields ride into the frozen child task.
        question_id=child_requested["question_id"],
        scope=child_scope,
        diagnostic_bands_hz=child_requested["diagnostic_bands_hz"],
        acquisition_allowed=child_requested["acquisition_allowed"],
        excluded_candidate_ids=child_requested["excluded_candidate_ids"],
        # The child's candidate ceiling must equal the one its state/request carries.
        # Dropping it here left the child TASK at the global default while the MCP
        # guard enforced the parent's smaller value, so the two records disagreed
        # about the same episode limit.
        max_unique_candidates_episode=child_requested["max_unique_candidates_episode"],
        acceptance={**(parent_task.get("acceptance") or {}),
                    "follow_up": instruction, "parent_run_id": parent_run_id,
                    **({"feedback_change": change_obj} if change_obj else {})},
    )
    taskmod.freeze(frozen)
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm=arm,
                            extra={"parent_run_id": parent_run_id,
                                   "demo_case_ref": parent_task.get("demo_case_ref")})
    for kind in ("raw", "derived"):
        source = parent / kind
        if source.exists():
            shutil.copytree(source, folder / kind, dirs_exist_ok=True)

    runlog.write_once(folder / "discovery.json", discovery)
    runlog.write_once(folder / "scope_resolution.json", child_scope)
    # Re-judge every carried record against the CHILD's frozen conditions (A3):
    # the parent's band-dependent judgements (band_support, method_applicability)
    # must not cross into a round whose target band differs. The raw/derived
    # waveforms were just copied into this run's folder, so re-analysis is local.
    acquired = copy.deepcopy(parent_state["acquired"])
    rejudged = rejudge_carried_records(folder, frozen, acquired)
    parent_tool_errors = parent_state.get("tool_errors")
    if isinstance(parent_tool_errors, list):
        parent_tool_errors = len(parent_tool_errors)
    if parent_tool_errors is None:
        parent_tool_errors = (parent_state.get("tool_activity") or {}).get("tool_errors", 0)
    initial = execution_state(
        run_id, frozen, discovery,
        acquired=acquired,
        parent_run_id=parent_run_id,
        extra={
            "follow_up": instruction,
            "change": change_obj,
            "carried_forward": [r["candidate_id"] for r in parent_state["acquired"]],
            # Which carried records were re-judged under THIS round's conditions;
            # the parent's files and summaries stay untouched.
            "rejudged_for_child_conditions": rejudged,
            # Inherited from the parent because they are the observations the
            # feedback reuses, not because they are this round's decisions. Always a
            # list: writing null let a later `setdefault(...).append(...)` crash.
            "exclusions": list(parent_state.get("exclusions") or []),
            "passed_forward": parent_state.get("passed_forward"),
            "attempted_candidate_ids_episode": _episode_attempts(parent_state),
            "analysis_level": analysis_level,
            # History only. These are NOT this round's counters: a parent's failed
            # tool call must never be reported as this run's interface failure.
            "parent_history": {
                "tool_errors": parent_tool_errors,
                "tool_attempts": (parent_state.get("tool_attempts")
                                  or (parent_state.get("tool_activity") or {})
                                  .get("attempts") or {}),
                "requests": parent_state.get("requests", 0),
                "failure_kind": (parent_eval.get("outcome") or {}).get("failure_kind"),
            },
        })
    initial["requested_conditions"] = child_requested
    # Apply the user's explicit change to the CHILD's observation copies, before the
    # initial state and binding are written (book §4.1). `precompute` is False for a
    # real-Agent child: the guard applies conditions and invalidates stale evidence;
    # the onset and grid are then the agent's own tool calls. The deterministic and
    # M flows pass precompute=True because they have no tools.
    applied = None
    if change_obj is not None:
        _, applied = feedback.apply_conditions(
            change_obj, initial, folder,
            requested_before=parent_requested, requested_after=child_requested,
            precompute=precompute, actor=("deterministic_guard" if not precompute
                                          else "deterministic_guard"),
            scope=child_scope)
    else:
        initial["requested_conditions"] = child_requested
    runlog.write_once(folder / "mcp_state.initial.json", initial)
    write_state(folder, initial)
    runlog.write_once(folder / "evaluator.json", {**parent_eval,
                                                  "task_id": frozen["task_id"],
                                                  "parent_run_id": parent_run_id})
    return {"run_id": run_id, "task_id": frozen["task_id"],
            "parent_run_id": parent_run_id, "instruction": instruction,
            "carried_forward": len(parent_state["acquired"]),
            "rejudged_for_child_conditions": rejudged,
            "user_question": frozen["user_question"],
            "target_band_hz": list(frozen.get("target_band_hz") or []),
            "method_goal": frozen.get("method_goal"),
            "scope": child_scope,
            "condition_id": child_requested.get("condition_id"),
            "change": change_obj,
            "applied": applied}


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    case_id = sys.argv[1]
    run_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    question = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else None
    core.fdsn.set_cache(core.CACHE)
    result = prepare(case_id, run_id, question)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
