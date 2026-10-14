"""The one execution-condition object shared by A, M, R, feedback and scoring (§4.1).

The round's P0 defect was that the *requested* condition and the *executed* one
were two different things with no shared record: a feedback asked for 3–12 Hz and
for EQUIL to be excluded, and the run still computed the default 12-configuration
grid over an unexcluded station while being scored as complete. Nothing in the
system could compare "what was asked" with "what was actually run".

This module defines that comparison:

  requested_conditions   derived from the frozen task plus the user's structured
                         change. Immutable for the run.
  actual_conditions      derived from this run's own state and real tool events.
                         Never a copy of the request — a copied request would be
                         an assertion, not evidence.

Three band-like quantities are kept apart on purpose and may never stand in for
one another:

  target_band_hz          the band the USER asked about ("1–12 Hz")
  onset_method_band_hz    the band the STA/LTA onset method actually runs in
  diagnostic_bands_hz     the finite grid this round's stability check evaluates

A stability-grid change updates `diagnostic_bands_hz` only. It must not rewrite
`target_band_hz`, because a diagnostic running on a narrowed band does not change
what the user asked about.

Everything here is pure: no network, no model, no run-directory writes. The
`condition_application` event is produced by the caller so the actor
(`deterministic_guard` vs `agent`) is explicit.
"""
from __future__ import annotations

import hashlib
import json
import math

SCHEMA = "seismic-execution-conditions/v1"

# The complete, pre-declared diagnostic grid (mirrors diagnostics.GRID["bands_hz"]).
ALLOWED_DIAGNOSTIC_BANDS = ((1.0, 8.0), (3.0, 12.0), (5.0, 20.0))
DEFAULT_DIAGNOSTIC_BANDS = tuple(ALLOWED_DIAGNOSTIC_BANDS)

SCOPE_KINDS = ("explicit_candidates", "all_allowed", "observed_scope")

# Fields that make up the condition identity. Timestamps, model prose and run ids
# are deliberately absent: two runs asked the same thing share a condition id even
# though their clocks differ.
CANONICAL_FIELDS = (
    "question_id", "required_field", "scope", "method_goal",
    "target_band_hz", "onset_method_band_hz", "diagnostic_bands_hz",
    "acquisition_allowed", "excluded_candidate_ids",
    "max_unique_candidates_episode", "requested_changes",
)

QUESTION_REQUIRED_FIELD = {
    "Q_propagating": "verdict",
    "Q_instrument": "channel_integrity_flags",
    "Q_usable": "method_applicability",
}


class ConditionError(ValueError):
    """A condition the protocol refuses: empty, non-finite, reversed, unpermitted."""


# ------------------------------------------------------------------ primitives
def _finite_pair(band, field):
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        raise ConditionError(f"{field} must be [low, high]")
    try:
        low, high = float(band[0]), float(band[1])
    except (TypeError, ValueError) as exc:
        raise ConditionError(f"{field} has a non-numeric bound") from exc
    if not (math.isfinite(low) and math.isfinite(high)):
        raise ConditionError(f"{field} has a non-finite bound (NaN/inf refused)")
    if not 0 < low < high:
        raise ConditionError(f"{field} must satisfy 0 < low < high")
    return [low, high]


def normalize_band(band, *, field="target_band_hz"):
    return _finite_pair(band, field)


def _same_band(a, b, tol=1e-9):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def normalize_diagnostic_bands(bands, *, field="diagnostic_bands_hz",
                               allowed=ALLOWED_DIAGNOSTIC_BANDS):
    """Validate a diagnostic grid against the pre-declared menu.

    Refuses, explicitly and loudly:
      * an empty list — the bug where `bands_hz=[]` fell through Python truthiness
        and silently became the full default grid;
      * NaN / infinite / non-numeric bounds;
      * reversed bands;
      * duplicate bands;
      * bands outside the permitted set.

    `None` means "the full declared grid" and is the ONLY way to get the default.
    """
    if bands is None:
        return [list(b) for b in allowed]
    if isinstance(bands, (str, bytes)) or not isinstance(bands, (list, tuple)):
        raise ConditionError(f"{field} must be a list of [low, high] bands")
    if len(bands) == 0:
        raise ConditionError(
            f"{field} is an empty grid; refusing to fall back to the default grid")
    out = []
    for band in bands:
        pair = _finite_pair(band, field)
        if not any(_same_band(pair, candidate) for candidate in allowed):
            raise ConditionError(
                f"{field} contains {pair}, which is outside the pre-declared grid "
                f"{[list(b) for b in allowed]}")
        if any(_same_band(pair, seen) for seen in out):
            raise ConditionError(f"{field} contains the duplicate band {pair}")
        out.append(pair)
    return out


