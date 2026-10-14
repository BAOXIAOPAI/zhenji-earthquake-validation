"""The acceptance instance, verified through the real HTTP interface.

The guide's instance:

  "In a state where a successful E1 record already exists, modify the question and
   let the new process fail -> the interface must show the NEW task as failed."

Only the model call is stubbed (the driver subprocess). Everything else — task
freezing, run identity, validation, cost lookup, the HTTP layer and the status the
page reads — is the real code path. This is the "mock before spending" step.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import run_agent_arm  # noqa: E402
import runlog  # noqa: E402
import server as servermod  # noqa: E402
import task as taskmod  # noqa: E402

ORIGINAL = "这个候选异常是不是多个台站共有的真实传播信号？"
MODIFIED = "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？"


def fake_discover(case, task=None):
    eligible = [{"candidate_id": f"cand{i:02d}", "network": "UW", "station": f"S{i:02d}",
                 "distance_km": round(1.5 * i, 2), "family": "HH?",
                 "analysis_eligible": True, "three_component": True,
                 "vertical_only": False, "reason": "synthetic",
                 "band_support": {"supported": True, "nyquist_hz": 50.0,
                                  "target_band_hz": [1.0, 12.0],
                                  "sampling_rate_hz": 100.0,
                                  "required_rate_hz": 24.0, "reason": "ok"}}
                for i in range(1, 7)]
    return {"station_query_url": "synthetic://stub", "station_rows": 6,
            "candidates": eligible, "eligible": eligible, "n_eligible": len(eligible),
            "target_band_hz": [1.0, 12.0], "method_band_hz": [1.0, 8.0]}


def synthetic_records(count=3, onset_step=0.5, rate=100.0, flags=None):
    records = []
    for i in range(count):
        support = core.band_support(rate, [1.0, 12.0])
        records.append({
            "candidate_id": f"cand{i:02d}", "station": f"S{i:02d}", "network": "UW",
            "distance_km": round(1.5 * (i + 1), 2), "family": "HH?",
            "window_start": "2025-03-03T13:02:20", "window_end": "2025-03-03T13:04:20",
            "target_band_hz": [1.0, 12.0], "method_band_hz": [1.0, 8.0],
            "status": "analysed", "acquisition_status": "ok",
            "onset_utc": 1000.0 + i * onset_step, "onset_computed": True,
            "channel_integrity_flags": (flags or []) if i == 0 else [],
            "channel_integrity_ok": not (flags and i == 0),
            "three_component_ok": True, "vertical_ok": True,
            "sampling_rate_hz": rate, "band_support": support,
            "method_applicability": {"three_component_pick": support["supported"],
                                     "vertical_onset": support["supported"]},
        })
    return records


def fake_acquire(case, candidate, state):
    """Offline stand-in for a real acquisition: no FDSN request is issued."""
    support = core.band_support(100.0, [1.0, 12.0])
    state["http_attempts"] = state.get("http_attempts", 0) + 2
    state["logical_acquisitions"] = state.get("logical_acquisitions", 0) + 1
    state["requests"] = state.get("requests", 0) + 2
    state["bytes"] = state.get("bytes", 0) + 4096
    record = {
        "candidate_id": candidate["candidate_id"], "station": candidate["station"],
        "network": candidate["network"], "distance_km": candidate["distance_km"],
        "family": candidate.get("family"), "status": "analysed",
        "acquisition_status": "ok", "onset_utc": 1000.0 + len(state["records"]),
        "window_start": "2025-03-03T13:02:20", "window_end": "2025-03-03T13:04:20",
        "target_band_hz": [1.0, 12.0], "method_band_hz": [1.0, 8.0],
        "channel_integrity_flags": [], "channel_integrity_ok": True,
        "three_component_ok": True, "vertical_ok": True,
        "sampling_rate_hz": 100.0, "band_support": support,
        "method_applicability": {"three_component_pick": True, "vertical_onset": True},
    }
    hook = state.get("on_step")
    if hook:
        hook({"kind": "acquire_start", "record": record})
        hook({"kind": "acquire_done", "record": record})
    return record


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(core, "discover", fake_discover)
    monkeypatch.setattr(prepare_agent.core, "discover", fake_discover)
    monkeypatch.setattr(core, "acquire", fake_acquire)
    monkeypatch.setattr(servermod, "RUNS", {})
    monkeypatch.setattr(servermod, "LAST", {})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), servermod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=20) as response:
        return json.loads(response.read())


def post(base, path, payload):
    request = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-EQ-Request": "1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def wait_for_review(base, job_id, tries=60):
    for _ in range(tries):
        data = get(base, f"/api/review/{job_id}")
        if data["status"] != "running":
            return data
        threading.Event().wait(0.2)
    raise AssertionError("review did not settle")


def stub_driver_failure(monkeypatch, message="session raised"):
    """The driver exits non-zero and writes no success record."""
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="",
                               stderr=f"FATAL model_fatal {message}")
    monkeypatch.setattr(run_agent_arm.subprocess, "run", fake_run)


def stub_driver_success(monkeypatch, conclusion="coherent_multistation",
                        records=None, requests=6):
    """The driver exits 0 and leaves the record and tool state it would have.

    The product Agent path runs `agent_run_optimization.mjs` with the product
    profile, so the record is written where that driver writes it and carries the
    same structured delivery contract the validator now requires.
    """
    def fake_run(cmd, **kwargs):
        # cmd = [tsx, main/agent_run_optimization.mjs, <run_id>, <profile>, <model>]
        run_id = cmd[2]
        profile = cmd[3] if len(cmd) > 4 else "A1"
        folder = runlog.run_dir(run_id)
        acquired = records if records is not None else synthetic_records(3)
        # The real driver writes the prompt and the task binding before it talks
        # to a model; the stub must leave the same contract behind. The real
        # prompt builder itself is verified in test_g0_acceptance.py.
        initial = json.loads((folder / "mcp_state.initial.json").read_text())
        prompt = f'TASK_ID={initial["task_id"]}\n\n  "{initial["user_question"]}"\n'
        (folder / "prompt.txt").write_text(prompt)
        (folder / "binding.json").write_text(json.dumps({
            "run_id": run_id, "task_id": initial["task_id"],
            "prompt_sha256": runlog.sha256_text(prompt),
            "user_question_sha256": runlog.sha256_text(initial["user_question"]),
            "mcp_state_sha256": runlog.sha256_text(
                (folder / "mcp_state.initial.json").read_text())}))
        state = json.loads((folder / "mcp_state.json").read_text())
        state["acquired"] = acquired
        state["requests"] = requests
        state["http_attempts"] = requests
        state["logical_acquisitions"] = len(acquired)
        (folder / "mcp_state.json").write_text(json.dumps(state))
        requested = initial.get("requested_conditions") or {}
        answer = {
            "conclusion": conclusion, "stop_reason": "enough evidence",
            "stations_used": len(acquired),
            "field": requested.get("required_field"),
            "condition_id": requested.get("condition_id"),
            "scope_candidate_ids": [r["candidate_id"] for r in acquired],
            "evidence_refs": [f"onset:{r['candidate_id']}" for r in acquired],
            "stop_reason_code": "public_criterion_satisfied",
            "limitations": [],
        }
        (folder / f"agent_execution_{profile}_{run_id}.json").write_text(
            json.dumps({"RUN": run_id, "RUN_ID": run_id, "profile": profile,
                        "status": "succeeded", "answer": json.dumps(answer),
                        "model_requests": requests, "elapsed_ms": 1234}))
        return SimpleNamespace(returncode=0, stdout='{"status":"succeeded"}', stderr="")
    monkeypatch.setattr(run_agent_arm.subprocess, "run", fake_run)


def test_prior_success_then_failed_modified_question_shows_failure(
        live_server, monkeypatch):
    """The heart of the acceptance instance."""
    # 1. a successful run already exists for E1
    stub_driver_success(monkeypatch)
    job = post(live_server, "/api/review",
               {"case_id": "E1_orcas_event", "mode": "agent", "question": ORIGINAL})
    first = wait_for_review(live_server, job["run_id"])
    assert first["status"] == "done"
    assert first["result"]["status"] == "succeeded"
    first_run_id = first["result"]["run_id"]
    assert first["result"]["question"] == ORIGINAL

    # 2. the question is modified and the new process fails
    stub_driver_failure(monkeypatch)
    job2 = post(live_server, "/api/review",
                {"case_id": "E1_orcas_event", "mode": "agent", "question": MODIFIED})
    second = wait_for_review(live_server, job2["run_id"])

    # the interface must report THIS run as failed
    assert second["status"] == "failed", "a failed run must not read as done"
    result = second["result"]
    assert result["status"] == "failed"
    assert result["failure_kind"] == "model_fatal"
    # it must be a new run carrying the MODIFIED question, not the old success
    assert result["run_id"] != first_run_id
    assert result["question"] == MODIFIED
    assert result["is_current_run"] is True

    # the failure is recorded on disk too, and the earlier run is untouched
    assert runlog.read_outcome(first_run_id)["status"] == "succeeded"
    assert runlog.read_outcome(result["run_id"])["status"] == "failed"

    # the page's history shows both, with the failure labelled
    runs = {r["run_id"]: r for r in get(live_server, "/api/runs")["runs"]}
    assert runs[first_run_id]["status"] == "succeeded"
    assert runs[result["run_id"]]["status"] == "failed"
    assert runs[result["run_id"]]["failure_kind"] == "model_fatal"
    assert runs[result["run_id"]]["user_question"] == MODIFIED


def test_failed_run_keeps_its_evidence_and_reports_unknown_cost(
        live_server, monkeypatch):
    stub_driver_success(monkeypatch)
    job = post(live_server, "/api/review",
               {"case_id": "E1_orcas_event", "mode": "agent", "question": ORIGINAL})
    wait_for_review(live_server, job["run_id"])

    stub_driver_failure(monkeypatch)
    job2 = post(live_server, "/api/review",
                {"case_id": "E1_orcas_event", "mode": "agent", "question": MODIFIED})
    settled = wait_for_review(live_server, job2["run_id"])

    cost = json.loads((runlog.run_dir(settled["result"]["run_id"])
                       / "cost.json").read_text())
    # no ledger entry for this run: the page must say unknown, not 0.0000
    assert cost["model_cost_known"] is False
    assert cost["model_cost_upper_cny"] is None
    assert settled["result"]["model_cost_known"] is False
    assert settled["result"]["model_cost_cny"] is None


def test_the_prompt_for_the_modified_question_is_the_modified_one(
        live_server, monkeypatch):
    """A successful run of the modified question binds prompt and state to it."""
    stub_driver_success(monkeypatch)
    job = post(live_server, "/api/review",
               {"case_id": "E1_orcas_event", "mode": "agent", "question": MODIFIED})
    settled = wait_for_review(live_server, job["run_id"])
    result = settled["result"]
    assert result["status"] == "succeeded"

    folder = runlog.run_dir(result["run_id"])
    state = json.loads((folder / "mcp_state.initial.json").read_text())
    assert state["user_question"] == MODIFIED
    binding = json.loads((folder / "binding.json").read_text())
    assert binding["task_id"] == result["task_id"]
    assert binding["user_question_sha256"] == runlog.sha256_text(MODIFIED)
    assert MODIFIED in (folder / "prompt.txt").read_text()
    assert binding["prompt_sha256"] == runlog.sha256_text(
        (folder / "prompt.txt").read_text())
    assert MODIFIED not in json.dumps(
        {"r": result["run_id"]}, ensure_ascii=False)  # the run id itself is opaque
    for leaked in ("E1_orcas_event", "injected_artifact", "\"kind\"", "\"split\""):
        assert leaked not in (folder / "mcp_state.initial.json").read_text()


def test_rejection_is_surfaced_rather_than_hidden(live_server, monkeypatch):
    """Model says one thing, evidence supports another: deliver the verified one."""
    stub_driver_success(monkeypatch, conclusion="not_decidable")
    job = post(live_server, "/api/review",
               {"case_id": "E1_orcas_event", "mode": "agent", "question": ORIGINAL})
    settled = wait_for_review(live_server, job["run_id"])
    result = settled["result"]

    assert result["model_claim"]["conclusion"] == "not_decidable"
    assert result["delivered"]["value"] == "coherent_multistation"
    assert result["delivered"]["model_claim_accepted"] is False
    assert "not_decidable" in result["delivered"]["rejection_reason"]
    assert result["agreement"] is False


def test_deterministic_modes_also_get_their_own_run(live_server, monkeypatch):
    """Every review is recorded under its own run id, never under the case name."""
    monkeypatch.setattr(core, "MAX_STATIONS", 2)
    job = post(live_server, "/api/review",
               {"case_id": "Q1_orcas_quiet", "mode": "full", "question": ORIGINAL})
    settled = wait_for_review(live_server, job["run_id"])
    result = settled["result"]
    assert result["run_id"].startswith("r-")
    assert result["task_id"].startswith("t-")
    assert result["is_current_run"] is True
    assert result["model_cost_known"] is False
    assert runlog.read_outcome(result["run_id"])["status"] == "succeeded"
    # the record is bound to the frozen task, not to the case name
    task = json.loads((runlog.run_dir(result["run_id"]) / "task.json").read_text())
    assert task["user_question"] == ORIGINAL
    assert task["demo_case_ref"] == "Q1_orcas_quiet"


def test_cross_origin_and_missing_header_are_still_refused(live_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            urllib.request.Request(live_server + "/api/health",
                                   headers={"Origin": "http://evil.example"}),
            timeout=20)
    assert excinfo.value.code == 403

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            urllib.request.Request(live_server + "/api/review", method="POST",
                                   data=b"{}",
                                   headers={"Content-Type": "application/json"}),
            timeout=20)
    assert excinfo.value.code == 403


# ------------------------------------------------------- the feedback closed loop
def first_run(live_server, monkeypatch, mode="question"):
    run = post(live_server, "/api/review",
               {"case_id": "E1_orcas_event", "mode": mode, "question": MODIFIED})
    settled = wait_for_review(live_server, run["run_id"])
    assert settled["status"] == "done", settled
    return settled["result"]


def feedback(live_server, parent_id, **body):
    job = post(live_server, "/api/followup", {"parent_run_id": parent_id, **body})
    settled = wait_for_review(live_server, job["run_id"])
    assert settled["status"] == "done", settled
    return settled


def test_feedback_makes_an_independent_child_run_and_shows_what_changed(
        live_server, monkeypatch):
    """The interface closed loop: a second run that reuses the first's evidence."""
    parent = first_run(live_server, monkeypatch)
    parent_id = parent["run_id"]
    first_station = parent["stations"][0]["station"]

    settled = feedback(live_server, parent_id,
                       change={"kind": "exclude", "station": first_station})
    assert settled["status"] == "done", settled
    result = settled["result"]
    # a new run with its own id, tied to the parent
    assert result["run_id"] != parent_id
    assert result["parent_run_id"] == parent_id
    assert result["mode"] == "feedback"
    fb = result["feedback"]
    assert fb["reused_observations"] == len(parent["stations"])
    assert fb["new_requests"] == 0, "a feedback recompute must not fetch data"
    assert fb["model_calls"] == 0
    assert fb["affected_fields"], "a recompute that changes nothing must say so"
    changed = {a["field"] for a in fb["affected_fields"]}
    assert {"verdict", "excluded_candidates"} <= changed
    # The exclusion is persisted in the child's evidence, not just a display filter.
    child_state = json.loads(
        (runlog.run_dir(result["run_id"]) / "mcp_state.json").read_text())
    excluded = [r["candidate_id"] for r in child_state["acquired"] if r.get("excluded")]
    assert excluded and set(excluded) <= set(result["layers"]["excluded_candidates"])

    # The parent's record is untouched, and both runs stay visible.
    parent_evidence = get(live_server, f"/api/evidence/{parent_id}")
    assert parent_evidence["parent_run_id"] is None
    assert parent_evidence["feedback"] is None
    child_evidence = get(live_server, f"/api/evidence/{result['run_id']}")
    assert child_evidence["parent_run_id"] == parent_id
    assert child_evidence["feedback"]["change"]["kind"] == "exclude"
    assert child_evidence["scope"]["parent_run_id"] == parent_id
    runs = {r["run_id"]: r for r in get(live_server, "/api/runs")["runs"]}
    assert runs[parent_id]["status"] == "succeeded"
    assert runs[result["run_id"]]["parent_run_id"] == parent_id

    # the raw evidence of the parent is still there and was copied forward
    assert (runlog.run_dir(result["run_id"]) / "feedback.json").exists()
    assert (runlog.run_dir(result["run_id"]) / "mcp_state.initial.json").exists()


