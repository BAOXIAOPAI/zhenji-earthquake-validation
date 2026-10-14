"""C 批次 runner 的守卫与输出路径测试（全部无付费）。

C0 出口条件之一是“runner只写新目录”。这里的测试锁住：
  - 输出路径固定 delivery/revision-5/C-RESULTS.json，源码不出现
    revision-1/G2-RESULTS 写入；
  - 版本快照 write-once（重复调用不覆盖、不改写历史）；
  - 付费入口的三道守卫（C0 出口、版本快照、预算）缺一拒绝；
  - 台账 halted 或余量不足时拒绝。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import run_c  # noqa: E402


def test_output_path_is_the_new_directory_only():
    assert run_c.OUT == ROOT / "delivery/revision-5/C-RESULTS.json"
    source = Path(run_c.__file__).read_text()
    # 写入目标只有一个：OUT（revision-5）。任何对旧结果的写入调用都不存在。
    write_targets = [line.strip() for line in source.splitlines()
                     if ".write_text(" in line or "write_once(" in line]
    assert write_targets, "runner 必须有落盘调用（OUT/版本快照）"
    for line in write_targets:
        assert "revision-1" not in line and "G2-RESULTS" not in line, (
            f"runner 出现对旧结果的写入：{line}")


def test_version_snapshot_is_write_once(tmp_path, monkeypatch):
    monkeypatch.setattr(run_c, "VERSIONS_DIR", tmp_path / "versions")
    first = run_c.version_snapshot("test-batch")
    data = json.loads(first.read_text())
    assert data["write_once"] is True
    assert set(data["source_sha256"]) >= {
        "main/run_agent_arm.py", "main/mcp_server.py", "main/core.py",
        "main/c_scorer.py", "main/run_c.py", "main/c_tasks.json",
        "delivery/revision-5/C0-共同合同.md"}
    # 同 label 再调用：返回既有文件，不产生新文件、不改内容。
    before = first.read_text()
    again = run_c.version_snapshot("test-batch")
    assert again == first
    assert len(list((tmp_path / "versions").glob("test-batch-*.json"))) == 1
    assert first.read_text() == before


def test_c0_exit_requires_the_counterexample_report(tmp_path, monkeypatch):
    monkeypatch.setattr(run_c, "COUNTEREXAMPLE_REPORT",
                        tmp_path / "missing.json")
    ok, why = run_c.c0_exit_ready()
    assert ok is False and "缺少" in why
    # 全部通过的报告放行。
    report = tmp_path / "ok.json"
    report.write_text(json.dumps({
        "summary": {"all_pass": True},
        "old_scorer_counterexamples_rerun":
            {"available": True, "all_pass": True}}))
    monkeypatch.setattr(run_c, "COUNTEREXAMPLE_REPORT", report)
    ok, why = run_c.c0_exit_ready()
    assert ok is True


def test_c0_exit_rejects_partial_reports(tmp_path, monkeypatch):
    report = tmp_path / "partial.json"
    report.write_text(json.dumps({
        "summary": {"all_pass": False}}))
    monkeypatch.setattr(run_c, "COUNTEREXAMPLE_REPORT", report)
    ok, why = run_c.c0_exit_ready()
    assert ok is False and "未通过" in why


def test_budget_guard_reads_the_real_ledger():
    """守卫必须读真实台账（金额随付费批次增长，不锁死具体数字）。"""
    budget = run_c.budget_state()
    assert budget["available"] is True
    assert budget["halted"] is False
    # 台账在 C1 v1 排查批次（≈0.72 元）后仍远高于 C1 预留。
    assert budget["total_cny"] >= 10.2
    assert budget["remaining_cny"] >= budget["reserve_cny"]


def test_budget_guard_rejects_when_short(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(
        {"halted": False,
         "entries": [{"cost_upper_cny": 19.5}]}))
    monkeypatch.setattr(run_c.runlog, "LEDGER", ledger)
    budget = run_c.budget_state()
    assert budget["available"] is False
    assert budget["remaining_cny"] < budget["reserve_cny"]


def test_budget_guard_stops_when_halted(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"halted": True, "entries": []}))
    monkeypatch.setattr(run_c.runlog, "LEDGER", ledger)
    budget = run_c.budget_state()
    assert budget["available"] is False and budget["halted"] is True


def test_dev_refuses_without_c0_exit(monkeypatch, capsys):
    monkeypatch.setattr(run_c, "c0_exit_ready",
                        lambda: (False, "反例未通过"))
    rc = run_c.cmd_dev(None)
    assert rc == 2
    assert "C0 出口未达标" in capsys.readouterr().err


def test_dev_refuses_without_budget(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(run_c, "c0_exit_ready", lambda: (True, "ok"))
    monkeypatch.setattr(run_c, "budget_state",
                        lambda reserve_cny=2.0: {
                            "available": False, "halted": True,
                            "remaining_cny": 0.1,
                            "reserve_cny": reserve_cny})
    # 版本快照目录指向临时目录：被拒绝的批次不得在真实交付目录留痕。
    monkeypatch.setattr(run_c, "VERSIONS_DIR", tmp_path / "versions")
    rc = run_c.cmd_dev(None)
    assert rc == 2
    assert "预算守卫" in capsys.readouterr().err
    assert not list((tmp_path / "versions").glob("*.json")), \
        "预算拒绝时不得创建版本快照"


def test_tasks_contract_is_complete():
    """任务表按合同携带三组同文的要素。"""
    tasks = run_c.TASKS["tasks"]
    assert [t["id"] for t in tasks] == ["T1", "T2", "T3", "T4"]
    for t in tasks:
        assert t["question_id"] in ("Q_usable", "Q_instrument",
                                    "Q_propagating")
        assert t["case_id"]
        assert t["repeats"] == 1, "三组同权：重复数一致且提前固定"
    t1 = tasks[0]
    assert t1["feedback"]["change"]["kind"] == "method_goal"
    assert t1["feedback"]["change"]["method_goal"] == "vertical_onset"
    t3 = tasks[2]
    assert set(t3["feedback"]) >= {"branch_a", "branch_b"}
    assert t3["feedback"]["branch_a"]["acquisition_allowed"] is False
    assert t3["feedback"]["branch_b"]["acquisition_allowed"] is True
    t4 = tasks[3]
    assert "三站/三秒" in t4["operational_standard"]
