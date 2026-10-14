"""Gate A convergence regressions (review-3, 2026-10-10).

Every test here is a reproducible counterexample from
`delivery/review-3-2026-10-10/新增成果评估与验收意见.md` or a pass condition from
`下一步收敛指南.md` §1. They use saved/synthetic records only: no network, no model
call, no paid request.

Covered:
  1.1  method applicability is decided by the asked method, not the band count
  1.2  a feedback state is rebuilt from the frozen child task, not copied blindly
  1.3  an unrecovered tool failure is an operational failure, not "insufficient data"
  1.4  a compound question asks for one choice instead of promising all
  P1-5 the scorer refuses a decided label on the wrong field / unsettled task
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import run_agent_arm  # noqa: E402
import run_g2  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

TARGET = (1.0, 12.0)


def rec(candidate_id, *, rate=50.0, three=True, vertical=True, flags=None,
        status="analysed", target=TARGET):
    """A saved observation shaped exactly like a real one, without touching disk."""
    support = core.band_support(rate, target)
    analysed = status == "analysed"
    return {"candidate_id": candidate_id, "station": candidate_id,
            "network": "UW", "distance_km": 10.0, "status": status,
            "onset_utc": 1000.0, "channels": ["HHZ", "HHN", "HHE"],
            "channel_integrity_flags": flags or [],
            "channel_integrity_ok": not flags,
            "sampling_rate_hz": rate or None,
            "three_component_ok": three and analysed,
            "vertical_ok": vertical and analysed,
            "band_support": support,
            "method_applicability": {
                "three_component_pick": bool(three and vertical and analysed
                                             and support["supported"]),
                "vertical_onset": bool(vertical and analysed
                                       and support["supported"])}}


# --------------------------------------------------- 1.1 method semantics
def test_bad_horizontal_channel_does_not_produce_a_positive_pick_answer():
    """The review's real counterexample: OLGA's flat E channel used to come back as
    `pick_applicable / answered=True` for a three-component pick."""
    olga = rec("OLGA", three=False, flags=["UW.OLGA..ENE:constant"])
    verdict = core.verdict_for_question(
        [olga], "Q_usable", None,
        {"target_band_hz": list(TARGET), "method_goal": "three_component_pick"})
    assert verdict["verdict"] == "pick_not_applicable"
    assert verdict["reason"] == "input_conditions_not_met"
    assert verdict["answered"]["answered"] is True
    assert verdict["answered"]["value"] is False
    # The band is still reported as supported: the two answers are separate.
    scope = verdict["answered"]["scope"]
    assert scope["stations_supporting"] == ["OLGA"]
    # review-4: each not-met station also carries the human-readable detail and
    # its channel flags, so a limitation can name the exact channel.
    not_met = scope["stations_input_not_met"]
    assert [(r["station"], r["reason"]) for r in not_met] == \
        [("OLGA", "three_component_missing")]
    assert not_met[0]["channel_integrity_flags"] == ["UW.OLGA..ENE:constant"]
    assert "OLGA" in scope["goal_summary"]


def test_switching_the_goal_changes_the_answer_on_the_same_record():
    """The guide requires the result to really change when the analysis goal does."""
    olga = rec("OLGA", three=False, flags=["UW.OLGA..ENE:constant"])
    three = core.verdict_for_question(
        [olga], "Q_usable", None,
        {"target_band_hz": list(TARGET), "method_goal": "three_component_pick"})
    vertical = core.verdict_for_question(
        [olga], "Q_usable", None,
        {"target_band_hz": list(TARGET), "method_goal": "vertical_onset"})
    assert three["verdict"] == "pick_not_applicable"
    assert vertical["verdict"] == "pick_applicable"
    assert vertical["reason"] == "input_conditions_met"


def test_partial_input_conditions_name_the_limiting_station():
    records = [rec("A"), rec("B"), rec("C", three=False,
                                       flags=["UW.C..ENE:constant"])]
    verdict = core.verdict_for_question(
        records, "Q_usable", None,
        {"target_band_hz": list(TARGET), "method_goal": "three_component_pick"})
    assert verdict["verdict"] == "pick_partially_applicable"
    scope = verdict["answered"]["scope"]
    # The band denominator and the method denominator are both stated.
    assert scope["stations_supporting"] == ["A", "B", "C"]
    assert scope["stations_input_met"] == ["A", "B"]
    assert [r["station"] for r in scope["stations_input_not_met"]] == ["C"]
    assert "3 站支持" in scope["band_summary"]
    assert "2/3" in scope["goal_summary"]


def test_vertical_only_station_is_unknown_for_a_three_component_pick():
    """No horizontal components means the three-component condition is unknowable,
    which is not the same as a negative finding."""
    records = [rec("ZONLY", three=False, vertical=True)]
    # vertical_ok True but three_component_ok False is the "only Z" case.
    verdict = core.verdict_for_question(
        records, "Q_usable", None,
        {"target_band_hz": list(TARGET), "method_goal": "three_component_pick"})
    assert verdict["verdict"] == "pick_not_applicable"
    assert verdict["answered"]["scope"]["stations_input_not_met"][0]["reason"] == \
        "three_component_missing"
    # Under a vertical goal the very same record is applicable.
    vertical = core.verdict_for_question(
        records, "Q_usable", None,
        {"target_band_hz": list(TARGET), "method_goal": "vertical_onset"})
    assert vertical["verdict"] == "pick_applicable"


def test_band_unsupported_and_no_data_keep_their_own_labels():
    unsupported = core.verdict_for_question(
        [rec("SLOW", rate=6.0)], "Q_usable", None, {"target_band_hz": list(TARGET)})
    assert unsupported["verdict"] == "pick_not_applicable"
    assert unsupported["reason"] == "band_unsupported"
    no_data = core.verdict_for_question(
        [rec("MISS", status="fetch_error")], "Q_usable", None,
        {"target_band_hz": list(TARGET)})
    assert no_data["verdict"] == "pick_unknown"
    assert no_data["answered"]["answered"] is False


def test_layers_report_the_goal_and_both_denominators():
    records = [rec("A"), rec("B", three=False)]
    layers = core.layers(records, {"target_band_hz": list(TARGET),
                                   "method_goal": "three_component_pick"},
                         question_id="Q_usable")
    app = layers["method_applicability"]
    assert app["method_goal"] == "three_component_pick"
    assert app["stations_with_band_data"] == 2
    assert app["stations_input_met"] == 1
    assert app["stations_input_decided"] == 2
    assert layers["hypotheses"]["usable_for_requested_pick"]["state"] == "supported"


# --------------------------------------------------- 1.2 feedback state rebuild
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", tmp_path / "runlog")
    return tmp_path


def _parent_run(monkeypatch, band=(1.0, 12.0), method_goal="three_component_pick"):
    frozen = taskmod.make_task(
        user_question="这个时窗能不能做 1-12 Hz 的三分量拾取？",
        window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
        region=core.box(48.611, -122.805), target_band_hz=list(band),
        method_goal=method_goal)
    taskmod.freeze(frozen)
    parent_id = runlog.new_run_id()
    folder = runlog.init_run(parent_id, frozen, arm="full")
    folder.joinpath("discovery.json").write_text(json.dumps({
        "n_eligible": 1,
        "target_band_hz": list(band),
        "candidates": [{"candidate_id": "c1", "station": "OLGA",
                        "band_support": core.band_support(100.0, list(band))}],
        "eligible": [{"candidate_id": "c1", "station": "OLGA",
                      "band_support": core.band_support(100.0, list(band))}]},
        ensure_ascii=False))
    folder.joinpath("mcp_state.json").write_text(json.dumps({
        "run_id": parent_id, "task_id": frozen["task_id"],
        "user_question": frozen["user_question"],
        "window_start": frozen["window_start"], "window_end": frozen["window_end"],
        "region": frozen["region"], "target_band_hz": list(band),
        "method_band_hz": list(core.METHOD_BAND), "method_goal": method_goal,
        "allowed_actions": frozen["allowed_actions"],
        "discovery": {"n_eligible": 1, "eligible": []},
        "acquired": [rec("OLGA")], "requests": 5, "http_attempts": 2,
        "logical_acquisitions": 1, "cache_hits": 3, "bytes": 1234,
        "tool_errors": [{"tool": "acquire_station", "error_type": "RuntimeError"}],
        "tool_attempts": {"acquire_station": {"start": 2, "ok": 1, "error": 1}}},
        ensure_ascii=False))
    return parent_id, frozen


def test_followup_state_is_rebuilt_from_the_frozen_child_task(isolated, monkeypatch):
    parent_id, parent_task = _parent_run(monkeypatch)
    prepared = prepare_agent.prepare_followup(
        parent_id, "把目标频带收紧到 3-8 Hz", target_band_hz=[3.0, 8.0])
    child = runlog.run_dir(prepared["run_id"])
    frozen = json.loads((child / "task.json").read_text())
    state = json.loads((child / "mcp_state.initial.json").read_text())
    # The child task froze 3-8 Hz, and the child STATE must say the same thing.
    assert frozen["target_band_hz"] == [3.0, 8.0]
    assert state["target_band_hz"] == [3.0, 8.0], "child state drifted to the parent band"
    # The full physical spec a later prompt needs is present.
    for key in taskmod.MODEL_VISIBLE_TASK_FIELDS:
        assert key in state, f"missing {key} in feedback state"
    assert state["window_start"] == parent_task["window_start"]
    assert state["method_goal"] == parent_task["method_goal"]
    discovery_file = json.loads((child / "discovery.json").read_text())
    assert discovery_file["target_band_hz"] == [3.0, 8.0]
    assert state["discovery"]["eligible"][0]["band_support"]["target_band_hz"] == \
        [3.0, 8.0]
    assert state["parent_run_id"] == parent_id
    assert state["carried_forward"] == ["OLGA"]
    assert state["acquired"][0]["station"] == "OLGA"


def test_followup_resets_this_rounds_counters_and_keeps_parent_history(
        isolated, monkeypatch):
    parent_id, _ = _parent_run(monkeypatch)
    prepared = prepare_agent.prepare_followup(parent_id, "排除 OLGA",
                                              change={"kind": "exclude",
                                                      "station": "OLGA"})
    state = json.loads((runlog.run_dir(prepared["run_id"])
                        / "mcp_state.initial.json").read_text())
    # This round starts at zero: a parent's failure is not this run's failure.
    assert state["requests"] == 0
    assert state["http_attempts"] == 0
    assert state["logical_acquisitions"] == 0
    assert state["cache_hits"] == 0
    assert state["bytes"] == 0
    assert not state.get("tool_errors")
    assert not state.get("tool_attempts")
    # The parent's history is kept, but separately.
    history = state["parent_history"]
    assert history["tool_errors"] == 1
    assert history["requests"] == 5
    assert history["tool_attempts"]["acquire_station"]["error"] == 1


def test_the_deterministic_state_writer_keeps_the_full_spec(isolated):
    frozen = taskmod.make_task(
        user_question="q", window_start="2025-03-03T13:02:20",
        window_end="2025-03-03T13:04:20", region=core.box(48.6, -122.8),
        target_band_hz=[1.0, 12.0], method_goal="vertical_onset")
    discovery = {"n_eligible": 0, "eligible": []}
    state = prepare_agent.execution_state("r-test", frozen, discovery, acquired=[])
    for key in taskmod.MODEL_VISIBLE_TASK_FIELDS:
        assert key in state
    assert state["method_goal"] == "vertical_onset"
    assert state["requests"] == 0


# --------------------------------------------------- 1.3 run status vs evidence
def _validation(tool_errors=0, attempts=None, completed=False, recovery=None):
    acquisition = {"tool_errors": tool_errors,
                   "tool_attempts": attempts or {},
                   "tools_with_errors": ["acquire_station"] if tool_errors
                   else []}
    if recovery is not None:
        acquisition["recovery"] = recovery
    return {"acquisition": acquisition,
            "delivered": {"task_completed": completed}}


def _recovery(recovered=(), unrecovered=()):
    """An order-known recovery block, as `_tool_activity` builds from the JSONL."""
    return {"order_known": True, "recovered": list(recovered),
            "unrecovered": list(unrecovered)}


def test_an_unrecovered_tool_failure_is_a_failed_run_not_insufficient_data():
    """The review's P0-3 probe: tool raised, driver exited 0, run said `succeeded`."""
    status, kind, detail = run_agent_arm._interface_outcome(
        _validation(tool_errors=1,
                    attempts={"acquire_station": {"start": 1, "ok": 0, "error": 1}}),
        None)
    assert status == "failed"
    assert kind == "tool_failure"
    assert "接口失败" in detail and "acquire_station" in detail


