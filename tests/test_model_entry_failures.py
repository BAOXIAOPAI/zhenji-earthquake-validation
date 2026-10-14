"""Regressions for the web model entry; no network or paid model calls."""
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core
import fdsn
import prepare_agent
import review
import runlog
import server
import task as taskmod
import task_input


def channel_row(network, station):
    return (f"{network}|{station}||HHZ|48.1|-122.8|100|0|0|-90|sensor|1|1|M/S|100|"
            "2020-01-01T00:00:00|")


def test_qualified_station_uses_separate_fdsn_parameters(monkeypatch):
    called = []

    def fake_get(url, timeout):
        called.append(url)
        return channel_row("UW", "CHIMA").encode(), False, "fixture"

    monkeypatch.setattr(fdsn, "cached_get", fake_get)
    rows, _ = fdsn.station_query({"start": "2023-12-24T10:00:00",
                                  "end": "2023-12-24T10:02:00",
                                  "stations": ["UW.CHIMA"]})
    params = parse_qs(urlparse(called[0]).query)
    assert params["net"] == ["UW"]
    assert params["sta"] == ["CHIMA"]
    assert [(r["network"], r["station"]) for r in rows] == [("UW", "CHIMA")]


def test_qualified_station_pairs_do_not_expand_to_a_cross_product(monkeypatch):
    payload = "\n".join(channel_row(n, s) for n in ("UW", "TA") for s in ("ONE", "TWO"))
    monkeypatch.setattr(fdsn, "cached_get", lambda *a: (payload.encode(), False, "fixture"))
    rows, _ = fdsn.station_query({"start": "2023-12-24T10:00:00",
                                  "end": "2023-12-24T10:02:00",
                                  "stations": ["UW.ONE", "TA.TWO"]})
    assert [(r["network"], r["station"]) for r in rows] == [("UW", "ONE"), ("TA", "TWO")]


def test_qualified_station_resolves_only_the_named_network():
    scope = prepare_agent.resolve_scope(
        {"region": {"kind": "stations", "stations": ["UW.CHIMA"]}},
        {"eligible": [{"candidate_id": "right", "network": "UW", "station": "CHIMA"},
                      {"candidate_id": "wrong", "network": "TA", "station": "CHIMA"}]})
    assert scope["candidate_ids"] == ["right"]
    assert scope["unmatched_stations"] == []


def test_station_query_preserves_explicit_network_constraint(monkeypatch):
    called = []

    def fake_get(url, timeout):
        called.append(parse_qs(urlparse(url).query))
        return channel_row("TA", "ONE").encode(), False, "fixture"

    monkeypatch.setattr(fdsn, "cached_get", fake_get)
    rows, _ = fdsn.station_query({"start": "2023-12-24T10:00:00",
                                  "end": "2023-12-24T10:02:00",
                                  "stations": ["UW.ONE"], "networks": ["TA"]})
    assert called[0]["net"] == ["TA"]
    assert rows == []  # A conflicting network must not silently widen the scope.


def test_mixed_station_codes_leave_unqualified_stations_open_to_all_networks(monkeypatch):
    called = []

    def fake_get(url, timeout):
        called.append(parse_qs(urlparse(url).query))
        payload = "\n".join(channel_row(n, s) for n in ("UW", "TA") for s in ("ONE", "TWO"))
        return payload.encode(), False, "fixture"

    monkeypatch.setattr(fdsn, "cached_get", fake_get)
    rows, _ = fdsn.station_query({"start": "2023-12-24T10:00:00",
                                  "end": "2023-12-24T10:02:00",
                                  "stations": ["UW.ONE", "TWO"]})
    assert "net" not in called[0]
    assert {(r["network"], r["station"]) for r in rows} == {
        ("UW", "ONE"), ("UW", "TWO"), ("TA", "TWO")}


