"""Example onboarding reads saved facts, without acquiring data or calling a model."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
import server  # noqa: E402


@pytest.fixture
def example_records(tmp_path, monkeypatch):
    monkeypatch.setattr(server.runlog, "RUNLOG", tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("只读示例说明不能执行核查")

    monkeypatch.setattr(server.review, "run_review", forbidden)
    monkeypatch.setattr(server.review, "run_followup", forbidden)
    for run_id, parent, amount in (
            (server.EXAMPLE_INITIAL_RUN, None, 0.31),
            (server.EXAMPLE_FEEDBACK_RUN, server.EXAMPLE_INITIAL_RUN, 0.47)):
        folder = tmp_path / run_id
        folder.mkdir()
        files = {
            "run": {"run_id": run_id, "task_id": run_id + "-task",
                    "arm": "adaptive_agent_a1", "parent_run_id": parent},
            "task": {"task_id": run_id + "-task", "user_question": "这段观测能不能用于拾取？",
                     "window_start": "2024-01-02T03:04:05", "window_end": "2024-01-02T03:06:05",
                     "region": {"kind": "stations", "stations": ["REAL"]},
                     "target_band_hz": [2.0, 11.0], "method_goal": "three_component_pick"},
            "mcp_state": {"acquired": [
                {"network": "XX", "station": "REAL", "status": "analysed"},
                {"network": "XX", "station": "NO_DATA", "status": "failed"}]},
            "result": {"run_id": run_id, "status": "succeeded"},
            "validation": {"run_id": run_id, "task_id": run_id + "-task",
                           "delivered": {"model_claim_accepted": True, "task_completed": True},
                           "finish_audit": {"can_finish": True},
                           "acceptance_checks": {"field_matches": True, "condition_matches": True}},
            "cost": {"model_cost_known": True, "model_cost_upper_cny": amount},
        }
        for name, value in files.items():
            (folder / (name + ".json")).write_text(json.dumps(value))
    return tmp_path


def rewrite(root, run_id, file_name, change):
    path = root / run_id / (file_name + ".json")
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def test_guide_uses_saved_task_and_actual_acquisition_without_writes(example_records):
    before = {p: p.read_bytes() for p in example_records.rglob("*") if p.is_file()}
    guide = server.demo_guide()
    assert guide["status"] == "ok"
    assert guide["form"] == {
        "source_run_id": server.EXAMPLE_INITIAL_RUN,
        "window_start": "2024-01-02T03:04:05Z", "window_end": "2024-01-02T03:06:05Z",
        "region_name": "", "stations": ["XX.REAL"], "target_band_hz": [2.0, 11.0],
        "method_goal": "three_component_pick", "question": "这段观测能不能用于拾取？"}
    parsed = server.task_input.build(guide["form"])
    assert parsed["ready"] is True
    assert guide["stats"]["scope"] == "accepted_examples"
    assert guide["stats"]["run_count"] == 2
    assert guide["stats"]["feedback_chain_count"] == 1
    assert guide["stats"]["model_cost_upper_cny"] == 0.78
    assert "success_rate" not in guide["stats"]
    assert "verification_count" not in guide["stats"]
    assert {p: p.read_bytes() for p in example_records.rglob("*") if p.is_file()} == before


def test_unknown_cost_does_not_become_zero_or_an_average(example_records):
    rewrite(example_records, server.EXAMPLE_FEEDBACK_RUN, "cost",
            lambda value: value.update(model_cost_known=False, model_cost_upper_cny=0.47))
    guide = server.demo_guide()
    assert guide["status"] == "ok"
    assert guide["stats"]["model_cost_known"] is False
    assert guide["stats"]["model_cost_upper_cny"] is None
    assert guide["runs"][1]["model_cost_upper_cny"] is None
    assert guide["stats"]["run_count"] == 2


@pytest.mark.parametrize("invalid", ["claim_rejected", "failed", "fixture", "mismatched_task"])
def test_directory_and_success_label_do_not_establish_acceptance(example_records, invalid):
    run_id = server.EXAMPLE_INITIAL_RUN
    if invalid == "claim_rejected":
        rewrite(example_records, run_id, "validation",
                lambda value: value["delivered"].update(model_claim_accepted=False))
    elif invalid == "failed":
        rewrite(example_records, run_id, "result", lambda value: value.update(status="failed"))
    elif invalid == "fixture":
        rewrite(example_records, run_id, "run", lambda value: value.update(fixture=True))
    else:
        rewrite(example_records, run_id, "validation", lambda value: value.update(task_id="other"))
    guide = server.demo_guide()
    assert guide["status"] == "incomplete"
    assert guide["form"] is None
    assert guide["stats"]["run_count"] is None
    assert guide["runs"][0]["accepted_example"] is False
    assert guide["missing"]


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_unreadable_evidence_is_reported_without_fallback(example_records, failure):
    path = example_records / server.EXAMPLE_INITIAL_RUN / "task.json"
    if failure == "missing":
        path.unlink()
    else:
        path.write_text("{not-json")
    guide = server.demo_guide()
    assert guide["form"] is None
    assert guide["stats"]["run_count"] is None
    assert guide["stats"]["model_cost_upper_cny"] is None
    assert any(m["file"] == "task.json" and m["run_id"] == server.EXAMPLE_INITIAL_RUN
               for m in guide["missing"])


def test_no_observed_station_does_not_use_a_hardcoded_station(example_records):
    rewrite(example_records, server.EXAMPLE_INITIAL_RUN, "mcp_state",
            lambda value: value.update(acquired=[]))
    assert server.demo_guide()["form"] is None


def test_invalid_saved_window_cannot_fill_an_unusable_form(example_records):
    rewrite(example_records, server.EXAMPLE_INITIAL_RUN, "task",
            lambda value: value.update(window_end="2024-01-02T03:00:00"))
    guide = server.demo_guide()
    assert guide["status"] == "incomplete"
    assert guide["form"] is None
    assert any("合法" in m["reason"] for m in guide["missing"])


def test_feedback_chain_must_point_to_the_accepted_initial_run(example_records):
    rewrite(example_records, server.EXAMPLE_FEEDBACK_RUN, "run",
            lambda value: value.update(parent_run_id="r-unrelated"))
    guide = server.demo_guide()
    assert guide["status"] == "incomplete"
    assert guide["stats"]["feedback_chain_count"] is None


@pytest.mark.parametrize("status, code", [("ok", 200), ("incomplete", 503)])
def test_http_guide_has_explicit_availability(monkeypatch, status, code):
    guide = {"status": status, "form": None, "missing": []}
    monkeypatch.setattr(server, "demo_guide", lambda: guide)
    handler = object.__new__(server.Handler)
    handler.path = "/api/demo-guide"
    handler.local = lambda: None
    responses = []
    handler.send = lambda response_code, body: responses.append((response_code, body))
    handler.do_GET()
    assert responses == [(code, guide)]


def test_saved_accepted_example_form_is_currently_parseable(monkeypatch):
    monkeypatch.setattr(server.runlog, "RUNLOG", ROOT / "replay/runlog")
    assert server.runlog.run_dir(server.EXAMPLE_INITIAL_RUN).is_dir()
    guide = server.demo_guide()
    assert guide["status"] == "ok", guide["missing"]
    assert server.task_input.build(guide["form"])["ready"] is True
    assert guide["form"]["stations"] == ["UW.CHIMA"]
    assert guide["stats"]["model_cost_upper_cny"] == 0.181172