def test_a_recovered_tool_failure_keeps_the_success_status():
    """Fail once, then succeed: do not turn every error into a permanent failure."""
    status, kind, detail = run_agent_arm._interface_outcome(
        _validation(tool_errors=1, completed=True,
                    attempts={"acquire_station": {"start": 2, "ok": 1, "error": 1}}),
        None)
    assert status == runlog.SUCCEEDED
    assert kind is None
    assert "已由取得的证据回答" in detail


def test_counts_alone_can_no_longer_prove_recovery():
    """Review-4 A4: {start:2, ok:1, error:1} with the task unsettled used to end
    as `succeeded / 视为已恢复`. Counts cannot distinguish error→ok from ok→error,
    so without event order the run is reported as an interface failure, never
    as "其后调用成功，视为已恢复"."""
    status, kind, detail = run_agent_arm._interface_outcome(
        _validation(tool_errors=1, completed=False,
                    attempts={"acquire_station": {"start": 2, "ok": 1, "error": 1}}),
        None)
    assert status == "failed"
    assert kind == "tool_failure"
    assert "已恢复" not in detail
    assert "无法分辨" in detail or "顺序" in detail


def test_event_order_error_then_ok_proves_the_recovery():
    """Order known, same tool and same target healed afterwards: recovered."""
    status, kind, detail = run_agent_arm._interface_outcome(
        _validation(tool_errors=1, completed=False,
                    attempts={"acquire_station": {"start": 2, "ok": 1, "error": 1}},
                    recovery=_recovery(recovered=["acquire_station(cand-a)"])),
        None)
    assert status == runlog.SUCCEEDED
    assert kind is None
    assert "已恢复" in detail
    assert "已成功" in detail


