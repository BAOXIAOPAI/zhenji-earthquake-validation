"""The production MCP entry, driven end to end without paying for a model.

These tests spawn the real `main/mcp_server.py` over stdio and speak the MCP
protocol. They do not import the tool functions and do not grep the source, which
is what the second review said the old suite did instead of an integration test.

Three things are covered, matching the first batch of the rectification guide:

* a free-input task (no `demo_case_ref`) can actually acquire a station — the old
  server looked the preset case up and died with `KeyError(None)` before fetching;
* a failed acquisition is attributed to the request that failed, and a tool that
  raises leaves a `start` + `error` pair rather than no record at all;
* the values crossing the tool boundary are serialized with the declared types.

Everything is offline: the successful acquisition is served from a cache the test
seeds itself, and the failing one has its traffic sent to a dead local proxy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv/bin/python"
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import fdsn  # noqa: E402
import core  # noqa: E402

WINDOW_START = "2025-03-03T13:02:20.000000Z"
WINDOW_END = "2025-03-03T13:04:20.000000Z"


def test_the_old_demo_case_lookup_really_would_have_raised():
    """Reproduce the old failure, so the fix is not just an assertion.

    A free-input task has `demo_case_ref=None`, and the old server looked the case
    up before fetching. `core.case_by_id(None)` is exactly that lookup.
    """
    with pytest.raises(KeyError):
        core.case_by_id(None)


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def _seed_cache(cache_dir: Path, url: str, payload: bytes) -> None:
    """Write one cache entry exactly the way fdsn.cached_get would."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(url)
    (cache_dir / f"{key}.bin").write_bytes(payload)
    (cache_dir / f"{key}.json").write_text(json.dumps(
        {"url": url, "file": f"{key}.bin",
         "sha256": hashlib.sha256(payload).hexdigest(),
         "bytes": len(payload)}))


def _synthetic_waveform() -> bytes:
    """A 3-component MiniSEED window. Real bytes, no network."""
    from obspy import Stream, Trace, UTCDateTime
    rng = np.random.default_rng(7)
    stream = Stream()
    for component, phase in (("Z", 0.0), ("N", 0.0), ("E", 0.0)):
        trace = Trace(data=rng.normal(size=1000).astype(np.float64))
        trace.stats.network, trace.stats.station = "UW", "TEST"
        trace.stats.location = ""
        trace.stats.channel = f"HH{component}"
        trace.stats.sampling_rate = 50.0
        trace.stats.starttime = UTCDateTime(WINDOW_START)
        # A short burst so the STA/LTA detector has something to find.
        trace.data[400:450] += 25.0 * np.sin(np.linspace(0, 20, 50))
        stream.append(trace)
    import io
    buffer = io.BytesIO()
    stream.write(buffer, format="MSEED")
    return buffer.getvalue()


