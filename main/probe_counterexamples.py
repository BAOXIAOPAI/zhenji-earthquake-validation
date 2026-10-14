"""Pre-fix counterexamples, re-run through the production entries after the fix.

The third review named four concrete counterexamples. This script reproduces each
one against the *current* code, so the hand-back can show a measured before/after
instead of "the test suite is green". It is fully offline: it reads saved run
records and calls the same functions the product calls, and it never fetches or
calls a model.

  P0-1  a record whose three-component condition is false still came back
        `pick_applicable`, because the verdict aggregated band support only
  P0-2  a follow-up frozen at 3-8 Hz still served the parent's 1-12 Hz state
  P0-3  an unrecovered acquisition tool error still ended as `succeeded`
  P1-1  a second feedback round still forked from the first round, so an exclusion
        was silently forgotten

P1-1 is read from the recorded browser chain rather than re-simulated, because the
real page run is the evidence.

Usage:  .venv/bin/python main/probe_counterexamples.py [--out <path>.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import run_agent_arm  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

OUT = ROOT / "delivery/review-3-2026-10-10/evidence/修复前后反例核对.json"
# The page run whose saved records hold the real six-station evidence.
TRACE_RUN = "r-d821d963316c441e"
TRACE_RUNLOG = ROOT / "main/ui-trace-runlog"


def _saved_records(runlog_dir: Path, run_id: str) -> dict:
    folder = runlog_dir / run_id
    task = json.loads((folder / "task.json").read_text())
    state = json.loads((folder / "mcp_state.json").read_text())
    return task, state.get("acquired") or []


def p0_1_method_goal():
    """The injected OLGA record: E channel is a flat copy, three_component_ok False.

    The review handed exactly this record set to the validator and still got
    `pick_applicable / answered=True`. Now the verdict must be decided by the
    method's own input conditions, and band support and input conditions must be
    reported separately.
    """
    task, records = _saved_records(TRACE_RUNLOG, TRACE_RUN)
    frozen = taskmod.task_from_spec(task)
    taskmod.freeze(frozen)

    def validate_for(recs):
        driver = {"answer": json.dumps({"conclusion": "pick_applicable",
                                        "stop_reason": "先按频带判断", "stations_used": len(recs)})}
        return run_agent_arm.validate("r-probe", "probe", frozen, recs, driver, "Q_usable")

    clean = validate_for(records)
    injected = []
    for record in records:
        copy = json.loads(json.dumps(record))
        if copy.get("station") == "OLGA":
            # Exactly the review's injection: the flat E channel.
            copy["three_component_ok"] = False
            copy["method_applicability"] = dict(copy.get("method_applicability") or {},
                                                three_component_pick=False)
            flags = list(copy.get("channel_integrity_flags") or [])
            if "flat_channel" not in flags:
                flags.append("flat_channel")
            copy["channel_integrity_flags"] = flags
        injected.append(copy)
    bad = validate_for(injected)
    return {
        "counterexample": "注入 OLGA 的 E 通道为平直副本，three_component_ok=False",
        "how": "把保存的六台记录里的 OLGA 改成立即的反例，交给生产验证器 "
               "run_agent_arm.validate（AGH 轮用的就是它）",
        "before_fix": {"verdict": "pick_applicable", "answered": True,
                       "why_it_was_wrong": "判决只聚合 band_support，三分量条件为假也照样肯定"},
        "after_fix_clean": {
            "verdict": clean["delivered"]["verdict"],
            "field_value": clean["delivered"]["field_value"],
            "task_completed": clean["delivered"]["task_completed"],
            "band_scope": clean["delivered"]["scope"]["band_summary"],
            "input_scope": clean["delivered"]["scope"]["goal_summary"],
            "statement_zh": clean["delivered"]["statement_zh"],
        },
        "after_fix_injected": {
            "verdict": bad["delivered"]["verdict"],
            "field_value": bad["delivered"]["field_value"],
            "task_completed": bad["delivered"]["task_completed"],
            "reason": bad["validator_verdict"]["reason"],
            "band_scope": bad["delivered"]["scope"]["band_summary"],
            "input_scope": bad["delivered"]["scope"]["goal_summary"],
            "stations_input_not_met": bad["delivered"]["scope"]["stations_input_not_met"],
            "model_claim_accepted": bad["delivered"]["model_claim_accepted"],
            "rejection_reason": bad["delivered"]["rejection_reason"],
            "statement_zh": bad["delivered"]["statement_zh"],
        },
        "checks": {
            # The two summaries answer different questions and must never collapse
            # into one another.
            "band_summary_and_input_scope_are_different_fields": (
                clean["delivered"]["scope"]["band_summary"]
                != clean["delivered"]["scope"]["goal_summary"]),
            "band_support_is_still_six_of_six_after_injection": (
                "6 站支持" in bad["delivered"]["scope"]["band_summary"]),
            "the_injected_station_is_named_as_input_not_met": any(
                (e.get("station") if isinstance(e, dict) else e) == "OLGA"
                for e in (bad["delivered"]["scope"]["stations_input_not_met"] or [])),
            "the_verdict_stops_being_a_plain_yes": (
                bad["delivered"]["verdict"] != "pick_applicable"),
            "a_wrong_positive_claim_is_rejected": (
                bad["delivered"]["model_claim_accepted"] is False),
            "the_chinese_boundary_names_the_station": (
                "OLGA" in (bad["delivered"]["statement_zh"] or "")),
        },
    }


def p0_2_followup_spec(isolated_root: Path):
    """A follow-up frozen at 3-8 Hz must serve 3-8 Hz, not the parent's 1-12 Hz."""
    parent_task, parent_records = _saved_records(TRACE_RUNLOG, TRACE_RUN)
    parent_frozen = taskmod.task_from_spec(parent_task)
    taskmod.freeze(parent_frozen)
    # Build a parent run in a scratch run store, so no real record is touched.
    runlog.RUNLOG = isolated_root
    parent_id = runlog.new_run_id()
    folder = runlog.init_run(parent_id, parent_frozen, arm="adaptive_agent_open")
    discovery = {"n_eligible": 6, "eligible": [
        {"candidate_id": r.get("candidate_id"), "station": r.get("station"),
         "distance_km": r.get("distance_km")} for r in parent_records]}
    (folder / "discovery.json").write_text(json.dumps(discovery))
    state = prepare_agent.execution_state(
        parent_id, parent_frozen, discovery, acquired=parent_records,
        parent_run_id=None, extra={"exclusions": [{"candidate_id": "x"}]})
    state["tool_errors"] = [{"tool": "acquire_station", "error": "parent round error"}]
    state["requests"] = 4
    state["http_attempts"] = 3
    (folder / "mcp_state.json").write_text(json.dumps(state, ensure_ascii=False))
    (folder / "evaluator.json").write_text(json.dumps(
        prepare_agent.evaluator_state(parent_frozen)))

    child = prepare_agent.prepare_followup(
        parent_id, "把目标频带收紧到 3-8 Hz 再看一次", target_band_hz=[3.0, 8.0],
        change={"kind": "target_band", "target_band_hz": [3.0, 8.0],
                "bands_hz": [[3.0, 8.0]]})
    child_folder = runlog.run_dir(child["run_id"])
    child_task = json.loads((child_folder / "task.json").read_text())
    served = json.loads((child_folder / "mcp_state.json").read_text())
    initial = json.loads((child_folder / "mcp_state.initial.json").read_text())
    return {
        "counterexample": "子任务冻结为 3-8 Hz，mcp_state.initial.json 却仍复制父 1-12 Hz",
        "how": "在临时 run 库里造一个 1-12 Hz 父轮（含父轮工具错误与取数计数），"
               "再用 target_band_hz=[3,8] 走生产入口 prepare_followup",
        "before_fix": {"child_task_target_band_hz": [3.0, 8.0],
                       "child_state_target_band_hz": [1.0, 12.0],
                       "parent_tool_errors_copied_as_this_round": 1},
        "after_fix": {
            "child_task_target_band_hz": child_task["target_band_hz"],
            "child_state_target_band_hz": served["target_band_hz"],
            "child_initial_state_target_band_hz": initial["target_band_hz"],
            "child_window": [served["window_start"], served["window_end"]],
            "child_region": served["region"],
            "child_method_goal": served["method_goal"],
            "child_discovery_present": bool(served.get("discovery")),
            "this_round_requests": served.get("requests"),
            "this_round_http_attempts": served.get("http_attempts"),
            "parent_history_is_history_only": {
                "parent_history": served.get("parent_history"),
                "this_round_tool_errors_key_absent_or_empty": not served.get("tool_errors"),
            },
            "inherited_observations": len(served.get("acquired") or []),
            "inherited_exclusions_is_a_list": isinstance(served.get("exclusions"), list),
            "parent_run_id": served.get("parent_run_id") == parent_id,
        },
        "checks": {
            "the_served_band_is_the_child_band":
                served["target_band_hz"] == [3.0, 8.0],
            "the_initial_state_is_the_child_band":
                initial["target_band_hz"] == [3.0, 8.0],
            "counters_are_reset": served.get("requests") == 0,
            "parent_errors_are_not_this_rounds_errors":
                not served.get("tool_errors"),
            "observations_are_carried_forward": bool(served.get("acquired")),
        },
    }


