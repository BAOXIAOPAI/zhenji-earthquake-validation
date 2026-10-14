"""regression tests: task freezing, run identity, failure semantics, cost honesty.

These cover the mechanisms the evaluator's acceptance instance depends on:

  "modify the question and let the new process fail -> the interface must show the
   new task as failed; then run the modified question successfully -> the verbatim
   question must appear in this run's AGH prompt and tool state, and the answer
   field must correspond."
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "analysis"))

import runlog  # noqa: E402
import task as taskmod  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never write into the real task run/record directories from a test."""
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    return tmp_path


def a_task(**over):
    fields = dict(user_question="这个候选异常是不是多个台站共有的真实传播信号？",
                  window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
                  region={"center_lat": 48.611, "center_lon": -122.805},
                  target_band_hz=[1.0, 12.0], demo_case_ref="E1_orcas_event",
                  acceptance={"expect": "catalogued_event", "kind": "event"})
    fields.update(over)
    return taskmod.make_task(**fields)


# ------------------------------------------------------------------ task freezing
def test_task_id_is_opaque_and_answer_free():
    frozen = a_task()
    assert frozen["task_id"].startswith("t-")
    for label in ("event", "quiet", "inject", "E1", "orcas"):
        assert label not in frozen["task_id"]


def test_model_view_hides_demo_reference_and_acceptance_labels():
    view = taskmod.model_view(a_task())
    for leaked in ("demo_case_ref", "acceptance", "kind", "split", "expect"):
        assert leaked not in view
    assert "E1_orcas_event" not in json.dumps(view, ensure_ascii=False)


def test_model_view_keeps_the_user_question_verbatim():
    question = "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？"
    view = taskmod.model_view(a_task(user_question=question))
    assert view["user_question"] == question
    assert view["target_band_hz"] == [1.0, 12.0]


def test_task_requires_a_question_and_a_window():
    with pytest.raises(ValueError, match="user_question"):
        a_task(user_question="   ")
    with pytest.raises(ValueError, match="window_start"):
        a_task(window_end=None)
    with pytest.raises(ValueError, match="target_band_hz"):
        a_task(target_band_hz=[12.0, 1.0])


def test_freeze_is_write_once():
    frozen = a_task()
    path = taskmod.freeze(frozen)
    assert path.exists()
    with pytest.raises(FileExistsError):
        taskmod.freeze(frozen)


def test_load_verifies_the_digest_and_detects_tampering():
    frozen = a_task()
    path = taskmod.freeze(frozen)
    loaded = taskmod.load_task(frozen["task_id"])
    assert loaded["user_question"] == frozen["user_question"]

    import os
    os.chmod(path, 0o644)  # the freeze made it read-only; tampering needs this
    record = json.loads(path.read_text())
    record["user_question"] = "悄悄换掉的问题"
    path.write_text(json.dumps(record, ensure_ascii=False))
    with pytest.raises(ValueError, match="digest mismatch"):
        taskmod.load_task(frozen["task_id"])


def test_unknown_task_is_reported():
    with pytest.raises(FileNotFoundError):
        taskmod.load_task("t-does-not-exist")


# ---------------------------------------------------------------- run identity
def test_run_ids_are_unique_and_directories_are_separate():
    frozen = a_task()
    first, second = runlog.new_run_id(), runlog.new_run_id()
    assert first != second
    runlog.init_run(first, frozen, arm="adaptive_agent_open")
    runlog.init_run(second, frozen, arm="adaptive_agent_open")
    assert runlog.run_dir(first) != runlog.run_dir(second)
    assert (runlog.run_dir(first) / "task.json").exists()
    assert (runlog.run_dir(second) / "task.json").exists()


def test_init_run_refuses_to_reuse_a_run_directory():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="rule")
    with pytest.raises(FileExistsError):
        runlog.init_run(run_id, frozen, arm="rule")


def test_records_are_write_once():
    frozen = a_task()
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm="rule")
    runlog.write_once(folder / "prompt.txt", "first prompt")
    with pytest.raises(FileExistsError):
        runlog.write_once(folder / "prompt.txt", "second prompt")
    assert (folder / "prompt.txt").read_text() == "first prompt"