def _synthetic_metadata() -> bytes:
    import io
    from obspy import UTCDateTime
    from obspy.core.inventory import Channel, Inventory, Network, Station
    channels = [
        Channel("HHZ", " ", 47.0, -122.0, elevation=0.0, depth=0.0,
                azimuth=0.0, dip=-90.0, sample_rate=50.0,
                start_date=UTCDateTime("2020-01-01")),
        Channel("HHN", " ", 47.0, -122.0, elevation=0.0, depth=0.0,
                azimuth=0.0, dip=0.0, sample_rate=50.0,
                start_date=UTCDateTime("2020-01-01")),
        Channel("HHE", " ", 47.0, -122.0, elevation=0.0, depth=0.0,
                azimuth=90.0, dip=0.0, sample_rate=50.0,
                start_date=UTCDateTime("2020-01-01")),
    ]
    inv = Inventory(networks=[Network("UW", stations=[
        Station("TEST", 47.0, -122.0, 0.0, channels=channels)])])
    buffer = io.BytesIO()
    inv.write(buffer, format="STATIONXML")
    return buffer.getvalue()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    """A hermetic run store and cache, plus the run state the server serves."""
    runlog_dir = tmp_path / "runlog"
    cache_dir = tmp_path / "cache"
    runlog_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)

    task_id = "t-mcptest00000001"
    run_id = "r-mcptest0000001"
    folder = runlog_dir / run_id
    (folder / "raw").mkdir(parents=True)
    (folder / "derived").mkdir(parents=True)

    ok_candidate = {"candidate_id": "cand-ok", "network": "UW", "station": "TEST",
                    "distance_km": 12.3, "family": "HH", "analysis_eligible": True,
                    "three_component": True, "vertical_only": False,
                    "band_support": {"supported": True},
                    "reason": "synthetic three-component station"}
    fail_candidate = {"candidate_id": "cand-fail", "network": "UW", "station": "DEAD",
                      "distance_km": 20.0, "family": "HH", "analysis_eligible": True,
                      "three_component": True, "vertical_only": False,
                      "band_support": {"supported": True},
                      "reason": "synthetic station with no cached data"}
    state = {
        "run_id": run_id, "task_id": task_id,
        "user_question": "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？",
        "window_start": WINDOW_START, "window_end": WINDOW_END,
        "region": {"center_lat": 47.0, "center_lon": -122.0, "minlatitude": 46.4,
                   "maxlatitude": 47.6, "minlongitude": -122.6, "maxlongitude": -121.4},
        "target_band_hz": [1.0, 12.0], "method_band_hz": [1.0, 8.0],
        "method_goal": "three_component_pick",
        "allowed_actions": ["discover", "acquire", "onset_check", "read_evidence"],
        "discovery": {"n_eligible": 2,
                      "eligible": [ok_candidate, fail_candidate]},
        "acquired": [], "requests": 0, "http_attempts": 0,
        "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
    }
    (folder / "mcp_state.json").write_text(json.dumps(state, ensure_ascii=False))
    (folder / "mcp_state.initial.json").write_text(json.dumps(state, ensure_ascii=False))
    # The evaluator side deliberately carries demo_case_ref=None: this is the exact
    # shape of a free-input task, and the exact shape that used to raise KeyError.
    (folder / "evaluator.json").write_text(json.dumps(
        {"task_id": task_id, "demo_case_ref": None, "acceptance": None,
         "kind": None, "split": None, "inject_first": False}))

    spec = {"start": WINDOW_START, "end": WINDOW_END}
    requests = fdsn.build_requests(
        {"network": "UW", "station": "TEST", "latitude": None, "longitude": None},
        spec, "HH")
    _seed_cache(cache_dir, requests["waveform_url"], _synthetic_waveform())
    _seed_cache(cache_dir, requests["metadata_url"], _synthetic_metadata())

    env = {**os.environ,
           "RUNLOG_DIR": str(runlog_dir),
           "EQ_CACHE_DIR": str(cache_dir),
           # Dead proxy: any real fetch fails immediately instead of reaching out.
           "https_proxy": "http://127.0.0.1:9",
           "http_proxy": "http://127.0.0.1:9",
           "HTTPS_PROXY": "http://127.0.0.1:9",
           "HTTP_PROXY": "http://127.0.0.1:9"}
    monkeypatch.setenv("RUNLOG_DIR", str(runlog_dir))
    monkeypatch.setenv("EQ_CACHE_DIR", str(cache_dir))
    return {"run_id": run_id, "folder": folder, "env": env}


def _payload(result):
    """The tool's dict, whichever way this MCP version serialized it."""
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured.get("result", structured)
    text = "".join(getattr(block, "text", "") for block in result.content)
    return json.loads(text)


def _is_error(result):
    return bool(getattr(result, "is_error", False))


