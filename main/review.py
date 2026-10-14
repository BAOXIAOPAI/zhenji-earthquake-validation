"""Review orchestration for the operable interface.

Every review — deterministic or model-driven — gets its own run id, its own task
snapshot and its own record. Nothing is keyed by case name, so a new question
never inherits an old run's result (evaluation P0-1).

Modes:
  question  read the free-text question, decide what would answer it, stop there
  rule      ignore the question, stop when the trigger-clustering rule is decided
  fixed     always acquire exactly three stations
  full      acquire every available station up to the cap
  agent     hand the decision to a real AGH session (costs money)

`question` classifies the free text with keywords. That is a deterministic
stand-in, NOT a model; the interface labels it as such. Only `agent` uses a model.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import fdsn  # noqa: E402
import feedback  # noqa: E402
import prepare_agent  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

MODES = {
    "question": "按问题停止（关键词判类，不调用模型）",
    "rule": "按一致传播判据停止（不理会问题）",
    "fixed": "固定取 3 个台站",
    "full": "取满全部可用台站",
    "agent": "交给真实 AGH 会话决策（消耗模型预算）",
}

# The mapping lives in core so the CLI and the interface cannot disagree about
# which criterion a given question implies. There is deliberately no second word
# table here: an earlier duplicate still had "通道" read as a fault word.
classify_question = core.classify_question

def _rel(path):
    """Path relative to the project when possible; run records may live elsewhere."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


_PUBLIC_RECORD = ("candidate_id", "station", "network", "distance_km", "status",
                  "acquisition_status", "channels", "three_component_ok", "vertical_ok",
                  "vertical_health", "channel_integrity_flags", "sampling_rate_hz",
                  "band_support", "method_applicability", "onset_utc", "gaps",
                  "error", "stability", "excluded", "exclusion_reason")


def _public_record(record):
    out = {k: record.get(k) for k in _PUBLIC_RECORD}
    # disclose THAT a variant exists, never what it is
    out["derived"] = bool(record.get("derivation"))
    return out


def _conditions(frozen, records, parent_run_id=None):
    """The conditions actually in force for a run, in one place.

    The interface needs this to show the user which parent and which physical
    conditions a feedback round will inherit. Without it a chained feedback turn
    looks like it continues from the latest run while it silently re-derives from
    the first one (the third review's P1-1).
    """
    active = core.active_records(records)
    goal = frozen.get("method_goal")
    return {
        "target_band_hz": list(frozen.get("target_band_hz") or []),
        "method_goal": goal,
        "method_goal_label": core.METHOD_GOAL_LABELS.get(goal, goal or ""),
        "window_start": frozen.get("window_start"),
        "window_end": frozen.get("window_end"),
        "included_stations": sorted(r.get("station") for r in active
                                    if r.get("station")),
        "excluded_stations": sorted(r.get("station") for r in records
                                    if r.get("excluded") and r.get("station")),
        "parent_run_id": parent_run_id,
    }


