"""Review-4 gate-A closure: the production-entry results the guide §1.1/§1.2
requires, on top of the review-3 convergence tests.

Every case drives the PRODUCTION entry named in the guide's table —
`core.quality_fields` on a real in-memory waveform copy, the production MCP
`exclude_observation` on a prepared run, `prepare_agent.prepare_followup` on a
saved parent state — and asserts from the measured quality entry down to the
final field, reason and `task_completed`. No network, no model, no payment.

Guide §1.1's five rows:

  healthy Z + constant E, three-component goal -> not met (kept: review-3 test)
  the same observation, vertical goal          -> met (kept: review-3 test)
  constant Z with three channels, vertical goal -> NOT usable; names Z:constant
  healthy Z + bad E, exclude another clean station in MCP, vertical goal
                                                 -> tool and validator agree on
                                                    the SAME goal and answer
  no measurable data                            -> unknown (kept: review-3 test)

Plus §1.2 (carried-evidence re-judgement), the stale-condition guard, §2.1
(counts consistency and the goal through a real stdio MCP session) and the
grid-vs-band free-text distinction (§3.3).
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import fdsn  # noqa: E402
import core  # noqa: E402
import prepare_agent  # noqa: E402
import review  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402
import run_agent_arm  # noqa: E402

# The production MCP server, loaded by path (prototype ships a same-named one).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("eq_main_mcp_server",
                                               str(ROOT / "main/mcp_server.py"))
mcp_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_server)

SOURCE_RUN = ROOT / "main/runlog/r-2da810ff3e97b731"
TARGET = (1.0, 12.0)


# ------------------------------------------------------------- §1.1 row 3: A1
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(mcp_server, "FOLDER", tmp_path / "runlog")
    monkeypatch.setattr(mcp_server, "STATE", tmp_path / "runlog/mcp_state.json")
    monkeypatch.setattr(mcp_server, "EVALUATOR", tmp_path / "runlog/evaluator.json")
    return tmp_path


def _task(goal="three_component_pick", band=TARGET):
    frozen = taskmod.make_task(
        user_question="这段观测能不能用于指定频带的三分量拾取？",
        window_start="2023-03-01T11:34:43", window_end="2023-03-01T11:36:43",
        region=core.box(48.611, -122.805), target_band_hz=list(band),
        method_goal=goal)
    taskmod.freeze(frozen)
    return frozen


def test_a_constant_z_with_three_channels_is_not_vertical_input():
    """Review-4 A1 on the reviewer's own source waveform, in memory only.

    Zeroing the Z component of a real saved acquisition must make the vertical
    input condition FAIL through `quality_fields` and the final verdict, with
    the reason naming the constant Z channel — not "Z is present, so usable".
    The stored file itself is never modified.
    """
    if not (SOURCE_RUN / "mcp_state.json").exists():
        pytest.skip("reviewer source run not present")
    import numpy as np
    from obspy import read, read_inventory

    saved = json.loads((SOURCE_RUN / "mcp_state.json").read_text())
    observation = copy.deepcopy(saved["acquired"][0])
    raw = SOURCE_RUN / "raw" / observation["candidate_id"]
    wave_hash = hashlib.sha256((raw / "waveform.mseed").read_bytes()).hexdigest()
    stream = read(str(raw / "waveform.mseed"))
    for trace in stream.select(component="Z"):
        trace.data = np.zeros_like(trace.data)
    inventory = read_inventory(str(raw / "metadata.xml"))
    job = {"case_id": "review-4-a1", "id": observation["candidate_id"],
           "net": observation["network"], "sta": observation["station"],
           "cha": observation["family"], "start": observation["window_start"],
           "end": observation["window_end"]}
    fields = core.quality_fields(stream, inventory, job, list(TARGET))

    assert fields["channel_integrity_flags"], "a constant Z must be flagged"
    assert any("Z" in flag and "constant" in flag
               for flag in fields["channel_integrity_flags"])
    # A present-but-constant Z is NOT healthy vertical input.
    assert fields["vertical_ok"] is False
    assert fields["vertical_health"]["ok"] is False
    assert fields["vertical_health"]["problems"], "the problem must be named"
    assert fields["method_applicability"]["vertical_onset"] is False
    assert fields["method_applicability"]["three_component_pick"] is False

    record = {**copy.deepcopy(observation), **fields}
    record["target_band_hz"] = list(TARGET)
    verdict = core.verdict_for_question([record], "Q_usable", None, _task("vertical_onset"))
    assert verdict["verdict"] == "pick_not_applicable"
    assert verdict["reason"] == "input_conditions_not_met"
    # The limit names the concrete channel and problem, not a bare "missing".
    reason = verdict["answered"]["scope"]["stations_input_not_met"][0]["reason"]
    assert reason == "vertical_unhealthy"
    detail = verdict["answered"]["scope"]["stations_input_not_met"][0]["detail"]
    assert "UW.OLGA..ENZ" in detail
    assert "常值" in detail
    # The stored evidence file is untouched.
    after = hashlib.sha256((raw / "waveform.mseed").read_bytes()).hexdigest()
    assert after == wave_hash


def test_vertical_health_reason_names_the_channel_and_problem():
    """A synthetic constant-Z station: the aggregation layer says which Z and
    which problem, instead of collapsing it into 'no vertical component'."""
    record = {
        "candidate_id": "c1", "station": "BADZ", "network": "UW",
        "distance_km": 1.0, "status": "analysed", "onset_utc": 1000.0,
        "channels": ["UW.BADZ..HHZ", "UW.BADZ..HHN", "UW.BADZ..HHE"],
        "channel_integrity_flags": ["UW.BADZ..HHZ:constant"],
        "channel_integrity_ok": False, "sampling_rate_hz": 100.0,
        "three_component_ok": False, "vertical_ok": False,
        "vertical_health": {"ok": False,
                            "problems": {"UW.BADZ..HHZ": ["constant"]},
                            "problem_text": {"UW.BADZ..HHZ": ["常值"]}},
        "band_support": core.band_support(100.0, list(TARGET)),
        "method_applicability": {"three_component_pick": False,
                                 "vertical_onset": False},
        "target_band_hz": list(TARGET),
    }
    verdict = core.verdict_for_question([record], "Q_usable", None, _task("vertical_onset"))
    assert verdict["verdict"] == "pick_not_applicable"
    not_met = verdict["answered"]["scope"]["stations_input_not_met"][0]
    assert not_met["reason"] == "vertical_unhealthy"
    assert "UW.BADZ..HHZ" in not_met["detail"]
    assert "常值" in not_met["detail"]


# ------------------------------------------- §1.1 row 4: MCP exclude keeps goal
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


def test_mcp_exclude_keeps_the_frozen_vertical_goal(isolated):
    """Review-4 A2: the task is frozen `vertical_onset`; the badE station (bad E,
    healthy Z) and a clean station are acquired. Excluding the CLEAN station
    through the production MCP tool must recompute against the SAME goal the
    final validator uses, so the tool and the validator cannot disagree."""
    frozen = _task("vertical_onset")
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm="review4_a2")
    candidates = [_record("badE", three=False), _record("clean")]
    state = {"run_id": run_id, "task_id": frozen["task_id"],
             "user_question": frozen["user_question"],
             "window_start": frozen["window_start"],
             "window_end": frozen["window_end"], "region": frozen["region"],
             "target_band_hz": list(TARGET), "method_band_hz": list(core.METHOD_BAND),
             "method_goal": "vertical_onset",
             "allowed_actions": frozen["allowed_actions"],
             "discovery": {"n_eligible": 2, "eligible": candidates},
             "acquired": copy.deepcopy(candidates), "requests": 0,
             "http_attempts": 0, "logical_acquisitions": 0, "cache_hits": 0,
             "bytes": 0, "tool_attempts": {}, "exclusions": []}
    prepare_agent.write_state(folder, state)

    mcp_server.bind(run_id)
    result = mcp_server.exclude_observation("cand-clean", "review-4 A2 probe")

    # The tool's recompute used the FROZEN goal, not the default.
    answer = result["answer_after_recompute"]
    scope = (result.get("recomputed") or {})
    assert answer["field"] == "method_applicability"
    assert answer["answered"] is True
    assert answer["value"] is True, "vertical input on badE is met (healthy Z)"
    evidence = mcp_server.get_evidence()
    assert evidence["method_goal"] == "vertical_onset"
    # The snapshot layer and the final validator share the goal and the answer.
    verdict = core.verdict_for_question(
        [r for r in state["acquired"] if not r.get("excluded")],
        "Q_usable", None, frozen)
    assert verdict["verdict"] == "pick_applicable"
    assert (verdict.get("answered") or {}).get("value") is True
    assert verdict["answered"]["scope"]["goal"] == "vertical_onset"
    # The A5 counters persisted with the successful call.
    persisted = json.loads((folder / "mcp_state.json").read_text())
    assert persisted["tool_attempts"]["exclude_observation"]["ok"] == 1


# ------------------------------------------------- §1.2: carried re-judgement
def test_carried_band_dependent_evidence_is_rejudged(isolated, monkeypatch):
    """Review-4 A3: a 20 Hz station cannot carry 1–12 Hz (parent, unsupported).
    `prepare_followup` with the child frozen at 3–8 Hz must re-judge the carried
    record's band_support and method_applicability — the raw waveform is reused,
    the old judgement is not — so the final delivery stops saying
    `pick_not_applicable` on supported data."""
    parent_task = _task("three_component_pick", (1.0, 12.0))
    parent_id = runlog.new_run_id()
    folder = runlog.init_run(parent_id, parent_task, arm="review4_a3")
    slow = _record("slow", rate=20.0, three=True, target=(1.0, 12.0))
    assert slow["band_support"]["supported"] is False
    runlog.write_once(folder / "discovery.json", {
        "n_eligible": 1, "target_band_hz": [1.0, 12.0],
        "candidates": [slow], "eligible": [slow]})
    state = {"run_id": parent_id, "task_id": parent_task["task_id"],
             "user_question": parent_task["user_question"],
             "window_start": parent_task["window_start"],
             "window_end": parent_task["window_end"],
             "region": parent_task["region"], "target_band_hz": [1.0, 12.0],
             "method_band_hz": list(core.METHOD_BAND),
             "method_goal": "three_component_pick",
             "allowed_actions": parent_task["allowed_actions"],
             "discovery": {"n_eligible": 1, "eligible": [slow]},
             "acquired": [slow], "requests": 2, "http_attempts": 2,
             "logical_acquisitions": 1, "cache_hits": 0, "bytes": 999,
             "tool_attempts": {}, "exclusions": []}
    prepare_agent.write_state(folder, state)

    prepared = prepare_agent.prepare_followup(
        parent_id, "目标频带改为 3–8 Hz", target_band_hz=[3.0, 8.0])
    child_state = json.loads(
        (runlog.run_dir(prepared["run_id"]) / "mcp_state.json").read_text())
    carried = child_state["acquired"][0]

    assert carried["target_band_hz"] == [3.0, 8.0]
    assert carried["band_support"]["supported"] is True
    assert carried["band_support"]["target_band_hz"] == [3.0, 8.0]
    assert carried["method_applicability"]["three_component_pick"] is True
    assert child_state["rejudged_for_child_conditions"] == ["cand-slow"]
    # The parent's own record and files are untouched.
    parent_state = json.loads((folder / "mcp_state.json").read_text())
    assert parent_state["acquired"][0]["band_support"]["supported"] is False
    # The final delivery agrees with the re-judged evidence.
    child_task = json.loads(
        (runlog.run_dir(prepared["run_id"]) / "task.json").read_text())
    verdict = core.verdict_for_question([carried], "Q_usable", None, child_task)
    assert verdict["verdict"] == "pick_applicable"


def test_a_stale_condition_is_unknown_not_the_old_answer():
    """The guard behind A3: a record whose band judgement was computed under a
    DIFFERENT target band must read as unknown, never as its stale True/False."""
    stale = _record("old", rate=20.0, target=(1.0, 12.0))
    rows = core.method_applicability([stale], [3.0, 8.0], "three_component_pick")
    row = next(r for r in rows["per_station"] if r["station"] == "old")
    assert row["band_state"] == "unknown"
    assert row["input_state"] == "unknown"
    assert row["input_reason"] == "stale_condition"
    verdict = core.verdict_for_question([stale], "Q_usable", None, _task("three_component_pick", (3.0, 8.0)))
    assert verdict["verdict"] == "pick_unknown"
    assert verdict["answered"]["answered"] is False


# ------------------------------------------------------ §3.3 grid vs band text
def _state_for_text(records):
    return {"acquired": records, "records": records}


def test_free_text_naming_the_grid_is_not_read_as_a_target_band():
    """Review-4 B: '把到时网格收紧到 3–12' used to parse as a target-band change
    (any two numbers did). It must now map to the onset grid narrowing, and a
    sentence naming BOTH conditions asks the user to choose."""
    records = [_record("OLGA")]
    change = review.feedback_change(_state_for_text(records), None,
                                    "把到时网格收紧到 3–12")
    assert change["kind"] == "stability_bands"
    assert change["target_band_hz"] == [3.0, 12.0]
    assert change["bands_hz"] == [[3.0, 12.0]]
    # A pure band sentence stays a band change.
    change = review.feedback_change(_state_for_text(records), None,
                                    "目标频带改成 2–8")
    assert change["kind"] == "band"
    assert change["target_band_hz"] == [2.0, 8.0]
    # Naming both is ambiguous: ask instead of guessing.
    with pytest.raises(ValueError, match="两个独立的条件"):
        review.feedback_change(_state_for_text(records), None,
                               "目标频带改成 2–8，到时网格收紧到 3–12")
    # Bare numbers with no grid words still mean the target band.
    change = review.feedback_change(_state_for_text(records), None, "改成 2 8")
    assert change["kind"] == "band"


# --------------------------------------------- §2.1 real stdio MCP chain (A2/A5)
WINDOW_START = "2025-03-03T13:02:20.000000Z"
WINDOW_END = "2025-03-03T13:04:20.000000Z"


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def _seed(cache_dir: Path, url: str, payload: bytes) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(url)
    (cache_dir / f"{key}.bin").write_bytes(payload)
    (cache_dir / f"{key}.json").write_text(json.dumps(
        {"url": url, "file": f"{key}.bin",
         "sha256": hashlib.sha256(payload).hexdigest(),
         "bytes": len(payload)}))


def _waveform(station: str) -> bytes:
    import io
    import numpy as np
    from obspy import Stream, Trace, UTCDateTime
    rng = np.random.default_rng(11)
    stream = Stream()
    for component in ("Z", "N", "E"):
        trace = Trace(data=rng.normal(size=6000).astype(np.float64))
        trace.stats.network, trace.stats.station = "UW", station
        trace.stats.location = ""
        trace.stats.channel = f"HH{component}"
        trace.stats.sampling_rate = 50.0
        trace.stats.starttime = UTCDateTime(WINDOW_START)
        trace.data[2000:2050] += 25.0 * np.sin(np.linspace(0, 20, 50))
        stream.append(trace)
    buffer = io.BytesIO()
    stream.write(buffer, format="MSEED")
    return buffer.getvalue()


def _metadata(station: str) -> bytes:
    import io
    from obspy import UTCDateTime
    from obspy.core.inventory import Channel, Inventory, Network, Station
    from obspy.core.inventory.response import Response
    channels = [
        Channel(f"HH{c}", " ", 47.0, -122.0, elevation=0.0, depth=0.0,
                azimuth=az, dip=-90.0 if c == "Z" else 0.0, sample_rate=50.0,
                start_date=UTCDateTime("2020-01-01"), response=Response())
        for c, az in (("Z", 0.0), ("N", 0.0), ("E", 90.0))]
    network = Network("UW", stations=[Station(station, 47.0, -122.0, 0.0,
                                              channels=channels)])
    buffer = io.BytesIO()
    Inventory(networks=[network]).write(buffer, format="STATIONXML")
    return buffer.getvalue()


@pytest.fixture
def stdio_env(tmp_path, monkeypatch):
    runlog_dir = tmp_path / "runlog"
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", runlog_dir)
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", runlog_dir)
    monkeypatch.setattr(core, "CACHE", cache_dir)
    core.fdsn.set_cache(cache_dir)

    frozen = taskmod.make_task(
        user_question="只用垂直分量判断：这段观测能不能用于 1-12 Hz 的到时检查？",
        window_start=WINDOW_START, window_end=WINDOW_END,
        region={"center_lat": 47.0, "center_lon": -122.0, "minlatitude": 46.4,
                "maxlatitude": 47.6, "minlongitude": -122.6, "maxlongitude": -121.4},
        target_band_hz=[1.0, 12.0], method_goal="vertical_onset")
    taskmod.freeze(frozen)
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm="review4_stdio")
    candidates = [
        {"candidate_id": "cand-inj", "network": "UW", "station": "TEST",
         "distance_km": 1.0, "family": "HH", "analysis_eligible": True,
         "three_component": True, "vertical_only": False,
         "band_support": {"supported": True}, "reason": "injected bad-E station"},
        {"candidate_id": "cand-clean", "network": "UW", "station": "TEST2",
         "distance_km": 2.0, "family": "HH", "analysis_eligible": True,
         "three_component": True, "vertical_only": False,
         "band_support": {"supported": True}, "reason": "clean station"}]
    state = {"run_id": run_id, "task_id": frozen["task_id"],
             "user_question": frozen["user_question"],
             "window_start": WINDOW_START, "window_end": WINDOW_END,
             "region": frozen["region"], "target_band_hz": [1.0, 12.0],
             "method_band_hz": list(core.METHOD_BAND),
             "method_goal": "vertical_onset",
             "allowed_actions": frozen["allowed_actions"],
             "discovery": {"n_eligible": 2, "eligible": candidates},
             "acquired": [], "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
             "tool_attempts": {}, "exclusions": []}
    prepare_agent.write_state(folder, state)
    (folder / "mcp_state.initial.json").write_text(json.dumps(state))
    (folder / "evaluator.json").write_text(json.dumps(
        {"task_id": frozen["task_id"], "demo_case_ref": None, "acceptance": None,
         "kind": None, "split": None, "inject_first": True}))

    spec = {"start": WINDOW_START, "end": WINDOW_END}
    for station in ("TEST", "TEST2"):
        requests = fdsn.build_requests(
            {"network": "UW", "station": station,
             "latitude": None, "longitude": None}, spec, "HH")
        _seed(cache_dir, requests["waveform_url"], _waveform(station))
        _seed(cache_dir, requests["metadata_url"], _metadata(station))

    import os
    env = {**os.environ,
           "RUNLOG_DIR": str(runlog_dir), "EQ_CACHE_DIR": str(cache_dir),
           "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
           "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}
    monkeypatch.setenv("RUNLOG_DIR", str(runlog_dir))
    monkeypatch.setenv("EQ_CACHE_DIR", str(cache_dir))
    return {"run_id": run_id, "folder": folder, "env": env,
            "frozen": frozen, "cache": cache_dir}


def _stdio_call(run_id, env, calls):
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def drive():
        params = StdioServerParameters(
            command=str(ROOT / ".venv/bin/python"),
            args=[str(ROOT / "main/mcp_server.py"), run_id], env=env)
        out = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for tool, arguments in calls:
                    result = await session.call_tool(tool, arguments)
                    structured = getattr(result, "structured_content", None)
                    if structured is None:
                        structured = json.loads(
                            "".join(getattr(b, "text", "") for b in result.content))
                    out.append(structured.get("result", structured))
        return out

    return asyncio.run(drive())


def test_stdio_chain_keeps_goal_counts_and_consistency(stdio_env):
    """§2.1 through the real stdio server: list, acquire x2 (the first derives
    the disclosed bad-E copy), onset, stability, exclude, evidence. The
    recompute inside exclude uses the frozen `vertical_onset` goal, the
    persisted ok counts equal the JSONL events, and a state re-read from disk
    (a restart) still shows them."""
    run_id = stdio_env["run_id"]
    folder = stdio_env["folder"]
    out = _stdio_call(run_id, stdio_env["env"], [
        ("list_candidates", {}),
        ("acquire_station", {"candidate_id": "cand-inj"}),
        ("acquire_station", {"candidate_id": "cand-clean"}),
        ("run_onset_check", {"candidate_id": "cand-inj"}),
        ("run_stability_check", {"candidate_id": "cand-inj"}),
        ("exclude_observation", {"candidate_id": "cand-clean",
                                  "reason": "排除另一干净台站"}),
        ("get_evidence", {}),
    ])
    # The injected station has a bad E but a healthy Z: vertical input met.
    acquired = out[1]
    assert acquired["channel_integrity_flags"], "injection derived the flat E"
    assert acquired["vertical_ok"] is True
    excluded = out[5]
    answer = excluded["answer_after_recompute"]
    assert answer["field"] == "method_applicability"
    assert answer["value"] is True
    scope = answer.get("scope") or {}
    assert scope.get("goal") == "vertical_onset"

    # The persisted counters equal the JSONL events, and survive a re-read.
    state = json.loads((folder / "mcp_state.json").read_text())
    events = [json.loads(line) for line in
              (folder / "tool_calls.jsonl").read_text().splitlines() if line.strip()]
    for tool, counts in (state.get("tool_attempts") or {}).items():
        for phase in ("start", "ok", "error"):
            jsonl = sum(1 for e in events if e["tool"] == tool
                        and e["phase"] == phase)
            assert counts.get(phase, 0) == jsonl, (tool, phase)
    expected_ok = {"acquire_station": 2, "run_onset_check": 1,
                   "run_stability_check": 1, "exclude_observation": 1,
                   "get_evidence": 1, "list_candidates": 1}
    for tool, ok in expected_ok.items():
        assert state["tool_attempts"][tool]["ok"] == ok, tool
    # The final validator and the tool's recompute share the goal and answer.
    frozen = stdio_env["frozen"]
    records = [r for r in state["acquired"] if not r.get("excluded")]
    verdict = core.verdict_for_question(records, "Q_usable", None, frozen)
    assert verdict["verdict"] == "pick_applicable"
    assert verdict["answered"]["scope"]["goal"] == "vertical_onset"


# ---------------------------------------------------- B1: feedback change table
def _followup_parent(records, isolated, band=(1.0, 12.0)):
    frozen = _task("three_component_pick", band)
    run_id = runlog.new_run_id()
    folder = runlog.init_run(run_id, frozen, arm="review4_b1")
    state = {"run_id": run_id, "task_id": frozen["task_id"],
             "user_question": frozen["user_question"],
             "window_start": frozen["window_start"],
             "window_end": frozen["window_end"], "region": frozen["region"],
             "target_band_hz": list(band), "method_band_hz": list(core.METHOD_BAND),
             "method_goal": "three_component_pick",
             "allowed_actions": frozen["allowed_actions"],
             "discovery": {"n_eligible": len(records), "eligible": records},
             "acquired": copy.deepcopy(records), "requests": 2,
             "http_attempts": 2, "logical_acquisitions": len(records),
             "cache_hits": 0, "bytes": 999, "tool_attempts": {}, "exclusions": []}
    prepare_agent.write_state(folder, state)
    runlog.write_once(folder / "discovery.json", {
        "n_eligible": len(records), "target_band_hz": list(band),
        "candidates": records, "eligible": records})
    return run_id, state


def test_narrow_grid_round_marks_excluded_stations_in_the_change_table(isolated):
    """Review-4 B1: after OLGA is excluded, the narrow-grid round's per-station
    change table must mark OLGA's row as excluded, and the run's own summary
    (verdict eligible stations) must not count it as participating."""
    olga = _record("OLGA", rate=100.0)
    other = _record("KEEP", rate=100.0)
    parent_id, parent_state = _followup_parent([olga, other], isolated)

    excluded = review.run_followup(parent_id, change={"kind": "exclude",
                                                      "station": "OLGA"})
    assert excluded["verdict"] in ("pick_applicable", "pick_partially_applicable")
    child_id = excluded["run_id"]

    narrowed = review.run_followup(child_id, change={"kind": "stability_bands",
                                                     "target_band_hz": [3.0, 12.0]})
    detail = narrowed["feedback"]["detail"]
    rows = {row["station"]: row for row in detail["stations"]}
    # OLGA's measured evidence is still recomputed and still visible…
    assert set(rows) == {"OLGA", "KEEP"}
    # …but it is MARKED, so the frontend can keep it out of the current table.
    assert rows["OLGA"]["excluded"] is True
    assert rows["KEEP"]["excluded"] is False
    # The public station list carries the same marking contract.
    stations = {s["station"]: s for s in narrowed["stations"]}
    assert stations["OLGA"]["excluded"] is True
    assert stations["OLGA"]["exclusion_reason"]
    assert stations["KEEP"].get("excluded") is None or \
        stations["KEEP"]["excluded"] is False
    # The verdict itself never counted the excluded station.
    child_records = json.loads(
        (runlog.run_dir(child_id) / "mcp_state.json").read_text())["acquired"]
    assert sorted(r["station"] for r in child_records
                  if r.get("excluded")) == ["OLGA"]


def test_restoring_a_station_keeps_the_narrowed_grid(isolated):
    """Guide §3.1: a final run that only restores OLGA must NOT reset the grid
    narrowed in the previous round."""
    olga = _record("OLGA", rate=100.0)
    other = _record("KEEP", rate=100.0)
    parent_id, _ = _followup_parent([olga, other], isolated)
    child1 = review.run_followup(parent_id, change={"kind": "exclude",
                                                    "station": "OLGA"})
    child2 = review.run_followup(child1["run_id"],
                                 change={"kind": "stability_bands",
                                         "target_band_hz": [3.0, 12.0]})
    child3 = review.run_followup(child2["run_id"], change={"kind": "include",
                                                           "station": "OLGA"})
    state3 = json.loads(
        (runlog.run_dir(child3["run_id"]) / "mcp_state.json").read_text())
    # OLGA is back in the current set…
    assert sorted(r["station"] for r in state3["acquired"]
                  if not r.get("excluded")) == ["KEEP", "OLGA"]
    # …and every carried record (including OLGA) still evaluates on the narrowed
    # grid, not the full declared one.
    for record in state3["acquired"]:
        assert record["stability_bands_hz"] == [[3.0, 12.0]]
    # The chain of parents is intact.
    assert child3["parent_run_id"] == child2["run_id"]
    assert child2["parent_run_id"] == child1["run_id"]
