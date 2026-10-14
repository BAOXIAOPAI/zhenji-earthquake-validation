"""Run-scoped MCP tool server for the adaptive arm.

Serves exactly one run. Reads:

  mcp_state.json    the anonymous live state (task id, verbatim question,
                    physical inputs, discovered candidates, acquisitions)
  evaluator.json    evaluator-side labels and the injection switch. Read ONLY to
                    decide whether to derive the injected variant. Never served.

Disclosure rules (evaluation P0-4): tool returns carry measured facts — station
codes, distances, sample rates, band support and channel conditions such as a
constant channel. They never carry the case name, an event/quiet/artifact label,
the word `injected_artifact`, the derivation record, or any reference answer.

Every tool call is appended to `tool_calls.jsonl` so the prompts, the tool
activity and the result can be tied to one run id. Each tool writes a `start`
line before it does any work and either an `ok` or an `error` line afterwards
(evaluation P0-1): a tool that raises used to leave no trace at all, which made
"the model never asked" and "the interface failed" indistinguishable.

The acquisition spec is built from the frozen task carried in the run state, NOT
from a preset demo case. A free-input task has no `demo_case_ref`, so looking one
up made every user download fail with `KeyError(None)` before the real fetch.
Preset cases are only a demo entry point and an evaluator-side injection switch;
they are not a required parameter of a user tool.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))
sys.path.insert(0, str(ROOT / "prototype"))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
import core  # noqa: E402
import diagnostics  # noqa: E402
import evidence_state  # noqa: E402
import execution_conditions as EC  # noqa: E402
import finish_audit  # noqa: E402
import resource_limits  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

def bind(run_id: str) -> None:
    """Point the tool server at exactly one run.

    Called at launch with the run id on the command line. It is a function so the
    tools can also be exercised directly by a test — an import that demanded argv
    made every tool untestable, which is how a crash inside `exclude_observation`
    reached a paid session before anyone saw it.
    """
    global RUN, FOLDER, STATE, EVALUATOR
    RUN = run_id
    FOLDER = runlog.run_dir(run_id)
    STATE = FOLDER / "mcp_state.json"
    EVALUATOR = FOLDER / "evaluator.json"
    if not STATE.exists():
        raise SystemExit(f"Unknown run: {run_id}")


RUN = ""
FOLDER = ROOT / "main/runlog"
STATE = FOLDER / "mcp_state.json"
EVALUATOR = FOLDER / "evaluator.json"
# Only a real launch binds a run: importing this module (a test, a probe) must not
# consume pytest's argv, which used to raise SystemExit during test collection.
if __name__ == "__main__" and len(sys.argv) > 1:
    bind(sys.argv[1])

# The waveform cache lives under the same relocatable root as the run records.
# Without this the server always hit the network even when the bytes were already
# on disk, so `cache_hits` was silently always zero.
core.fdsn.set_cache(core.CACHE)

# Fields a tool may return for one acquisition. `derivation`,
# `derived_waveform_sha256`, `parent_record_id` and `injected_artifact` are
# deliberately absent: they would disclose that the data were altered.
ACQUISITION_FIELDS = (
    "candidate_id", "station", "network", "status", "acquisition_status",
    "distance_km", "channels", "three_component_ok", "vertical_ok",
    "channel_integrity_flags", "sampling_rate_hz", "band_support",
    "method_applicability", "gaps", "error",
    # Failure attribution. Without these the model only saw the word
    # "metadata_unavailable" and had to guess what it meant; the distinction and
    # the conditions that would unblock it are the useful part of a failed fetch.
    # None of these reveal the injection switch or the reference labels.
    "failed_request", "metadata_available", "waveform_available",
    "distinction", "evidence_preserved", "supplementary_conditions", "next_step",
)

CANDIDATE_FIELDS = ("candidate_id", "distance_km", "analysis_eligible",
                    "three_component", "vertical_only", "band_support", "reason")

server = MCPServer(
    "seismic-review",
    instructions=(
        "You are reviewing one seismic candidate window. Acquire the observations "
        "you need and stop as soon as the evidence is decisive. Reference material "
        "is not available here. Channel conditions you observe are measurements, "
        "not conclusions about what caused the candidate."
    ),
)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load():
    return json.loads(STATE.read_text())


def _save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))


def _evaluator():
    return json.loads(EVALUATOR.read_text()) if EVALUATOR.exists() else {}


def _requested(state):
    """This run's frozen requested conditions, rebuilt from the state if absent."""
    conditions = state.get("requested_conditions")
    if conditions and conditions.get("condition_id"):
        return conditions
    task = taskmod.task_from_state_view(state)
    return task.get("requested_conditions") or taskmod.requested_conditions_for(task)


def _runtime_metrics():
    """The bridge's per-run metering facts. Missing/stale means UNKNOWN, not zero.

    `runtime_metrics.json` is written atomically by the bridge (the model-request
    counter) while this server owns the tool-execution counter. The two processes
    never write the same file, so `task_status` can report both without a race.
    """
    path = FOLDER / "runtime_metrics.json"
    if not path.exists():
        return {"known": False, "reason": "runtime_metrics_missing"}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {"known": False, "reason": "runtime_metrics_unreadable"}
    if data.get("run_id") not in (None, RUN):
        return {"known": False, "reason": "runtime_metrics_other_run"}
    return {"known": True, **data}