def run_review(case_id, question, mode, on_step=None, user_task=None,
               method_goal=None, operational_standard=None):
    """Run one review. Returns a JSON-serialisable result; never raises for data issues.

    `user_task` is a task the user wrote themselves (already frozen). When it is
    given, the window and region come from the user and no preset case is involved;
    the same pipeline and the same verdict layer are used either way, so a user
    request is not a second-class path.
    """
    def emit(message, **extra):
        if on_step:
            on_step({"message": message, **extra})

    # The runtime entry point has to wire the content-addressed cache. Without it the
    # same window is re-downloaded on every run, `cache_hits` stays permanently 0,
    # and no run can be served offline — which the second review asks the product to
    # support. Entry points that already wire it are unaffected (same path).
    core.fdsn.set_cache(core.CACHE)

    case_ref = None
    if user_task is not None:
        frozen = user_task
        case = taskmod.case_for_task(frozen)
        case_id = case["case_id"]
        if mode == "agent":
            return _agent(case_id, question, emit, user_task=frozen)
        question_id = classify_question(question)
    else:
        if mode == "agent":
            return _agent(case_id, question, emit)
        case = core.case_by_id(case_id)
        question_id = (classify_question(question) if mode == "question"
                       else case["question_id"])
        frozen = taskmod.task_from_case(case, user_question=question,
                                        method_goal=method_goal,
                                        operational_standard=operational_standard)
        taskmod.freeze(frozen)
        case_ref = case_id
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm=mode, extra={"demo_case_ref": case_ref})

    emit("正在查询台站元数据", phase="discovery", kind="discovery")
    # 严格离线下材料缺失立即说明：发现查询未命中缓存时返回失败结果
    # （带缺失 URL 与类别），不联网回退、不抛出到调用方/页面。
    try:
        discovery = core.discover(case, frozen)
    except fdsn.OfflineMaterialMissing as exc:
        runlog.outcome(run_id, "failed",
                       failure_kind="internal_error",
                       failure_detail=f"离线材料缺失：{exc.category}")
        return {"run_id": run_id, "task_id": frozen["task_id"],
                "case_id": case_id, "mode": mode,
                "question": frozen["user_question"],
                "question_id": question_id,
                "status": "failed",
                "failure_kind": "offline_material_missing",
                "failure_category": exc.category,
                "missing_url": exc.url,
                "reason": str(exc),
                "stations": [], "stations_acquired": 0,
                "n_eligible": None, "requests_to_verdict": 0,
                "http_attempts": 0, "logical_acquisitions": 0,
                "verdict": None, "field_value": None,
                "statement": "严格离线模式：发现材料缺失，已停止。"
                             "不联网补齐。",
                "offline_request_counts": fdsn.offline_request_counts(),
                "model_cost_known": False, "model_cost_cny": None}
    runlog.write_once(folder / "discovery.json", discovery)
    # Deterministic runs use the same run layout as agent runs, so a feedback turn
    # (and a reviewer) always finds the evaluator record it expects.
    runlog.write_once(folder / "evaluator.json", prepare_agent.evaluator_state(
        {**frozen, "acceptance": {**(frozen.get("acceptance") or {}),
                                  "injected": bool(case.get("injected")),
                                  "inject_station": case.get("inject_station")}}))
    emit(f"发现 {discovery['n_eligible']} 个可用台站", phase="discovery",
         kind="discovery", n_eligible=discovery["n_eligible"])

    state = {"run_id": run_id, "case_id": case_id, "task_id": frozen["task_id"],
             "task": frozen, "question_id": question_id, "arm": mode,
             "records": [], "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
             "inject_first": bool(case.get("injected")),
             "inject_station": case.get("inject_station")}

    def hook(event):
        record = event.get("record") or {}
        if event.get("kind") == "acquire_start":
            emit(f"获取 {record.get('network')}.{record.get('station')}"
                 f"（{record.get('distance_km')} km）", phase="acquire",
                 kind="acquire_start", station=_public_record(record))
        elif event.get("kind") == "acquire_done":
            detail = _public_record(record)
            note = []
            if detail.get("channel_integrity_flags"):
                note.append("通道条件：" + "、".join(detail["channel_integrity_flags"]))
            if detail.get("status") != "analysed":
                note.append("不可用：" + str(detail.get("status")))
            emit(f"  → {detail.get('station')} {detail.get('status')}"
                 + ("；" + "；".join(note) if note else ""),
                 phase="acquire", kind="acquire_done", station=detail)

    state["on_step"] = hook

    if mode == "question":
        for candidate in discovery["eligible"][:core.MAX_STATIONS]:
            state["records"].append(core.acquire(case, candidate, state))
            if core.question_is_decided(state["records"], question_id, None, frozen):
                state["stop_reason"] = "问题已可判"
                break
        else:
            state["stop_reason"] = "候选台站已用尽"
    elif mode == "rule":
        core.arm_adaptive_rule(case, discovery, state)
    elif mode == "fixed":
        core.arm_fixed_pipeline(case, discovery, state)
    elif mode == "full":
        core.arm_full_evidence(case, discovery, state)
    else:
        raise ValueError(f"未知模式：{mode}")

    verdict = core.verdict_for_question(state["records"], question_id, None, frozen)
    layers = core.layers(state["records"], frozen, question_id=question_id,
                         robustness=core.stability_layer(state["records"]))
    # Stored so a later feedback turn (or a reviewer) can diff against the exact
    # judgement this run delivered, not a re-derivation from memory.
    runlog.write_once(folder / "verdict.json", verdict)
    runlog.write_once(folder / "layers.json", layers)
    emit(f"判决：{verdict['verdict']}（{verdict['reason']}）", phase="verdict",
         kind="verdict", verdict=verdict["verdict"], requests=state["requests"])

    # The deterministic run gets the same full execution state as an agent run, so a
    # later feedback turn can build its prompt from this run's physical spec
    # (window/region/band/method_goal/discovery), not just its task id.
    prepare_agent.write_state(folder, prepare_agent.execution_state(
        run_id, frozen, discovery, acquired=state["records"],
        extra={"requests": state["requests"], "http_attempts": state["http_attempts"],
               "logical_acquisitions": state["logical_acquisitions"],
               "cache_hits": state["cache_hits"], "bytes": state["bytes"],
               "exclusions": []}))
    cost = runlog.write_cost(run_id, {"known": False, "reason": "no_model_used"},
                             http_attempts=state["http_attempts"],
                             cache_hits=state["cache_hits"],
                             logical_acquisitions=state["logical_acquisitions"],
                             bytes_total=state["bytes"])
    runlog.outcome(run_id, runlog.SUCCEEDED, stations_acquired=len(state["records"]),
                   model_cost_known=False)

    return {
        "run_id": run_id, "task_id": frozen["task_id"],
        "case_id": case_id, "mode": mode, "question": frozen["user_question"],
        "question_id": question_id,
        "classified_by": ("keyword" if (mode == "question" or user_task is not None)
                          else "case_definition"),
        "from_user_task": user_task is not None,
        "question_label": core.QUESTIONS[question_id],
        "n_eligible": discovery["n_eligible"],
        "stations": [_public_record(r) for r in state["records"]],
        "stations_acquired": len(state["records"]),
        "requests_to_verdict": state["requests"],
        "http_attempts": cost["http_attempts"],
        "logical_acquisitions": cost["logical_acquisitions"],
        "status": runlog.SUCCEEDED,
        "failure_kind": None,
        "is_current_run": True,
        "verdict": verdict["verdict"], "reason": verdict["reason"],
        "field_value": (verdict.get("answered") or {}).get("value"),
        "statement": verdict["statement"],
        "statement_zh": verdict.get("statement_zh"),
        "coincidence": verdict["coincidence"],
        "eligible_stations": verdict["eligible_stations"],
        "integrity_flag": verdict["integrity_flag"],
        "terminated_by": verdict.get("terminated_by"),
        "field_answered": verdict.get("field_answered"),
        "answered": verdict.get("answered"),
        "layers": layers,
        "model_cost_known": False,
        "model_cost_cny": None,
        "cost_basis": "未调用模型",
        "conditions": _conditions(frozen, state["records"]),
        "injected": bool(case.get("injected")),
        "injection_note": case.get("injection"),
        # Whether the disclosed injection actually reached an observation. If it did
        # not, the banner must not claim it did.
        "injection_applied": any(r.get("injected") for r in state["records"]),
        "injection_failed": [r.get("injection_failed") for r in state["records"]
                             if r.get("injection_failed")] or None,
        "evidence_dir": _rel(folder),
    }


