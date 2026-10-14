"""The user entry point: free input becomes a structured, checkable task.

The guide requires that a missing window, region or target band raises a
clarification rather than quietly falling back to a preset event, that compound
questions are split, and that out-of-scope requests are limited explicitly.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "analysis"))

import core  # noqa: E402
import task as taskmod  # noqa: E402
import task_input  # noqa: E402

WINDOW = {"window_start": "2025-03-03T11:00:00Z", "window_end": "2025-03-03T11:02:00Z"}
REGION = {"center_lat": 48.611, "center_lon": -122.805, "radius_deg": 1.0}
ASK_USABLE = "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？"


def ready(**extra):
    return task_input.build({**WINDOW, **REGION, "question": ASK_USABLE, **extra})


# ---------------------------------------------------------------- the window
def test_a_complete_request_is_ready_and_echoed_in_plain_language():
    parsed = ready(target_band_hz=[1.0, 12.0], method_goal="three_component_pick")
    assert parsed["ready"] is True
    assert parsed["missing"] == []
    assert "2025-03-03T11:00:00Z" in parsed["echo"]
    assert "1–12 Hz" in parsed["echo"] or "1-12 Hz" in parsed["echo"]
    assert "能不能用于指定频带的拾取" in parsed["echo"]
    assert "三分量" in parsed["echo"]


def test_a_missing_window_is_a_clarification_not_a_default_event():
    parsed = task_input.build({**REGION, "question": ASK_USABLE})
    assert parsed["ready"] is False
    fields = [m["field"] for m in parsed["missing"]]
    assert "window_start" in fields and "window_end" in fields
    assert parsed["structured"].get("window_start") is None
    # nothing may be filled in from a preset
    assert "demo_case_ref" not in parsed["structured"]
    assert "未开始核查" in parsed["echo"]


def test_only_a_start_plus_an_explicit_duration_is_accepted():
    parsed = task_input.build({**REGION, "question": ASK_USABLE,
                               "window_start": "2025-03-03T11:00:00Z",
                               "window_seconds": 120})
    assert parsed["ready"] is True
    assert parsed["structured"]["window_end"] == "2025-03-03T11:02:00"
    assert any("推算" in n for n in parsed["notes"])


def test_a_start_alone_does_not_invent_an_end():
    parsed = task_input.build({**REGION, "question": ASK_USABLE,
                               "window_start": "2025-03-03T11:00:00Z"})
    assert parsed["ready"] is False
    assert [m["field"] for m in parsed["missing"]] == ["window_end"]


def test_an_inverted_window_is_refused():
    parsed = task_input.build({**REGION, "question": ASK_USABLE,
                               "window_start": "2025-03-03T11:02:00Z",
                               "window_end": "2025-03-03T11:00:00Z"})
    assert parsed["ready"] is False
    assert any(m["field"] == "window_end" for m in parsed["missing"])


def test_a_pasted_window_is_recognised_from_the_question_text():
    parsed = task_input.build({
        **REGION,
        "question": "2025-03-03T11:00:00Z 到 2025-03-03T11:02:00Z 这段观测能不能用？"})
    assert parsed["structured"]["window_start"] == "2025-03-03T11:00:00"
    assert parsed["structured"]["window_end"] == "2025-03-03T11:02:00"
    assert any("识别到时窗" in n for n in parsed["notes"])


def test_unix_seconds_are_accepted_and_normalised_to_utc():
    iso, unix = task_input.parse_time(1741000000)
    assert iso == "2025-03-03T11:06:40"
    assert task_input.parse_time("2025-03-03T11:00:00+08:00")[0] == "2025-03-03T03:00:00"


def test_a_long_window_is_flagged_without_being_refused():
    parsed = task_input.build({**REGION, "question": ASK_USABLE,
                               "window_start": "2025-03-03T00:00:00Z",
                               "window_end": "2025-03-03T12:00:00Z"})
    assert parsed["ready"] is True
    assert any("小时" in n for n in parsed["notes"])


# ---------------------------------------------------------------- the region
def test_a_missing_region_is_a_clarification():
    parsed = task_input.build({**WINDOW, "question": ASK_USABLE})
    assert parsed["ready"] is False
    assert any(m["field"] == "region" for m in parsed["missing"])
    assert any("中心点" in m["example"] for m in parsed["missing"])


def test_a_station_list_is_accepted_as_a_region():
    parsed = task_input.build({**WINDOW, "question": ASK_USABLE,
                               "stations": ["UW.OLGA", "UW.MCW"]})
    assert parsed["ready"] is True
    assert parsed["structured"]["region"]["kind"] == "stations"


def test_an_explicit_bounding_box_is_accepted():
    parsed = task_input.build({**WINDOW, "question": ASK_USABLE,
                               "minlatitude": 47.5, "maxlatitude": 48.5,
                               "minlongitude": -123.5, "maxlongitude": -122.0})
    assert parsed["ready"] is True
    assert parsed["structured"]["region"]["kind"] == "box"


def test_a_preset_case_remains_available_as_a_demo_entry():
    parsed = task_input.build({**WINDOW, "question": ASK_USABLE,
                               "demo_case_ref": "A1_injected_artifact"})
    assert parsed["ready"] is True
    assert parsed["structured"]["region"]["kind"] == "preset"


# ---------------------------------------------------------------- the question
def test_a_compound_question_asks_for_one_choice_instead_of_promising_all():
    """parsing used to promise per-subtask checking that the
    review/run path never performs. The honest contract is to require one choice."""
    parsed = task_input.build({
        **WINDOW, **REGION,
        "question": "这个台站是不是仪器故障，而且这段数据还能不能用来做拾取？"})
    ids = [s["question_id"] for s in parsed["subtasks"]]
    assert "Q_instrument" in ids and "Q_usable" in ids
    # It must NOT claim the sub-tasks were checked separately.
    assert not any("已拆成子任务" in n for n in parsed["notes"])
    assert any("一次只核查一个问题" in n for n in parsed["notes"])
    assert parsed["ready"] is False
    assert any(m["field"] == "question" and "请选择其中一项" in m["why"]
               for m in parsed["missing"])


def test_a_single_concern_is_not_split():
    parsed = ready()
    assert parsed["subtasks"] == []
    assert [c["question_id"] for c in parsed["concerns"]] == ["Q_usable"]


def test_an_unrecognisable_question_is_not_mapped_onto_a_default_criterion():
    parsed = task_input.build({**WINDOW, **REGION, "question": "帮我看看这个。"})
    assert parsed["ready"] is False
    assert any(m["field"] == "question" for m in parsed["missing"])
    assert parsed["structured"].get("question_ids") in (None, [])


def test_an_empty_question_is_a_clarification():
    parsed = task_input.build({**WINDOW, **REGION})
    assert parsed["ready"] is False
    assert any(m["field"] == "question" for m in parsed["missing"])


@pytest.mark.parametrize("text,expected", [
    ("这个事件的震级是多少？", "震级"),
    ("能给我震中位置吗？", "定位"),
    ("震源深度多少？", "深度"),
    ("帮我做余震预测", "预测"),
])
def test_out_of_scope_requests_are_limited_explicitly(text, expected):
    parsed = task_input.build({**WINDOW, **REGION, "question": text})
    topics = "".join(u["statement"] for u in parsed["unsupported"])
    assert any(expected in u["topic"] or expected in u["statement"] or expected in text
               for u in parsed["unsupported"])
    assert topics, "an out-of-scope topic must be named"


def test_an_out_of_scope_request_does_not_silently_become_a_supported_one():
    parsed = task_input.build({**WINDOW, **REGION, "question": "这个事件的震级是多少？"})
    assert parsed["unsupported"]
    assert parsed["ready"] is False


# ---------------------------------------------------------------- the band
@pytest.mark.parametrize("band_text", ["5–20 Hz", "5-20 Hz", "5 至 20 Hz", "5 Hz 到 20 hz", "5–20Hz"])
def test_a_text_band_is_used_instead_of_the_default(band_text):
    parsed = ready(question=f"这段观测能不能用来做 {band_text} 的 P/S 拾取？")
    assert parsed["ready"] is True
    assert parsed["structured"]["target_band_hz"] == [5.0, 20.0]
    assert any("识别到目标频带 5–20 Hz" in note for note in parsed["notes"])
    frozen = taskmod.task_from_spec(parsed["structured"])
    assert frozen["target_band_hz"] == [5.0, 20.0]


def test_conflicting_text_and_form_bands_require_a_choice():
    parsed = ready(question="这段观测能不能用来做 5–20 Hz 的 P/S 拾取？",
                   target_band_hz=[1.0, 12.0])
    assert parsed["ready"] is False
    assert any(m["field"] == "target_band_hz" and "5–20 Hz" in m["why"]
               and "1–12 Hz" in m["why"] and "请统一" in m["why"]
               for m in parsed["missing"])


def test_multiple_different_text_bands_are_not_silently_reduced_to_one():
    parsed = ready(question="1–12 Hz 和 5–20 Hz 的观测能不能用来做拾取？")
    assert parsed["ready"] is False
    assert any(m["field"] == "target_band_hz" and "多个不同频带" in m["why"]
               for m in parsed["missing"])


def test_an_unspecified_band_is_explicit_in_the_preview_and_notes():
    parsed = ready(question="这段观测能不能用来做 P/S 拾取？")
    assert parsed["ready"] is True
    assert parsed["structured"]["target_band_hz"] == [1.0, 12.0]
    assert "目标频带 1–12 Hz" in parsed["echo"]
    assert any("默认 1–12 Hz" in note for note in parsed["notes"])


def test_a_malformed_band_is_a_clarification():
    parsed = ready(target_band_hz=[12.0])
    assert parsed["ready"] is False
    assert any(m["field"] == "target_band_hz" for m in parsed["missing"])


def test_an_inverted_band_is_a_clarification():
    parsed = ready(target_band_hz=[12.0, 1.0])
    assert parsed["ready"] is False
    assert any(m["field"] == "target_band_hz" for m in parsed["missing"])


def test_an_unsupported_goal_is_refused_with_the_supported_list():
    parsed = ready(method_goal="locate_event")
    assert parsed["ready"] is False
    problem = next(m for m in parsed["missing"] if m["field"] == "method_goal")
    assert "three_component_pick" in problem["example"]


# ------------------------------------------------- freezing the user's task
def test_a_ready_request_freezes_into_a_task_with_the_users_own_words(tmp_path,
                                                                    monkeypatch):
    monkeypatch.setattr(taskmod, "TASKS", tmp_path / "tasks")
    parsed = ready(target_band_hz=[1.0, 12.0])
    frozen = taskmod.task_from_spec(parsed["structured"])
    assert frozen["user_question"] == ASK_USABLE
    assert frozen["window_start"] == "2025-03-03T11:00:00"
    assert frozen["target_band_hz"] == [1.0, 12.0]
    assert frozen["demo_case_ref"] is None  # not a preset
    assert frozen["region"]["kind"] == "center"
    taskmod.freeze(frozen)
    assert taskmod.load_task(frozen["task_id"])["task_sha256"] == frozen["task_sha256"]


def test_a_task_cannot_be_frozen_without_a_window():
    with pytest.raises(ValueError, match="explicit UTC window"):
        taskmod.task_from_spec({"user_question": ASK_USABLE, "region": REGION})


def test_a_task_cannot_be_frozen_without_a_question():
    with pytest.raises(ValueError, match="question being asked"):
        taskmod.task_from_spec({"window_start": "2025-03-03T11:00:00",
                                "window_end": "2025-03-03T11:02:00",
                                "region": REGION})


def test_a_user_task_gets_a_synthetic_case_that_leaks_no_label():
    frozen = {"task_id": "t-0123456789abcdef", "window_start": "2025-03-03T11:00:00",
              "region": REGION, "acceptance": {"question_ids": ["Q_usable"]}}
    case = taskmod.case_for_task(frozen)
    assert case["case_id"].startswith("user-")
    assert case["split"] == "user"
    assert case["expect"] is None
    for leaked in ("event", "quiet", "injected", "artifact"):
        assert leaked not in case["case_id"]


def test_a_station_list_task_has_no_centre_until_stations_return(monkeypatch):
    """Discovery must work for a station-list request, which has no map centre."""
    region = {"kind": "stations", "stations": ["UW.OLGA"], "networks": []}

    # Build rows through the real parser so the shape matches production exactly.
    def line(chan, dip, azimuth):
        return (f"UW|OLGA||{chan}|48.1|-122.8|100.0|0.0|{azimuth}|{dip}|"
                "Nanometrics Trillium|1.0|1.0|M/S|100.0|2020-01-01T00:00:00|")
    rows = core.fdsn.parse_channel_text("\n".join(
        [line("HHZ", -90.0, 0.0), line("HHN", 0.0, 0.0), line("HHE", 0.0, 90.0)]))
    monkeypatch.setattr(core.fdsn, "station_query",
                        lambda spec: (rows, "synthetic://"))
    result = core.discover({"case_id": "user-x", "start": "2025-03-03T11:00:00",
                            "region": region})
    assert result["n_eligible"] == 1
    assert result["eligible"][0]["distance_km"] is None  # no centre claimed
