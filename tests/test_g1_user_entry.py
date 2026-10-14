"""The user's own request is a first-class entry point (G1 4.1).

Two things are checked here that the earlier suite missed:

* a request the user typed ends up as a real run whose frozen task carries their
  window, their region and their words — not a preset event;
* `classify_question` no longer reads a method word as a fault word. A bare "通道"
  used to sit in the instrument table, so "能不能做三通道拾取" was answered as if
  the user had asked whether the instrument had failed.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import review  # noqa: E402
import task as taskmod  # noqa: E402
import task_input  # noqa: E402

WINDOW = {"window_start": "2025-03-03T13:02:20", "window_end": "2025-03-03T13:04:20"}
ASK = "这个时窗能不能做三通道到时拾取？"


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core.fdsn, "set_cache", lambda *a, **k: None, raising=False)
    return tmp_path


# --- classification ---------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("能不能做三通道到时拾取？", "Q_usable"),
    ("这段数据可用于 1-12Hz 的拾取吗？", "Q_usable"),
    ("只做垂直分量的到时检查行不行？", "Q_usable"),
    ("这台站的通道是不是坏了？", "Q_instrument"),
    ("这个异常是不是仪器或元数据问题造成的？", "Q_instrument"),
    ("OLGA 的异常是不是通道故障造成的？", "Q_instrument"),
    ("这个信号是多台共同记录到的吗？", "Q_propagating"),
])
def test_method_words_are_not_read_as_fault_words(text, expected):
    assert core.classify_question(text) == expected


def test_a_bare_channel_word_no_longer_implies_instrument_failure():
    """The regression that mislabelled a three-component pick question."""
    assert core.classify_question("能不能做三通道拾取") == "Q_usable"
    assert "通道" not in dict(core.QUESTION_KEYWORDS)["Q_instrument"]


# --- named regions ----------------------------------------------------------

def test_named_regions_carry_their_source():
    for name, entry in task_input.NAMED_REGIONS.items():
        assert entry["label"], name
        assert entry["source"], name
        assert entry["minlatitude"] < entry["maxlatitude"], name
        assert entry["minlongitude"] < entry["maxlongitude"], name
        assert "development" in entry["source"] or "失败样例" in entry["source"]


def test_a_named_region_resolves_to_a_box():
    parsed = task_input.build({**WINDOW, "question": ASK, "region": "orcas"})
    assert parsed["ready"] is True
    region = parsed["structured"]["region"]
    assert region["kind"] == "preset_region"
    assert region["center_lat"] == 48.611
    assert region["source"]


def test_an_unknown_region_name_asks_rather_than_guesses():
    parsed = task_input.build({**WINDOW, "question": ASK, "region": "atlantis"})
    assert parsed["ready"] is False
    gap = [m for m in parsed["missing"] if m["field"] == "region"]
    assert gap and "atlantis" in gap[0]["why"]
    assert "orcas" in gap[0]["example"]


def test_no_region_at_all_still_asks_and_never_defaults_to_a_case():
    parsed = task_input.build({**WINDOW, "question": ASK})
    assert parsed["ready"] is False
    assert [m for m in parsed["missing"] if m["field"] == "region"]


# --- the whole path ---------------------------------------------------------

def test_a_user_request_becomes_a_real_run_with_the_users_own_task():
    raw = {**WINDOW, "question": ASK, "region": "orcas"}
    parsed = task_input.build(raw)
    assert parsed["ready"] is True
    frozen = taskmod.task_from_spec(parsed["structured"], user_question=raw["question"])
    taskmod.freeze(frozen)

    result = review.run_review(None, raw["question"], "question", user_task=frozen)

    assert result["from_user_task"] is True
    assert result["case_id"].startswith("user-")
    assert result["question"] == raw["question"], "the user's words must be verbatim"
    assert result["question_id"] == "Q_usable"
    # the frozen task on disk carries the same window the user gave
    folder = ROOT / result["evidence_dir"]
    task_on_disk = json.loads((folder / "task.json").read_text())
    assert task_on_disk["window_start"].startswith("2025-03-03T13:02:20")
    assert task_on_disk["window_end"].startswith("2025-03-03T13:04:20")
    assert task_on_disk["user_question"] == raw["question"]
    # and the run is recorded under its own id, as a success
    outcome = json.loads((folder / "result.json").read_text())
    assert outcome["status"] == "succeeded"


def test_a_user_task_does_not_carry_an_evaluation_label():
    """A synthetic case must not smuggle in a kind/expect that the model could see."""
    raw = {**WINDOW, "question": ASK, "region": "orcas"}
    parsed = task_input.build(raw)
    frozen = taskmod.task_from_spec(parsed["structured"], user_question=raw["question"])
    case = taskmod.case_for_task(frozen)
    assert case["split"] == "user"
    assert case["expect"] is None
    assert case["kind"] == "user_task"
    view = taskmod.model_view(frozen)
    assert "expect" not in view and "kind" not in view and "split" not in view


def test_a_user_task_without_a_window_is_refused_not_defaulted():
    with pytest.raises(ValueError, match="explicit UTC window"):
        taskmod.task_from_spec({"region": task_input.NAMED_REGIONS["orcas"]},
                               user_question=ASK)