def normalize_scope(scope, *, explicit=None):
    """A scope is one of the three frozen kinds, never a bare station list.

    `stations` is the task-level declaration (stable, part of the condition hash);
    `candidate_ids` is the execution-level resolution against the real candidate
    list, which only exists after discovery. The hash covers the declaration, so a
    task and its run state always agree on the condition id even though the run
    state carries the resolved ids.
    """
    if scope is None:
        return {"scope_kind": "all_allowed", "stations": [], "candidate_ids": []}
    if isinstance(scope, str):
        if scope not in SCOPE_KINDS:
            raise ConditionError(f"unknown scope kind: {scope}")
        return {"scope_kind": scope, "stations": [], "candidate_ids": []}
    if not isinstance(scope, dict):
        raise ConditionError("scope must be a kind string or an object")
    kind = scope.get("scope_kind", scope.get("kind"))
    if kind not in SCOPE_KINDS:
        raise ConditionError(f"unknown scope kind: {kind!r}")
    stations = sorted({str(s) for s in (scope.get("stations") or []) if str(s)})
    ids = [str(c) for c in (scope.get("candidate_ids") or list(explicit or [])) if str(c)]
    if kind == "explicit_candidates" and not (stations or ids):
        raise ConditionError(
            "explicit_candidates scope needs at least one station or candidate id")
    return {"scope_kind": kind, "stations": stations, "candidate_ids": ids}


def normalize_candidate_ids(ids, *, field="excluded_candidate_ids"):
    if ids is None:
        return []
    if isinstance(ids, (str, bytes)) or not isinstance(ids, (list, tuple, set)):
        raise ConditionError(f"{field} must be a list of candidate ids")
    seen, out = set(), []
    for value in ids:
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


# --------------------------------------------------------------------- build
def build(*, question_id, scope, method_goal, target_band_hz,
          onset_method_band_hz, diagnostic_bands_hz=None,
          acquisition_allowed=True, excluded_candidate_ids=None,
          max_unique_candidates_episode=6, required_field=None,
          requested_changes=None) -> dict:
    """Build a validated requested-conditions object, id included."""
    if question_id not in QUESTION_REQUIRED_FIELD:
        raise ConditionError(f"unknown question_id: {question_id!r}")
    if method_goal not in ("three_component_pick", "vertical_onset"):
        raise ConditionError(f"unknown method_goal: {method_goal!r}")
    conditions = {
        "schema": SCHEMA,
        "question_id": question_id,
        "required_field": required_field or QUESTION_REQUIRED_FIELD[question_id],
        "scope": normalize_scope(scope),
        "method_goal": method_goal,
        "target_band_hz": normalize_band(target_band_hz),
        "onset_method_band_hz": normalize_band(onset_method_band_hz,
                                               field="onset_method_band_hz"),
        "diagnostic_bands_hz": normalize_diagnostic_bands(diagnostic_bands_hz),
        "acquisition_allowed": bool(acquisition_allowed),
        "excluded_candidate_ids": normalize_candidate_ids(excluded_candidate_ids),
        "max_unique_candidates_episode": int(max_unique_candidates_episode),
        "requested_changes": list(requested_changes or []),
    }
    conditions["condition_id"] = condition_id(conditions)
    return conditions


def canonical_payload(conditions: dict) -> dict:
    """Only the condition-bearing fields, canonically serialisable.

    The scope contributes only its kind and its declared station set. The resolved
    `candidate_ids` are an execution detail: hashing them would make the same
    request produce a different condition id depending on which stations happened
    to be eligible at discovery time.
    """
    payload = {k: conditions.get(k) for k in CANONICAL_FIELDS if k in conditions}
    scope = payload.get("scope") or {}
    payload["scope"] = {"scope_kind": scope.get("scope_kind", scope.get("kind")),
                        "stations": sorted(scope.get("stations") or [])}
    return payload