def test_discovery_failure_is_saved_and_returned_before_model_start(tmp_path, monkeypatch):
    monkeypatch.setattr(runlog, "RUNLOG", tmp_path / "runs")
    monkeypatch.setattr(core, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    parsed = task_input.build({"window_start": "2023-12-24T10:00:00Z",
                               "window_end": "2023-12-24T10:02:00Z",
                               "stations": ["UW.CHIMA"],
                               "question": "这段观测能不能用来做 P/S 拾取？"})
    frozen = taskmod.task_from_spec(parsed["structured"])
    taskmod.freeze(frozen)

    def no_data(*args):
        raise HTTPError("https://service.earthscope.org/fixture", 404, "No data", {}, None)

    monkeypatch.setattr(core, "discover", no_data)
    import run_agent_arm
    monkeypatch.setattr(run_agent_arm, "_execute", lambda *a, **k: pytest.fail("model started"))
    events = []
    result = review.run_review("user-fixture", frozen["user_question"], "agent",
                               user_task=frozen, on_step=events.append)
    assert result["status"] == "failed"
    assert result["failure_kind"] == "discovery_failure"
    assert result["model_started"] is False
    assert result["model_cost_cny"] == 0
    assert "台站代码与时窗" in result["statement_zh"]
    folder = runlog.run_dir(result["run_id"])
    assert runlog.read_outcome(result["run_id"])["failure_kind"] == "discovery_failure"
    assert json.loads((folder / "errors.json").read_text())["failures"][0]["error_type"] == "HTTPError"
    assert json.loads((folder / "cost.json").read_text())["model_cost_upper_cny"] == 0
    assert events[-1]["phase"] == "error"


@pytest.fixture
def ready_runtime(tmp_path, monkeypatch):
    root = tmp_path / "project"
    for path in (root / ".venv/bin/python",
                 root / "vendor/agnes-harness/node_modules/.bin/tsx",
                 root / "vendor/agnes-harness/packages/cli/dist/local/agnes.mjs"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "deepseek_model.txt").write_text("fixture credential placeholder")
    monkeypatch.setattr(server, "ROOT", root)
    monkeypatch.setattr(server.shutil, "which", lambda _: "/fixture/node")
    monkeypatch.delenv("EQ_STRICT_OFFLINE", raising=False)
    cfg = {"input_token_reservation": 100, "input_million_peak": 2,
           "max_output_tokens": 100, "output_million_peak": 8, "total_limit": 20}
    return root, cfg


def test_local_preflight_refuses_missing_key_without_reading_credentials(ready_runtime):
    root, cfg = ready_runtime
    (root.parent / "deepseek_model.txt").unlink()
    result = server.model_readiness(cfg, {"entries": []})
    assert result["model_available"] is False
    assert "密钥" in result["model_unavailable_reason"]


def test_local_preflight_refuses_offline_or_halted_budget(ready_runtime, monkeypatch):
    _, cfg = ready_runtime
    halted = server.model_readiness(cfg, {"entries": [], "halted": True})
    assert halted["model_available"] is False
    assert "台账已暂停" in halted["model_unavailable_reason"]
    monkeypatch.setenv("EQ_STRICT_OFFLINE", "1")
    offline = server.model_readiness(cfg, {"entries": []})
    assert offline["model_available"] is False
    assert "离线" in offline["model_unavailable_reason"]


def test_local_preflight_checks_reservation_and_keeps_zero_settled_cost(ready_runtime):
    _, cfg = ready_runtime
    assert server.model_readiness(cfg, {"entries": []})["model_available"] is True
    spent = {"entries": [{"cost_upper_cny": 20}]}
    assert server.model_readiness(cfg, spent)["model_available"] is False
    free = {"entries": [{"cost_upper_cny": 0, "reserved_cny": 20}]}
    assert server.model_readiness(cfg, free)["model_available"] is True


def test_replay_health_without_cost_material_stays_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "model_readiness", lambda *a: pytest.fail("credential preflight attempted"))
    result = server.health()
    assert result["status"] == "ok"
    assert result["model_available"] is False
    assert result["model_unavailable_reason"]
    assert result["cumulative_cost_upper_cny"] is None
    assert result["total_limit_cny"] is None
    assert not (tmp_path / "runs/model-budget-ledger.json").exists()


def test_replay_health_missing_ledger_keeps_configured_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    (tmp_path / "prototype").mkdir()
    (tmp_path / "prototype/model-budget.json").write_text(json.dumps({"total_limit": 20}))
    result = server.health()
    assert result["model_available"] is False
    assert result["cumulative_cost_upper_cny"] is None
    assert result["total_limit_cny"] == 20
    assert not (tmp_path / "runs/model-budget-ledger.json").exists()
