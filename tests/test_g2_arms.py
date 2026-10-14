"""三组对照必须共用同一套验证、同一套证据字段、同一套失败处理。

指南 5.3 要求 R/M/A 三组"质量校验、最终验证和错误处理一致"。这些测试把
"一致"变成可失败的断言：如果哪一天某一组偷偷用了不同的字段白名单或
不同的判决入口，这里就会红。
"""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

def _acquisition_fields():
    """Read ACQUISITION_FIELDS from mcp_server.py without importing it.

    mcp_server.py consumes sys.argv at import, so it cannot be imported here.
    """
    source = (ROOT / "main/mcp_server.py").read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ACQUISITION_FIELDS"
                for t in node.targets):
            return [e.value for e in node.value.elts]
    raise AssertionError("ACQUISITION_FIELDS not found")


def test_the_m_arm_reads_its_evidence_fields_from_the_mcp_server():
    """The M arm must not keep a second copy of the whitelist."""
    source = (ROOT / "main/one_shot.mjs").read_text()
    assert "mcp_server.py" in source, "M arm must derive the whitelist"
    assert "ACQUISITION_FIELDS" in source
    # the regex it uses must actually match the real block
    block = re.search(r"ACQUISITION_FIELDS\s*=\s*\(([\s\S]*?)\)",
                      (ROOT / "main/mcp_server.py").read_text())
    assert block, "one_shot.mjs's regex would not match mcp_server.py"
    stripped = re.sub(r"#[^\n]*", "", block.group(1))
    found = [m.group(1) for m in re.finditer(r'"([a-z_]+)"', stripped)]
    assert found == _acquisition_fields()
    # and the comment case must not leak in
    assert "metadata_unavailable" not in found


def test_the_m_arm_offers_exactly_the_agent_arms_evidence_fields():
    """Whatever the M arm shows the model, the tool layer shows the same fields."""
    fields = _acquisition_fields()
    source = (ROOT / "main/one_shot.mjs").read_text()
    # the arm selects by the whitelist rather than naming fields itself
    assert "for(const f of ACQ_FIELDS)" in source
    assert "injected_artifact" in fields or "derivation" not in fields
    for leaked in ("derivation", "derived_waveform_sha256", "parent_record_id",
                   "injected_artifact"):
        assert leaked not in fields, leaked


def test_the_m_arm_acquires_before_it_calls_the_model():
    """M's whole definition is that acquisition is decided without the model."""
    source = (ROOT / "main/run_agent_arm.py").read_text()
    # 采集在一个独立函数里，模型调用在另一个里：入口必须先调用前者。
    acquire_fn = source[source.index("def _one_shot_acquire("):]
    acquire_fn = acquire_fn[:acquire_fn.index("\ndef ", 10)]
    assert "core.acquire(case, candidate, state)" in acquire_fn
    for entry in ("def run_one_shot(", "def run_one_shot_task("):
        fn = source[source.index(entry):]
        fn = fn[:fn.index("\ndef ", 10) if "\ndef " in fn[10:] else len(fn)]
        assert fn.index("_one_shot_acquire(") < fn.index("_execute("), \
            f"{entry}: acquisition must precede the model call"
    assert "one_shot_model" in fn
    assert "acquired_up_front" in acquire_fn


def test_all_three_arms_go_through_one_execute_and_one_validator():
    source = (ROOT / "main/run_agent_arm.py").read_text()
    # every arm entry point must delegate to _execute, not reimplement validation
    for entry in ("def run_one(", "def run_one_shot(", "def run_followup("):
        body = source[source.index(entry):]
        body = body[:body.index("\ndef ", 10) if "\ndef " in body[10:] else len(body)]
        assert "_execute(" in body, f"{entry} must go through _execute"
    # and _execute must call the single validator
    execute = source[source.index("def _execute("):]
    execute = execute[:execute.index("\ndef ", 10) if "\ndef " in execute[10:] else len(execute)]
    assert "validate(" in execute


def test_the_one_shot_driver_releases_the_budget_lock_even_on_failure():
    """A leftover lock file blocks every later accounting call."""
    source = (ROOT / "main/one_shot.mjs").read_text()
    assert "finally{budget.close();}" in source.replace(" ", "")
    try_at = source.index("finally{budget.close();}")
    assert source.index("budget.reserve") < try_at, "reserve must be inside the guard"


def test_the_one_shot_driver_rejects_a_reused_run_id():
    """The run id is the accounting key; records must belong to this run."""
    source = (ROOT / "main/one_shot.mjs").read_text()
    assert "RUN_ID:RUN" in source.replace(" ", "")
    assert "case_id:RUN" in source.replace(" ", "")


def test_the_review_modes_do_not_claim_a_model_was_used_when_it_was_not():
    """R is deterministic. Its record must not carry a model cost."""
    import review
    assert "agent" in review.MODES and "question" in review.MODES
    source = (ROOT / "main/review.py").read_text()
    assert '"known": False' in source and "no_model_used" in source


def test_the_m_arm_writes_an_acquisition_plan_marking_the_decision_source():
    source = (ROOT / "main/run_agent_arm.py").read_text()
    assert "acquisition_plan.json" in source
    assert "before any model call" in source