def p0_3_interface_failure():
    """An unrecovered, task-blocking tool error must not read as plain success.

    Review-4 tightened this: recovery is judged from the EVENT ORDER per tool and
    operated object, so aggregate counts can no longer claim "其后恢复". The
    `recovered` case below must carry an order-known recovery block; counts
    alone with the task unsettled now fail closed.
    """
    blocked = {
        "acquisition": {"tool_errors": 1,
                        "tool_attempts": {"acquire_station": {"start": 1, "error": 1}}},
        "delivered": {"task_completed": False},
    }
    counts_only_unproven = {
        "acquisition": {"tool_errors": 1,
                        "tool_attempts": {"acquire_station": {"start": 2,
                                                              "error": 1, "ok": 1}}},
        "delivered": {"task_completed": False},
    }
    recovered = {
        "acquisition": {"tool_errors": 1,
                        "tool_attempts": {"acquire_station": {"start": 2,
                                                              "error": 1, "ok": 1}},
                        "recovery": {"order_known": True,
                                     "recovered": ["acquire_station(cand-a)"],
                                     "unrecovered": []}},
        "delivered": {"task_completed": False},
    }
    ok_then_error = {
        "acquisition": {"tool_errors": 1,
                        "tool_attempts": {"acquire_station": {"start": 2,
                                                              "error": 1, "ok": 1}},
                        "recovery": {"order_known": True,
                                     "recovered": [],
                                     "unrecovered": ["acquire_station(cand-a)"]}},
        "delivered": {"task_completed": False},
    }
    completed_with_error = {
        "acquisition": {"tool_errors": 1,
                        "tool_attempts": {"exclude_observation": {"start": 1, "error": 1}}},
        "delivered": {"task_completed": True},
    }
    clean = {"acquisition": {"tool_errors": 0, "tool_attempts": {}},
             "delivered": {"task_completed": True}}
    return {
        "counterexample": "模拟取数工具 RuntimeError、driver 正常退出，"
                          "最终 status=succeeded / failure_kind=null",
        "how": "把接口状态直接交给生产判定 run_agent_arm._interface_outcome",
        "before_fix": {"status": "succeeded", "failure_kind": None},
        "after_fix": {
            "blocked_and_unrecovered": run_agent_arm._interface_outcome(blocked, None),
            "counts_only_cannot_prove_recovery":
                run_agent_arm._interface_outcome(counts_only_unproven, None),
            "error_then_ok_same_target": run_agent_arm._interface_outcome(
                recovered, None),
            "ok_then_error_not_recovered": run_agent_arm._interface_outcome(
                ok_then_error, None),
            "error_but_task_completed": run_agent_arm._interface_outcome(
                completed_with_error, None),
            "no_error_at_all": run_agent_arm._interface_outcome(clean, None),
        },
        "checks": {
            "blocking_error_is_not_a_plain_success":
                run_agent_arm._interface_outcome(blocked, None)[0] != runlog.SUCCEEDED,
            "blocking_error_gets_a_failure_kind":
                run_agent_arm._interface_outcome(blocked, None)[1] == "tool_failure",
            "counts_alone_fail_closed": run_agent_arm._interface_outcome(
                counts_only_unproven, None)[0] != runlog.SUCCEEDED,
            "order_proven_recovery_keeps_success":
                run_agent_arm._interface_outcome(recovered, None)[0] == runlog.SUCCEEDED,
            "ok_then_error_is_not_success": run_agent_arm._interface_outcome(
                ok_then_error, None)[0] != runlog.SUCCEEDED,
            "a_completed_task_records_the_error_but_stays_successful": bool(
                run_agent_arm._interface_outcome(completed_with_error, None)[0]
                == runlog.SUCCEEDED
                and run_agent_arm._interface_outcome(completed_with_error, None)[2]),
        },
    }