def _episode_attempts(state):
    """Independent candidate attempts across the whole episode, never re-set.

    A third feedback round must not restart the count by inheriting only its
    parent's newly added ids (book §6.2).
    """
    return list(state.get("attempted_candidate_ids_episode") or state.get(
        "parent_attempted_candidate_ids") or [])


def _scope_candidate_ids(state):
    scope = (_requested(state).get("scope") or {})
    return list(scope.get("candidate_ids") or scope.get("stations") or [])


def _acquisition_gate(state, candidate_id):
    """Everything that must hold BEFORE any external request is issued.

    Returns (allowed, refusal). A refusal is recorded as a contract event and the
    tool returns it without issuing an HTTP request. The previous code checked the
    permission only in the prompt and in the bridge, so a run could be scored as
    compliant while the model had in fact been allowed to keep fetching.
    """
    requested = _requested(state)
    if not requested.get("acquisition_allowed", True):
        return False, {"status": "refused", "reason": "acquisition_not_allowed",
                       "detail": "用户本轮明确要求不新增取数；只能复用已有观测。",
                       "candidate_id": candidate_id, "http_issued": False}
    limits = resource_limits.limits()
    attempted = set(_episode_attempts(state))
    attempted.add(candidate_id)
    ceiling = resource_limits.unique_candidate_ceiling(
        requested.get("max_unique_candidates_episode"))
    if len(attempted) > ceiling:
        return False, {"status": "refused", "reason": "candidate_ceiling",
                       "detail": (f"本轮独立候选尝试上限为 {ceiling}，"
                                      f"再加该候选会超出。"),
                       "candidate_id": candidate_id,
                       "attempted": len(attempted), "ceiling": ceiling,
                       "http_issued": False}
    scope = requested.get("scope") or {}
    kind = scope.get("scope_kind") or scope.get("kind")
    allowed_ids = set(scope.get("candidate_ids") or [])
    if kind == "explicit_candidates" and candidate_id not in allowed_ids:
        # This also covers the empty resolution: a user who named a station that
        # matched no candidate asked about that station, not about the network.
        return False, {"status": "refused", "reason": "outside_scope",
                       "detail": "该候选不在用户明确指定的范围内。",
                       "candidate_id": candidate_id, "http_issued": False}
    if kind == "observed_scope":
        observed = {r.get("candidate_id") for r in (state.get("acquired") or [])}
        if candidate_id not in observed:
            return False, {"status": "refused", "reason": "outside_scope",
                           "detail": "本轮范围是已观测集合，不能新增其它候选。",
                           "candidate_id": candidate_id, "http_issued": False}
    return True, None


def _record_contract_event(state, event):
    _mutable(state, "contract_events", list).append({"at": _now(), **event})


def _log_event(tool, arguments, phase, *, state=None, result=None, **extra):
    """One line per tool lifecycle event, tied to this run.

    `phase` is `start`, `ok` or `error`. `ok` means the tool ran to completion —
    a completed acquisition that returned no data is still `ok` and carries its
    own `status`/`failed_request`. `error` means the tool itself raised, which is
    an interface failure and must never be reported as "no data".
    """
    entry = {"at": _now(), "run_id": RUN, "tool": tool, "arguments": arguments,
             "phase": phase}
    if result is not None:
        entry["result"] = result
    entry.update(extra)
    runlog.append_jsonl(FOLDER / "tool_calls.jsonl", entry)
    if state is not None:
        counts = _mutable(state, "tool_attempts", dict).setdefault(
            tool, {"start": 0, "ok": 0, "error": 0})
        counts[phase] = counts.get(phase, 0) + 1
        # The same events the JSONL carries, kept in state so `audit_finish` and
        # `task_status` can read them without re-parsing the log.
        _mutable(state, "tool_events", list).append(entry)


def _mutable(state, key, kind):
    """Get `state[key]` as a fresh container, tolerating an explicit null.

    A follow-up state was once written with `exclusions: null`, and `setdefault`
    does not replace an existing None — the tool then crashed *after* it had already
    changed the records, so it reported an error while the change had happened.
    """
    current = state.get(key)
    if not isinstance(current, kind):
        current = kind()
        state[key] = current
    return current


def _record_tool_error(state, tool, arguments, exc):
    """Durable record that a tool raised, so it is never read as a data finding."""
    error = {"at": _now(), "tool": tool, "arguments": arguments,
             "error_type": type(exc).__name__, "error": str(exc)[:300]}
    _mutable(state, "tool_errors", list).append(error)
    return error


def _tool_error_result(tool, exc, *, candidate_id=None):
    """What a raised acquisition tool returns: an interface failure, not evidence.

    Returning a structured record (instead of only an MCP protocol error) lets the
    model see *which layer* failed. The record says the interface failed, which is
    a different situation from "this station recorded nothing" — the distinction
    the previous version collapsed into `not_decidable`.
    """
    result = {"status": "tool_error", "acquisition_status": "tool_error",
              "tool": tool, "error_type": type(exc).__name__,
              "error": str(exc)[:200], "failed_request": "tool",
              "metadata_available": None, "waveform_available": None,
              "distinction": ("取数工具本身执行失败（接口/代码错误），因此本次既没有"
                              "取得数据，也没有测到该台站是否有记录；这不是"
                              "“该台无记录”，也不是“选择不取证”。"),
              "next_step": ("这是接口层失败：如实报告接口错误，不要把它当作观测结论，"
                            "也不要据此回答频带或信号问题。"),
              "tool_error_count": 1}
    if candidate_id is not None:
        result["candidate_id"] = candidate_id
    return result


