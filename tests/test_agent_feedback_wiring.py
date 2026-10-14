"""An Agent parent's web feedback must go back through the real Agent path.

No model is ever called here. The real Agent entry point
(`server._agent_followup`) is replaced by a stub, so these tests pin only the
ROUTING decision the HTTP layer makes: for a given parent run it must pick the
engine the parent implies, an explicit override must be honoured, and asking for
the Agent path on a deterministic parent must be a clear 400 rather than a
silent model spend. The same `live_server` shape as test_g0_interface.py is used
so the real Handler, validation and run registry are exercised.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import review  # noqa: E402
import runlog  # noqa: E402
import server as servermod  # noqa: E402
import task as taskmod  # noqa: E402

QUESTION = "这个候选异常是不是多个台站共有的真实传播信号？"


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
    monkeypatch.setattr(servermod, "ACTIVE", None)

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


def start_parent(live_server):
    """A real deterministic parent run (arm == the mode), with observations."""
    job = post(live_server, "/api/review",
               {"case_id": "E1_orcas_event", "mode": "full", "question": QUESTION})
    settled = wait_for_review(live_server, job["run_id"])
    assert settled["status"] == "done", settled
    return settled["result"]


def relabel_parent_as_agent(parent_id):
    """Make the parent read as a real Agent run, as a live AGH round would."""
    folder = runlog.run_dir(parent_id)
    meta = json.loads((folder / "run.json").read_text())
    meta["arm"] = "adaptive_agent_open"
    (folder / "run.json").write_text(json.dumps(meta, ensure_ascii=False))
    return folder


def test_unavailable_model_blocks_agent_feedback_but_allows_rule_recompute(live_server, monkeypatch):
    parent = start_parent(live_server)
    parent_id = parent["run_id"]
    relabel_parent_as_agent(parent_id)
    monkeypatch.setattr(servermod, "model_readiness", lambda: {
        "model_available": False, "model_unavailable_reason": "fixture: 模型未配置"})
    monkeypatch.setattr(servermod, "_agent_followup", lambda *a: pytest.fail("model started"))
    payload = {"parent_run_id": parent_id,
               "change": {"kind": "exclude", "station": parent["stations"][0]["station"]}}
    before = len(servermod.RUNS)
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(live_server, "/api/followup", payload)
    assert exc.value.code == 503
    assert json.loads(exc.value.read())["error"] == "fixture: 模型未配置"
    assert len(servermod.RUNS) == before
    rule = post(live_server, "/api/followup", {**payload, "engine": "rule"})
    assert rule["executed_by"] == "deterministic"
    assert wait_for_review(live_server, rule["run_id"])["status"] == "done"


def test_agent_parent_routes_followup_through_the_real_agent(live_server, monkeypatch):
    parent = start_parent(live_server)
    parent_id = parent["run_id"]
    relabel_parent_as_agent(parent_id)
    calls = {}

    def stub_agent(parent_run_id, change, instruction):
        """A model-free stand-in for run_agent_arm.run_followup."""
        calls["parent_run_id"] = parent_run_id
        calls["change"] = change
        calls["instruction"] = instruction
        task = json.loads((runlog.run_dir(parent_run_id) / "task.json").read_text())
        child_id = runlog.new_run_id()
        runlog.init_run(child_id, task, arm="adaptive_agent_open_followup",
                        extra={"parent_run_id": parent_run_id})
        calls["child_id"] = child_id
        return {"run_id": child_id, "task_id": task["task_id"],
                "parent_run_id": parent_run_id,
                "arm": "adaptive_agent_open_followup", "status": "succeeded"}

    monkeypatch.setattr(servermod, "_agent_followup", stub_agent)
    # If the wiring ever fell back to the deterministic path, this would fire.
    monkeypatch.setattr(review, "run_followup", lambda *a, **k: (
        _ for _ in ()).throw(AssertionError("deterministic recompute was used")))

    station = parent["stations"][0]["station"]
    job = post(live_server, "/api/followup",
               {"parent_run_id": parent_id,
                "change": {"kind": "exclude", "station": station}})
    # The 202 the page reads already names the engine it chose.
    assert job["mode"] == "agent_feedback"
    assert job["executed_by"] == "agent"
    assert job["parent_run_id"] == parent_id

    settled = wait_for_review(live_server, job["run_id"])
    assert settled["status"] == "done", settled
    assert settled["result"]["mode"] == "agent_feedback"
    assert settled["result"]["executed_by"] == "agent"

    # The stub really ran, received the parent and the change, and its child run
    # exists on disk with the parent recorded.
    child_id = calls["child_id"]
    child = runlog.run_dir(child_id)
    assert child.exists()
    assert (child / "run.json").exists()
    child_meta = json.loads((child / "run.json").read_text())
    assert child_meta["parent_run_id"] == parent_id
    assert child_meta["arm"] == "adaptive_agent_open_followup"
    assert calls["parent_run_id"] == parent_id
    assert calls["change"]["kind"] == "exclude"
    assert calls["change"]["station"] == station
    # The parent is a NEW run's parent, never rewritten.
    parent_meta = json.loads((runlog.run_dir(parent_id) / "run.json").read_text())
    assert parent_meta["arm"] == "adaptive_agent_open"
    assert child_id != parent_id


def test_deterministic_parent_keeps_the_recompute_path(live_server, monkeypatch):
    parent = start_parent(live_server)          # arm == "full"
    parent_id = parent["run_id"]

    def should_not_run(*args, **kwargs):
        raise AssertionError("the Agent engine ran for a deterministic parent")

    monkeypatch.setattr(servermod, "_agent_followup", should_not_run)

    station = parent["stations"][0]["station"]
    job = post(live_server, "/api/followup",
               {"parent_run_id": parent_id,
                "change": {"kind": "exclude", "station": station}})
    assert job["mode"] == "feedback"
    assert job["executed_by"] == "deterministic"

    settled = wait_for_review(live_server, job["run_id"])
    assert settled["status"] == "done", settled
    result = settled["result"]
    assert result["mode"] == "feedback"
    assert result["parent_run_id"] == parent_id
    assert result["run_id"] != parent_id
    assert result["feedback"]["model_calls"] == 0
    assert result["feedback"]["new_requests"] == 0


def test_explicit_agent_engine_on_a_deterministic_parent_is_refused(
        live_server, monkeypatch):
    parent = start_parent(live_server)          # arm == "full"
    parent_id = parent["run_id"]
    before = dict(servermod.RUNS)               # the parent's own review entry

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(live_server, "/api/followup",
             {"parent_run_id": parent_id, "engine": "agent",
              "change": {"kind": "band", "target_band_hz": [2.0, 8.0]}})
    assert excinfo.value.code == 400
    body = json.loads(excinfo.value.read())
    assert "Agent" in body["error"]
    # Nothing was started and nothing was spent.
    assert servermod.ACTIVE is None
    assert servermod.RUNS == before