def test_a_new_run_does_not_touch_the_previous_run():
    """The old defect replaced the record for a case; two runs must coexist."""
    frozen = a_task()
    first = runlog.new_run_id()
    runlog.init_run(first, frozen, arm="adaptive_agent_open")
    runlog.write_once(runlog.run_dir(first) / "prompt.txt", "run one prompt")
    runlog.outcome(first, runlog.SUCCEEDED)

    second = runlog.new_run_id()
    runlog.init_run(second, frozen, arm="adaptive_agent_open")
    runlog.write_once(runlog.run_dir(second) / "prompt.txt", "run two prompt")

    assert (runlog.run_dir(first) / "prompt.txt").read_text() == "run one prompt"
    assert (runlog.run_dir(second) / "prompt.txt").read_text() == "run two prompt"
    assert runlog.read_outcome(first)["status"] == runlog.SUCCEEDED
    assert runlog.read_outcome(second) is None


# ------------------------------------------------------------ failure semantics
def test_failure_outcome_requires_a_known_kind():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="agnostic")
    with pytest.raises(ValueError, match="must carry a failure kind"):
        runlog.outcome(run_id, "failed")
    with pytest.raises(ValueError, match="unknown failure kind"):
        runlog.outcome(run_id, "failed", failure_kind="looks_bad")


def test_success_cannot_carry_a_failure_kind():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="agnostic")
    with pytest.raises(ValueError, match="cannot carry a failure kind"):
        runlog.outcome(run_id, runlog.SUCCEEDED, failure_kind="timeout")


def test_a_failed_run_records_the_kind_and_detail():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="agnostic")
    runlog.outcome(run_id, "failed", failure_kind="subprocess_exit",
                   failure_detail="driver exited 1")
    record = runlog.read_outcome(run_id)
    assert record["status"] == "failed"
    assert record["failure_kind"] == "subprocess_exit"
    assert "exited 1" in record["failure_detail"]


def test_a_terminal_outcome_cannot_be_rewritten():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="agnostic")
    runlog.outcome(run_id, "failed", failure_kind="model_fatal", failure_detail="boom")
    with pytest.raises(FileExistsError):
        runlog.outcome(run_id, runlog.SUCCEEDED)


def test_failures_accumulate_and_are_never_cleared():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="agnostic")
    runlog.record_failure(run_id, "tool_failure", "acquire_station raised")
    runlog.record_failure(run_id, "timeout", "session prompt timed out")
    errors = json.loads((runlog.run_dir(run_id) / "errors.json").read_text())
    assert [f["kind"] for f in errors["failures"]] == ["tool_failure", "timeout"]


def test_a_failed_run_keeps_its_evidence_and_its_cost():
    """Failure must not erase what was already acquired or already paid for."""
    frozen = a_task()
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm="agnostic")
    (folder / "raw" / "abc").mkdir()
    (folder / "raw" / "abc" / "waveform.mseed").write_bytes(b"pretend miniseed")
    runlog.write_cost(run_id, {"known": True, "cost_upper_cny": 0.031,
                               "entries": 2, "settled": True},
                      http_attempts=4, logical_acquisitions=2)
    runlog.record_failure(run_id, "subprocess_exit", "driver exited 1")
    runlog.outcome(run_id, "failed", failure_kind="subprocess_exit",
                   failure_detail="driver exited 1")
    assert (folder / "raw" / "abc" / "waveform.mseed").exists()
    cost = json.loads((folder / "cost.json").read_text())
    assert cost["model_cost_upper_cny"] == 0.031
    assert cost["http_attempts"] == 4


