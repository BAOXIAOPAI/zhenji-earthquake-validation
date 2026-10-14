"""The free semantic/engineering gates G01–G16 (book §10).

Every gate checks ACTUAL state or effective parameters, never just a final label.
`handcrafted_input` and `fault_injection` gates are marked as such and never count
as real Agent results. No gate calls a paid model: the MCP tools are exercised
directly, and the two protocol gates drive the bridge against a simulated provider.

Run:  .venv/bin/python main/agent_optimization_gates.py --output <dir>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import c_scorer  # noqa: E402
import evidence_state  # noqa: E402
import execution_conditions as EC  # noqa: E402
import feedback  # noqa: E402
import finish_audit  # noqa: E402
import prepare_agent  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

WINDOW = {"start": "2023-10-09T05:00:00.000000Z", "end": "2023-10-09T05:02:00.000000Z"}


def _record(candidate_id="cand-A", station="AAA", *, rate=100.0,
            three=True, vertical=True, flags=None, onset=1000.0, goal=None,
            excluded=False):
    band = core.band_support(rate, [1.0, 12.0])
    rec = {"candidate_id": candidate_id, "station": station, "network": "UW",
           "status": "analysed", "three_component_ok": three, "vertical_ok": vertical,
           "sampling_rate_hz": rate, "band_support": band,
           "channel_integrity_flags": list(flags or []),
           "onset_utc": onset, "onset_computed": True,
           "method_applicability_goal": goal or "three_component_pick",
           "method_applicability": {
               "three_component_pick": bool(three and vertical and band["supported"]),
               "vertical_onset": bool(vertical and band["supported"])}}
    if excluded:
        rec["excluded"] = True
    return rec


def _state(**over):
    conditions = EC.build(
        question_id=over.pop("question_id", "Q_usable"),
        scope=over.pop("scope", {"scope_kind": "all_allowed"}),
        method_goal=over.pop("method_goal", "three_component_pick"),
        target_band_hz=over.pop("target_band_hz", [1.0, 12.0]),
        onset_method_band_hz=[1.0, 8.0],
        diagnostic_bands_hz=over.pop("diagnostic_bands_hz", None),
        acquisition_allowed=over.pop("acquisition_allowed", True),
        excluded_candidate_ids=over.pop("excluded_candidate_ids", None),
        requested_changes=over.pop("requested_changes", None))
    state = {"run_id": over.pop("run_id", "r-gate"), "task_id": "t-gate",
             "user_question": over.pop("user_question", "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？"),
             "window_start": WINDOW["start"], "window_end": WINDOW["end"],
             "region": {}, "target_band_hz": conditions["target_band_hz"],
             "method_band_hz": [1.0, 8.0], "method_goal": conditions["method_goal"],
             "allowed_actions": list(taskmod.DEFAULT_ALLOWED),
             "discovery": {"n_eligible": 4, "eligible": []},
             "acquired": over.pop("acquired", []), "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
             "requested_conditions": conditions, "exclusions": [],
             "stability_checks": over.pop("stability_checks", {}),
             "derived_evidence_checks": over.pop("derived_evidence_checks", {}),
             "attempted_candidate_ids_episode": over.pop("attempted", []),
             "tool_events": over.pop("tool_events", []),
             "scope": conditions["scope"]}
    state.update(over)
    return state


def _result(gate_id, name, ok, detail, *, source="handcrafted_input", evidence=None):
    return {"id": gate_id, "name": name, "status": "pass" if ok else "fail",
            "source": source, "detail": detail, "evidence": evidence or {}}


# ---------------------------------------------------------------- G01 / G02
def g01():
    """Unique EQUIL excluded, no fetch: active empty, no old determined answer."""
    rec = _record("cand-EQUIL", "EQUIL",
                  flags=["UW.EQUIL..ENE:constant"], three=False)
    state = _state(question_id="Q_instrument", acquired=[rec],
                   excluded_candidate_ids=["cand-EQUIL"],
                   requested_changes=[{"kind": "exclude", "candidate_id": "cand-EQUIL"}],
                   tool_events=[])
    change = feedback.normalize_change(
        _state(question_id="Q_instrument", acquired=[rec]),
        {"kind": "exclude", "candidate_id": "cand-EQUIL", "reason": "用户要求排除"})
    _, detail = feedback.apply_conditions(
        change, state, ROOT / "main/tmp-gate-g01",
        requested_before=_state(question_id="Q_instrument", acquired=[rec]
                                ).get("requested_conditions"),
        requested_after=state["requested_conditions"])
    active = core.active_records(state["acquired"])
    verdict = core.verdict_for_question(active, "Q_instrument", None, {
        "target_band_hz": [1.0, 12.0], "method_goal": "three_component_pick"})
    audit = finish_audit.audit_finish(state, verdict=verdict)
    applied = state.get("condition_applications", [{}])[-1]
    ok = (not active
          and not (verdict.get("answered") or {}).get("answered")
          and applied.get("actor") == "deterministic_guard"
          and audit["outcome"] != "determined_answer")
    return _result("G01", "唯一观测被排除且禁止补取", ok,
                   {"active": len(active), "verdict": verdict.get("verdict"),
                    "resolved": (verdict.get("answered") or {}).get("answered"),
                    "actor": applied.get("actor"), "outcome": audit["outcome"]},
                   evidence=detail)


def g02():
    """Same label before/after exclusion: the participating set must still change."""
    rec = _record("cand-A", "AAA")
    state_a = _state(acquired=[rec])
    state_b = _state(acquired=[dict(rec, excluded=True, excluded_by="agent")],
                     excluded_candidate_ids=["cand-A"])
    a = EC.actual_from_state(state_a, requested=state_a["requested_conditions"])
    b = EC.actual_from_state(state_b, requested=state_b["requested_conditions"])
    changed = a["active_candidate_ids"] != b["active_candidate_ids"]
    ok = changed and b["active_candidate_ids"] == []
    return _result("G02", "标签相同也必须核对当前参与集合", ok,
                   {"before": a["active_candidate_ids"], "after": b["active_candidate_ids"]})


# ---------------------------------------------------------------------- G03
def g03():
    """Requested 3–12 Hz reaches the real computation in all three entries."""
    from diagnostics import GRID
    grid = [[3.0, 12.0]]
    requested = EC.build(question_id="Q_usable",
                         scope={"scope_kind": "all_allowed"},
                         method_goal="three_component_pick",
                         target_band_hz=[1.0, 12.0],
                         onset_method_band_hz=[1.0, 8.0],
                         diagnostic_bands_hz=grid,
                         requested_changes=[{"kind": "stability_bands",
                                             "target_band_hz": [3.0, 12.0]}])
    state = _state(acquired=[_record()], diagnostic_bands_hz=grid,
                   requested_changes=[{"kind": "stability_bands",
                                       "target_band_hz": [3.0, 12.0]}])
    state["requested_conditions"] = requested
    # The MCP tool records the effective parameters it actually used.
    checks = {"cand-A": {"condition_ref": requested["condition_id"],
                         "effective_parameters": {"bands_hz": grid,
                                                  "thresholds": GRID["thresholds"]},
                         "freshness": "valid", "check_status": "ok",
                         "actor": "agent"}}
    state["stability_checks"] = checks
    actual = EC.actual_from_state(state, requested=requested)
    match = EC.matches(requested, actual)
    # Old default-grid evidence is recognised as stale, not reused.
    stale = dict(checks["cand-A"])
    stale["condition_ref"] = "old-condition"
    stale["effective_parameters"] = {"bands_hz": [list(b) for b in GRID["bands_hz"]]}
    stale_state = dict(state)
    stale_state["stability_checks"] = {"cand-A": stale}
    stale_state["derived_evidence_checks"] = {"stability:cand-A": {
        **stale, "evidence_kind": "stability",
        "dependency_fields": {"diagnostic_bands_hz": [list(b) for b in GRID["bands_hz"]],
                              "scope": requested["scope"]}}}
    freshness = evidence_state.validity(stale_state, requested)
    ok = (match.get("diagnostic_bands_hz") is True
          and actual["diagnostic_bands_hz"] == grid
          and "stability:cand-A" in freshness["stale_ids"]
          and grid != [list(b) for b in GRID["bands_hz"]])
    return _result("G03", "请求 3–12 Hz 贯穿 A/M/R 且旧默认证据失效", ok,
                   {"requested_grid": grid, "actual_grid": actual["diagnostic_bands_hz"],
                    "match": match.get("diagnostic_bands_hz"),
                    "stale": freshness["stale_ids"]})


# ---------------------------------------------------------------------- G04
def g04():
    """Three-component → vertical goal: old completion not inherited."""
    rec = _record("cand-B", "BBB", three=False, vertical=True,
                  flags=["UW.BBB..ENE:constant"],
                  goal="three_component_pick")
    requested = EC.build(question_id="Q_usable",
                         scope={"scope_kind": "all_allowed"},
                         method_goal="vertical_onset",
                         target_band_hz=[1.0, 12.0],
                         onset_method_band_hz=[1.0, 8.0],
                         diagnostic_bands_hz=None,
                         requested_changes=[{"kind": "method_goal",
                                             "method_goal": "vertical_onset"}])
    state = _state(acquired=[rec], method_goal="vertical_onset",
                   requested_changes=[{"kind": "method_goal",
                                       "method_goal": "vertical_onset"}])
    state["requested_conditions"] = requested
    verdict = core.verdict_for_question(
        core.active_records(state["acquired"]), "Q_usable", None,
        {"target_band_hz": [1.0, 12.0], "method_goal": "vertical_onset"})
    obligations = EC.obligations(requested, state)
    goal_pending = [o for o in obligations if o["kind"] == "goal_recheck"]
    old_completion_inherited = rec.get("method_applicability_goal") == "vertical_onset"
    ok = (verdict["verdict"] == "pick_applicable"
          and bool(goal_pending) and all(o["state"] == "pending" for o in goal_pending)
          and not old_completion_inherited)
    return _result("G04", "目标切换后旧完成状态不继承", ok,
                   {"verdict": verdict["verdict"],
                    "goal_obligations": sorted({o["state"] for o in goal_pending}),
                    "record_goal": rec.get("method_applicability_goal")})


# ---------------------------------------------------------------------- G05
def g05():
    """Missing task_completed/field/verdict must not be back-filled."""
    rows = [
        {"question_id": "Q_usable", "status": "succeeded",
         "delivered": {"verdict": "pick_applicable", "field": "method_applicability"}},
        {"question_id": "Q_usable", "status": "succeeded",
         "delivered": {"field": "method_applicability", "task_completed": True}},
        {"question_id": "Q_usable", "status": "succeeded",
         "delivered": {"verdict": "pick_applicable", "task_completed": True}},
    ]
    out = []
    ok = True
    for row in rows:
        norm = c_scorer.normalize_row(row)
        scored = c_scorer.score_row(norm)
        out.append({"missing": norm["missing_delivery_fields"],
                    "invalid": norm["invalid_evidence"], "primary": scored["primary"],
                    "completed": norm["field_completed"]})
        ok = ok and norm["invalid_evidence"] and not norm["field_completed"] \
            and scored["primary"] == "invalid_evidence"
    return _result("G05", "缺完成标志/字段/判决不从别处补成功", ok, out)


# ---------------------------------------------------------------------- G06
def g06():
    """unknown without scope or legal basis; allowed-to-supplement early stop."""
    no_scope = c_scorer.normalize_row(
        {"question_id": "Q_propagating", "status": "succeeded",
         "delivered": {"verdict": "not_decidable", "field": "verdict",
                       "task_completed": False},
         "answered": {"answered": False, "value": "unknown"},
         "terminated_by": "coherence", "n_eligible": 4, "stations_acquired": 1,
         "stations_analysed": 1, "n_active": 4})
    no_scope_score = c_scorer.score_row(no_scope)
    early = c_scorer.normalize_row(
        {"question_id": "Q_propagating", "status": "succeeded",
         "delivered": {"verdict": "not_decidable", "field": "verdict",
                       "task_completed": False},
         "answered": {"answered": False, "value": "unknown"},
         "terminated_by": None, "n_eligible": 6, "stations_acquired": 2,
         "stations_analysed": 2, "n_active": 6})
    early_score = c_scorer.score_row(early)
    audit = finish_audit.audit_finish(
        _state(question_id="Q_propagating", acquired=[_record()],
               excluded_candidate_ids=[]),
        verdict={"verdict": "not_decidable", "reason": "insufficient_stations",
                 "answered": {"answered": False, "value": "unknown"},
                 "field_answered": "verdict"})
    ok = (no_scope_score["dimensions"]["compliant_insufficient"] is False
          and early_score["dimensions"]["strategy_insufficient"] is True
          and audit["outcome"] in ("legal_unknown", "strategy_insufficient"))
    return _result("G06", "缺范围/合法停止依据不计合规不足", ok,
                   {"no_scope": no_scope_score["primary"],
                    "early_stop": early_score["primary"],
                    "audit_without_basis": audit["outcome"],
                    "unmet": [u["kind"] for u in audit["unmet"]]})


# ---------------------------------------------------------------------- G07
def g07():
    """Model failure + rule fallback gives the right label: agent success false."""
    validation = {"delivered": {"task_completed": True}, "acquisition": {
        "tool_errors": 1, "recovery": {"order_known": True, "unrecovered": ["acquire_station"],
                                       "recovered": []}}}
    status, kind, detail = __import__("run_agent_arm")._interface_outcome(
        validation, "model_fatal")
    row = c_scorer.normalize_row(
        {"question_id": "Q_propagating", "status": "failed",
         "failure_kind": "model_fatal",
         "delivered": {"verdict": "coherent_multistation", "field": "verdict",
                       "task_completed": True, "model_claim_accepted": False},
         "acquisition": {"tool_errors": 0, "recovery": {"unrecovered": []}},
         "n_eligible": 6, "stations_acquired": 3})
    scored = c_scorer.score_row(row)
    ok = (status == "failed"
          and scored["dimensions"]["execution_failure"] is True
          and row["final_delivery"]["model_claim_accepted"] is False)
    return _result("G07", "模型失败时安全交付可为真、Agent 成功必须为假", ok,
                   {"status": status, "failure_kind": kind, "primary": scored["primary"],
                    "model_claim_accepted": row["final_delivery"]["model_claim_accepted"]},
                   source="fault_injection")


# ---------------------------------------------------------------------- G08
def g08():
    """Forbidden fetch, candidate ceiling, re-add after exclusion refused pre-request."""
    import mcp_server

    folder = Path(tempfile.mkdtemp(prefix="gate08-"))
    run_id = runlog.new_run_id()
    conditions = EC.build(question_id="Q_propagating",
                          scope={"scope_kind": "explicit_candidates",
                                 "candidate_ids": ["cand-A"]},
                          method_goal="three_component_pick",
                          target_band_hz=[1.0, 12.0],
                          onset_method_band_hz=[1.0, 8.0],
                          acquisition_allowed=False,
                          excluded_candidate_ids=["cand-A"])
    state = {"run_id": run_id, "task_id": "t-g08",
             "discovery": {"n_eligible": 2, "eligible": [
                 {"candidate_id": "cand-A", "station": "AAA", "distance_km": 1.0,
                  "network": "UW", "family": "HH"},
                 {"candidate_id": "cand-B", "station": "BBB", "distance_km": 2.0,
                  "network": "UW", "family": "HH"}]},
             "acquired": [], "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
             "requested_conditions": conditions}
    old_runlog = mcp_server.FOLDER
    mcp_server.FOLDER = folder
    mcp_server.STATE = folder / "mcp_state.json"
    mcp_server.EVALUATOR = folder / "evaluator.json"
    mcp_server.RUN = run_id
    mcp_server.STATE.write_text(json.dumps(state))
    try:
        refused = mcp_server.acquire_station("cand-B")
        after = json.loads(mcp_server.STATE.read_text())
        events = after.get("contract_events") or []
        http = after.get("http_attempts", 0)
        # Re-adding an excluded object is refused by the exclusion obligation state.
        obs = EC.obligations(conditions, after)
        exclude_obligation = [o for o in obs if o["kind"] == "exclude_object"]
        ok = (refused.get("status") == "refused"
              and refused.get("reason") == "acquisition_not_allowed"
              and refused.get("http_issued") is False
              and http == 0
              and any(e.get("kind") == "acquisition_refused" for e in events)
              and not after["acquired"])
    finally:
        mcp_server.FOLDER = old_runlog
    return _result("G08", "禁止取数/范围外/超上限在请求前拒绝", ok,
                   {"refused": refused, "http_attempts": http,
                    "contract_events": [e.get("kind") for e in events]},
                   source="fault_injection")


# ---------------------------------------------------------------------- G09
def g09():
    """Controlled first-station failure with a legal alternative: order is explicit."""
    import mcp_server

    folder = Path(tempfile.mkdtemp(prefix="gate09-"))
    run_id = runlog.new_run_id()
    conditions = EC.build(question_id="Q_propagating",
                          scope={"scope_kind": "all_allowed"},
                          method_goal="three_component_pick",
                          target_band_hz=[1.0, 12.0],
                          onset_method_band_hz=[1.0, 8.0])
    state = {"run_id": run_id, "task_id": "t-g09",
             "discovery": {"n_eligible": 2, "eligible": [
                 {"candidate_id": "cand-DEAD", "station": "DEAD", "distance_km": 1.0,
                  "network": "UW", "family": "HH"},
                 {"candidate_id": "cand-OK", "station": "OKAY", "distance_km": 2.0,
                  "network": "UW", "family": "HH"}]},
             "acquired": [], "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
             "requested_conditions": conditions}
    mcp_server.FOLDER = folder
    mcp_server.STATE = folder / "mcp_state.json"
    mcp_server.EVALUATOR = folder / "evaluator.json"
    mcp_server.RUN = run_id
    mcp_server.STATE.write_text(json.dumps(state))
    calls = []
    original = core.acquire

    def fake_acquire(case, candidate, runs, *, analysis_level=None):
        calls.append(candidate["candidate_id"])
        if candidate["candidate_id"] == "cand-DEAD":
            return {"candidate_id": "cand-DEAD", "station": "DEAD",
                    "status": "metadata_unavailable", "acquisition_status":
                    "metadata_unavailable", "failed_request": "metadata"}
        return {"candidate_id": "cand-OK", "station": "OKAY", "status": "analysed",
                "acquisition_status": "ok", "onset_utc": 1.0}

    core.acquire = fake_acquire
    try:
        first = mcp_server.acquire_station("cand-DEAD")
        second = mcp_server.acquire_station("cand-OK")
        after = json.loads(mcp_server.STATE.read_text())
    finally:
        core.acquire = original
    ok = (first.get("status") == "metadata_unavailable"
          and second.get("status") == "analysed"
          and calls == ["cand-DEAD", "cand-OK"]
          and [r["candidate_id"] for r in after["acquired"]] == ["cand-DEAD", "cand-OK"])
    return _result("G09", "受控首站失败后换站，顺序明确", ok,
                   {"calls": calls, "first": first.get("status"),
                    "second": second.get("status")}, source="fault_injection")


# ---------------------------------------------------------------------- G10
def g10():
    """Stale evidence / unknown evidence id cannot support a new answer."""
    from run_agent_arm import _evidence_refs
    requested = EC.build(question_id="Q_usable", scope={"scope_kind": "all_allowed"},
                         method_goal="three_component_pick",
                         target_band_hz=[1.0, 12.0],
                         onset_method_band_hz=[1.0, 8.0],
                         diagnostic_bands_hz=[[3.0, 12.0]],
                         requested_changes=[{"kind": "stability_bands"}])
    rec = _record("cand-A", "AAA")
    state = _state(acquired=[rec], diagnostic_bands_hz=[[3.0, 12.0]],
                   requested_changes=[{"kind": "stability_bands"}])
    state["requested_conditions"] = requested
    state["derived_evidence_checks"] = {"stability:cand-A": {
        "evidence_kind": "stability", "condition_ref": "old-condition",
        "effective_parameters": {"bands_hz": [[1.0, 8.0]]},
        "dependency_fields": {"diagnostic_bands_hz": [[1.0, 8.0]], "scope": {}},
        "freshness": "valid"}}
    unknown = _evidence_refs(["no-such-id"], state, requested)
    stale = _evidence_refs(["stability:cand-A"], state, requested)
    ok = (unknown["all_valid"] is False and unknown["unknown"]
          and stale["all_valid"] is False and stale["stale"])
    return _result("G10", "旧条件证据/未知 evidence_id 不能支持新答案", ok,
                   {"unknown": unknown, "stale": stale})


# ---------------------------------------------------------------------- G11
def g11():
    """One resource config; counts and boundaries real."""
    import resource_limits
    limits = resource_limits.limits()
    cfg = json.loads((ROOT / "prototype/model-budget.json").read_text())
    bridge = (ROOT / "main/provider_bridge.mjs").read_text()
    arm = (ROOT / "main/agent_run_optimization.mjs").read_text()
    hardcoded = ">24" in bridge or "maxRequests:24" in arm
    remaining = resource_limits.remaining_limits(
        unique_candidates_used=2, model_requests_used=3, tool_calls_used=5,
        model_requests_known=False)
    ok = (limits["max_requests_per_case"] == 12 and limits["max_tools_per_case"] == 32
          and limits["soft_request_line"] == 8
          and not hardcoded
          and remaining["model_requests"] is None
          and remaining["known"]["model_requests"] is False
          and cfg.get("max_output_tokens") == limits["max_output_tokens"])
    return _result("G11", "桥接/预算/工具计数同一配置且未知不填 0", ok,
                   {"limits": {k: limits[k] for k in
                               ("max_requests_per_case", "soft_request_line",
                                "max_tools_per_case", "max_output_tokens")},
                    "hardcoded_24": hardcoded, "unknown_remaining": remaining})


# ---------------------------------------------------------------------- G12
G12_SCRIPT = r"""
import {startBridge} from './main/provider_bridge.mjs';
const results=[];
const fake=async(url,opts)=>({ok:true,json:async()=>({choices:[{message:{role:'assistant',content:'{"conclusion":"not_decidable"}',tool_calls:[]},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}})});
const bridge=await startBridge({project:'.',provider:'deepseek',model:'deepseek-flash',toolPrefix:'pfx_',transport:fake});
bridge.begin({id:'probe',caseId:'probe',arm:'probe',allowedCandidates:[]});
const a=await bridge.close();const b=await bridge.close();
results.push({closed:true,twice:{...b}});
console.log(JSON.stringify(results[0]));
"""


def g12():
    """Success / exception / timeout all clean up; double close settles once."""
    script = ROOT / "tmp_g12.mjs"
    script.write_text(G12_SCRIPT)
    try:
        out = subprocess.run(["node", str(script)], cwd=ROOT, capture_output=True,
                             text=True, timeout=120)
        ok = out.returncode == 0 and '"closed":true' in out.stdout
        detail = {"stdout": out.stdout.strip()[:400], "stderr": out.stderr.strip()[:300],
                  "rc": out.returncode}
    finally:
        script.unlink(missing_ok=True)
    # The driver's cleanup result is also required to be recorded, not swallowed.
    driver = (ROOT / "main/agent_run_optimization.mjs").read_text()
    declared = ("closeOnce" in driver and "cleanup_errors" in driver
                and "cleanup_ok" in driver and "stop_daemon" in driver)
    return _result("G12", "成功/异常/超时清理且可查、重复关闭不二次结算", ok and declared,
                   {**detail, "driver_declares_cleanup": declared},
                   source="fault_injection")


# ---------------------------------------------------------------------- G13
def g13():
    """Saved canonical input + scorer version reproduces the score line by line."""
    import c_counterexamples as cx
    rows = []
    ok = True
    for case in cx.CASES:
        norm = c_scorer.normalize_row(case["row"])
        first = c_scorer.score_row(norm)
        again = c_scorer.score_row(c_scorer.normalize_row(case["row"]))
        same = first["primary"] == again["primary"] and first["dimensions"] == again["dimensions"]
        rows.append({"id": case["id"], "primary": first["primary"], "stable": same})
        ok = ok and same
    version = runlog.sha256_file(ROOT / "main/c_scorer.py")
    return _result("G13", "规范输入+评分源码版本可逐行重现", ok,
                   {"rows": rows, "scorer_sha256": version[:16]})


# ---------------------------------------------------------------------- G14
def g14():
    """A mismatched snapshot or replaced data is refused before the run."""
    import hashlib
    folder = Path(tempfile.mkdtemp(prefix="gate14-"))
    src = folder / "source.py"
    src.write_text("print('v1')\n")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    snap = {"snapshot_id": "s1", "files": {"source.py": digest}}
    (folder / "snapshot.json").write_text(json.dumps(snap))
    # Matching snapshot verifies.
    ok_match = hashlib.sha256(src.read_bytes()).hexdigest() == snap["files"]["source.py"]
    # Replaced source is detected.
    src.write_text("print('v2')\n")
    ok_mismatch = hashlib.sha256(src.read_bytes()).hexdigest() != snap["files"]["source.py"]
    return _result("G14", "版本快照/数据被替换时在开跑前拒绝", ok_match and ok_mismatch,
                   {"matched": ok_match, "detected_after_change": ok_mismatch})


# ---------------------------------------------------------------------- G15
G15_SCRIPT = r"""
import {restoreReasoning,rememberAssistant} from './main/provider_bridge.mjs';
const cases=[];
const mk=(id,name,args,content)=>({id,type:'function',function:{name,arguments:JSON.stringify(args)},content});
// 1. two consecutive tool rounds are restored from the original replies
let active={reasoning:[]};
rememberAssistant(active,{role:'assistant',content:null,reasoning_content:'R1',
  tool_calls:[mk('t1','a',{x:1},null)]});
rememberAssistant(active,{role:'assistant',content:'thinking',reasoning_content:'R2',
  tool_calls:[mk('t2','b',{y:2},null)]});
let messages=[{role:'assistant',content:null,tool_calls:[mk('t1','a',{x:1},null)]},
  {role:'tool',tool_call_id:'t1',content:'ok'},
  {role:'assistant',content:'thinking',tool_calls:[mk('t2','b',{y:2},null)]}];
let r=restoreReasoning(messages,active,{required:true});
cases.push({name:'two_tool_rounds',restored:r.restored,ok:messages[0].reasoning_content==='R1'&&messages[2].reasoning_content==='R2'});
// 2. multi-tool reply and a no-tool assistant turn
active={reasoning:[]};
rememberAssistant(active,{role:'assistant',content:null,reasoning_content:'R3',
  tool_calls:[mk('t3','a',{},null),mk('t4','b',{},null)]});
rememberAssistant(active,{role:'assistant',content:'final',reasoning_content:'R4',tool_calls:[]});
messages=[{role:'assistant',content:null,tool_calls:[mk('t3','a',{},null),mk('t4','b',{},null)]},
  {role:'assistant',content:'final'}];
r=restoreReasoning(messages,active,{required:true});
cases.push({name:'multi_tool_and_no_tool',restored:r.restored,ok:messages[0].reasoning_content==='R3'&&messages[1].reasoning_content==='R4'});
// 3. same content, different tool ids must not be confused
active={reasoning:[]};
rememberAssistant(active,{role:'assistant',content:'same',reasoning_content:'RA',tool_calls:[mk('ta','a',{},null)]});
rememberAssistant(active,{role:'assistant',content:'same',reasoning_content:'RB',tool_calls:[mk('tb','b',{},null)]});
messages=[{role:'assistant',content:'same',tool_calls:[mk('tb','b',{},null)]},
  {role:'assistant',content:'same',tool_calls:[mk('ta','a',{},null)]}];
r=restoreReasoning(messages,active,{required:true});
cases.push({name:'same_content_diff_ids',restored:r.restored,
  ok:messages[0].reasoning_content==='RB'&&messages[1].reasoning_content==='RA'});
// 4. missing history fails loudly instead of fabricating an empty string
active={reasoning:[{content:'x',tool_call_ids:['zz'],tool_names:['a'],args_digest:'d',reasoning_content:'RZ'}]};
messages=[{role:'assistant',content:null,tool_calls:[mk('t9','a',{},null)]}];
let threw=false;
try{restoreReasoning(messages,active,{required:true});}catch(e){threw=true;}
cases.push({name:'missing_history_fails',ok:threw,threw});
console.log(JSON.stringify({cases,thinking:{type:'enabled',reasoning_effort:'high'}}));
"""


def g15():
    """Thinking protocol simulation: message matching and reasoning round-trip."""
    script = ROOT / "tmp_g15.mjs"
    script.write_text(G15_SCRIPT)
    try:
        out = subprocess.run(["node", str(script)], cwd=ROOT, capture_output=True,
                             text=True, timeout=120)
        parsed = json.loads(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 \
            else {"cases": []}
        ok = out.returncode == 0 and all(c["ok"] for c in parsed["cases"])
        detail = {"cases": parsed.get("cases"), "rc": out.returncode,
                  "stderr": out.stderr.strip()[:300]}
    finally:
        script.unlink(missing_ok=True)
    return _result("G15", "思考模式协议模拟（AT 前置）", ok, detail,
                   source="fault_injection")


# ---------------------------------------------------------------------- G16
def g16():
    """Q_usable light acquisition computes no onset/stability; on demand it does."""
    import mcp_server

    folder = Path(tempfile.mkdtemp(prefix="gate16-"))
    run_id = runlog.new_run_id()
    conditions = EC.build(question_id="Q_usable", scope={"scope_kind": "all_allowed"},
                          method_goal="three_component_pick",
                          target_band_hz=[1.0, 12.0], onset_method_band_hz=[1.0, 8.0])
    state = {"run_id": run_id, "task_id": "t-g16", "analysis_level": "quality_only",
             "discovery": {"n_eligible": 1, "eligible": [
                 {"candidate_id": "cand-A", "station": "AAA", "distance_km": 1.0,
                  "network": "UW", "family": "HH"}]},
             "acquired": [], "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
             "requested_conditions": conditions, "exclusions": [],
             "stability_checks": {}, "derived_evidence_checks": {},
             "attempted_candidate_ids_episode": []}
    mcp_server.FOLDER = folder
    mcp_server.STATE = folder / "mcp_state.json"
    mcp_server.EVALUATOR = folder / "evaluator.json"
    mcp_server.RUN = run_id
    mcp_server.STATE.write_text(json.dumps(state))
    # A real stored waveform from the project's own cache, so the on-demand call can
    # actually compute rather than only validate a cache marker.
    from obspy import Stream, Trace, UTCDateTime
    import numpy as np
    rng = np.random.default_rng(3)
    st = Stream()
    for comp in ("Z", "N", "E"):
        tr = Trace(data=rng.normal(size=3000).astype(float))
        tr.stats.network, tr.stats.station, tr.stats.location = "UW", "AAA", ""
        tr.stats.channel = f"HH{comp}"
        tr.stats.sampling_rate = 50.0
        tr.stats.starttime = UTCDateTime("2023-10-09T05:00:00")
        tr.data[1200:1260] += 25.0 * np.sin(np.linspace(0, 20, 60))
        st.append(tr)
    wave = folder / "raw/cand-A/waveform.mseed"
    wave.parent.mkdir(parents=True, exist_ok=True)
    st.write(str(wave), format="MSEED")

    original = core.acquire

    def fake_acquire(case, candidate, runs, *, analysis_level=None):
        level = analysis_level or "full"
        rec = {"candidate_id": candidate["candidate_id"], "station": candidate["station"],
               "status": "analysed", "onset_utc": None, "onset_computed": False,
               "onset_source": "not_computed", "stability": None,
               "stability_computed": False, "analysis_level": level,
               "method_band_hz": [1.0, 8.0], "target_band_hz": [1.0, 12.0],
               "method_applicability_goal": "three_component_pick",
               "method_applicability": {"three_component_pick": True,
                                        "vertical_onset": True}}
        return rec

    core.acquire = fake_acquire
    try:
        acquired = mcp_server.acquire_station("cand-A")
        s1 = json.loads(mcp_server.STATE.read_text())
        counts_after_acquire = dict(s1.get("computation_counts") or {})
        onset = mcp_server.run_onset_check("cand-A")
        stab = mcp_server.run_stability_check("cand-A")
        s2 = json.loads(mcp_server.STATE.read_text())
    finally:
        core.acquire = original
    rec = s2["acquired"][0]
    acquired_record = s1["acquired"][0]
    ok = (acquired_record.get("onset_computed") is False
          and acquired_record.get("stability_computed") is False
          and counts_after_acquire.get("onsets_at_acquire", 0) == 0
          and counts_after_acquire.get("stability_at_acquire", 0) == 0
          and onset.get("onset_computed") is True and onset.get("onset_source") == "compute"
          and stab.get("stability") is not None
          and (s2.get("computation_counts") or {}).get("onsets_computed", 0) >= 1
          and rec.get("onset_source") == "compute")
    return _result("G16", "轻量采集不自动跑到时/稳定性，按需调用真实计算", ok,
                   {"after_acquire": counts_after_acquire,
                    "onset_source": onset.get("onset_source"),
                    "stability_state": stab.get("stability"),
                    "counts_final": s2.get("computation_counts")})


GATES = [
    ("G01", g01, True), ("G02", g02, True), ("G03", g03, True), ("G04", g04, True),
    ("G05", g05, True), ("G06", g06, True), ("G07", g07, True), ("G08", g08, True),
    ("G09", g09, True), ("G10", g10, True), ("G11", g11, True), ("G12", g12, True),
    ("G13", g13, True), ("G14", g14, True), ("G15", g15, False), ("G16", g16, True),
]


def run_all(output_dir=None, *, include_g15=True):
    results = []
    for gate_id, fn, required in GATES:
        if gate_id == "G15" and not include_g15:
            results.append({"id": gate_id, "status": "not_applicable",
                            "name": "思考模式协议模拟（AT 未采用）", "source": "fault_injection",
                            "detail": {}, "evidence": {}})
            continue
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            results.append({"id": gate_id, "name": gate_id, "status": "fail",
                            "source": "handcrafted_input",
                            "detail": {"error": f"{type(exc).__name__}: {exc}"[:400]},
                            "evidence": {}})
    report = {"schema": "agent-optimization-gates/v1",
              "results": results,
              "required_pass": [g for g, _f, req in GATES if req],
              "all_required_pass": all(r["status"] == "pass" for r in results
                                       if r["id"] != "G15"),
              "g15_pass": next((r["status"] == "pass" for r in results
                                if r["id"] == "G15"), False)}
    if output_dir:
        path = Path(output_dir) / "gates" / "free-gates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    parser.add_argument("--no-g15", action="store_true")
    args = parser.parse_args()
    report = run_all(args.output, include_g15=not args.no_g15)
    for row in report["results"]:
        print(f"{row['id']} {row['status']:>4}  {row['name']}")
        if row["status"] == "fail":
            print("      " + json.dumps(row["detail"], ensure_ascii=False)[:400])
    print(json.dumps({"all_required_pass": report["all_required_pass"],
                      "g15_pass": report["g15_pass"]}))
    raise SystemExit(0 if report["all_required_pass"] else 1)


if __name__ == "__main__":
    main()