def _public_candidate(candidate):
    return {k: candidate[k] for k in CANDIDATE_FIELDS if k in candidate}


def _public_acquisition(record):
    return {k: record[k] for k in ACQUISITION_FIELDS if k in record}


def _task_context(state):
    """A case-shaped object built from the frozen task in the run state.

    Only the physical task inputs are used. There is no `core.CASES` lookup, so a
    task the user wrote (which has no demo case) is served exactly like a preset.
    """
    return {"case_id": "user-" + str(state.get("task_id") or "task")[:24],
            "region": state.get("region") or {},
            "start": state.get("window_start"),
            "question_id": None, "kind": "user_task", "split": "user",
            "expect": None, "origin": None}


def _acquisition_state(state):
    """The mutable accounting dict `core.acquire` expects, with this run's task."""
    evaluator = _evaluator()
    runs = {"run_id": RUN, "records": [], "case_id": None,
            "task_id": state["task_id"], "task": state,
            "requests": 0, "http_attempts": 0, "logical_acquisitions": 0,
            "cache_hits": 0, "bytes": 0,
            "inject_first": bool(evaluator.get("inject_first")),
            # C 批次 T1：注入可固定到指定台站，使 R/M/A 三组对照使用同一份
            # 派生坏通道副本，而不是各自“第一个取得”的不同台站。
            "inject_station": evaluator.get("inject_station")}
    if any(record.get("injected") for record in state["acquired"]):
        # The injection applies to the first acquisition that actually accepted it.
        # A station whose derivation failed does not burn the switch: the evaluator
        # still wants a disclosed injected observation in this run.
        runs["_injected"] = True
    return runs


