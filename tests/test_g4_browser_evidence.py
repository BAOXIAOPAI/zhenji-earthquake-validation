"""Check that the Gate B browser-acceptance write-up matches its machine record.

The third review's complaint was not only that behaviour was wrong, but that the
evidence could not be checked. This test reads the trace JSON produced by
`main/ui_trace.mjs` and asserts the Chinese write-up in the same folder states the
same run ids, the same parent chain and the same exclusions. It skips when the
browser artifacts are absent (they are produced by a browser run, not by pytest).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "delivery/review-3-2026-10-10/evidence"
TRACE = EVIDENCE / "ui-operation-trace-gate-b.json"
WRITEUP = EVIDENCE / "浏览器验收-2026-10-10.md"

pytestmark = pytest.mark.skipif(
    not TRACE.exists() or not WRITEUP.exists(),
    reason="浏览器验收产物不存在（需先运行 main/ui_trace.mjs）")


@pytest.fixture(scope="module")
def trace():
    return json.loads(TRACE.read_text())


@pytest.fixture(scope="module")
def writeup():
    return WRITEUP.read_text()


def test_the_writeup_names_every_run_the_browser_actually_produced(trace, writeup):
    steps = trace["steps"]
    assert [s["step"] for s in steps] == [
        "initial_run", "exclude_olga", "narrow_grid", "restore_olga"]
    for step in steps:
        assert step["run_id"], step["step"]
        assert step["run_id"] in writeup, step["run_id"]


def test_the_chain_in_the_writeup_is_the_chain_the_page_showed(trace, writeup):
    """Each step must name the previous step's run as its recorded parent."""
    steps = trace["steps"]
    for previous, step in zip(steps, steps[1:]):
        assert step["parent"] == previous["run_id"], step["step"]
        # The page's own "子 run / 父 run" line is the operator-visible form.
        assert step["run_id"] in step["feedback_head"]
        assert previous["run_id"] in step["feedback_head"]
    assert steps[1]["parent"] in writeup
    assert steps[2]["parent"] in writeup


def test_the_writeup_reports_the_exclusion_the_page_kept(trace):
    by_step = {s["step"]: s for s in trace["steps"]}
    assert "OLGA" in by_step["exclude_olga"]["conditions"]["已排除台站"]
    # The P1-1 regression: the narrowed round must NOT have quietly reinstated OLGA.
    assert "OLGA" in by_step["narrow_grid"]["conditions"]["已排除台站"]
    assert "（无）" in by_step["restore_olga"]["conditions"]["已排除台站"]


def test_the_writeup_does_not_claim_a_model_call_or_a_fresh_fetch(trace, writeup):
    for step in trace["steps"]:
        assert step["run_id"] in step["feedback_head"] or step["step"] == "initial_run"
    assert "并未产生新的额度消耗" in writeup or "不产生新的额度消耗" in writeup
    assert "模型调用 0" in trace["steps"][1]["feedback_head"]


def test_the_writeup_says_which_rounds_reused_the_cache(trace, writeup):
    """A warm-cache trace must not be written up as a live fetch."""
    assert "cache_hits=12" in writeup or "缓存" in writeup
    assert "不是一次新的外部下载" in writeup


def test_the_layout_measurement_is_recorded_at_both_widths(trace):
    assert trace["layout_1280"]["viewport"] == [1280, 720]
    assert trace["layout_1280"]["page_user_active"] is True
    assert trace["layout_1280"]["page_lab"]["w"] == 0
    assert trace["layout_900"]["page_user"]["w"] <= 900
    assert trace["console_errors"] == []


def test_every_screenshot_the_trace_names_exists(trace):
    names = [s["screenshot"] for s in trace["steps"]]
    names += ["页面布局-1280x720.png", "页面布局-900宽.png"]
    for name in names:
        path = EVIDENCE / name
        assert path.exists() and path.stat().st_size > 1000, name


def test_the_writeup_states_its_own_limits(trace, writeup):
    """A single-browser, warm-cache run must not be presented as full coverage."""
    assert "证明什么" in writeup, "the write-up must have an explicit limits section"
    assert "不是一次新的外部下载" in writeup
    assert "Chrome" in writeup and "Firefox" in writeup
    assert "没有调用任何模型" in writeup