_SNAPSHOT_KEYS = (
    "verdict", "field", "field_value", "task_completed", "coincidence",
    "integrity_flag", "terminated_by", "included_candidates", "excluded_candidates",
    "stations_supporting_target_band", "stations_not_supporting_target_band",
    "stations_with_unknown_support", "stations_checked", "n_onsets",
    "next_action_needs", "target_band_hz",
    # The parameter-stability layer is part of what a feedback round can change,
    # so its before/after must be diffable too.
    "stability_state", "stability_spread_s", "stability_full_grid",
    "stability_bands_evaluated", "stability_by_station",
)


def _stability_snapshot(robustness):
    """One comparable summary of the stability layer over the reviewed stations.

    A single station's stability is not the answer: the reviewer needs to know
    whether *any* station's automatic onset disagreed with itself, and by how much.
    """
    per = robustness or {}
    states = sorted({(v or {}).get("stability") for v in per.values()
                     if (v or {}).get("stability")})
    spreads = [v.get("spread_s") for v in per.values()
               if isinstance((v or {}).get("spread_s"), (int, float))]
    # The aggregate max can hide a station that went from unstable to stable, so the
    # per-station view travels with it and is diffed as part of the same change.
    by_station = {sta: {"state": (v or {}).get("stability"),
                        "spread_s": (v or {}).get("spread_s"),
                        "n_valid": (v or {}).get("n_valid"),
                        "full_grid": (v or {}).get("full_grid")}
                  for sta, v in sorted(per.items())}
    return {
        "stability_state": (states[0] if len(states) == 1 else
                            ("mixed" if states else None)),
        "stability_spread_s": (round(max(spreads), 3) if spreads else None),
        "stability_full_grid": (all(bool((v or {}).get("full_grid", True))
                                    for v in per.values()) if per else None),
        "stability_bands_evaluated": sorted(
            {tuple(b) for v in per.values()
             for b in ((v or {}).get("bands_evaluated") or [])}),
        "stability_by_station": by_station,
    }