def _call(run_id, env, calls):
    """Run a sequence of (tool, arguments) through the real stdio MCP server."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def drive():
        params = StdioServerParameters(
            command=str(PY), args=[str(ROOT / "main/mcp_server.py"), run_id], env=env)
        out = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = sorted(t.name for t in listed.tools)
                for tool, arguments in calls:
                    try:
                        result = await session.call_tool(tool, arguments)
                        out.append((tool, _is_error(result), _payload(result)))
                    except Exception as exc:  # protocol-level error
                        out.append((tool, True, {"raised": type(exc).__name__,
                                                 "error": str(exc)[:200]}))
        return names, out

    return asyncio.run(drive())


def _tool_log(folder: Path):
    path = folder / "tool_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_raw_material_tool_result_and_state_share_one_run(environment):
    """The frozen task, the candidate, the raw waveform and the tool record must all
    belong to the same run id — the evidence the review asks to be jointly inspectable.
    """
    state_on_disk = json.loads((environment["folder"] / "mcp_state.json").read_text())
    assert state_on_disk["run_id"] == environment["run_id"]
    task_id = state_on_disk["task_id"]

    _, out = _call(environment["run_id"], environment["env"], [
        ("acquire_station", {"candidate_id": "cand-ok"}),
        ("get_evidence", {}),
    ])
    acquired = out[0][2]
    assert acquired["candidate_id"] == "cand-ok"
    assert acquired["station"] == "TEST"

    # raw material written under the same run id, next to the record that describes it
    raw = environment["folder"] / "raw" / "cand-ok"
    assert (raw / "waveform.mseed").exists()
    assert (raw / "metadata.xml").exists()
    assert (environment["folder"] / "mcp_state.json").exists()
    assert (environment["folder"] / "evaluator.json").exists()

    state_after = json.loads((environment["folder"] / "mcp_state.json").read_text())
    assert state_after["task_id"] == task_id
    assert state_after["acquired"][0]["candidate_id"] == "cand-ok"
    assert state_after["acquired"][0]["status"] == "analysed"

    log = _tool_log(environment["folder"])
    assert log and all(entry["run_id"] == environment["run_id"] for entry in log)


def test_the_server_exposes_exactly_the_declared_tools(environment):
    names, _ = _call(environment["run_id"], environment["env"], [])
    assert names == ["acquire_station", "exclude_observation", "get_evidence",
                     "list_candidates", "run_onset_check", "run_stability_check",
                     "task_status"]


def test_a_free_input_task_acquires_a_station_without_a_demo_case(environment):
    """P0-1 regression: demo_case_ref=None must not block the real download."""
    names, out = _call(environment["run_id"], environment["env"], [
        ("list_candidates", {}),
        ("acquire_station", {"candidate_id": "cand-ok"}),
        ("get_evidence", {}),
    ])
    assert not out[0][1], out[0][2]
    assert [c["candidate_id"] for c in out[0][2]["candidates"]] == ["cand-ok",
                                                                   "cand-fail"]

    tool, is_error, acquired = out[1]
    assert not is_error, acquired
    assert acquired["status"] == "analysed", acquired
    assert acquired["station"] == "TEST"
    # The field contract crossing the boundary: exactly the declared fields.
    for field in ("station", "network", "status", "acquisition_status",
                  "sampling_rate_hz", "band_support", "method_applicability",
                  "channel_integrity_flags", "vertical_ok"):
        assert field in acquired, f"{field} missing from the tool reply"
    # Vectors are JSON arrays, booleans are JSON booleans — not strings.
    assert acquired["band_support"]["target_band_hz"] == [1.0, 12.0]
    assert isinstance(acquired["vertical_ok"], bool)
    assert isinstance(acquired["method_applicability"], dict)

    evidence = out[2][2]
    assert evidence["stations_acquired"] == 1
    assert evidence["failed_requests"] == []
    assert evidence["tool_errors"] == 0

    # The raw evidence was written under this run id, tied to the frozen task.
    assert (environment["folder"] / "raw/cand-ok/waveform.mseed").exists()
    assert (environment["folder"] / "raw/cand-ok/metadata.xml").exists()

    log = _tool_log(environment["folder"])
    phases = [(e["tool"], e["phase"]) for e in log]
    assert ("acquire_station", "start") in phases
    assert ("acquire_station", "ok") in phases


def test_a_failed_download_is_attributed_and_never_called_insufficient_data(environment):
    """A request that returns nothing is a data fact, not an interface error."""
    _, out = _call(environment["run_id"], environment["env"], [
        ("acquire_station", {"candidate_id": "cand-fail"}),
        ("get_evidence", {}),
    ])
    tool, is_error, record = out[0]
    assert not is_error, record
    assert record["status"] in ("metadata_unavailable", "waveform_unavailable"), record
    assert record["failed_request"] in ("metadata", "waveform")
    assert record["metadata_available"] is False
    assert record["distinction"] and record["next_step"]

    evidence = out[1][2]
    assert evidence["tool_errors"] == 0, "a completed request is not a tool error"
    assert evidence["failed_requests"], "the failed request must be listed"
    assert evidence["stations_returning_no_data"] == ["DEAD"]


def test_a_raised_tool_is_logged_as_an_error_and_surfaced(environment):
    """A tool that raises used to write no line at all, hiding interface failure."""
    _, out = _call(environment["run_id"], environment["env"], [
        ("acquire_station", {"candidate_id": "not-in-the-list"}),
        ("run_onset_check", {"candidate_id": "cand-ok"}),
        ("get_evidence", {}),
    ])
    assert out[0][1] is True, "an out-of-list candidate must be a tool error"
    assert out[1][1] is True, "a check before acquisition must be a tool error"

    evidence = out[2][2]
    assert evidence["tool_errors"] == 2
    assert set(evidence["tools_with_errors"]) == {"acquire_station", "run_onset_check"}

    log = _tool_log(environment["folder"])
    errors = [e for e in log if e["phase"] == "error"]
    assert {e["tool"] for e in errors} == {"acquire_station", "run_onset_check"}
    assert all(e["error_type"] == "ValueError" for e in errors)

    # The failure is also durable in the run state the validator reads.
    state = json.loads((environment["folder"] / "mcp_state.json").read_text())
    assert len(state["tool_errors"]) == 2
    assert state["tool_attempts"]["get_evidence"]["ok"] == 1


def test_acquisition_start_is_recorded_before_any_work(environment):
    """The start line is what lets 'never asked' be told from 'failed after asking'."""
    _call(environment["run_id"], environment["env"], [
        ("acquire_station", {"candidate_id": "cand-ok"}),
    ])
    log = _tool_log(environment["folder"])
    acquire = [e for e in log if e["tool"] == "acquire_station"]
    assert [e["phase"] for e in acquire] == ["start", "ok"]
    assert acquire[0]["at"] <= acquire[1]["at"]


def test_exclude_recomputes_and_returns_the_asked_field(environment):
    """Regression for the NameError: `affected` was used before it was assigned.

    The tool must return a before/after for every affected check, not raise.
    """
    _, out = _call(environment["run_id"], environment["env"], [
        ("acquire_station", {"candidate_id": "cand-ok"}),
        ("exclude_observation", {"candidate_id": "cand-ok",
                                 "reason": "用户排除这台站"}),
    ])
    assert not out[0][1], out[0][2]
    tool, is_error, result = out[1]
    assert not is_error, result
    assert result["status"] == "excluded"
    assert result["stations_remaining"] == 0
    assert "recomputed" in result and "unchanged_checks" in result
    # With nothing left, the asked field is unknown, not a negative finding.
    answered = result["answer_after_recompute"]
    assert answered["answered"] is False
    assert answered["value"] == "unknown"

    log = _tool_log(environment["folder"])
    assert ("exclude_observation", "start") in [(e["tool"], e["phase"]) for e in log]
    assert ("exclude_observation", "ok") in [(e["tool"], e["phase"]) for e in log]

def test_a_disclosed_injection_really_derives_a_separate_bad_channel(environment):
    """The demo injection, through the production entry, on a free-input task.

    The second review wants the main display to use a real window plus a *disclosed*
    injection. This checks that the switch the evaluator writes actually reaches the
    acquisition layer, that the derived copy is a separate file, and that the raw
    download is untouched — while the tool reply says only what a user could observe.
    """
    folder = environment["folder"]
    evaluator = json.loads((folder / "evaluator.json").read_text())
    evaluator["inject_first"] = True
    (folder / "evaluator.json").write_text(json.dumps(evaluator))

    raw = folder / "raw" / "cand-ok" / "waveform.mseed"

    _, out = _call(environment["run_id"], environment["env"], [
        ("acquire_station", {"candidate_id": "cand-ok"}),
    ])
    tool, is_error, acquired = out[0]
    assert not is_error, acquired
    assert acquired["status"] == "analysed", acquired
    # the observable a user sees: a constant channel, reported as such
    flags = acquired["channel_integrity_flags"] or []
    assert any(f.endswith(":constant") for f in flags), flags

    # The injection is a derived object, not a rewritten raw file: the download keeps
    # its real E channel, the derived copy carries the constant one.
    derived = folder / "derived" / "cand-ok" / "waveform.mseed"
    assert derived.exists()
    assert (derived.parent / "derivation.json").exists()
    from obspy import read as obspy_read
    raw_e = np.asarray(obspy_read(str(raw), format="MSEED")
                       .select(component="E")[0].data, float)
    derived_e = np.asarray(obspy_read(str(derived), format="MSEED")
                           .select(component="E")[0].data, float)
    assert float(np.ptp(raw_e)) > 0.0, "the raw download must keep its real channel"
    assert float(np.ptp(derived_e)) == 0.0, "the derived channel must be constant"

    state = json.loads((folder / "mcp_state.json").read_text())
    record = state["acquired"][0]
    assert record["derivation"]["synthetic"] is True
    assert record["derived_waveform_sha256"] != record["raw_waveform_sha256"]
    # the tool reply must not disclose the evaluator-side transform internals
    for leaked in ("derivation", "derived_waveform_sha256", "parent_record_id",
                   "injected_artifact", "synthetic"):
        assert leaked not in acquired, f"{leaked} leaked to the tool reply"

    log = _tool_log(folder)
    assert ("acquire_station", "start") in [(e["tool"], e["phase"]) for e in log]
