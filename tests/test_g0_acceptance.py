"""The evaluator's acceptance instance, verified offline.

The guide specifies:

  "In a state where a successful E1 record already exists, modify the question and
   let the new process fail -> the interface must show the NEW task as failed. Then
   run the modified question successfully -> the verbatim question must appear in
   this run's AGH prompt and tool state, and the answer field must correspond."

Discovery is stubbed so the test is offline and deterministic; the prompt itself is
produced by the real `main/agent_run.mjs` in dry-run mode, so what is verified is
the actual prompt the driver builds.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import run_agent_arm  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

TSX = ROOT / "vendor/agnes-harness/node_modules/.bin/tsx"
EDITED = "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？"


def fake_discover(case, task=None):
    """Deterministic stand-in for the FDSN station query."""
    eligible = [{"candidate_id": f"cand{i:02d}", "network": "UW", "station": f"S{i:02d}",
                 "distance_km": round(1.5 * i, 2), "family": "HH?",
                 "analysis_eligible": True, "three_component": True,
                 "vertical_only": False, "reason": "synthetic",
                 "band_support": {"supported": True, "nyquist_hz": 50.0,
                                  "target_band_hz": [1.0, 12.0],
                                  "sampling_rate_hz": 100.0, "required_rate_hz": 24.0,
                                  "reason": "ok"}}
                for i in range(1, 7)]
    return {"station_query_url": "synthetic://stub", "station_rows": 6,
            "candidates": eligible, "eligible": eligible, "n_eligible": len(eligible),
            "target_band_hz": [1.0, 12.0], "method_band_hz": [1.0, 8.0]}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(core, "discover", fake_discover)
    return tmp_path


def drive_dry(run_id):
    """Run the real driver in dry-run mode: writes prompt.txt, touches no model."""
    env = {**os.environ, "AGENT_DRY_RUN": "1",
           "RUNLOG_DIR": str(runlog.RUNLOG)}
    return subprocess.run([str(TSX), "main/agent_run.mjs", run_id],
                          cwd=ROOT, capture_output=True, text=True, timeout=180,
                          env=env)


requires_tsx = pytest.mark.skipif(not TSX.exists(), reason="tsx loader not installed")


# ------------------------------------------------- the verbatim question reaches AGH
@requires_tsx
def test_edited_question_reaches_the_actual_prompt(isolated):
    prepared = prepare_agent.prepare("E1_orcas_event", question=EDITED)
    run_id = prepared["run_id"]

    proc = drive_dry(run_id)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "dry_run" in proc.stdout

    folder = runlog.run_dir(run_id)
    prompt = (folder / "prompt.txt").read_text()
    assert EDITED in prompt, "the edited question must appear verbatim in the prompt"
    assert prepared["task_id"] in prompt

    # the tool state carries the same question, and nothing that leaks the answer
    state = json.loads((folder / "mcp_state.initial.json").read_text())
    assert state["user_question"] == EDITED
    assert state["target_band_hz"] == [1.0, 12.0]

    # the prompt and the tool state are bound to the same frozen task
    binding = json.loads((folder / "binding.json").read_text())
    assert binding["task_id"] == prepared["task_id"]
    assert binding["prompt_sha256"] == runlog.sha256_text(prompt)
    assert binding["user_question_sha256"] == runlog.sha256_text(EDITED)


@requires_tsx
def test_prompt_does_not_disclose_evaluation_labels(isolated):
    prepared = prepare_agent.prepare("A1_injected_artifact", question=EDITED)
    run_id = prepared["run_id"]
    assert drive_dry(run_id).returncode == 0
    folder = runlog.run_dir(run_id)
    combined = (folder / "prompt.txt").read_text() + \
        (folder / "mcp_state.initial.json").read_text()
    for leaked in ("A1_injected_artifact", "injected_artifact", "E1_orcas_event",
                   "\"kind\"", "\"split\"", "demo_case_ref", "expect"):
        assert leaked not in combined, f"{leaked!r} leaked into model-visible content"


@requires_tsx
def test_the_injection_switch_is_evaluator_side_only(isolated):
    prepared = prepare_agent.prepare("A1_injected_artifact", question=EDITED)
    folder = runlog.run_dir(prepared["run_id"])
    evaluator = json.loads((folder / "evaluator.json").read_text())
    assert evaluator["inject_first"] is True
    assert "never served" in evaluator["note"]
    # ...but the model-visible state must not carry the switch
    assert "inject_first" not in (folder / "mcp_state.initial.json").read_text()


# ------------------------------------------- failure is this run's failure, not a stale one
@requires_tsx
def test_prior_success_does_not_mask_a_later_failure(isolated):
    first = prepare_agent.prepare("E1_orcas_event")
    assert drive_dry(first["run_id"]).returncode == 0
    runlog.outcome(first["run_id"], runlog.SUCCEEDED)

    second = prepare_agent.prepare("E1_orcas_event", question=EDITED)
    assert second["run_id"] != first["run_id"]
    runlog.record_failure(second["run_id"], "timeout", "driver exceeded 900 s")
    runlog.outcome(second["run_id"], "failed", failure_kind="timeout",
                   failure_detail="driver exceeded 900 s")

    assert runlog.read_outcome(first["run_id"])["status"] == runlog.SUCCEEDED
    failed = runlog.read_outcome(second["run_id"])
    assert failed["status"] == "failed"
    assert failed["failure_kind"] == "timeout"

    # the two runs hold different questions and neither overwrote the other
    state_a = json.loads((runlog.run_dir(first["run_id"])
                          / "mcp_state.initial.json").read_text())
    state_b = json.loads((runlog.run_dir(second["run_id"])
                          / "mcp_state.initial.json").read_text())
    assert state_a["user_question"] != state_b["user_question"]
    assert state_b["user_question"] == EDITED


@requires_tsx
def test_missing_driver_record_is_recorded_as_a_failure(isolated):
    """Exit code 0 with no record must not be read as success."""
    prepared = prepare_agent.prepare("Q1_orcas_quiet")
    run_id = prepared["run_id"]
    assert not (ROOT / "main/out" / f"agent_execution_open_{run_id}.json").exists()
    runlog.record_failure(run_id, "no_execution_record", "driver wrote no record")
    runlog.outcome(run_id, "failed", failure_kind="no_execution_record")
    assert runlog.read_outcome(run_id)["failure_kind"] == "no_execution_record"


# ------------------------------------------------------------------ layered validation
def records_for(onsets, flags=None, rate=100.0, three=True):
    task = {"target_band_hz": [1.0, 12.0]}
    out = []
    for i, onset in enumerate(onsets):
        support = core.band_support(rate, task["target_band_hz"])
        out.append({"candidate_id": f"cand{i:02d}", "station": f"S{i:02d}",
                    "status": "analysed", "onset_utc": onset, "onset_computed": True,
                    "channel_integrity_flags": (flags or []) if i == 0 else [],
                    "three_component_ok": three, "vertical_ok": True,
                    "sampling_rate_hz": rate, "band_support": support,
                    "method_applicability": {
                        "three_component_pick": bool(three and support["supported"]),
                        "vertical_onset": support["supported"]}})
    return out


def _structured(task, conclusion, records, *, stop_reason="ok", field=None,
                stop_reason_code=None, condition_id=None, evidence=True,
                question_id="Q_propagating"):
    """Build the O6 structured answer the validator now requires.

    `conclusion`/`stop_reason` stay for compatibility; a full delivery also names
    the asked field, this run's condition id, the scope and the evidence ids it
    relies on. Acceptance is all-or-nothing over these items.
    """
    import execution_conditions as EC
    requested = EC.from_task(task, question_id=question_id)
    answer = {"conclusion": conclusion, "stop_reason": stop_reason,
              "stations_used": len(records),
              "field": field or requested["required_field"],
              "condition_id": condition_id or requested["condition_id"],
              "scope_candidate_ids": [r["candidate_id"] for r in records],
              # The delivery contract requires an explicit stop code; a claim that
              # asserts an answer must name the public criterion it rests on.
              "stop_reason_code": stop_reason_code or "public_criterion_satisfied"}
    answer["evidence_refs"] = ([f"onset:{r['candidate_id']}" for r in records]
                               if evidence else [])
    return {"answer": json.dumps(answer)}


def test_validate_separates_claim_from_verdict_and_flags_rejection():
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    records = records_for([1000.0, 1000.5, 1001.0])
    driver = _structured(task, "not_decidable", records, stop_reason="unsure")
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task, records, driver,
                                    "Q_propagating")
    assert result["validator_verdict"]["verdict"] == "coherent_multistation"
    assert result["model_claim"]["conclusion"] == "not_decidable"
    assert result["agreement"] is False
    assert result["delivered"]["model_claim_accepted"] is False
    rejection = result["delivered"]["rejection_reason"]
    assert "not_decidable" in rejection and "coherent_multistation" in rejection
    # the delivered result is the validated one, not the model's wording
    assert result["delivered"]["value"] == "coherent_multistation"


def test_validate_accepts_a_claim_the_evidence_supports():
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    records = records_for([1000.0, 1000.5, 1001.0])
    driver = _structured(task, "coherent_multistation", records,
                         stop_reason="3 onsets in 3 s")
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task, records, driver,
                                    "Q_propagating")
    assert result["agreement"] is True
    assert result["delivered"]["model_claim_accepted"] is True
    assert result["delivered"]["rejection_reason"] is None


def test_validate_rejects_a_claim_when_the_asked_field_was_not_settled():
    """Q_usable: a propagation conclusion does not answer the band question."""
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    records = records_for([1000.0, 1000.5, 1001.0], rate=6.192, three=False)
    driver = _structured(task, "coherent_multistation", records,
                         stop_reason="looks coherent", question_id="Q_usable")
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task, records, driver,
                                    "Q_usable")
    assert result["validator_verdict"]["reason"] == "band_unsupported"
    assert result["delivered"]["field"] == "method_applicability"
    assert result["delivered"]["value"] is False
    assert result["delivered"]["model_claim_accepted"] is False


def test_validate_records_acquisition_sufficiency_separately():
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    records = records_for([1000.0])
    driver = _structured(task, "single_station_only", records,
                         stop_reason="stopped early",
                         stop_reason_code="resource_limited")
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task, records, driver,
                                    "Q_propagating")
    assert result["acquisition"]["stations_acquired"] == 1
    assert result["acquisition"]["sufficient_for_asked_field"] is False
    assert result["acquisition"]["next_action"]["stop"] is False
    assert "more analysed station" in result["acquisition"]["next_action"]["needs"]


def test_validate_handles_an_unparsable_model_answer():
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task,
                                    records_for([1000.0, 1000.5, 1001.0]),
                                    {"answer": "I could not decide, sorry."},
                                    "Q_propagating")
    assert result["delivered"]["model_claim_accepted"] is False
    assert "no parsable conclusion" in result["delivered"]["rejection_reason"]


def test_a_failed_driver_record_is_never_read_as_success(isolated):
    """A run whose driver failed keeps failed status even with acquired evidence."""
    prepared = prepare_agent.prepare("E1_orcas_event", question=EDITED)
    run_id = prepared["run_id"]
    folder = runlog.run_dir(run_id)
    state = json.loads((folder / "mcp_state.json").read_text())
    state["acquired"] = records_for([1000.0])
    state["requests"] = 2
    (folder / "mcp_state.json").write_text(json.dumps(state))
    runlog.record_failure(run_id, "model_fatal", "session raised")
    runlog.outcome(run_id, "failed", failure_kind="model_fatal",
                   failure_detail="session raised", stations_acquired=1)
    outcome = runlog.read_outcome(run_id)
    assert outcome["status"] == "failed"
    assert outcome["stations_acquired"] == 1
    assert (folder / "mcp_state.json").exists(), "evidence must survive the failure"


# ------------------------------------------------- P1-1 label / field-value split
def test_validate_keeps_label_field_value_completion_and_acceptance_apart():
    """A channel flag has value True; a label is a string. Never conflate them."""
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    records = records_for([1000.0, 1000.5, 1001.0],
                          flags=["UW.OLGA..ENE:constant"], three=False)
    driver = _structured(task, "instrument_artifact", records,
                         stop_reason="flat E channel",
                         field="channel_integrity_flags", question_id="Q_instrument")
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task, records, driver,
                                    "Q_instrument")
    delivered = result["delivered"]
    assert delivered["verdict"] == "instrument_artifact"      # the label
    assert delivered["field"] == "channel_integrity_flags"    # the asked field
    assert delivered["field_value"] is True                   # its value
    assert delivered["value"] is True                         # compat alias of value
    assert delivered["task_completed"] is True                # was it settled
    assert delivered["model_claim_accepted"] is True          # was the claim accepted
    # The regression: the old feedback gate compared `value` to a label string.
    assert delivered["value"] != delivered["verdict"]


def test_validate_no_flag_instrument_answer_is_a_scoped_negative():
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    records = records_for([1000.0, 1000.5, 1001.0])
    driver = _structured(task, "no_instrument_anomaly_in_scope", records,
                         stop_reason="checked three stations, no flag",
                         field="channel_integrity_flags",
                         stop_reason_code="scope_exhausted",
                         question_id="Q_instrument")
    result = run_agent_arm.validate("r-1", "E1_orcas_event", task, records, driver,
                                    "Q_instrument")
    delivered = result["delivered"]
    assert delivered["verdict"] == "no_instrument_anomaly_in_scope"
    assert delivered["task_completed"] is True
    assert delivered["field_value"] is False
    assert delivered["model_claim_accepted"] is True
    assert "not a global statement" in delivered["statement"]


def test_validate_reports_tool_errors_separately_from_missing_data(isolated):
    """A raised tool is an interface failure; a completed request with no data is
    a data fact. They must not be merged into one 'insufficient' verdict."""
    task = {"task_id": "t-x", "user_question": EDITED, "target_band_hz": [1.0, 12.0]}
    prepared = prepare_agent.prepare("E1_orcas_event", question=EDITED)
    run_id = prepared["run_id"]
    folder = runlog.run_dir(run_id)
    runlog.append_jsonl(folder / "tool_calls.jsonl",
                        {"at": "t0", "run_id": run_id, "tool": "acquire_station",
                         "arguments": {"candidate_id": "c1"}, "phase": "start"})
    runlog.append_jsonl(folder / "tool_calls.jsonl",
                        {"at": "t1", "run_id": run_id, "tool": "acquire_station",
                         "arguments": {"candidate_id": "c1"}, "phase": "error",
                         "error_type": "KeyError", "error": "None"})
    result = run_agent_arm.validate(run_id, "E1_orcas_event", task,
                                    records_for([1000.0]), {}, "Q_propagating")
    acquisition = result["acquisition"]
    assert acquisition["tool_errors"] == 1
    assert acquisition["tools_with_errors"] == ["acquire_station"]
    assert acquisition["interface_failure"] is True
    # A5 shape: every tool reports all three phases, zeros included, so the
    # validation record equals the persisted state's shape exactly.
    assert acquisition["tool_attempts"]["acquire_station"] == \
        {"start": 1, "ok": 0, "error": 1}