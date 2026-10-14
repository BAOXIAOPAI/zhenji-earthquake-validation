"""C 批次共同合同的可行性测试（review-5 指南 §2.2，全部无付费）。

C0 出口条件之一是“三组合同和证据可得性逐项一致”。这里的测试驱动
生产入口证明三组各自拿得到同一份合同要素，特别是 T1 需要的
method_goal 反馈链路（review-4 时期尚未实现）：

  - R（确定性反馈重算）：goal 变更后按新目标重判，父记录不动；
  - 结转证据：method_applicability 两个目标各自有效；
  - prepare_followup 接受 method_goal 并写进子任务的冻结任务；
  - 自由文字“只看垂直分量”解析为 method_goal 反馈；
  - 评分器对 T1 的两个分支（三分量不适用 / 垂直适用）都能正确计分。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import c_scorer  # noqa: E402
import prepare_agent  # noqa: E402
import review  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

TARGET = (1.0, 12.0)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", tmp_path / "runlog")
    return tmp_path


def _record(name, rate=100.0, three=True, target=TARGET):
    support = core.band_support(rate, target)
    return {
        "candidate_id": "cand-" + name, "station": name, "network": "UW",
        "distance_km": 1.0, "status": "analysed", "onset_utc": 1000.0,
        "channels": [f"UW.{name}..HH{c}" for c in "ZNE"],
        "channel_integrity_flags": [] if three else [f"UW.{name}..HHE:constant"],
        "channel_integrity_ok": three, "sampling_rate_hz": rate,
        "three_component_ok": three, "vertical_ok": True,
        "band_support": support, "target_band_hz": list(target),
        "method_applicability": {
            "three_component_pick": bool(three and support["supported"]),
            "vertical_onset": bool(support["supported"])},
    }


def _task(goal="three_component_pick", band=TARGET, question=None):
    frozen = taskmod.make_task(
        user_question=question or "这段观测能不能用于指定频带的三分量拾取？",
        window_start="2023-03-01T11:34:43", window_end="2023-03-01T11:36:43",
        region=core.box(48.611, -122.805), target_band_hz=list(band),
        method_goal=goal)
    taskmod.freeze(frozen)
    return frozen


def _parent_run(isolated, records, goal="three_component_pick"):
    """构造一个已取得观测的父 run（合成记录，无网络无模型）。"""
    frozen = _task(goal)
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm="c0_contract_parent")
    discovery = {"n_eligible": len(records), "eligible":
                 [{k: v for k, v in r.items()
                   if k in ("candidate_id", "station", "network", "family",
                            "distance_km", "band_support")}
                  for r in records]}
    runlog.write_once(folder / "discovery.json", discovery)
    state = {"run_id": run_id, "task_id": frozen["task_id"],
             "user_question": frozen["user_question"],
             "window_start": frozen["window_start"],
             "window_end": frozen["window_end"], "region": frozen["region"],
             "target_band_hz": list(TARGET),
             "method_band_hz": list(core.METHOD_BAND), "method_goal": goal,
             "allowed_actions": frozen["allowed_actions"],
             "discovery": discovery,
             "acquired": copy.deepcopy(records), "requests": 0,
             "http_attempts": 0, "logical_acquisitions": 0,
             "cache_hits": 0, "bytes": 0, "exclusions": []}
    prepare_agent.write_state(folder, state)
    return run_id, frozen, state


def test_method_goal_feedback_rejudges_by_new_goal(isolated):
    """R 臂反馈：坏 E 好 Z 的记录，goal 改垂直后输入条件从 not_met 变 met。

    父任务按三分量冻结（badE 记录 not applicable）；用户反馈“只看垂直
    分量”后，子 run 的判决层按 vertical_onset 重判，同一记录的输入条件
    成立——T1 要比较的正是这个区分。
    """
    records = [_record("badE", three=False)]
    run_id, frozen, _ = _parent_run(isolated, records,
                                    goal="three_component_pick")
    first = core.verdict_for_question(records, "Q_usable", None, frozen)
    assert first["verdict"] == "pick_not_applicable"

    child = review.run_followup(run_id, instruction="只看垂直分量，方法适用性会变成什么？")
    assert child["status"] == "succeeded"
    assert child["verdict"] == "pick_applicable"
    assert (child.get("answered") or {}).get("scope", {}).get("goal") \
        == "vertical_onset"
    # 父 run 的任务与判决原样未动。
    parent_task = json.loads(
        (runlog.run_dir(run_id) / "task.json").read_text())
    assert parent_task["method_goal"] == "three_component_pick"
    # 反馈对象是条件变更，不是数据变更：无新请求、无重分析。
    fb = child["feedback"]
    assert fb["new_requests"] == 0
    assert fb["change"]["kind"] == "method_goal"


def test_prepare_followup_carries_method_goal(isolated):
    """prepare_followup 接受 method_goal，写进子任务；父任务不受影响。"""
    records = [_record("badE", three=False), _record("clean")]
    run_id, frozen, _ = _parent_run(isolated, records)
    prepared = prepare_agent.prepare_followup(
        run_id, "只看垂直分量", arm="c0_goal_followup",
        method_goal="vertical_onset",
        change={"kind": "method_goal", "method_goal": "vertical_onset"})
    child_task = json.loads(
        (runlog.run_dir(prepared["run_id"]) / "task.json").read_text())
    assert child_task["method_goal"] == "vertical_onset"
    parent_task = json.loads(
        (runlog.run_dir(run_id) / "task.json").read_text())
    assert parent_task["method_goal"] == "three_component_pick"
    # 子任务的任务 id 不同（新 run），父 run 记录不被改写。
    assert prepared["run_id"] != run_id


def test_free_text_vertical_parses_to_method_goal(isolated):
    """自由文字“只看垂直分量”解析为 method_goal 反馈，不再是“尚未实现”。"""
    records = [_record("badE", three=False)]
    run_id, _, state = _parent_run(isolated, records)
    change = review.feedback_change(state, None, "只看垂直分量重算")
    assert change["kind"] == "method_goal"
    assert change["method_goal"] == "vertical_onset"
    # 三分量反向也支持。
    change = review.feedback_change(state, None, "改回三分量目标")
    assert change["method_goal"] == "three_component_pick"
    with pytest.raises(ValueError):
        review.feedback_change(state, {"kind": "method_goal",
                                       "method_goal": "p_wave_first"})


def test_carried_applicability_covers_both_goals(isolated):
    """结转记录的 method_applicability 两个目标各自有效（T1 的分支基础）。"""
    records = [_record("badE", three=False)]
    run_id, _, _ = _parent_run(isolated, records)
    prepared = prepare_agent.prepare_followup(
        run_id, "只看垂直分量", arm="c0_goal_followup",
        method_goal="vertical_onset",
        change={"kind": "method_goal", "method_goal": "vertical_onset"})
    child_state = json.loads(
        (runlog.run_dir(prepared["run_id"]) / "mcp_state.json").read_text())
    for rec in child_state["acquired"]:
        assert rec["method_applicability"]["three_component_pick"] is False
        assert rec["method_applicability"]["vertical_onset"] is True


def test_scorer_separates_t1_branches():
    """T1 两个分支的评分：三分量不适用（有据否定）与垂直适用（有据肯定）
    都是字段完成，且 method_goal 进入规范 row。"""
    not_applicable = {
        "question_id": "Q_usable", "status": "succeeded",
        "method_goal": "three_component_pick",
        "verdict": "pick_not_applicable",
        "delivered": {"verdict": "pick_not_applicable",
                      "field": "method_applicability",
                      "field_value": False, "answer_kind": "negative",
                      "task_completed": True},
        "terminated_by": "band_check",
        "answered": {"answered": True, "field": "method_applicability",
                     "value": False, "answer_kind": "negative"},
        "n_eligible": 2, "stations_acquired": 2, "stations_analysed": 2}
    vertical_ok = dict(not_applicable, method_goal="vertical_onset",
                       verdict="pick_applicable")
    vertical_ok["delivered"] = {
        "verdict": "pick_applicable", "field": "method_applicability",
        "field_value": True, "answer_kind": "positive",
        "task_completed": True}
    vertical_ok["answered"] = {"answered": True, "field": "method_applicability",
                               "value": True, "answer_kind": "positive"}
    for raw in (not_applicable, vertical_ok):
        norm = c_scorer.normalize_row(raw)
        result = c_scorer.score_row(norm)
        assert result["dimensions"]["field_completed"] is True, result["why"]
        assert result["dimensions"]["wrong_delivery"] is False
    assert c_scorer.normalize_row(not_applicable)["method_goal"] \
        == "three_component_pick"
    assert c_scorer.normalize_row(vertical_ok)["method_goal"] \
        == "vertical_onset"
