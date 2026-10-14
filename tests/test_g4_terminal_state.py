"""Production-terminal-state regressions for tool errors and recovery (review-4 §2.2).

The four required cases must run through the REAL `_execute`: a tsx subprocess
(the controlled driver), the production MCP tool layer (`main/mcp_server.py`,
imported and called exactly as a session would call it), the production
validator, the cost record and the outcome record. No model, no provider, no
payment; successes come from a cache this test seeds, data-level failures from
a dead local proxy, interface failures from the controlled driver's forced
raise.

  case 1  key acquisition error, no valid supplement, clean driver exit
          -> failed / tool_failure, interface reason visible
  case 2  error then a valid ok on the SAME tool and target, field settled
          -> succeeded, error history kept, recovery proven by event order
  case 3  ok followed by a later key error, task unsettled
          -> failed; never "其后恢复"
  case 4  an unrecovered error but independently sufficient evidence
          -> succeeded; completion and the exception reported separately

These tests also pin review-4 A5 on the real path: after the run, the JSONL
event counts, the persisted `mcp_state.json` counters and the validation's
`tool_attempts` agree, and a fresh process reading the state from disk still
sees the success counts.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv/bin/python"
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import fdsn  # noqa: E402
import core  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402
import run_agent_arm  # noqa: E402

WINDOW_START = "2025-03-03T13:02:20.000000Z"
WINDOW_END = "2025-03-03T13:04:20.000000Z"
QUESTION = "这个时窗能不能用来做 1–12 Hz 的 P/S 拾取？"
TSX_OK = (ROOT / "vendor/agnes-harness/node_modules/.bin/tsx").exists()
pytestmark = pytest.mark.skipif(not TSX_OK, reason="tsx 不可用（受控driver无法运行）")


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def _seed_cache(cache_dir: Path, url: str, payload: bytes) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(url)
    (cache_dir / f"{key}.bin").write_bytes(payload)
    (cache_dir / f"{key}.json").write_text(json.dumps(
        {"url": url, "file": f"{key}.bin",
         "sha256": hashlib.sha256(payload).hexdigest(),
         "bytes": len(payload)}))


def _synthetic_waveform() -> bytes:
    import io
    import numpy as np
    from obspy import Stream, Trace, UTCDateTime
    rng = np.random.default_rng(7)
    stream = Stream()
    for component in ("Z", "N", "E"):
        trace = Trace(data=rng.normal(size=3000).astype(np.float64))
        trace.stats.network, trace.stats.station = "UW", "TEST"
        trace.stats.location = ""
        trace.stats.channel = f"HH{component}"
        trace.stats.sampling_rate = 50.0
        trace.stats.starttime = UTCDateTime(WINDOW_START)
        trace.data[1000:1050] += 25.0 * np.sin(np.linspace(0, 20, 50))
        stream.append(trace)
    buffer = io.BytesIO()
    stream.write(buffer, format="MSEED")
    return buffer.getvalue()


def _synthetic_metadata() -> bytes:
    import io
    from obspy import UTCDateTime
    from obspy.core.inventory import Channel, Inventory, Network, Station
    from obspy.core.inventory.response import Response
    channels = [
        Channel(f"HH{c}", " ", 47.0, -122.0, elevation=0.0, depth=0.0,
                azimuth=az, dip=-90.0 if c == "Z" else 0.0, sample_rate=50.0,
                start_date=UTCDateTime("2020-01-01"), response=Response())
        for c, az in (("Z", 0.0), ("N", 0.0), ("E", 90.0))]
    network = Network("UW", stations=[Station("TEST", 47.0, -122.0, 0.0,
                                              channels=channels)])
    inventory = Inventory(networks=[network])
    buffer = io.BytesIO()
    inventory.write(buffer, format="STATIONXML")
    return buffer.getvalue()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    """A runlog + cache pair the parent process and the driver subprocess share."""
    runlog_dir = tmp_path / "runlog"
    cache_dir = tmp_path / "cache"
    tasks_dir = tmp_path / "tasks"
    runlog_dir.mkdir()
    # The parent process (validator/outcome writers) and the subprocess (the
    # controlled driver) must resolve the same roots.
    monkeypatch.setenv("RUNLOG_DIR", str(runlog_dir))
    monkeypatch.setenv("EQ_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(runlog, "RUNLOG", runlog_dir)
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", runlog_dir)
    monkeypatch.setattr(core, "CACHE", cache_dir)
    monkeypatch.setattr(taskmod, "TASKS", tasks_dir)
    core.fdsn.set_cache(cache_dir)

    spec = {"start": WINDOW_START, "end": WINDOW_END}
    requests = fdsn.build_requests(
        {"network": "UW", "station": "TEST", "latitude": None, "longitude": None},
        spec, "HH")
    _seed_cache(cache_dir, requests["waveform_url"], _synthetic_waveform())
    _seed_cache(cache_dir, requests["metadata_url"], _synthetic_metadata())
    return {"runlog": runlog_dir, "cache": cache_dir}


def _candidates():
    ok = {"candidate_id": "cand-ok", "network": "UW", "station": "TEST",
          "distance_km": 1.0, "family": "HH", "analysis_eligible": True,
          "three_component": True, "vertical_only": False,
          "band_support": {"supported": True},
          "reason": "synthetic three-component station"}
    dead = {"candidate_id": "cand-dead", "network": "UW", "station": "DEAD",
            "distance_km": 20.0, "family": "HH", "analysis_eligible": True,
            "three_component": True, "vertical_only": False,
            "band_support": {"supported": True},
            "reason": "synthetic station with no cached data"}
    return [ok, dead]


def _fresh_run(env):
    """One prepared run: frozen task, run folder, MCP state, evaluator side."""
    frozen = taskmod.make_task(
        user_question=QUESTION, window_start=WINDOW_START, window_end=WINDOW_END,
        region={"center_lat": 47.0, "center_lon": -122.0, "minlatitude": 46.4,
                "maxlatitude": 47.6, "minlongitude": -122.6, "maxlongitude": -121.4},
        target_band_hz=[1.0, 12.0], method_goal="three_component_pick")
    taskmod.freeze(frozen)
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm="adaptive_agent_open")
    candidates = _candidates()
    state = {"run_id": run_id, "task_id": frozen["task_id"],
             "user_question": QUESTION,
             "window_start": WINDOW_START, "window_end": WINDOW_END,
             "region": frozen["region"], "target_band_hz": [1.0, 12.0],
             "method_band_hz": list(core.METHOD_BAND),
             "method_goal": "three_component_pick",
             "allowed_actions": frozen["allowed_actions"],
             "discovery": {"n_eligible": len(candidates), "eligible": candidates},
             "acquired": [], "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0}
    (folder / "mcp_state.json").write_text(json.dumps(state, ensure_ascii=False))
    (folder / "mcp_state.initial.json").write_text(
        json.dumps(state, ensure_ascii=False))
    (folder / "evaluator.json").write_text(json.dumps(
        {"task_id": frozen["task_id"], "demo_case_ref": None, "acceptance": None,
         "kind": None, "split": None, "inject_first": False}))
    return {"run_id": run_id, "task_id": frozen["task_id"], "folder": folder}


def _drive(env, scenario, monkeypatch):
    """Run one scenario through the production `_execute` with the controlled driver."""
    prepared = _fresh_run(env)
    # The scenario reaches the driver subprocess through the environment.
    monkeypatch.setenv("CONTROLLED_SCENARIO", scenario)
    # A dead proxy keeps any cache miss a data-level failure instead of network use.
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    row = run_agent_arm._execute(
        "user-terminal-state", prepared, "Q_usable", QUESTION,
        driver_script="controlled_driver.mjs")
    return prepared, row


def _tool_log(folder: Path):
    path = folder / "tool_calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()] if path.exists() else []


def _counts(events, tool, phase):
    return sum(1 for e in events if e.get("tool") == tool
               and e.get("phase") == phase)


def _assert_three_way_consistency(folder, row):
    """Review-4 A5 on the real path: JSONL == persisted state == validation."""
    events = _tool_log(folder)
    state = json.loads((folder / "mcp_state.json").read_text())
    validation = json.loads((folder / "validation.json").read_text())
    persisted = state.get("tool_attempts") or {}
    for tool, counts in (validation["acquisition"]["tool_attempts"] or {}).items():
        disk = persisted.get(tool) or {}
        for phase in ("start", "ok", "error"):
            assert counts.get(phase, 0) == disk.get(phase, 0), (tool, phase)
            assert counts.get(phase, 0) == _counts(events, tool, phase), (tool, phase)
    # A fresh process reading the state from disk still sees the same counters:
    # the persisted file, not an in-memory copy, is what survives a restart.
    reread = json.loads((folder / "mcp_state.json").read_text())
    assert (reread.get("tool_attempts") or {}) == persisted


def test_case_1_key_acquisition_error_blocks_the_run(environment, monkeypatch):
    prepared, row = _drive(environment, "acquire_error_unrecovered", monkeypatch)
    folder = prepared["folder"]
    assert row["status"] == "failed"
    assert row["failure_kind"] == "tool_failure"
    assert row["interface_blocked"] is True
    # The interface reason is visible, and it is not read as missing evidence.
    assert "接口失败" in (row["failure_detail"] or "")
    assert "acquire_station" in (row["failure_detail"] or "")
    assert row["delivered"]["task_completed"] is False
    events = _tool_log(folder)
    assert _counts(events, "acquire_station", "error") == 1
    assert _counts(events, "acquire_station", "ok") == 0
    recovery = row["acquisition"]["recovery"]
    assert recovery["order_known"] is True
    assert recovery["unrecovered"] and not recovery["recovered"]
    _assert_three_way_consistency(folder, row)


def test_case_2_error_then_ok_on_the_same_target_recovers(environment, monkeypatch):
    prepared, row = _drive(environment, "error_then_ok_same_target", monkeypatch)
    folder = prepared["folder"]
    assert row["status"] == "succeeded"
    assert row["failure_kind"] is None
    assert row["delivered"]["task_completed"] is True
    assert row["stations_acquired"] == 1
    # The error history is kept AND the recovery is proven by event order.
    assert "已由取得的证据回答" in (row["failure_detail"] or "")
    assert "已成功" in (row["failure_detail"] or "")
    recovery = row["acquisition"]["recovery"]
    assert recovery["recovered"] == ["acquire_station(cand-ok)"]
    assert recovery["unrecovered"] == []
    events = _tool_log(folder)
    order = [e["phase"] for e in events
             if e["tool"] == "acquire_station" and e["phase"] in ("ok", "error")]
    assert order == ["error", "ok"]
    _assert_three_way_consistency(folder, row)


def test_case_3_ok_then_later_error_is_not_a_recovery(environment, monkeypatch):
    prepared, row = _drive(environment, "ok_then_error_incomplete", monkeypatch)
    folder = prepared["folder"]
    assert row["status"] == "failed"
    assert row["failure_kind"] == "tool_failure"
    assert row["delivered"]["task_completed"] is False
    # The A4 defect: cumulative counts used to say "其后调用成功，视为已恢复".
    detail = row["failure_detail"] or ""
    assert "已恢复" not in detail
    assert "未恢复" in detail
    recovery = row["acquisition"]["recovery"]
    assert recovery["recovered"] == []
    assert recovery["unrecovered"] == ["acquire_station(cand-ok)"]
    events = _tool_log(folder)
    order = [e["phase"] for e in events
             if e["tool"] == "acquire_station" and e["phase"] in ("ok", "error")]
    assert order == ["ok", "error"]
    # The data-level no-data fact stays a data fact, not a tool error.
    statuses = [r["status"] for r in
                json.loads((folder / "mcp_state.json").read_text())["acquired"]]
    assert statuses == ["metadata_unavailable"]
    _assert_three_way_consistency(folder, row)


def test_case_4_unrecovered_error_but_independent_evidence(environment, monkeypatch):
    prepared, row = _drive(environment, "independent_evidence_ok", monkeypatch)
    folder = prepared["folder"]
    assert row["status"] == "succeeded"
    assert row["delivered"]["task_completed"] is True
    assert row["stations_acquired"] == 1
    detail = row["failure_detail"] or ""
    # Completion and the exception are reported separately: never "已恢复".
    assert "已由取得的证据回答" in detail
    assert "未恢复异常" in detail
    assert "已恢复" not in detail
    recovery = row["acquisition"]["recovery"]
    assert recovery["unrecovered"] == ["acquire_station(cand-dead)"]
    assert recovery["recovered"] == []
    _assert_three_way_consistency(folder, row)


def test_success_counts_survive_a_fresh_process(environment, monkeypatch):
    """A5's restart clause: after a real stdio MCP session, a NEW server process
    reading the state from disk still sees the success counts."""
    prepared = _fresh_run(environment)
    run_id = prepared["run_id"]
    folder = prepared["folder"]
    env = {**os.environ,
           "RUNLOG_DIR": str(environment["runlog"]),
           "EQ_CACHE_DIR": str(environment["cache"]),
           "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
           "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}
    import subprocess
    proc = subprocess.run(
        [str(PY), str(ROOT / "main/mcp_server_probe.py"), run_id,
         "acquire_station:candidate_id=cand-ok"],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-400:]
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["tool_attempts"]["acquire_station"] == \
        {"start": 1, "ok": 1, "error": 0}
    # ...and the same numbers when the state is re-read from disk (a restart).
    state = json.loads((folder / "mcp_state.json").read_text())
    assert state["tool_attempts"]["acquire_station"] == \
        {"start": 1, "ok": 1, "error": 0}
    events = _tool_log(folder)
    assert _counts(events, "acquire_station", "ok") == 1
