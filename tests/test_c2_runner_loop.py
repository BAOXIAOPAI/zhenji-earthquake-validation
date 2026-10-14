"""C 批次 runner 的循环结构回归（无付费）。

C2 冻结批次首跑暴露的真实缺陷：批量入口在第一个任务后即返回——
`for task` 循环外层的收尾 print/return 曾被缩进进循环体，导致只跑了
C2-T1 就“成功”结束（45 行而非 99 行）。这类结构错误不会报错、只会
少跑，必须用测试锁住。

本测试用 monkeypatch 把三个臂替换成零成本假实现，验证：
  - 所有任务 × 所有重复 × 三个臂都被执行；
  - 每个臂的反馈变体被执行；
  - 结果文件一次性写全（行数 = 首轮 + 反馈）；
  - C2 的 R 参照基准按任务建立并传给 M/A。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import run_c  # noqa: E402


def _fake_result(arm, task, n):
    """假运行结果：形状与真实臂一致，零成本。"""
    return {
        "run_id": f"r-fake-{task['id']}-{arm}-{n}",
        "status": "succeeded", "failure_kind": None,
        "verdict": "not_decidable", "field_answered": "verdict",
        "answered": {"answered": False, "value": "unknown",
                     "field": "verdict"},
        "n_eligible": 3, "stations_acquired": 1, "stations_analysed": 1,
        "terminated_by": "coherence", "coincidence": 1,
        "layers": {"excluded_candidates": []},
        "stations": [{"station": "AAA", "status": "analysed"}],
        "model_claim": {"conclusion": None, "stop_reason": None},
        "delivered": {"verdict": "not_decidable", "field": "verdict",
                      "task_completed": False},
        "http_attempts": 0, "logical_acquisitions": 1,
    }


def test_batch_runs_every_task_repeat_and_arm(monkeypatch, tmp_path, capsys):
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({
        "schema": "test", "arms": ["R", "M", "A"], "ceiling": 6,
        "expected_verdict_source": "r_full_evidence_first_round",
        "budget": {"c1_dev_reserve_cny": 2.0, "c2_locked_reserve_cny": 3.0},
        "tasks": [
            {"id": f"X{i}", "case_id": "E1_orcas_event",
             "question": "q", "question_id": "Q_propagating",
             "method_goal": None, "repeats": 2,
             "feedback": {"instruction": "改一下",
                          "change": {"kind": "stability_bands",
                                     "target_band_hz": [3, 12]}}}
            for i in (1, 2)],
    }), encoding="utf-8")
    out = tmp_path / "RESULTS.json"
    versions = tmp_path / "versions"
    calls = []

    def fake_arm(arm):
        def run(task, **kw):
            calls.append((task["id"], arm))
            return _fake_result(arm, task, len(calls))
        return run

    monkeypatch.setattr(run_c, "OUT", out)
    monkeypatch.setattr(run_c, "VERSIONS_DIR", versions)
    monkeypatch.setattr(run_c, "c0_exit_ready", lambda: (True, "ok"))
    monkeypatch.setattr(run_c, "budget_state",
                        lambda reserve_cny=None: {
                            "available": True, "halted": False,
                            "total_cny": 1.0, "remaining_cny": 19.0,
                            "reserve_cny": reserve_cny or 2.0})
    monkeypatch.setattr(run_c, "ARM_RUNNERS",
                        {a: fake_arm(a) for a in ("R", "M", "A")})
    monkeypatch.setattr(run_c, "run_feedback",
                        lambda task, arm, parent, variant:
                        _fake_result(arm, task, len(calls)))
    monkeypatch.setenv("EQ_C_TASKS", str(tasks_file))
    monkeypatch.setenv("C1_LABEL", "test-loop")

    rc = run_c.cmd_dev(None)
    assert rc == 0, capsys.readouterr().err
    data = json.loads(out.read_text())
    # 2 任务 × 2 重复 × 3 臂 = 12 首轮 + 12 反馈 = 24 行，全部写全。
    assert len(data["runs"]) == 24, len(data["runs"])
    firsts = [r for r in data["runs"] if "-followup" not in r["batch"]]
    assert len(firsts) == 12
    assert sorted({(r["task_id"], r["repeat"]) for r in firsts}) == [
        ("X1", 1), ("X1", 2), ("X2", 1), ("X2", 2)]
    assert {r["arm"] for r in firsts} == {"R", "M", "A"}
    # 臂调用覆盖两个任务，不是首个任务后即返回。
    assert {t for t, _ in calls} == {"X1", "X2"}
    # R 参照基准传给了同任务的 M/A 行。
    for row in firsts:
        if row["arm"] in ("M", "A"):
            assert row.get("expected_verdict") == "not_decidable"


def test_missing_task_file_is_not_silently_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("EQ_C_TASKS", str(tmp_path / "nope.json"))
    monkeypatch.setattr(run_c, "VERSIONS_DIR", tmp_path / "versions")
    try:
        run_c.cmd_dev(None)
    except FileNotFoundError:
        return
    raise AssertionError("缺少任务文件必须显式失败，不能静默跑空")