def _tool_failure_summary(state):
    """Tool-level failures, kept separate from acquisition-level "no data"."""
    errors = state.get("tool_errors") or []
    return {"tool_errors": len(errors),
            "tools_with_errors": sorted({e["tool"] for e in errors}),
            "error_types": sorted({e["error_type"] for e in errors}),
            "attempts": state.get("tool_attempts") or {},
            "note": ("tool_errors counts MCP tools that RAISED. It is not evidence "
                     "about the observation: a raised acquire_station means the "
                     "interface failed, while an `ok` result with status "
                     "metadata_unavailable/waveform_unavailable means the request "
                     "was made and returned no data.")}


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_candidates(limit: int = 20) -> dict:
    """List the nearest analysis-eligible stations for this window. Free: no requests."""
    state = _load()
    _log_event("list_candidates", {"limit": limit}, "start", state=state)
    try:
        eligible = state["discovery"]["eligible"]
        limit = max(1, min(int(limit), 40))
        shown = eligible[:limit]
        result = {"task_id": state["task_id"], "question": state["user_question"],
                  "window_start": state["window_start"], "window_end": state["window_end"],
                  "target_band_hz": state["target_band_hz"],
                  "n_eligible": state["discovery"]["n_eligible"],
                  "shown": len(shown), "truncated": len(eligible) > len(shown),
                  "candidates": [_public_candidate(c) for c in shown],
                  "note": "Only analysis-eligible candidates, nearest first. "
                          "Each acquisition costs 2 external requests."}
    except Exception as exc:  # noqa: BLE001
        _log_event("list_candidates", {"limit": limit}, "error", state=state,
                   error_type=type(exc).__name__, error=str(exc)[:300])
        _record_tool_error(state, "list_candidates", {"limit": limit}, exc)
        _save(state)
        raise
    _log_event("list_candidates", {"limit": limit}, "ok", state=state,
               result={"shown": len(shown)})
    _save(state)
    return result


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def acquire_station(candidate_id: str) -> dict:
    """Download, hash and validate one station observation. Costs 2 external requests.
    Returns channel conditions only — no onset times."""
    state = _load()
    _log_event("acquire_station", {"candidate_id": candidate_id}, "start", state=state)
    if any(r["candidate_id"] == candidate_id for r in state["acquired"]):
        _log_event("acquire_station", {"candidate_id": candidate_id}, "ok",
                   state=state, result={"status": "already_acquired"})
        _save(state)
        return {"status": "already_acquired", "candidate_id": candidate_id}
    candidate = next((c for c in state["discovery"]["eligible"]
                      if c["candidate_id"] == candidate_id), None)
    if candidate is None:
        exc = ValueError("Candidate outside this run's eligible list")
        _log_event("acquire_station", {"candidate_id": candidate_id}, "error",
                   state=state, error_type="ValueError", error=str(exc))
        _record_tool_error(state, "acquire_station", {"candidate_id": candidate_id}, exc)
        _save(state)
        raise exc
    # Permission, scope and the episode candidate ceiling are checked BEFORE any
    # external request. A refusal issues no HTTP and is recorded as a contract
    # event, so "the model proposed it" and "the run did it" stay separate.
    allowed, refusal = _acquisition_gate(state, candidate_id)
    if not allowed:
        _record_contract_event(state, {"kind": "acquisition_refused",
                                       "tool": "acquire_station", **refusal})
        _log_event("acquire_station", {"candidate_id": candidate_id}, "ok",
                   state=state, result=refusal)
        _save(state)
        return refusal
    # The attempt is registered before the I/O: a candidate that fails still counts
    # against the episode ceiling and is never re-tried as if it were new.
    attempts = _mutable(state, "attempted_candidate_ids_episode", list)
    if candidate_id not in attempts:
        attempts.append(candidate_id)
    analysis_level = state.get("analysis_level") or "full"
    try:
        runs = _acquisition_state(state)
        record = core.acquire(_task_context(state), candidate, runs,
                              analysis_level=analysis_level)
    except Exception as exc:  # noqa: BLE001
        # The pre-fix code raised KeyError(None) here for every free-input task and
        # left no tool_calls line at all. Record it, count it, and hand the model an
        # explicit interface-failure record instead of a silent stop.
        _log_event("acquire_station", {"candidate_id": candidate_id}, "error",
                   state=state, error_type=type(exc).__name__, error=str(exc)[:300])
        _record_tool_error(state, "acquire_station", {"candidate_id": candidate_id}, exc)
        _save(state)
        return _tool_error_result("acquire_station", exc, candidate_id=candidate_id)

    state["requests"] += runs.get("requests", 0)
    state["http_attempts"] += runs.get("http_attempts", 0)
    state["logical_acquisitions"] += runs.get("logical_acquisitions", 0)
    state["cache_hits"] += runs.get("cache_hits", 0)
    state["bytes"] += runs.get("bytes", 0)
    state["acquired"].append(record)
    # Quality and method-applicability facts are measured at acquisition, so they
    # are registered as first-class evidence ids here. That is what lets a
    # quality/applicability question be answered (and cited) WITHOUT computing an
    # onset it does not need.
    requested_now = _requested(state)
    if evidence_state.quality_available(record):
        evidence_state.register(
            state, kind="quality", evidence_id=f"quality:{record['candidate_id']}",
            requested=requested_now,
            effective_parameters={
                "status": record.get("status"),
                "channel_integrity_flags": record.get("channel_integrity_flags"),
                "sampling_rate_hz": record.get("sampling_rate_hz")},
            source_sha256=evidence_state.source_sha(record),
            participating=[record["candidate_id"]], check_status="ok",
            tool_event_ref=f"acquire_station:{record['candidate_id']}", actor="agent")
    if record.get("method_applicability"):
        evidence_state.register(
            state, kind="method_applicability",
            evidence_id=f"applicability:{record['candidate_id']}",
            requested=requested_now,
            effective_parameters={"method_goal": requested_now.get("method_goal"),
                                  "target_band_hz": record.get("target_band_hz")},
            source_sha256=evidence_state.source_sha(record),
            participating=[record["candidate_id"]], check_status="ok",
            tool_event_ref=f"acquire_station:{record['candidate_id']}", actor="agent")
    # Record what this acquisition actually computed, so the O2 free gate can read
    # a real count instead of trusting that dropping a tool name meant dropping the
    # computation.
    _mutable(state, "computation_counts", dict)
    counts = state["computation_counts"]
    counts["acquisitions"] = counts.get("acquisitions", 0) + 1
    if record.get("onset_computed"):
        counts["onsets_at_acquire"] = counts.get("onsets_at_acquire", 0) + 1
    if record.get("stability_computed"):
        counts["stability_at_acquire"] = counts.get("stability_at_acquire", 0) + 1
    public = _public_acquisition(record)
    # Log BEFORE saving: `_log_event` also increments this round's persisted
    # tool_attempts counters, so saving first dropped every `ok` count from the
    # state while the JSONL carried it (review-4 A5).
    _log_event("acquire_station", {"candidate_id": candidate_id}, "ok", state=state,
               result={"station": record.get("station"), "status": record.get("status"),
                       "failed_request": record.get("failed_request"),
                       "metadata_available": record.get("metadata_available"),
                       "waveform_available": record.get("waveform_available")})
    _save(state)
    return public


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def run_onset_check(candidate_id: str) -> dict:
    """Compute the first STA/LTA onset for an already acquired station. Local, costs no requests."""
    state = _load()
    _log_event("run_onset_check", {"candidate_id": candidate_id}, "start", state=state)
    record = next((r for r in state["acquired"] if r["candidate_id"] == candidate_id), None)
    if record is None:
        exc = ValueError("Acquire the station before running a check on it")
        _log_event("run_onset_check", {"candidate_id": candidate_id}, "error",
                   state=state, error_type="ValueError", error=str(exc))
        _record_tool_error(state, "run_onset_check", {"candidate_id": candidate_id}, exc)
        _save(state)
        raise exc
    requested = _requested(state)
    counts = _mutable(state, "computation_counts", dict)
    source = "cache"
    # O2: the onset is genuinely computed here unless this condition's value is
    # already present. The previous tool only read `record["onset_utc"]`, which had
    # been computed during acquisition — so "the agent chose to run the onset check"
    # did not correspond to any computation.
    condition_ref = requested.get("condition_id")
    if (record.get("onset_computed") is True
            and record.get("onset_condition_ref") == condition_ref):
        source = "cache"
    else:
        path = _stored_waveform(candidate_id)
        result = core.compute_onset(record, path) if path else {
            "computed": False, "reason": "no_stored_waveform", "onset_utc": None}
        record["onset_utc"] = result.get("onset_utc")
        record["onset_computed"] = bool(result.get("computed"))
        record["onset_source"] = "compute" if result.get("computed") else result.get("reason")
        record["onset_condition_ref"] = condition_ref
        record["onset_band_hz"] = result.get("band_hz")
        source = "compute" if result.get("computed") else result.get("reason")
        if result.get("computed"):
            counts["onsets_computed"] = counts.get("onsets_computed", 0) + 1
    evidence_state.register(
        state, kind="onset", evidence_id=f"onset:{candidate_id}",
        requested=requested,
        effective_parameters={"band_hz": record.get("onset_band_hz")
                              or list(record.get("method_band_hz") or core.METHOD_BAND),
                              "algorithm": "stalta_z"},
        source_sha256=evidence_state.source_sha(record),
        participating=[candidate_id], check_status=("ok" if record.get("onset_computed")
                                                    else "no_onset"),
        tool_event_ref=f"run_onset_check:{candidate_id}", actor="agent")
    result = {"candidate_id": candidate_id, "station": record["station"],
              "status": record.get("status"), "onset_utc": record.get("onset_utc"),
              "band_hz": record.get("onset_band_hz") or record.get("method_band_hz"),
              "onset_source": source,
              "onset_computed": bool(record.get("onset_computed")),
              "condition_id": condition_ref,
              "channel_integrity_flags": record.get("channel_integrity_flags", [])}
    _log_event("run_onset_check", {"candidate_id": candidate_id}, "ok", state=state,
               result={"onset_utc": record.get("onset_utc"), "source": source})
    _save(state)
    return result


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_evidence() -> dict:
    """Current evidence: which stations were acquired, the onsets found, and any channel flags.

    This returns raw evidence only. Whether it is decisive is your judgement; the
    decision rule you will be scored against is not shown here.
    """
    state = _load()
    _log_event("get_evidence", {}, "start", state=state)
    records = state["acquired"]
    requested = _requested(state)
    actual = EC.actual_from_state(state, requested=requested)
    validity = evidence_state.validity(state, requested)
    onsets = [{"station": r["station"], "onset_utc": r.get("onset_utc")}
              for r in records if r.get("onset_utc") is not None]
    # An unchecked station is `pending`, not "no onset": the two must never merge.
    onset_pending = [r["station"] for r in records
                     if not r.get("onset_computed")]
    failed = [{"station": r.get("station"), "status": r.get("status"),
               "failed_request": r.get("failed_request"),
               "error": r.get("error")}
              for r in records if r.get("status") not in ("analysed",)]
    result = {"question": state["user_question"],
              "target_band_hz": state["target_band_hz"],
              # The frozen analysis goal, disclosed like the band: the model judges
              # input conditions against THIS, and so does the final validator.
              "method_goal": state.get("method_goal"),
              "method_goal_label": core.METHOD_GOAL_LABELS.get(
                  state.get("method_goal"), state.get("method_goal")),
              # The method's own fixed band (the picker's operating range), so the
              # goal, the asked band and the method band are three distinct,
              # disclosed numbers instead of one ambiguous "band".
              "method_band_hz": state.get("method_band_hz") or list(core.METHOD_BAND),
              # O1: the requested and the actually-executed condition, side by side.
              "requested_conditions": requested,
              "actual_conditions": actual,
              "conditions_match": actual.get("matches_requested"),
              "scope": requested.get("scope"),
              "diagnostic_bands_hz": requested.get("diagnostic_bands_hz"),
              "acquisition_allowed": requested.get("acquisition_allowed"),
              "stale_evidence_ids": validity["stale_ids"],
              "valid_evidence_ids": validity["valid_ids"],
              "obligations": EC.obligations(requested, state,
                                            tool_events=state.get("tool_events")),
              "stations_acquired": len(records),
              "stations_analysed": sum(1 for r in records if r.get("status") == "analysed"),
              "stations_returning_no_data": [r["station"] for r in records
                                             if r.get("status") != "analysed"],
              "failed_requests": failed,
              "onsets": onsets,
              "onsets_pending": onset_pending,
              "channel_integrity_flags": [f for r in records
                                          for f in (r.get("channel_integrity_flags") or [])],
              "band_support": [{"station": r["station"], "band_support": r.get("band_support")}
                               for r in records],
              "evidence_ids": {
                  "per_station": [{"candidate_id": r["candidate_id"],
                                   "station": r.get("station"),
                                   "onset": f"onset:{r['candidate_id']}"
                                   if r.get("onset_computed") else None,
                                   "stability": f"stability:{r['candidate_id']}"
                                   if r["candidate_id"] in (state.get("stability_checks")
                                                            or {}) else None,
                                   # Facts measured at acquisition: citable without
                                   # forcing an unnecessary onset computation.
                                   "quality": f"quality:{r['candidate_id']}"
                                   if evidence_state.quality_available(r) else None,
                                   "applicability": f"applicability:{r['candidate_id']}"
                                   if r.get("method_applicability") else None}
                                  for r in records],
                  "valid": validity["valid_ids"], "stale": validity["stale_ids"]},
              # Kept apart on purpose: a raised tool is an interface failure, a
              # completed request that returned nothing is a data fact.
              **_tool_failure_summary(state),
              "requests_spent": state["requests"],
              "candidates_remaining": state["discovery"]["n_eligible"] - len(records)}
    _log_event("get_evidence", {}, "ok", state=state,
               result={"stations_acquired": len(records)})
    _save(state)
    return result


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def task_status(stage: str = "progress") -> dict:
    """Report progress facts and finish eligibility. Free, local, no requests.

    It reports facts, the public task obligations and whether the run may finish.
    It never returns the final verdict, never writes the model's action sequence and
    never performs a missing check on the model's behalf. Station facts already
    measured (distance, channels, sample rate, access outcome) are included; test
    labels ("this station is the quiet one") are not, because they do not exist
    here.
    """
    state = _load()
    if stage not in ("progress", "finish"):
        raise ValueError("stage must be 'progress' or 'finish'")
    _log_event("task_status", {"stage": stage}, "start", state=state)
    requested = _requested(state)
    records = state["acquired"]
    actual = EC.actual_from_state(state, requested=requested)
    validity = evidence_state.validity(state, requested)
    obligations_list = EC.obligations(requested, state,
                                      tool_events=state.get("tool_events"))
    attempts = _episode_attempts(state)
    eligible = state["discovery"]["eligible"]
    remaining_allowed = [c["candidate_id"] for c in eligible
                         if c["candidate_id"] not in set(attempts)]
    metrics = _runtime_metrics()
    counts = state.get("computation_counts") or {}
    tool_calls = sum((c.get("start") or 0)
                     for c in (state.get("tool_attempts") or {}).values())
    remaining = resource_limits.remaining_limits(
        unique_candidates_used=len(attempts),
        model_requests_used=metrics.get("model_requests") or 0,
        tool_calls_used=tool_calls,
        user_requested_candidates=requested.get("max_unique_candidates_episode"),
        model_requests_known=bool(metrics.get("known")),
        tool_calls_known=True)
    audit = None
    if stage == "finish":
        audit = finish_audit.audit_finish(state, tool_events=state.get("tool_events"))
    result = {
        "stage": stage,
        "condition_id": requested.get("condition_id"),
        "required_field": requested.get("required_field"),
        "question_id": requested.get("question_id"),
        "scope": requested.get("scope"),
        "active_candidate_ids": actual.get("active_candidate_ids"),
        "excluded_candidate_ids": actual.get("excluded_candidate_ids"),
        "attempted_candidate_ids_episode": attempts,
        "remaining_allowed_candidates": remaining_allowed,
        "remaining_limits": remaining,
        "obligations": obligations_list,
        "stale_evidence": validity["stale_ids"],
        "valid_evidence": validity["valid_ids"],
        "checks_available": ["list_candidates", "acquire_station",
                             "run_onset_check", "run_stability_check",
                             "get_evidence", "exclude_observation"],
        "acquisition_allowed": requested.get("acquisition_allowed"),
        "diagnostic_bands_hz": requested.get("diagnostic_bands_hz"),
        "method_goal": requested.get("method_goal"),
        "target_band_hz": requested.get("target_band_hz"),
        "counters": {"stations_acquired": len(records),
                     "tool_calls_executed": tool_calls,
                     "model_requests": metrics.get("model_requests"),
                     "model_requests_known": bool(metrics.get("known")),
                     "onsets_computed": counts.get("onsets_computed", 0),
                     "stability_computed": counts.get("stability_computed", 0)},
        "finish_audit": (None if audit is None else
                         {"can_finish": audit["can_finish"],
                          "outcome": audit["outcome"],
                          "unmet": audit["unmet"],
                          "stop_basis": audit["stop_basis"],
                          "obligation_summary": audit["obligation_summary"],
                          # The full audit is written to the run record; the model
                          # gets the decision and the unmet list, not a verdict.
                          "evidence_path": "mcp_state.json#finish_audit"}),
        "note": ("这里只报告事实与公开义务。是否结束由你决定；本工具不会替你完成缺失的检查，"
                 "也不会给出最终判决。计数来自实际执行记录，未测得的计数返回 unknown。"),
    }
    if audit is not None:
        state["finish_audit"] = audit
    _log_event("task_status", {"stage": stage}, "ok", state=state,
               result={"can_finish": None if audit is None else audit["can_finish"]})
    _save(state)
    return result