def _snapshot(verdict, layers):
    """The fields a feedback round can actually change, in one comparable dict."""
    app = (layers or {}).get("method_applicability") or {}
    integ = (layers or {}).get("channel_integrity") or {}
    trig = (layers or {}).get("trigger_evidence") or {}
    nxt = (layers or {}).get("next_action") or {}
    answered = verdict.get("answered") or {}
    return {
        "verdict": verdict.get("verdict"),
        "field": answered.get("field") or verdict.get("field_answered"),
        "field_value": answered.get("value"),
        "task_completed": bool(answered.get("answered")),
        "coincidence": verdict.get("coincidence"),
        "integrity_flag": verdict.get("integrity_flag"),
        "terminated_by": verdict.get("terminated_by"),
        "included_candidates": sorted((layers or {}).get("included_candidates") or []),
        "excluded_candidates": sorted((layers or {}).get("excluded_candidates") or []),
        "stations_supporting_target_band": app.get("stations_supporting_target_band"),
        "stations_not_supporting_target_band": app.get("stations_not_supporting_target_band"),
        "stations_with_unknown_support": app.get("stations_with_unknown_support"),
        "stations_checked": integ.get("stations_checked"),
        "n_onsets": trig.get("n_onsets"),
        **_stability_snapshot((layers or {}).get("robustness")),
        "next_action_needs": nxt.get("needs"),
        "target_band_hz": list((app or {}).get("target_band_hz") or []),
    }


def _affected(before, after):
    out = []
    for key in _SNAPSHOT_KEYS:
        if before.get(key) != after.get(key):
            out.append({"field": key, "before": before.get(key), "after": after.get(key)})
    return out


def _records_of(state):
    """The observations of a run state. A follow-up state uses `acquired`, the
    recompute workspace uses `records`; both mean the same list."""
    return feedback.records_of(state)


def _resolve_record(state, change):
    """Find the observation the feedback names, by candidate id or station name."""
    return feedback.resolve_record(state, change)


# O1: normalisation and application live in one thin module so `prepare_agent` can
# apply a user's explicit change before the child's state is written, without a
# review<->prepare import cycle. These names stay as the public surface the
# interface, the historical runner and the tests already use.
feedback_change = feedback.normalize_change


def _run_injected(run_id):
    """Whether a stored run used the disclosed demo injection.

    Returns None when the run predates the record. `False` would assert "no
    injection", which is exactly what an old run cannot tell us.
    """
    folder = runlog.run_dir(run_id)
    try:
        if (folder / "evaluator.json").exists():
            acc = (json.loads((folder / "evaluator.json").read_text())
                   .get("acceptance") or {})
            if "injected" in acc:
                return bool(acc["injected"])
        state = folder / "mcp_state.json"
        if state.exists():
            records = json.loads(state.read_text()).get("acquired") or []
            return any(r.get("injected") or r.get("derivation") for r in records)
    except (ValueError, OSError):
        return None
    return None


def _analysis_waveform(folder, candidate_id):
    """The waveform the parent actually analysed: its derived variant, else the raw.

    Reading `raw/` unconditionally would silently undo a disclosed demo injection on
    the first feedback round: the child would recompute from the untouched download
    while still claiming the injection applied.
    """
    return feedback._analysis_waveform(folder, candidate_id)