# --------------------------------------------------------------------- cost honesty
def test_cost_is_unknown_when_the_ledger_has_no_entry_for_the_run():
    """absent cost must read as unknown, never as a fake zero."""
    # ledger file absent entirely
    missing = runlog.cost_for_run("r-nonexistent")
    assert missing["known"] is False
    assert missing["cost_upper_cny"] is None
    assert missing["reason"] == "ledger_missing"

    # ledger present, but holds nothing for this run
    runlog.LEDGER.write_text(json.dumps({"entries": [
        {"case_id": "r-somebody-else", "status": "settled", "cost_upper_cny": 0.01},
    ], "halted": False}))
    absent = runlog.cost_for_run("r-nonexistent")
    assert absent["known"] is False
    assert absent["cost_upper_cny"] is None
    assert absent["reason"] == "no_ledger_entries_for_run"
    assert absent["entries"] == 0


def test_cost_sums_only_this_runs_ledger_entries():
    ledger = {"entries": [
        {"case_id": "r-mine", "status": "settled", "cost_upper_cny": 0.02},
        {"case_id": "r-mine", "status": "settled", "cost_upper_cny": 0.03},
        {"case_id": "r-other", "status": "settled", "cost_upper_cny": 9.99},
    ], "halted": False}
    runlog.LEDGER.write_text(json.dumps(ledger))
    result = runlog.cost_for_run("r-mine")
    assert result["known"] is True
    assert result["cost_upper_cny"] == 0.05
    assert result["entries"] == 2


def test_cost_falls_back_to_reserved_when_upper_is_absent():
    ledger = {"entries": [{"case_id": "r-x", "status": "reserved",
                           "reserved_cny": 0.5}], "halted": False}
    runlog.LEDGER.write_text(json.dumps(ledger))
    assert runlog.cost_for_run("r-x")["cost_upper_cny"] == 0.5


def test_cost_record_separates_logical_operations_from_real_attempts():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="rule")
    record = runlog.write_cost(run_id, {"known": False, "cost_upper_cny": None,
                                        "reason": "no_ledger_entries_for_run"},
                               http_attempts=6, cache_hits=2,
                               logical_acquisitions=3, bytes_total=1234)
    assert record["http_attempts"] == 6
    assert record["logical_acquisitions"] == 3
    assert record["cache_hits"] == 2
    assert record["model_cost_known"] is False
    assert record["model_cost_upper_cny"] is None


# ------------------------------------------------------------------- task binding
def test_binding_links_prompt_and_mcp_state_to_the_task():
    frozen = a_task()
    run_id = runlog.new_run_id()
    runlog.init_run(run_id, frozen, arm="agnostic")
    binding = runlog.record_task_binding(run_id, frozen["task_id"],
                                         runlog.sha256_text("the prompt"),
                                         runlog.sha256_text("the mcp state"))
    assert binding["task_id"] == frozen["task_id"]
    assert binding["prompt_sha256"] == runlog.sha256_text("the prompt")
    written = json.loads((runlog.run_dir(run_id) / "binding.json").read_text())
    assert written == binding


def test_task_from_case_uses_the_override_question_and_keeps_labels_private():
    import core
    case = core.case_by_id("E1_orcas_event")
    edited = "能否做 1–12 Hz P/S 拾取？"
    frozen = taskmod.task_from_case(case, user_question=edited)
    assert frozen["user_question"] == edited
    assert taskmod.model_view(frozen)["user_question"] == edited
    assert "E1_orcas_event" not in json.dumps(taskmod.model_view(frozen),
                                              ensure_ascii=False)

def test_failure_attribution_reaches_the_model_but_injection_still_does_not():
    """The distinction is useless if the tool hides it; the leak must stay hidden.

    mcp_server.py consumes sys.argv at import, so the field tuple is read from the
    source with ast rather than by importing the module.
    """
    import ast

    source = (ROOT / "main/mcp_server.py").read_text()
    fields = None
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ACQUISITION_FIELDS"
                for t in node.targets):
            fields = [e.value for e in node.value.elts]
    assert fields is not None, "ACQUISITION_FIELDS not found"
    for field in ("failed_request", "metadata_available", "waveform_available",
                  "distinction", "evidence_preserved", "supplementary_conditions",
                  "next_step"):
        assert field in fields, field
    for field in ("derivation", "derived_waveform_sha256", "parent_record_id",
                  "injected_artifact"):
        assert field not in fields, field