def canonical_json(conditions: dict) -> str:
    return json.dumps(canonical_payload(conditions), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"), default=str)


def condition_id(conditions: dict) -> str:
    return hashlib.sha256(canonical_json(conditions).encode("utf-8")).hexdigest()


def from_task(task: dict, *, scope=None, change=None, question_id=None,
              diagnostic_bands_hz=None, acquisition_allowed=None,
              excluded_candidate_ids=None, requested_changes=None) -> dict:
    """The requested conditions of a frozen task, optionally narrowed by a change.

    Defaults come from the task; a structured `change` overrides only the fields it
    explicitly names. A grid-only change therefore leaves `method_goal` and
    `target_band_hz` alone.
    """
    change = dict(change or {})
    question = question_id or task.get("question_id")
    if not question:
        try:
            import core  # noqa: PLC0415
            question = core.classify_question(task.get("user_question"))
        except Exception:  # noqa: BLE001 - pure fallback, no core import required
            question = "Q_propagating"
    scope_value = scope if scope is not None else task.get("scope")
    method_goal = change.get("method_goal") or task.get("method_goal") \
        or "three_component_pick"
    target_band = change.get("target_band_hz") or task.get("target_band_hz") \
        or [1.0, 12.0]
    onset_band = task.get("method_band_hz") or [1.0, 8.0]
    diagnostic = diagnostic_bands_hz
    if diagnostic is None:
        diagnostic = change.get("diagnostic_bands_hz")
    if diagnostic is None:
        diagnostic = task.get("diagnostic_bands_hz")
    allowed = acquisition_allowed
    if allowed is None:
        allowed = change.get("acquisition_allowed")
    if allowed is None:
        allowed = task.get("acquisition_allowed", True)
    excluded = excluded_candidate_ids
    if excluded is None:
        excluded = change.get("excluded_candidate_ids")
    if excluded is None:
        excluded = task.get("excluded_candidate_ids") or []
    changes = requested_changes if requested_changes is not None \
        else change.get("requested_changes")
    return build(
        question_id=question, scope=scope_value, method_goal=method_goal,
        target_band_hz=target_band, onset_method_band_hz=onset_band,
        diagnostic_bands_hz=diagnostic, acquisition_allowed=allowed,
        excluded_candidate_ids=excluded,
        max_unique_candidates_episode=task.get("max_unique_candidates_episode", 6),
        requested_changes=changes)


# ------------------------------------------------------------------- actual
ACTUAL_FIELDS = ("condition_id", "question_id", "required_field", "scope",
                 "method_goal", "target_band_hz", "onset_method_band_hz",
                 "diagnostic_bands_hz", "acquisition_allowed",
                 "excluded_candidate_ids", "participating_candidate_ids",
                 "active_candidate_ids", "attempted_candidate_ids_episode")


def actual_from_state(state: dict, *, requested: dict | None = None,
                      participating=None, active=None) -> dict:
    """What this run ACTUALLY executed, read from its own state.

    Every field is taken from observed state:
      * `excluded_candidate_ids` from records that carry the excluded marker;
      * `diagnostic_bands_hz` from the grid the stability check actually used
        (recorded per check as `effective_parameters`), falling back to what the
        request named only when no check ran — and then the field is marked
        `diagnostic_bands_observed: false` so a reader cannot mistake a request for
        a measurement.
    """
    conditions = state.get("requested_conditions") or requested or {}
    records = state.get("acquired") or []
    excluded = [r["candidate_id"] for r in records if r.get("excluded")]
    checks = state.get("stability_checks") or {}
    executed_bands = []
    for entry in checks.values():
        for band in ((entry.get("effective_parameters") or {}).get("bands_hz") or []):
            if not any(_same_band(list(band), dict_band)
                       for dict_band in executed_bands):
                executed_bands.append([float(x) for x in band])
    observed = bool(executed_bands)
    if participating is None:
        participating = [r["candidate_id"] for r in records if not r.get("excluded")]
    if active is None:
        active = list(participating)
    attempted = state.get("attempted_candidate_ids_episode")
    if attempted is None:
        attempted = sorted({r["candidate_id"] for r in records})
    actual = {
        "condition_id": conditions.get("condition_id"),
        "question_id": conditions.get("question_id"),
        "required_field": conditions.get("required_field"),
        "scope": conditions.get("scope"),
        "method_goal": conditions.get("method_goal"),
        "target_band_hz": conditions.get("target_band_hz"),
        "onset_method_band_hz": conditions.get("onset_method_band_hz"),
        "diagnostic_bands_hz": (sorted(executed_bands) if observed
                                else list(conditions.get("diagnostic_bands_hz") or [])),
        "diagnostic_bands_observed": observed,
        "acquisition_allowed": conditions.get("acquisition_allowed"),
        "excluded_candidate_ids": sorted(excluded),
        "participating_candidate_ids": list(participating),
        "active_candidate_ids": list(active),
        "attempted_candidate_ids_episode": list(attempted),
    }
    if requested:
        actual["matches_requested"] = matches(requested, actual)
    return actual


def matches(requested: dict, actual: dict) -> dict:
    """Field-by-field comparison of the requested and executed conditions.

    Returns per-field booleans plus an overall flag. When the user explicitly asked
    for a diagnostic grid, an unobserved grid is reported as `None` — not a match.
    When the grid is only the default and was never asked for, it is not part of the
    fulfilment question and compares as True.
    """
    pairs = {
        "method_goal": (requested.get("method_goal"), actual.get("method_goal")),
        "target_band_hz": (requested.get("target_band_hz"), actual.get("target_band_hz")),
        "onset_method_band_hz": (requested.get("onset_method_band_hz"),
                                 actual.get("onset_method_band_hz")),
        "acquisition_allowed": (requested.get("acquisition_allowed"),
                                actual.get("acquisition_allowed")),
    }
    out = {k: bool(a == b) for k, (a, b) in pairs.items()}
    requested_excluded = sorted(requested.get("excluded_candidate_ids") or [])
    out["excluded_candidate_ids"] = (
        requested_excluded == sorted(actual.get("excluded_candidate_ids") or []))
    explicit_diagnostics = "stability_bands" in explicit_change_kinds(requested)
    if not actual.get("diagnostic_bands_observed"):
        out["diagnostic_bands_hz"] = None if explicit_diagnostics else True
    else:
        want = sorted([list(b) for b in (requested.get("diagnostic_bands_hz") or [])])
        got = sorted([list(b) for b in (actual.get("diagnostic_bands_hz") or [])])
        out["diagnostic_bands_hz"] = bool(want == got)
    out["all"] = all(v is True for v in out.values())
    return out


def explicit_change_kinds(requested: dict) -> set:
    """The change kinds the user actually asked for this round.

    A condition is only an *obligation* when the user named it. The default full
    diagnostic grid is not something the user requested, so a run is not obliged to
    have executed it before it may finish; a user who narrowed the grid is owed that
    specific narrower run.
    """
    kinds = set()
    for change in requested.get("requested_changes") or []:
        if not isinstance(change, dict):
            continue
        if change.get("kind"):
            kinds.add(change["kind"])
        if change.get("diagnostic_bands_hz") or change.get("bands_hz"):
            kinds.add("stability_bands")
        if change.get("method_goal"):
            kinds.add("method_goal")
        if change.get("target_band_hz") and change.get("kind") != "stability_bands":
            kinds.add("band")
    return kinds


# --------------------------------------------------------------- obligations
OBLIGATION_STATES = ("pending", "satisfied", "blocked")


def obligations(requested: dict, state: dict, *, tool_events=None) -> list:
    """Program-computed obligation list. `pending / satisfied / blocked`.

    The states are derived from observed state and real tool events, never from the
    model's prose:

      exclude one object      satisfied when that record carries the excluded mark
      diagnostic on a grid    satisfied when a stability check for an in-scope
                              station recorded that exact grid as effective
      no new acquisition      satisfied when this run issued no acquisition that
                              added a candidate
      re-check under a goal   satisfied when each participating record's
                              `method_applicability` was judged under the current
                              goal and the current target band
    """
    events = list(tool_events or [])
    records = state.get("acquired") or []
    checks = state.get("stability_checks") or {}
    condition_ref = requested.get("condition_id")
    out = []

    for candidate_id in requested.get("excluded_candidate_ids") or []:
        record = next((r for r in records if r.get("candidate_id") == candidate_id), None)
        if record is None:
            out.append({"kind": "exclude_object", "candidate_id": candidate_id,
                        "state": "blocked",
                        "reason": "该对象不在本轮已取得的观测中，无法执行排除"})
        elif record.get("excluded"):
            out.append({"kind": "exclude_object", "candidate_id": candidate_id,
                        "state": "satisfied",
                        "basis": record.get("exclusion_reason"),
                        "actor": record.get("excluded_by") or "deterministic_guard"})
        else:
            out.append({"kind": "exclude_object", "candidate_id": candidate_id,
                        "state": "pending",
                        "reason": "用户明确要求排除，但该记录尚未被标记排除"})

    if not requested.get("acquisition_allowed", True):
        added = [e for e in events
                 if e.get("tool") == "acquire_station" and e.get("phase") == "ok"
                 and (e.get("result") or {}).get("status") not in
                 ("already_acquired", "tool_error", None)]
        out.append({"kind": "no_new_acquisition", "state": "satisfied" if not added
                    else "blocked", "new_acquisitions": len(added),
                    "reason": None if not added else "用户禁止新取数，但发生了新取数"})

    kinds = explicit_change_kinds(requested)
    # An exclusion changes WHICH observations participate, not how the input
    # conditions are judged; only a goal/band change creates the re-check duty.
    goal_kinds = kinds & {"band", "method_goal"}
    in_scope = set(records_by_scope(requested, records))
    if "stability_bands" in kinds:
        grid = {tuple(b) for b in (requested.get("diagnostic_bands_hz") or [])}
        for candidate_id in sorted(in_scope):
            entry = checks.get(candidate_id)
            recorded = {tuple(b) for b in
                        ((entry or {}).get("effective_parameters") or {})
                        .get("bands_hz", [])}
            ref = (entry or {}).get("condition_ref")
            if entry and recorded == grid and ref == condition_ref:
                out.append({"kind": "diagnostic", "candidate_id": candidate_id,
                            "state": "satisfied", "grid": [list(b) for b in recorded],
                            "actor": entry.get("actor") or "agent"})
            elif entry is None:
                out.append({"kind": "diagnostic", "candidate_id": candidate_id,
                            "state": "pending",
                            "reason": "尚未对范围内观测执行本轮诊断网格"})
            else:
                out.append({"kind": "diagnostic", "candidate_id": candidate_id,
                            "state": "blocked" if ref != condition_ref else "pending",
                            "reason": ("已有诊断使用旧条件或其它网格；需要按当前条件重算"
                                       if ref != condition_ref else "网格不完全一致")})

    goal = requested.get("method_goal")
    band = [float(b) for b in (requested.get("target_band_hz") or [])]
    for record in records:
        if record.get("excluded"):
            continue
        applicability = record.get("method_applicability")
        judged_goal = record.get("method_applicability_goal")
        judged_band = (record.get("band_support") or {}).get("target_band_hz")
        if (judged_goal == goal and judged_band
                and len(judged_band) == 2 and _same_band(list(judged_band), band)):
            out.append({"kind": "goal_recheck", "candidate_id": record["candidate_id"],
                        "state": "satisfied"})
        elif not goal_kinds:
            # No condition change this round: acquisition already judged the input
            # conditions under the current goal and band, so there is nothing to
            # re-check. Only a changed goal/band creates the obligation.
            out.append({"kind": "goal_recheck", "candidate_id": record["candidate_id"],
                        "state": "satisfied", "basis": "judged at acquisition"})
        elif applicability is None:
            out.append({"kind": "goal_recheck", "candidate_id": record["candidate_id"],
                        "state": "blocked", "reason": "该记录缺少方法适用性判定"})
        else:
            out.append({"kind": "goal_recheck", "candidate_id": record["candidate_id"],
                        "state": "pending",
                        "reason": "旧目标或旧频带下的适用性判定不能继承"})
    return out


def records_by_scope(requested: dict, records) -> list:
    """Which acquired records are inside the requested scope.

    `observed_scope` and `all_allowed` cover everything actually acquired;
    `explicit_candidates` covers only the named ids.
    """
    scope = requested.get("scope") or {}
    kind = scope.get("scope_kind", scope.get("kind"))
    ids = set(scope.get("candidate_ids") or [])
    if kind == "explicit_candidates":
        return [r["candidate_id"] for r in records if r["candidate_id"] in ids]
    return [r["candidate_id"] for r in records]


def remaining_scope_candidates(requested: dict, state: dict) -> tuple[list, dict]:
    """In-scope candidates this run could still acquire under its current permission.

    An EMPTY result is what licenses `scope_exhausted`: nothing inside the requested
    scope is both untried and still acquirable. A run that has not started, while an
    in-scope candidate is available and fetching is allowed, has permitted actions
    left and may not finish as an unknown. The three legitimate empty cases are kept:
    the user forbade new acquisition, the episode ceiling is reached, or every
    in-scope candidate was already attempted/excluded.
    """
    import resource_limits  # noqa: PLC0415

    discovery = state.get("discovery") or {}
    eligible = [c.get("candidate_id") for c in (discovery.get("eligible") or [])
                if c.get("candidate_id")]
    eligible_set = set(eligible)
    scope = requested.get("scope") or {}
    kind = scope.get("scope_kind", scope.get("kind"))
    if kind == "explicit_candidates":
        in_scope = [c for c in (scope.get("candidate_ids") or []) if c in eligible_set]
    elif kind == "observed_scope":
        # The scope is the set already observed; no new acquisition is in scope.
        in_scope = []
    else:
        in_scope = list(eligible)
    records = state.get("acquired") or []
    attempted = set(state.get("attempted_candidate_ids_episode") or [])
    attempted |= {r.get("candidate_id") for r in records if r.get("candidate_id")}
    excluded = set(requested.get("excluded_candidate_ids") or [])
    untried = [c for c in in_scope if c not in attempted and c not in excluded]
    ceiling = resource_limits.unique_candidate_ceiling(
        requested.get("max_unique_candidates_episode"))
    detail = {"scope_kind": kind, "in_scope": len(in_scope),
              "attempted_episode": len(attempted), "ceiling": ceiling,
              "untried_in_scope": len(untried)}
    if not requested.get("acquisition_allowed", True):
        detail["reason"] = "acquisition_not_allowed"
        return [], detail
    if len(attempted) >= ceiling:
        detail["reason"] = "candidate_ceiling_reached"
        return [], detail
    detail["reason"] = None if not untried else "in_scope_candidates_untried"
    return untried, detail


def obligation_summary(obligations_list) -> dict:
    states = [o.get("state") for o in obligations_list]
    return {"pending": states.count("pending"),
            "satisfied": states.count("satisfied"),
            "blocked": states.count("blocked"),
            "total": len(states)}


# ------------------------------------------------------------------ staleness
# The dependency matrix from the book §4.2. Each derived-evidence kind lists the
# condition fields it depends on; a change confined to another field does not
# invalidate it, so the guard does not re-run everything after one grid edit.
DEPENDENCY_MATRIX = {
    "raw_waveform": (),
    "quality": ("scope",),
    "method_applicability": ("method_goal", "target_band_hz", "scope"),
    "onset": ("onset_method_band_hz", "scope"),
    "stability": ("diagnostic_bands_hz", "scope"),
    "aggregate": ("scope", "excluded_candidate_ids", "method_goal",
                  "target_band_hz"),
}


def changed_fields(before: dict, after: dict) -> list:
    return sorted(k for k in CANONICAL_FIELDS if before.get(k) != after.get(k))


def evidence_is_stale(evidence: dict, current: dict) -> bool:
    """Does this derived item depend on a condition field that changed?"""
    kind = evidence.get("evidence_kind") or "aggregate"
    deps = DEPENDENCY_MATRIX.get(kind, ("scope",))
    ref = evidence.get("condition_ref")
    if not deps:
        return False
    if ref == current.get("condition_id"):
        return False
    recorded = evidence.get("dependency_fields") or {}
    return any(recorded.get(k) != current.get(k) for k in deps)


def mark_stale(state: dict, requested: dict) -> list:
    """Mark derived evidence that the current conditions invalidated.

    History is preserved: the item gains `freshness="stale"` and a reason; it is
    never deleted, and the model can see WHY it lapsed. A stale item can no longer
    support a determined conclusion.
    """
    stale = []
    for bucket in ("derived_evidence_checks", "stability_checks"):
        for key, entry in (state.get(bucket) or {}).items():
            if not isinstance(entry, dict):
                continue
            if evidence_is_stale(entry, requested):
                entry["freshness"] = "stale"
                entry["stale_reason"] = (
                    "该派生证据依赖的条件字段在本轮发生变化，需按当前条件重算")
                entry["condition_ref"] = entry.get("condition_ref")
                stale.append(f"{bucket}:{key}")
    return stale