def _apply_change(change, state, folder):
    """Apply the condition and recompute what it touches (deterministic flows).

    The reference flow has no tools, so it must produce complete evidence locally:
    this is the `precompute=True` path. The real-Agent child applies the same
    conditions through `prepare_agent.prepare_followup` with `precompute=False`,
    leaving the waveform diagnostics to the agent's own tool calls.
    """
    return feedback.recompute_after_change(state, folder, change)


def run_followup(parent_run_id, change=None, instruction=None, on_step=None):
    """The user's feedback turn, recomputed from the parent's own observations.

    This is the interface closed loop the second review asks for. It is a *new*
    run with its own id: the parent run's record is never rewritten. No model is
    called and no data are fetched, so the reuse/new-request counts are real
    numbers rather than a claim.
    """
    def emit(message, **extra):
        if on_step:
            on_step({"message": message, **extra})

    parent = runlog.run_dir(parent_run_id)
    if not (parent / "task.json").exists():
        raise ValueError(f"未知的上一轮运行：{parent_run_id}")
    parent_task = json.loads((parent / "task.json").read_text())
    parent_state = json.loads((parent / "mcp_state.json").read_text())
    if not parent_state.get("acquired"):
        raise ValueError("上一轮没有取得任何观测，无法在不新增请求的前提下重算")
    # `acceptance` can legitimately be absent/None on a bare task, so the access
    # must tolerate that instead of raising on a follow-up.
    parent_question_id = ((parent_task.get("acceptance") or {})
                          .get("question_ids") or [])
    parent_question_id = (parent_question_id[0] if parent_question_id
                          else core.classify_question(parent_task["user_question"]))

    change = feedback_change(parent_state, change, instruction)
    instruction = instruction or _describe(change)
    band = change["target_band_hz"] if change["kind"] == "band" else None
    goal = change.get("method_goal") if change["kind"] == "method_goal" else None
    # Snapshot the parent's judgement BEFORE anything is applied. `_apply_change`
    # mutates records in place, so snapshotting after it would compare the new
    # condition against itself and report no change at all.
    before = _snapshot(
        core.verdict_for_question(core.active_records(parent_state["acquired"]),
                                  parent_question_id, None, parent_task),
        core.layers(parent_state["acquired"], parent_task,
                    question_id=parent_question_id,
                    robustness=core.stability_layer(parent_state["acquired"])))
    emit(f"反馈重算：{instruction}（复用上一轮观测，不新增请求）",
         phase="feedback", kind="feedback", change=change)

    prepared = prepare_agent.prepare_followup(
        parent_run_id, instruction, arm="user_feedback_recompute",
        target_band_hz=band, method_goal=goal, change=change)
    run_id = prepared["run_id"]
    folder = runlog.run_dir(run_id)
    frozen = json.loads((folder / "task.json").read_text())
    state = json.loads((folder / "mcp_state.json").read_text())
    # `prepare_followup` already re-judged the carried records against THIS
    # child's frozen conditions; use those, not a fresh copy of the parent's
    # still-parent-conditioned records. (For a pure exclude/include the two are
    # identical; for a band change the child's are the correct starting point.)
    state.update({"run_id": run_id, "task": frozen, "task_id": frozen["task_id"],
                  "records": copy.deepcopy(state.get("acquired")
                                           or parent_state["acquired"]),
                  "question_id": parent_question_id,
                  "arm": "user_feedback_recompute", "requests": 0,
                  "http_attempts": 0, "logical_acquisitions": 0, "cache_hits": 0,
                  "bytes": 0})

    recomputed, detail = _apply_change(change, state, folder)
    emit(f"已应用：{detail.get('applied')}"
         + (f"，重新分析 {recomputed} 条观测" if recomputed else ""),
         phase="feedback", kind="feedback_applied", detail=detail)

    verdict = core.verdict_for_question(core.active_records(state["records"]),
                                        parent_question_id, None, frozen)
    layers = core.layers(state["records"], frozen, question_id=parent_question_id,
                         robustness=core.stability_layer(state["records"]))
    after = _snapshot(verdict, layers)
    affected = _affected(before, after)

    # Persist the recomputed state and the comparison. `verdict.json`/`layers.json`
    # are written so a later feedback round can diff against this one too.
    runlog.write_once(folder / "verdict.json", verdict)
    runlog.write_once(folder / "layers.json", layers)
    # The live state is rewritten in place (the initial snapshot stays untouched).
    # It keeps the full physical spec the initial state carried, so the next
    # feedback round can chain off this run without a missing-field failure.
    discovery = json.loads((folder / "discovery.json").read_text())
    persisted = prepare_agent.execution_state(
        run_id, frozen, discovery, acquired=state["records"],
        parent_run_id=parent_run_id,
        extra={"follow_up": instruction, "change": change,
               "carried_forward": [r["candidate_id"] for r in parent_state["acquired"]],
               "exclusions": state.get("exclusions"),
               "parent_history": state.get("parent_history"),
               "requests": 0, "http_attempts": 0, "logical_acquisitions": 0,
               "cache_hits": 0, "bytes": 0})
    prepare_agent.write_state(folder, persisted)
    feedback = {
        "parent_run_id": parent_run_id, "instruction": instruction, "change": change,
        "detail": detail, "reused_observations": len(parent_state["acquired"]),
        "new_requests": 0, "recomputed_observations": recomputed,
        "model_calls": 0, "affected_fields": affected,
    }
    runlog.write_once(folder / "feedback.json", feedback)
    cost = runlog.write_cost(run_id, {"known": False, "reason": "no_model_used"},
                             http_attempts=0, cache_hits=0,
                             logical_acquisitions=0, bytes_total=0)
    runlog.outcome(run_id, runlog.SUCCEEDED,
                   stations_acquired=len(state["records"]), model_cost_known=False)
    emit(f"重算完成：{verdict['verdict']}，影响 {len(affected)} 个字段",
         phase="verdict", kind="verdict", verdict=verdict["verdict"])

    return {
        "run_id": run_id, "task_id": frozen["task_id"], "mode": "feedback",
        "parent_run_id": parent_run_id,
        "case_id": (frozen.get("acceptance") or {}).get("case_ref"),
        "question": frozen["user_question"], "question_id": parent_question_id,
        "classified_by": "parent_run",
        "question_label": core.QUESTIONS[parent_question_id],
        "stations": [_public_record(r) for r in state["records"]],
        "stations_acquired": len(state["records"]),
        "requests_to_verdict": 0,
        "http_attempts": 0,
        "logical_acquisitions": 0,
        "status": runlog.SUCCEEDED, "failure_kind": None, "is_current_run": True,
        "verdict": verdict["verdict"], "reason": verdict["reason"],
        "field_value": (verdict.get("answered") or {}).get("value"),
        "statement": verdict["statement"],
        "statement_zh": verdict.get("statement_zh"),
        "coincidence": verdict["coincidence"],
        "eligible_stations": verdict["eligible_stations"],
        "integrity_flag": verdict["integrity_flag"],
        "terminated_by": verdict.get("terminated_by"),
        "field_answered": verdict.get("field_answered"),
        "answered": verdict.get("answered"),
        "layers": layers,
        "model_cost_known": False, "model_cost_cny": None,
        "cost_basis": "本次反馈重算未调用模型",
        "injected": bool((frozen.get("acceptance") or {}).get("injected")),
        # The carried observations keep their injected variant, so the child run
        # discloses the same demo injection the parent did.
        "injection_applied": any(r.get("injected") or r.get("derivation")
                                 for r in state["records"]),
        "injection_note": (frozen.get("acceptance") or {}).get("injection_note"),
        "evidence_dir": _rel(folder),
        # The conditions this child run actually carries, so the interface can show
        # the user what a *next* feedback round would inherit from here.
        "conditions": _conditions(frozen, state["records"], parent_run_id),
        "feedback": feedback,
        "affected_fields": affected,
    }