def _stored_waveform(candidate_id):
    """The waveform already on disk for this candidate: derived if it exists, else raw.

    Reading from here is what makes 'no new fetches' still allow a full diagnostic.
    """
    for kind in ("derived", "raw"):
        path = FOLDER / kind / candidate_id / "waveform.mseed"
        if path.exists():
            return path
    return None


def _task_from_state(state):
    """The frozen task, rebuilt from run state so the core can be called directly.

    Built by the single contract constructor (`task.task_from_state_view`): the
    hand-listed field table here dropped `method_goal`, so a recompute inside
    `exclude_observation` judged against the DEFAULT goal and contradicted the
    final validator, which reads the frozen task (review-4 A2).
    """
    return taskmod.task_from_state_view(state)


def _snapshot(records, task, question_id):
    """Every check that depends on which observations are included."""
    active = core.active_records(records)
    verdict = core.verdict_for_question(active, question_id, None, task)
    all_layers = core.layers(active, task, question_id=question_id)
    integrity = all_layers["channel_integrity"]
    acquisition = all_layers["acquisition_status"]
    excluded_stations = set(integrity["stations_with_flags_excluded"])
    return {
        "stations_used": len(active),
        "stations_excluded": len(records) - len(active),
        "coincidence": verdict["coincidence"],
        "eligible_stations": verdict["eligible_stations"],
        "stations_with_flags": sorted(integrity["stations_with_flags"]),
        "stations_with_flags_still_used": sorted(
            st for st in integrity["stations_with_flags"]
            if st not in excluded_stations),
        "acquisition": {"attempted": acquisition["attempted"],
                        "analysed": acquisition["analysed"],
                        "excluded_by_user": acquisition["excluded_by_user"]},
        "onsets": sorted(o["station"] for o in all_layers["trigger_evidence"]["onsets"]),
        "stations_supporting_target_band":
            all_layers["method_applicability"]["stations_supporting_target_band"],
        "verdict": verdict["verdict"],
        "reason": verdict["reason"],
        "answered": verdict.get("answered"),
        "next_action": all_layers["next_action"],
    }


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def run_stability_check(candidate_id: str) -> dict:
    """Check whether one automatic onset is stable across a fixed, pre-declared grid.

    Local only: it re-reads the waveform already stored for this station, so it
    costs no external requests. The grid is fixed in advance and is not tuned per
    case. It reports how the configurations disagree; it never consults a curated
    reference arrival time, and it cannot tell you whether an event occurred.
    """
    state = _load()
    _log_event("run_stability_check", {"candidate_id": candidate_id}, "start", state=state)
    record = next((r for r in state["acquired"] if r["candidate_id"] == candidate_id), None)
    if record is None:
        exc = ValueError("Acquire the station before running a stability check on it")
        _log_event("run_stability_check", {"candidate_id": candidate_id}, "error",
                   state=state, error_type="ValueError", error=str(exc))
        _record_tool_error(state, "run_stability_check", {"candidate_id": candidate_id}, exc)
        _save(state)
        raise exc
    path = _stored_waveform(candidate_id)
    if path is None:
        exc = ValueError("No stored waveform for this station")
        _log_event("run_stability_check", {"candidate_id": candidate_id}, "error",
                   state=state, error_type="ValueError", error=str(exc))
        _record_tool_error(state, "run_stability_check", {"candidate_id": candidate_id}, exc)
        _save(state)
        raise exc
    try:
        requested = _requested(state)
        # O1: the requested diagnostic grid is passed through to the real
        # computation. The pre-fix code called `diagnostics.stability(path)` with no
        # bands, so a user's "narrow the grid to 3–12 Hz" was scored as complete
        # while the default 12-configuration grid actually ran.
        bands = requested.get("diagnostic_bands_hz")
        result = core.compute_stability(
            record, path, bands_hz=bands,
            target_band_hz=requested.get("target_band_hz"))
        state_ref = requested.get("condition_id")
        counts = _mutable(state, "computation_counts", dict)
        counts["stability_computed"] = counts.get("stability_computed", 0) + 1
    except EC.ConditionError as exc:
        _log_event("run_stability_check", {"candidate_id": candidate_id}, "error",
                   state=state, error_type="ConditionError", error=str(exc)[:300])
        _record_tool_error(state, "run_stability_check",
                           {"candidate_id": candidate_id}, exc)
        _save(state)
        raise ValueError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _log_event("run_stability_check", {"candidate_id": candidate_id}, "error",
                   state=state, error_type=type(exc).__name__, error=str(exc)[:300])
        _record_tool_error(state, "run_stability_check",
                           {"candidate_id": candidate_id}, exc)
        _save(state)
        raise
    effective = {"bands_hz": [list(b) for b in (result.get("bands_evaluated")
                                                or bands or [])],
                 "detector_bands": result.get("detectors"),
                 "grid_size": result.get("grid_size"),
                 "thresholds": diagnostics.GRID["thresholds"],
                 "target_band_hz": requested.get("target_band_hz")}
    entry = {"station": record.get("station"), "at": _now(),
             "stability": result["stability"], "spread_s": result["spread_s"],
             "n_valid": result["n_valid"], "statement": result["statement"],
             "next_step": result["next_step"], "clusters": result["clusters"],
             "grid_size": result["grid_size"],
             # O1 provenance: which condition this check belongs to, the exact
             # parameters it used, and who ran it.
             "condition_ref": requested.get("condition_id"),
             "effective_parameters": effective,
             "aggregate_parameters": {
                 "onsets_min_s": result.get("onsets_min_s"),
                 "onsets_max_s": result.get("onsets_max_s"),
                 "onsets_median_s": result.get("onsets_median_s"),
                 "mad_s": result.get("mad_s"),
                 "n_configurations": result.get("n_configurations"),
                 "n_configurations_error": result.get("n_configurations_error"),
                 "n_configurations_not_applicable":
                     result.get("n_configurations_not_applicable"),
                 "detectors": result.get("detectors"),
                 "alignment": result.get("alignment")},
             "participating_candidate_ids": [candidate_id],
             "freshness": "valid", "check_status": "ok",
             "tool_event_ref": f"run_stability_check:{candidate_id}",
             "actor": "agent"}
    _mutable(state, "stability_checks", dict)[candidate_id] = entry
    evidence_state.register(
        state, kind="stability", evidence_id=f"stability:{candidate_id}",
        requested=requested, effective_parameters=effective,
        source_sha256=evidence_state.source_sha(record),
        participating=[candidate_id], check_status="ok",
        tool_event_ref=f"run_stability_check:{candidate_id}", actor="agent")
    # Log BEFORE saving: the ok count is part of the persisted state (A5).
    _log_event("run_stability_check", {"candidate_id": candidate_id}, "ok", state=state,
               result={"stability": result["stability"], "spread_s": result["spread_s"],
                       "bands_evaluated": result.get("bands_evaluated"),
                       "full_grid": result.get("full_grid")})
    _save(state)
    return {"candidate_id": candidate_id, "station": record.get("station"),
            "condition_id": requested.get("condition_id"),
            "effective_bands_hz": effective["bands_hz"], **result}


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def exclude_observation(candidate_id: str, reason: str = "") -> dict:
    """Exclude one acquired observation and RECOMPUTE everything that depends on it.

    Returns a before/after for each affected check, so the change is inspectable
    rather than asserted. Checks that were not affected are listed as unchanged.
    """
    state = _load()
    _log_event("exclude_observation", {"candidate_id": candidate_id,
                                       "reason": reason}, "start", state=state)
    records = state["acquired"]
    record = next((r for r in records if r["candidate_id"] == candidate_id), None)
    if record is None:
        exc = ValueError("Only an acquired observation can be excluded")
        _log_event("exclude_observation", {"candidate_id": candidate_id}, "error",
                   state=state, error_type="ValueError", error=str(exc))
        _record_tool_error(state, "exclude_observation",
                           {"candidate_id": candidate_id}, exc)
        _save(state)
        raise exc
    try:
        task = _task_from_state(state)
        question_id = core.classify_question(state.get("user_question"))
        # Work on per-record copies. `core.exclude` marks the record in place, so
        # excluding the live list meant a later failure left the exclusion applied in
        # the saved state while the tool reported an error — the two must not split.
        working = [dict(r) for r in records]
        before = _snapshot(working, task, question_id)
        working, changed = core.exclude(working, candidate_id, reason)
        if not changed:
            _log_event("exclude_observation", {"candidate_id": candidate_id}, "ok",
                       state=state, result={"status": "already_excluded"})
            _save(state)
            return {"status": "already_excluded", "candidate_id": candidate_id,
                    "station": record.get("station")}
        # The model did this one, so the marker says so: the actor is `agent`, not
        # the deterministic guard. The two contributions are never merged.
        for row in working:
            if row.get("candidate_id") == candidate_id:
                row["excluded_by"] = "agent"
        after = _snapshot(working, task, question_id)
        # `affected` must be computed BEFORE it is written into the exclusion record;
        # the previous version referenced it one statement too early and the whole
        # tool raised NameError.
        affected = {k: {"before": before[k], "after": after[k]}
                    for k in before if before[k] != after[k]}
        unchanged = [k for k in before if before[k] == after[k]]
        # Atomic and None-tolerant. A follow-up state used to be written with
        # `exclusions: null`, and `setdefault` does not replace an existing None — so
        # this used to raise AttributeError *after* `acquired` had already been
        # reassigned. The tool then reported an error while the exclusion had in fact
        # taken effect, which is exactly the kind of split the review forbids. Build
        # the whole new state first, then assign it in one step.
        exclusions = list(state.get("exclusions") or [])
        exclusions.append(
            {"candidate_id": candidate_id, "station": record.get("station"),
             "reason": reason, "at": _now(),
             # the full before/after, so the change is inspectable later rather than
             # only asserted in the tool reply
             "recomputed": affected, "answer_after_recompute": after["answered"],
             "verdict_after_recompute": after["verdict"]})
        state["acquired"] = working
        state["exclusions"] = exclusions
        # The same condition_application event shape the guard writes, so the run
        # record shows WHO removed the observation.
        requested_now = _requested(state)
        after_conditions = EC.build(
            question_id=requested_now.get("question_id") or "Q_propagating",
            scope=requested_now.get("scope"),
            method_goal=requested_now.get("method_goal") or "three_component_pick",
            target_band_hz=requested_now.get("target_band_hz") or [1.0, 12.0],
            onset_method_band_hz=requested_now.get("onset_method_band_hz") or [1.0, 8.0],
            diagnostic_bands_hz=requested_now.get("diagnostic_bands_hz"),
            acquisition_allowed=requested_now.get("acquisition_allowed", True),
            excluded_candidate_ids=sorted(
                set(requested_now.get("excluded_candidate_ids") or []) | {candidate_id}),
            max_unique_candidates_episode=requested_now.get(
                "max_unique_candidates_episode", 6),
            requested_changes=(requested_now.get("requested_changes") or [])
            + [{"kind": "exclude", "candidate_id": candidate_id, "reason": reason,
                "actor": "agent"}])
        state.setdefault("condition_applications", []).append(
            {"actor": "agent", "source": "exclude_observation",
             "change": {"kind": "exclude", "candidate_id": candidate_id,
                        "reason": reason},
             "before": requested_now, "after": after_conditions})
        state["requested_conditions"] = after_conditions
    except Exception as exc:  # noqa: BLE001
        _log_event("exclude_observation", {"candidate_id": candidate_id}, "error",
                   state=state, error_type=type(exc).__name__, error=str(exc)[:300])
        _record_tool_error(state, "exclude_observation",
                           {"candidate_id": candidate_id}, exc)
        _save(state)
        raise
    # One write on the success path: the ok counter is logged into the state
    # BEFORE the single save, so JSONL, validation and the persisted
    # tool_attempts can never disagree (review-4 A5).
    _log_event("exclude_observation", {"candidate_id": candidate_id}, "ok", state=state,
               result={"affected": sorted(affected)})
    _save(state)
    return {
        "status": "excluded",
        "excluded": {"candidate_id": candidate_id, "station": record.get("station"),
                     "reason": reason,
                     "channel_integrity_flags": record.get("channel_integrity_flags", [])},
        "recomputed": affected,
        "unchanged_checks": unchanged,
        "stations_remaining": after["stations_used"],
        "answer_after_recompute": after["answered"],
        "next_action": after["next_action"],
        "note": "Excluding an observation removes it from every later check. "
                "It does not remove the facts already measured on it.",
    }


if __name__ == "__main__":
    server.run(transport="stdio")