def test_a_band_change_recomputes_the_field_instead_of_filtering_the_view(
        live_server, monkeypatch):
    parent = first_run(live_server, monkeypatch)
    parent_id = parent["run_id"]
    settled = feedback(live_server, parent_id,
                       change={"kind": "band", "target_band_hz": [40.0, 90.0]})
    result = settled["result"]
    assert result["verdict"] == "pick_not_applicable"
    changed = {a["field"] for a in result["affected_fields"]}
    assert {"verdict", "field_value", "target_band_hz",
            "stations_not_supporting_target_band"} <= changed
    assert result["layers"]["method_applicability"]["target_band_hz"] == [40.0, 90.0]
    # the parent keeps its own band
    assert get(live_server, f"/api/evidence/{parent_id}")["task"]["target_band_hz"] \
        == [1.0, 12.0]


def test_a_free_text_feedback_that_names_a_station_is_understood(
        live_server, monkeypatch):
    parent = first_run(live_server, monkeypatch)
    station = parent["stations"][0]["station"]
    settled = feedback(live_server, parent["run_id"],
                       instruction=f"请排除 {station} 这台站，它离得最近")
    fb = settled["result"]["feedback"]
    assert fb["change"]["kind"] == "exclude"
    assert fb["change"]["station"] == station


def test_an_unresolvable_or_unsupported_feedback_is_refused_with_a_reason(
        live_server, monkeypatch):
    parent = first_run(live_server, monkeypatch)
    parent_id = parent["run_id"]

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(live_server, "/api/followup",
             {"parent_run_id": parent_id, "change": {"kind": "vertical_only"}})
    assert excinfo.value.code == 400
    assert "垂直分量" in json.loads(excinfo.value.read())["error"]

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(live_server, "/api/followup",
             {"parent_run_id": parent_id, "instruction": "再帮我看看"})
    assert excinfo.value.code == 400
    assert "无法" in json.loads(excinfo.value.read())["error"]

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(live_server, "/api/followup",
             {"parent_run_id": "r-does-not-exist",
              "change": {"kind": "band", "target_band_hz": [2.0, 8.0]}})
    assert excinfo.value.code == 404