def _describe(change):
    if change["kind"] == "exclude":
        return f"排除台站 {change.get('station') or change['candidate_id']}"
    if change["kind"] == "include":
        return f"恢复台站 {change.get('station') or change['candidate_id']}"
    if change["kind"] == "band":
        lo, hi = change["target_band_hz"]
        return f"目标频带改为 {lo:g}–{hi:g} Hz"
    if change["kind"] == "method_goal":
        return (f"方法目标改为 {change.get('method_goal')}"
                f"（{core.METHOD_GOAL_LABELS.get(change.get('method_goal'), '')}）")
    return str(change.get("kind"))


def _agent(case_id, question, emit, user_task=None):
    """Run one real AGH session in-process, so failure is *this* run's failure.

    The old implementation shelled out and then read whichever record happened to
    be on disk for the case, so a failed new question could display an old success
    (evaluation P0-1). Here the outcome, the cost and the evidence all come from
    the run id this call created.
    """
    import run_agent_arm

    emit("正在准备台站资料，完成后启动模型核查（模型调用会产生费用）", phase="agent", kind="agent")
    try:
        row = (run_agent_arm.run_user_task(user_task, question) if user_task is not None
               else run_agent_arm.run_one(case_id, question))
    except prepare_agent.PreparationError as exc:
        emit(exc.detail, phase="error", kind="failure",
             failure_kind="discovery_failure")
        return {"run_id": exc.run_id, "task_id": exc.task_id, "case_id": case_id,
                "mode": "agent", "question": question,
                "status": "failed", "failure_kind": "discovery_failure",
                "failure_detail": exc.detail, "reason": exc.detail,
                "statement": exc.detail, "statement_zh": exc.detail,
                "model_started": False, "model_cost_known": True,
                "model_cost_cny": 0, "stations": [], "stations_acquired": 0,
                "verdict": None, "is_current_run": True,
                "evidence_dir": _rel(runlog.run_dir(exc.run_id))}
    run_id = row["run_id"]
    cost = runlog.cost_for_run(run_id)
    status = row.get("status", "failed")
    if status != runlog.SUCCEEDED:
        emit(f"本次运行失败：{row.get('failure_kind')}", phase="error", kind="failure",
             failure_kind=row.get("failure_kind"))
    else:
        # The label and the asked field's value are different things; say both.
        delivered = row.get("delivered") or {}
        detail = delivered.get("verdict")
        if delivered.get("field") and delivered.get("field") != "verdict":
            detail += f"（{delivered['field']} = {delivered.get('field_value')}）"
        emit(f"交付结果：{detail}",
             phase="verdict", kind="verdict",
             verdict=delivered.get("verdict"))
    return {
        "run_id": run_id, "task_id": row["task_id"], "case_id": case_id, "mode": "agent",
        "question": row["question"], "question_id": row["question_id"],
        "classified_by": "live_agh_session",
        "question_label": row["question"],
        "stations": row.get("stations", []),
        "stations_acquired": row.get("stations_acquired"),
        "requests_to_verdict": row.get("requests_to_verdict"),
        "http_attempts": row.get("http_attempts"),
        "logical_acquisitions": row.get("logical_acquisitions"),
        "status": status,
        "failure_kind": row.get("failure_kind"),
        # Resource cleanup result (package D): a failure or timeout is visible here,
        # not hidden behind "succeeded".
        "cleanup": row.get("cleanup"),
        "is_current_run": True,
        # the three things the evaluation requires to stay separate
        "acquisition": row.get("acquisition"),
        "model_claim": row.get("model_claim"),
        "delivered": row.get("delivered"),
        "agreement": row.get("agreement"),
        # `verdict` is the label; the asked field's value lives in `field_value`.
        # The old code put `delivered.value` here, so a boolean channel flag could be
        # shown as the conclusion (evaluation P1-1).
        "verdict": (row.get("delivered") or {}).get("verdict"),
        "field_value": (row.get("delivered") or {}).get("field_value"),
        "task_completed": (row.get("delivered") or {}).get("task_completed"),
        "reason": (row.get("validator_verdict") or {}).get("reason"),
        "statement": (row.get("delivered") or {}).get("statement"),
        "statement_zh": (row.get("delivered") or {}).get("statement_zh"),
        "coincidence": (row.get("validator_verdict") or {}).get("coincidence"),
        "eligible_stations": (row.get("validator_verdict") or {}).get("eligible_stations"),
        "integrity_flag": (row.get("validator_verdict") or {}).get("integrity_flag"),
        "terminated_by": (row.get("validator_verdict") or {}).get("terminated_by"),
        "model_stop_reason": (row.get("model_claim") or {}).get("stop_reason"),
        "model_cost_known": cost["known"],
        "model_cost_cny": cost["cost_upper_cny"],
        "cost_basis": f"台账中本次 run（{run_id}）的记录",
        "injected": _run_injected(run_id),
        "evidence_dir": _rel(runlog.run_dir(run_id)),
    }