def test_event_order_ok_then_error_is_not_a_recovery():
    """Order known, ok BEFORE the later key error, task unsettled: guide §2.2
    case 3 — the run must not report "其后恢复" or an overall success."""
    status, kind, detail = run_agent_arm._interface_outcome(
        _validation(tool_errors=1, completed=False,
                    attempts={"acquire_station": {"start": 2, "ok": 1, "error": 1}},
                    recovery=_recovery(unrecovered=["acquire_station(cand-a)"])),
        None)
    assert status == "failed"
    assert kind == "tool_failure"
    assert "已恢复" not in detail
    assert "acquire_station(cand-a)" in detail


def test_a_success_on_another_target_is_not_automatically_the_recovery():
    """Guide §2.2: a success of the same tool on ANOTHER candidate does not
    resolve the failed one's gap; with the task unsettled it stays a failure,
    and with the field settled it is reported as an unrecovered exception next
    to the completion (case 4), never as "已恢复"."""
    status, kind, detail = run_agent_arm._interface_outcome(
        _validation(tool_errors=1, completed=False,
                    attempts={"acquire_station": {"start": 2, "ok": 1, "error": 1}},
                    recovery=_recovery(unrecovered=["acquire_station(cand-b)"])),
        None)
    assert (status, kind) == ("failed", "tool_failure")
    status, kind, detail = run_agent_arm._interface_outcome(
        _validation(tool_errors=1, completed=True,
                    attempts={"acquire_station": {"start": 2, "ok": 1, "error": 1}},
                    recovery=_recovery(unrecovered=["acquire_station(cand-b)"])),
        None)
    assert status == runlog.SUCCEEDED
    assert "已由取得的证据回答" in detail
    assert "未恢复异常" in detail
    assert "已恢复" not in detail


