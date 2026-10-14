"""Check frozen operational standards, model-visible tasks and derived diagnostics."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "analysis"))

import task as taskmod  # noqa: E402

STD = "3 秒窗内 ≥3 个台站触发"


def _frozen(**over):
    base = dict(
        user_question="这个候选异常是不是多个台站共有的真实传播信号？",
        window_start="2025-03-03T13:02:20", window_end="2025-03-03T13:04:20",
        region={"minlatitude": 48.0, "maxlatitude": 49.0,
                "minlongitude": -123.5, "maxlongitude": -122.0},
        target_band_hz=[1, 12], method_goal="three_component_pick",
    )
    base.update(over)
    return taskmod.make_task(**base)


def test_standard_is_model_visible_when_declared():
    frozen = _frozen(operational_standard=STD)
    view = taskmod.model_view(frozen)
    assert view["operational_standard"] == STD


def test_standard_defaults_to_none_and_stays_model_visible():
    frozen = _frozen()
    assert taskmod.model_view(frozen)["operational_standard"] is None
    assert "operational_standard" in taskmod.MODEL_VISIBLE_TASK_FIELDS


def test_standard_is_part_of_the_frozen_digest():
    """标准进了冻结摘要：事后改动可被检出。"""
    a = _frozen(operational_standard=STD)
    b = _frozen(operational_standard=None)
    assert a["task_sha256"] != b["task_sha256"]


def test_task_from_case_forwards_the_standard():
    import core
    case = core.case_by_id("E1_orcas_event")
    frozen = taskmod.task_from_case(case, operational_standard=STD)
    assert frozen["operational_standard"] == STD
    assert taskmod.model_view(frozen)["operational_standard"] == STD


def test_execution_state_carries_the_standard():
    import prepare_agent
    frozen = _frozen(operational_standard=STD)
    state = prepare_agent.model_state("r-test", frozen, {"n_eligible": 1,
                                                         "eligible": []})
    assert state["operational_standard"] == STD


def test_both_prompts_render_the_standard_section():
    """A/M 提示构造都含公开标准段（源码级锁定 + 运行期渲染）。"""
    for name in ("one_shot.mjs", "agent_run.mjs"):
        text = (ROOT / "main" / name).read_text()
        assert "state.operational_standard" in text, name
        assert "public operational standard" in text, name




def test_m_derived_evidence_includes_clusters_detectors_alignment():
    """M 证据面必须含逐站簇/detectors/alignment，且只有一个实现。"""
    import run_agent_arm
    src = Path(run_agent_arm.__file__).read_text()
    for key in ("clusters", "detectors", "alignment"):
        assert f'"{key}": res["{key}"]' in src, key
    # 两个入口（M 首轮 / M 反馈轮）委托同一实现，不再各自维护。
    assert "derived_evidence(folder, state[\"records\"]," in src
    # 结构级：用真实 run 的 layers.robustness 检查这些键确实存在可选。
    sample = None
    for layers in (ROOT / "main/runlog").glob("r-*/layers.json"):
        data = json.loads(layers.read_text())
        rob = (data or {}).get("robustness") or {}
        for st, v in rob.items():
            if isinstance(v, dict) and v.get("clusters") is not None:
                sample = v
                break
        if sample:
            break
    if sample is not None:
        assert set(("clusters", "detectors", "alignment")) <= set(sample)
