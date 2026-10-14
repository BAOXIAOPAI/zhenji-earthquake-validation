"""Gate B convergence regressions (review-3, 2026-10-10).

Pins the diagnostics rewrite (P1-3/P1-4) and the feedback-continuation contract
(P1-1) on saved/synthetic data. No network, no model call.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import diagnostics  # noqa: E402
import prepare_agent  # noqa: E402
import review  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

# The real tool server, loaded by path. `main/core.py` puts `prototype/` ahead of
# `main/` on sys.path and prototype ships its own `mcp_server.py`, so a plain
# `import mcp_server` here would silently exercise the wrong module.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("eq_main_mcp_server",
                                              str(ROOT / "main/mcp_server.py"))
mcp_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_server)

FS = 100.0


def _trace(component, data, rate=FS, start=0.0, station="TEST"):
    from obspy import Trace, UTCDateTime
    return Trace(np.asarray(data, dtype=float), header={
        "station": station, "channel": "HH" + component,
        "sampling_rate": rate, "starttime": UTCDateTime(start)})


# ------------------------------------------------ 3.1 honest cluster wording
def test_majority_cluster_with_one_outlier_is_not_called_no_majority():
    """TURTL's real shape: 12 of 13 configurations in one cluster, one outlier.
    The old wording said "没有形成多数一致的簇", which is false."""
    valid = [50.0 + 0.01 * i for i in range(12)] + [59.44]
    clusters = diagnostics._cluster(valid)
    kind, statement, step = diagnostics.classify_spread(
        valid, clusters, max(valid) - min(valid))
    assert kind == "unstable"
    assert "没有形成多数一致的簇" not in statement
    assert "12" in statement and "多数" in statement and "离群" in statement
    assert "主要簇" in step


def test_two_separated_groups_stay_bimodal():
    valid = [40.0, 40.1, 40.2, 41.0, 90.0, 90.1, 90.2, 91.0]
    clusters = diagnostics._cluster(valid)
    kind, statement, _ = diagnostics.classify_spread(valid, clusters, 51.0)
    assert kind == "unstable_bimodal"
    assert "簇" in statement


def test_a_genuinely_scattered_set_still_says_no_majority():
    valid = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    clusters = diagnostics._cluster(valid)
    kind, statement, _ = diagnostics.classify_spread(valid, clusters, 50.0)
    assert kind == "unstable"
    assert "没有形成多数一致的簇" in statement


# ------------------------------------------------ 3.2 execution disclosure
def _noise(n, seed):
    return np.random.RandomState(seed).randn(n)


def test_each_algorithm_discloses_its_own_bands_and_counts():
    from obspy import Stream
    n = int(FS * 60)
    stream = Stream([_trace("Z", _noise(n, 0)),
                     _trace("N", _noise(n, 1)),
                     _trace("E", _noise(n, 2))])
    result = diagnostics.stability(stream, bands_hz=[[3.0, 12.0]])
    detectors = result["detectors"]
    # The narrowed grid is 4 STA/LTA configs at 3-12 Hz ...
    assert detectors["stalta_z"]["bands_hz"] == [[3.0, 12.0]]
    assert detectors["stalta_z"]["n_configurations"] == 4
    # ... plus AR-AIC, which keeps its own 1-20 Hz band and must say so.
    assert detectors["ar_aic_3c"]["bands_hz"] == [[1.0, 20.0]]
    assert detectors["ar_aic_3c"]["n_configurations"] == 1
    assert result["bands_evaluated"] == [[3.0, 12.0]]
    assert result["full_grid"] is False


def test_every_onset_carries_a_reference_and_a_unit():
    from obspy import Stream
    n = int(FS * 60)
    result = diagnostics.stability(Stream([_trace("Z", _noise(n, 0))]))
    assert result["onset_reference"]
    assert "s" in result["onset_reference"]


def test_misaligned_components_refuse_the_three_component_pick():
    """Changing one component's time base must give a concrete refusal, not an
    estimate outside the window (the review's P1-4 probe)."""
    from obspy import Stream
    n = int(FS * 60)
    stream = Stream([_trace("Z", _noise(n, 0), rate=100.0),
                     _trace("N", _noise(n, 1), rate=50.0),
                     _trace("E", _noise(n, 2), rate=100.0)])
    result = diagnostics.stability(stream)
    assert result["alignment"]["aligned"] is False
    assert result["alignment"]["reason"] == "sampling_rate_mismatch"
    aic = [e for e in result["estimates"] if e["detector"] == "ar_aic_3c"][0]
    assert aic["status"] == "not_applicable"
    assert aic["reason"] == "component_alignment_mismatch"
    # The vertical detector is unaffected and still runs.
    assert result["detectors"]["stalta_z"]["n_configurations"] == 12


def test_a_shifted_start_time_is_also_refused():
    from obspy import Stream
    n = int(FS * 60)
    stream = Stream([_trace("Z", _noise(n, 0), start=0.0),
                     _trace("N", _noise(n, 1), start=1.0),
                     _trace("E", _noise(n, 2), start=0.0)])
    result = diagnostics.stability(stream)
    assert result["alignment"]["aligned"] is False
    assert result["alignment"]["reason"] == "start_time_mismatch"


# ------------------------------------------------ 2.1 feedback continuation
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    monkeypatch.setattr(runlog, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(core, "RUNLOG", tmp_path / "runlog")
    return tmp_path


def _record(station, candidate_id=None, excluded=False):
    cid = candidate_id or ("cand-" + station)
    return {"candidate_id": cid, "station": station, "network": "UW",
            "distance_km": 10.0, "status": "analysed", "onset_utc": 1000.0,
            "channels": ["HHZ", "HHN", "HHE"], "channel_integrity_flags": [],
            "channel_integrity_ok": True, "sampling_rate_hz": 100.0,
            "three_component_ok": True, "vertical_ok": True,
            "band_support": core.band_support(100.0, [1.0, 12.0]),
            "excluded": excluded, "derivation": None,
            "method_applicability": {"three_component_pick": True,
                                     "vertical_onset": True},
            "stability": {"stability": "unstable", "spread_s": 9.44}}


def _parent(isolated, band=(1.0, 12.0)):
    frozen = taskmod.make_task(
        user_question="这个时窗能不能做三分量拾取？",
        window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
        region=core.box(48.611, -122.805), target_band_hz=list(band),
        method_goal="three_component_pick")
    taskmod.freeze(frozen)
    parent_id = runlog.new_run_id()
    folder = runlog.init_run(parent_id, frozen, arm="full")
    records = [_record("OLGA"), _record("MCW"), _record("ORCA")]
    folder.joinpath("discovery.json").write_text(json.dumps({
        "n_eligible": 3, "target_band_hz": list(band),
        "candidates": [], "eligible": []}, ensure_ascii=False))
    folder.joinpath("mcp_state.json").write_text(json.dumps(
        prepare_agent.execution_state(parent_id, frozen,
                                      {"n_eligible": 3, "eligible": []},
                                      acquired=records),
        ensure_ascii=False))
    return parent_id, frozen


def test_conditions_block_reports_the_effective_parent_and_band(isolated):
    parent_id, frozen = _parent(isolated)
    records = [_record("OLGA", excluded=True), _record("MCW")]
    cond = review._conditions(frozen, records, parent_id)
    assert cond["parent_run_id"] == parent_id
    assert cond["target_band_hz"] == [1.0, 12.0]
    assert cond["method_goal"] == "three_component_pick"
    assert cond["excluded_stations"] == ["OLGA"]
    assert cond["included_stations"] == ["MCW"]


def test_a_followup_result_names_its_parent_and_inherited_conditions(isolated):
    """The interface reads this to advance the chain instead of silently
    re-deriving from the first run (P1-1)."""
    parent_id, _ = _parent(isolated)
    result = review.run_followup(
        parent_id, change={"kind": "exclude", "candidate_id": "cand-OLGA"},
        instruction="排除 OLGA")
    assert result["parent_run_id"] == parent_id
    assert result["feedback"]["reused_observations"] == 3
    assert result["feedback"]["new_requests"] == 0
    assert result["feedback"]["model_calls"] == 0
    cond = result["conditions"]
    assert cond["parent_run_id"] == parent_id
    assert cond["excluded_stations"] == ["OLGA"]
    assert "MCW" in cond["included_stations"]
    # The child keeps the full execution spec for the next round.
    state = json.loads((runlog.run_dir(result["run_id"])
                        / "mcp_state.json").read_text())
    assert state["target_band_hz"] == [1.0, 12.0]
    assert state["parent_run_id"] == parent_id
    assert state["discovery"]


def test_chaining_a_second_feedback_off_the_child_keeps_both_conditions(isolated):
    parent_id, _ = _parent(isolated)
    first = review.run_followup(
        parent_id, change={"kind": "exclude", "candidate_id": "cand-OLGA"},
        instruction="排除 OLGA")
    # The second round continues from the CHILD, not from the initial run.
    second = review.run_followup(
        first["run_id"], change={"kind": "band", "target_band_hz": [3.0, 8.0]},
        instruction="目标频带改为 3-8 Hz")
    assert second["parent_run_id"] == first["run_id"]
    cond = second["conditions"]
    assert cond["target_band_hz"] == [3.0, 8.0]
    assert cond["excluded_stations"] == ["OLGA"], "the exclusion was not inherited"
    assert second["feedback"]["reused_observations"] == 3
    # The first run's own record is untouched.
    first_state = json.loads((runlog.run_dir(first["run_id"])
                              / "mcp_state.json").read_text())
    assert first_state["target_band_hz"] == [1.0, 12.0]


def _exclusions(run_id):
    state = json.loads((runlog.run_dir(run_id) / "mcp_state.json").read_text())
    return [e["candidate_id"] for e in (state.get("exclusions") or [])]


def test_the_persisted_state_says_which_observations_were_taken_out(isolated):
    """`exclusions` used to be written as None on every recompute path, so the
    state could not say what this round had removed or why."""
    parent_id, _ = _parent(isolated)
    assert _exclusions(parent_id) == []          # the root starts as a real list
    first = review.run_followup(
        parent_id, change={"kind": "exclude", "candidate_id": "cand-OLGA"},
        instruction="排除 OLGA")
    assert _exclusions(first["run_id"]) == ["cand-OLGA"]
    # The child's *initial* snapshot is the inherited baseline, before this round's
    # own change; the live state is the one after it.
    second = review.run_followup(
        first["run_id"], change={"kind": "band", "target_band_hz": [3.0, 8.0]},
        instruction="目标频带改为 3-8 Hz")
    assert _exclusions(second["run_id"]) == ["cand-OLGA"]
    third = review.run_followup(
        second["run_id"], change={"kind": "include", "candidate_id": "cand-OLGA"},
        instruction="恢复 OLGA")
    assert _exclusions(third["run_id"]) == []
    assert third["conditions"]["excluded_stations"] == []

def test_the_interface_statement_has_a_chinese_counterpart():
    """Review 3.3: the main result must read in Chinese without weakening the frozen
    rule wording. The English `statement` stays byte-identical (the rule and the
    regression tests read it); `statement_zh` is what the interface shows."""
    records = [_record(s) for s in ("OLGA", "MCW", "ORCA", "TURTL")]
    frozen = taskmod.make_task(
        user_question="这个时窗能不能做三分量拾取？",
        window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
        region=core.box(48.611, -122.805), target_band_hz=[1.0, 12.0],
        method_goal="three_component_pick")
    verdict = core.verdict_for_question(records, "Q_usable", None, frozen)
    assert verdict["statement_zh"] != verdict["statement"]
    assert "Method applicability was judged" in verdict["statement"]
    assert "三分量 P/S 拾取" in verdict["statement_zh"]
    assert "不是一个" not in verdict["statement_zh"]


def test_every_frozen_verdict_carries_its_chinese_counterpart():
    """No branch may render an English-only conclusion boundary."""
    frozen = taskmod.make_task(
        user_question="这个时窗能不能做三分量拾取？",
        window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
        region=core.box(48.611, -122.805), target_band_hz=[1.0, 12.0],
        method_goal="three_component_pick")
    ghosts = [
        [],                                             # no observations
        [dict(_record("OLGA"), status="failed")],       # nothing analysable
        [_record("OLGA"), _record("MCW")],              # fewer than 3 stations
        [_record(s) for s in ("OLGA", "MCW", "ORCA")],  # coherent
    ]
    for records in ghosts:
        verdict = core.verdict_for_question(records, "Q_propagating", None, frozen)
        assert verdict.get("statement"), records
        assert verdict.get("statement_zh"), verdict["verdict"]
        assert verdict["statement_zh"] != verdict["statement"]


def test_the_agh_followup_state_carries_what_the_driver_reads(isolated):
    """`main/agent_run.mjs` reads these fields before it can start an agent turn.
    A missing one spends a model call and then fails, so it is checked for free."""
    parent_id, _ = _parent(isolated)
    child = prepare_agent.prepare_followup(
        parent_id, "排除台站 OLGA，允许补取同范围内其他台站",
        arm="adaptive_agent_open_followup")
    folder = runlog.run_dir(child["run_id"])
    state = json.loads((folder / "mcp_state.json").read_text())
    assert child["parent_run_id"] == parent_id
    assert state["parent_run_id"] == parent_id
    # The driver maps `state.discovery.eligible` to the bridge's allowed candidates.
    assert "eligible" in state["discovery"] and "n_eligible" in state["discovery"]
    assert (folder / "discovery.json").exists()
    # A follow-up must still be allowed to fill the gap the exclusion opened.
    assert state["allowed_actions"] == taskmod.DEFAULT_ALLOWED
    assert {"discover", "acquire"} <= set(state["allowed_actions"])
    assert state["target_band_hz"] == [1.0, 12.0]
    assert state["method_goal"] == "three_component_pick"
    assert state["window_start"] and state["window_end"]
    assert state["user_question"]
    # O1: the user's explicit exclusion is a constraint the application layer must
    # honour, not a hope that the model edits a field. It is applied to the child's
    # OWN observation copies before the state is written, and the application is
    # recorded with its actor so it is never mistaken for an agent decision.
    assert state["acquired"]
    assert any(r.get("excluded") for r in state["acquired"])
    assert [e["candidate_id"] for e in state["exclusions"]] == ["cand-OLGA"]
    assert state["condition_applications"][-1]["actor"] == "deterministic_guard"


# --------------------------------------------------------------------------- MCP
# The §2.2 paid AGH run surfaced this: the follow-up state carried `exclusions: null`,
# `setdefault` does not replace an existing None, and the tool raised AttributeError
# *after* it had already reassigned `acquired`. The model was told the exclusion
# failed while the exclusion had in fact taken effect.

def _mcp_state(tmp_path, monkeypatch, exclusions):
    """A frozen state in exactly the shape prepare_followup used to write."""
    folder = tmp_path / "runlog" / "r-child-mcp"
    folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runlog")
    frozen = taskmod.make_task(
        user_question="这个时窗能不能做三分量拾取？",
        window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
        region=core.box(48.611, -122.805), target_band_hz=[1.0, 12.0],
        method_goal="three_component_pick")
    state = prepare_agent.execution_state(
        "r-child-mcp", frozen, {"n_eligible": 3, "eligible": []},
        acquired=[_record("OLGA"), _record("MCW"), _record("ORCA")],
        parent_run_id="r-parent-mcp", extra={"exclusions": exclusions})
    (folder / "mcp_state.json").write_text(json.dumps(state, ensure_ascii=False))
    # Bind only once the state exists: `bind` refuses a run with no state, on purpose.
    mcp_server.bind("r-child-mcp")
    return folder, state


def test_exclude_observation_works_when_the_state_carries_a_null_exclusion_list(
        tmp_path, monkeypatch):
    folder, _ = _mcp_state(tmp_path, monkeypatch, exclusions=None)
    result = mcp_server.exclude_observation("cand-OLGA", "用户要求排除")
    assert result["status"] == "excluded"
    state = json.loads((folder / "mcp_state.json").read_text())
    assert [e["candidate_id"] for e in state["exclusions"]] == ["cand-OLGA"]
    assert state["exclusions"][0]["station"] == "OLGA"
    assert not state.get("tool_errors"), "a successful tool call must record no error"
    assert next(r for r in state["acquired"]
                if r["candidate_id"] == "cand-OLGA")["excluded"] is True


def test_a_tool_failure_never_leaves_the_exclusion_half_applied(tmp_path, monkeypatch):
    """The report and the state must agree: either both changed or neither did."""
    folder, _ = _mcp_state(tmp_path, monkeypatch, exclusions=None)
    calls = {"n": 0}
    real_snapshot = mcp_server._snapshot

    def explode(records, task, question_id):
        calls["n"] += 1
        if calls["n"] == 2:          # the "after" snapshot
            raise RuntimeError("synthetic failure after the records were re-derived")
        return real_snapshot(records, task, question_id)

    monkeypatch.setattr(mcp_server, "_snapshot", explode)
    with pytest.raises(RuntimeError):
        mcp_server.exclude_observation("cand-OLGA", "用户要求排除")
    state = json.loads((folder / "mcp_state.json").read_text())
    assert not [r for r in state["acquired"] if r.get("excluded")], \
        "the exclusion showed as applied even though the tool reported an error"
    assert not state.get("exclusions")
    assert state.get("tool_errors"), "the failure must still be recorded"


def test_excluding_an_observation_that_is_not_acquired_is_refused(tmp_path, monkeypatch):
    _mcp_state(tmp_path, monkeypatch, exclusions=[])
    with pytest.raises(ValueError):
        mcp_server.exclude_observation("cand-NOPE", "not acquired")


def test_the_followup_state_never_writes_a_null_exclusion_list(isolated):
    """The root cause: the follow-up writer emitted null, so fix it at the source.

    O1 补充：不仅不能是 null，本轮明确要求的排除必须已经落到子记录的副本上，
    并带 deterministic_guard 的应用事件。
    """
    parent_id, _ = _parent(isolated)
    parent_state = runlog.run_dir(parent_id) / "mcp_state.json"
    task = json.loads(parent_state.read_text())
    task.pop("exclusions", None)                 # an older parent without the key
    parent_state.write_text(json.dumps(task, ensure_ascii=False))
    child = prepare_agent.prepare_followup(parent_id, "排除 OLGA",
                                          arm="adaptive_agent_open_followup")
    for name in ("mcp_state.json", "mcp_state.initial.json"):
        state = json.loads((runlog.run_dir(child["run_id"]) / name).read_text())
        assert isinstance(state["exclusions"], list), name
        assert [e["candidate_id"] for e in state["exclusions"]] == ["cand-OLGA"], name
        assert any(r.get("excluded") for r in state["acquired"]), name
        applied = state["condition_applications"][-1]
        assert applied["actor"] == "deterministic_guard", name
        assert applied["before"]["condition_id"] != applied["after"]["condition_id"], name