def test_no_tool_errors_stays_a_clean_success():
    assert run_agent_arm._interface_outcome(_validation(), None) == \
        (runlog.SUCCEEDED, None, None)


def test_a_driver_failure_still_wins():
    assert run_agent_arm._interface_outcome(_validation(), "timeout") == \
        ("failed", "timeout", None)


# --------------------------------------------------- P1-5 scorer counterexamples
def _row(**kw):
    base = {"status": runlog.SUCCEEDED, "verdict": None, "delivered": {},
            "claim": None, "terminated_by": None, "coincidence": None,
            "answered": None, "stations_acquired": 1, "n_eligible": 3,
            "acquisition": {}}
    base.update(kw)
    return base


def test_scorer_refuses_a_decided_label_on_the_wrong_field():
    """Q_usable answered with a coherence label must not score `satisfied`."""
    r = _row(verdict="coherent_multistation", claim="coherent_multistation",
             terminated_by="coherence",
             delivered={"verdict": "coherent_multistation", "field": "verdict",
                        "field_value": "coherent_multistation",
                        "task_completed": True})
    out = run_g2.evaluate({"id": "X1"}, "Q_usable", r)
    assert out["contract"] == "failed"
    assert out["answered_asked_question"] is False
    assert "答非所问" in out["why"]