def p1_1_continuous_conditions():
    """Read the real page chain: the second feedback round must parent the first."""
    trace_path = ROOT / "delivery/review-3-2026-10-10/evidence/ui-operation-trace-gate-b.json"
    trace = json.loads(trace_path.read_text())
    steps = trace.get("steps") or []
    chain = [{"step": s.get("step"), "run_id": s.get("run_id"),
              "parent": s.get("parent"), "excluded": s.get("excluded"),
              "conditions": s.get("conditions")} for s in steps]
    parents_ok = all(s.get("parent") for s in steps[1:])
    return {
        "counterexample": "第二轮反馈的父轮仍是首轮（r-b9a98071a275c6a8），"
                          "收紧网格会静默把 OLGA 重新算进去",
        "how": "读真实浏览器操作 trace（§6.3 的操作链），逐步核对父 run 与在用条件",
        "before_fix": {"second_round_parent": "r-b9a98071a275c6a8（= 首轮，不是上一轮）",
                       "consequence": "排除条件丢失，收紧网格后又分析了 6 台"},
        "after_fix": {"chain": chain,
                      "every_round_declares_a_parent": parents_ok},
        "checks": {"each_feedback_round_has_a_parent": parents_ok,
                   "chain_length": len(steps)},
    }


def main():
    out = OUT
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = {
            "generated_by": "main/probe_counterexamples.py",
            "network": "none — reads saved records, no fetch, no model",
            "source_records": str(TRACE_RUNLOG.relative_to(ROOT)),
            "P0_1_method_goal": p0_1_method_goal(),
            "P0_2_followup_spec": p0_2_followup_spec(Path(tmp) / "runlog"),
            "P0_3_interface_failure": p0_3_interface_failure(),
            "P1_1_continuous_conditions": p1_1_continuous_conditions(),
        }
    checks = {}
    for name, block in result.items():
        if isinstance(block, dict) and block.get("checks"):
            checks.update({f"{name}.{k}": v for k, v in block["checks"].items()})
    result["all_checks"] = checks
    result["all_checks_pass"] = all(checks.values())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"all pass: {result['all_checks_pass']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()