def test_the_m_arm_is_not_denied_local_evidence_the_agent_can_compute():
    """扣留 A 能算的东西，再把差异说成能力差异，是不公平的对照。

    到时聚集与稳定性网格都是本地计算（不产生外部请求），因此 M 也必须拿到。
    """
    source = (ROOT / "main/run_agent_arm.py").read_text()
    assert "derived_evidence" in source
    for needed in ("coincidence", "onsets", "stability"):
        assert f'"{needed}"' in source, needed
    # 稳定性只对已分析的台站算，且真的调用了同一个诊断器
    assert "diagnostics.stability" in source or "_diag.stability" in source


def test_the_one_shot_prompt_carries_the_derived_block():
    source = (ROOT / "main/one_shot.mjs").read_text()
    assert "derived_evidence" in source
    assert "cost no\nexternal requests" in source or "external requests" in source


def test_a_certainty_claim_without_enough_stations_is_a_violation():
    """安全指标 1 必须在合同之前判，否则会被当成普通的未满足。"""
    import run_g2
    row = {"status": "succeeded", "delivered": {"value": "not_decidable"},
           "claim": "coherent_multistation", "coincidence": 2,
           "stations_acquired": 2, "reason": ""}
    verdict = run_g2.evaluate({}, "Q_propagating", row)
    assert verdict["contract"] == "violation", verdict


def test_an_insufficiency_without_a_legitimate_reason_is_not_a_pass():
    """不能靠一律回答未知刷完成率。"""
    import run_g2
    row = {"status": "succeeded", "delivered": {"value": "not_decidable"},
           "claim": "not_decidable", "coincidence": 4, "reason": "i am not sure"}
    assert run_g2.evaluate({}, "Q_propagating", row)["contract"] == "failed"


def test_the_three_arms_share_one_observation_ceiling():
    import core
    import run_g2
    assert run_g2.TASKS["ceiling"] == core.MAX_STATIONS


def test_a_user_task_with_no_named_question_type_still_builds_a_case():
    """自由文本规格可以不带 question_ids；空列表不是错误。"""
    import task as taskmod
    frozen = taskmod.task_from_spec(
        {"window_start": "2025-01-01T00:00:00", "window_end": "2025-01-01T00:02:00",
         "region": {"label": "x", "center_lat": 48.0, "center_lon": -122.0}},
        user_question="这里有没有异常？")
    case = taskmod.case_for_task(frozen)
    assert case["question_id"] == "Q_propagating"


def test_every_locked_task_builds_from_its_frozen_spec():
    """锁定任务的时窗与区域在协议中写定，必须能原样构造。"""
    import run_g2
    import task as taskmod
    for task in run_g2.TASKS["locked"]:
        frozen = taskmod.task_from_spec(run_g2.spec_for_locked(task),
                                        user_question=task["question"])
        assert frozen["window_start"] == task["window_start"]
        assert frozen["window_end"] == task["window_end"]
        assert taskmod.case_for_task(frozen)["case_id"].startswith("user-")


def test_the_budget_ledger_recovers_from_a_killed_process(tmp_path):
    """进程被杀会留下未结算条目和陈旧锁；两者都必须能恢复，否则一次崩溃
    就永久堵死所有付费运行。挂起条目按预留额全额计入，绝不漏记费用。"""
    import subprocess
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"halted": False, "entries": [
        {"id": "old", "provider": "deepseek", "case_id": "r-x",
         "status": "pending", "reserved_cny": 2.5}]}))
    # 模拟崩溃留下的锁：属主 PID 不可能存在
    (tmp_path / "ledger.json.lock").write_text("999999")
    script = tmp_path / "probe.mjs"
    script.write_text(
        "import {readFileSync} from 'node:fs';\n"
        "import {Budget} from " + json.dumps(
            str(ROOT / "src/budget.mjs")) + ";\n"
        "const cfg=JSON.parse(readFileSync(" + json.dumps(
            str(ROOT / "prototype/model-budget.json")) + ",'utf8'));\n"
        "const b=new Budget(" + json.dumps(str(ledger)) + ",cfg);\n"
        "const st=JSON.parse(readFileSync(" + json.dumps(str(ledger)) + ",'utf8'));\n"
        "console.log(JSON.stringify({total:b.total(),"
        "status:st.entries[0].status,cost:st.entries[0].cost_upper_cny}));\n"
        "b.close();\n")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True,
                         cwd=ROOT, check=True)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["status"] == "abandoned"
    assert got["cost"] == 2.5, "a reservation must never be dropped"
    assert abs(got["total"] - 2.5) < 1e-9
    assert not (tmp_path / "ledger.json.lock").exists(), "the lock must be released"


def test_a_live_lock_is_not_stolen(tmp_path):
    """只有确认属主已死才抢占；活着的进程必须继续独占台账。"""
    import subprocess
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"halted": False, "entries": []}))
    import os
    (tmp_path / "ledger.json.lock").write_text(str(os.getpid()))
    script = tmp_path / "probe.mjs"
    script.write_text(
        "import {readFileSync} from 'node:fs';\n"
        "import {Budget} from " + json.dumps(str(ROOT / "src/budget.mjs")) + ";\n"
        "const cfg=JSON.parse(readFileSync(" + json.dumps(
            str(ROOT / "prototype/model-budget.json")) + ",'utf8'));\n"
        "try{new Budget(" + json.dumps(str(ledger)) + ",cfg);"
        "console.log('ACQUIRED')}catch(e){console.log('REFUSED')}\n")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True,
                         cwd=ROOT)
    assert "REFUSED" in out.stdout, out.stderr