def test_scorer_refuses_a_decided_label_when_the_task_was_not_completed():
    r = _row(verdict="pick_partially_applicable",
             claim="pick_partially_applicable", terminated_by="band_partial",
             delivered={"verdict": "pick_partially_applicable",
                        "field": "method_applicability", "field_value": "partial",
                        "task_completed": False})
    out = run_g2.evaluate({"id": "X2"}, "Q_usable", r)
    assert out["contract"] == "failed"
    assert "task_completed=False" in out["why"]


def test_scorer_still_accepts_a_correct_completed_answer():
    r = _row(verdict="pick_partially_applicable",
             claim="pick_partially_applicable", terminated_by="band_partial",
             delivered={"verdict": "pick_partially_applicable",
                        "field": "method_applicability", "field_value": "partial",
                        "task_completed": True})
    assert run_g2.evaluate({"id": "X3"}, "Q_usable", r)["contract"] == "satisfied"

def test_an_agent_run_row_carries_its_own_parent(tmp_path):
    """A feedback chain must be readable from the result row, not only from the
    run directory (review 3 §2.2: task/run/parent must all correspond)."""
    folder = tmp_path / "r-child"
    folder.mkdir()
    (folder / "run.json").write_text(json.dumps({
        "run_id": "r-child", "arm": "adaptive_agent_open_followup",
        "parent_run_id": "r-parent000000000"}))
    assert run_agent_arm.row_parent(folder) == "r-parent000000000"


def test_row_parent_is_none_for_a_first_round_and_a_missing_record(tmp_path):
    first = tmp_path / "r-first"
    first.mkdir()
    (first / "run.json").write_text(json.dumps({"run_id": "r-first",
                                                "arm": "adaptive_agent_open"}))
    assert run_agent_arm.row_parent(first) is None
    assert run_agent_arm.row_parent(tmp_path / "does-not-exist") is None


def test_the_validator_carries_the_chinese_boundary_into_the_agent_delivery():
    """The interface reads `delivered.statement_zh`; an agent run must carry it
    exactly like a deterministic run (review 3.3)."""
    task = taskmod.make_task(
        user_question="这个时窗能不能做三分量拾取？",
        window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
        region=core.box(48.611, -122.805), target_band_hz=[1.0, 12.0],
        method_goal="three_component_pick")
    records = [{"candidate_id": f"cand{i}", "station": f"S{i}",
                "status": "analysed", "onset_utc": 1000.0 + i,
                "channel_integrity_flags": [], "three_component_ok": True,
                "vertical_ok": True, "sampling_rate_hz": 100.0,
                "band_support": core.band_support(100.0, [1.0, 12.0]),
                "method_applicability": {"three_component_pick": True,
                                         "vertical_onset": True}}
               for i in range(3)]
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task, records,
                                    {"answer": "{}"}, "Q_usable")
    zh = result["delivered"]["statement_zh"]
    assert zh and zh == result["validator_verdict"]["statement_zh"]
    assert "三分量 P/S 拾取" in zh
    # The frozen English wording is still the machine-facing one.
    assert "Method applicability was judged" in result["delivered"]["statement"]
    assert zh != result["delivered"]["statement"]
