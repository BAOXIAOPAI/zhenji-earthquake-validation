"""C 合同接线测试（review-5 审计后的两处修复，全部无付费）。

审计发现 C1 两个真实合同缺口，指南要求“发现合同或评分错误先修无付费
入口，修复后标版本”：

  1. 公开操作标准（三站/三秒）只写在 c_tasks.json 合同文档里，没有接线
     到冻结任务与 R/M/A 提示 —— 违反 §2.2“A 默认 open 也须给它”。
     修复：task 模型可见字段 → prepare 状态 → one_shot/agent_run 提示。
  2. M 臂的派生证据只有 stability/spread/n_valid，缺 §2.2 要求的
     逐站簇、detectors、alignment。修复：derive 证据补齐全部诊断字段。

这两处不重跑 C1 已跑结果（那会变成“看结果调参”）；C1 报告已追加更正
披露，C2 用修好的合同冻结运行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

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


def test_task_contracts_declare_the_standard_for_propagation():
    c1 = json.loads((ROOT / "main/c_tasks.json").read_text())
    t4 = next(t for t in c1["tasks"] if t["id"] == "T4")
    assert "3 秒窗内" in t4["operational_standard"]
    c2 = json.loads((ROOT / "main/c2_tasks.json").read_text())
    prop = [t for t in c2["tasks"] if t["question_id"] == "Q_propagating"]
    assert prop and all("3 秒窗内" in t["operational_standard"] for t in prop)
    non_prop = [t for t in c2["tasks"] if t["question_id"] != "Q_propagating"]
    assert all(not t.get("operational_standard") for t in non_prop)


def test_m_derived_evidence_includes_clusters_detectors_alignment():
    """M 证据面必须含逐站簇/detectors/alignment，且只有一个实现。"""
    import run_c
    import run_agent_arm
    src = Path(run_agent_arm.__file__).read_text()
    for key in ("clusters", "detectors", "alignment"):
        assert f'"{key}": res["{key}"]' in src, key
    # 两个入口（M 首轮 / M 反馈轮）委托同一实现，不再各自维护。
    assert "run_agent_arm.derived_evidence" in Path(run_c.__file__).read_text()
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

def test_single_variant_feedback_honours_declared_permission():
    """C2 缺陷 2：单变体反馈也必须读合同声明的 acquisition_allowed。

    指令写“不要重新取数”而变体仍默认 allowed=True 时，照指令不取数的 A
    会被误判“策略不足”——这是合同不一致，不是模型缺陷。
    """
    import run_c
    task = {"id": "X", "feedback": {
        "instruction": "排除一个台站，不要重新取数。",
        "change": {"kind": "exclude", "station": None},
        "acquisition_allowed": False}}
    variants = run_c._feedback_variants(task)
    assert len(variants) == 1
    assert variants[0]["acquisition_allowed"] is False
    # 未声明时默认允许。
    task2 = {"id": "Y", "feedback": {"instruction": "改一下",
                                     "change": {"kind": "exclude",
                                                "station": None}}}
    assert run_c._feedback_variants(task2)[0]["acquisition_allowed"] is True


def test_c2_declares_permission_where_the_instruction_forbids_refetching():
    c2 = json.loads((ROOT / "main/c2_tasks.json").read_text())
    for t in c2["tasks"]:
        fb = t.get("feedback") or {}
        if "不要重新取数" in str(fb.get("instruction", "")):
            assert fb.get("acquisition_allowed") is False, t["id"]


def test_r_arm_answers_the_declared_question_not_the_case_preset():
    """C2 缺陷 1 锁定：R 必须按任务声明的问题分类，而不是案例预置问题。

    C2-T3 复用 E4 事件窗问方法适用性、C2-T4 复用 Q4 安静窗问仪器归因；
    若走 case['question_id']，R 会答成传播问题并成为错误参照。
    """
    import inspect
    import run_c
    src = inspect.getsource(run_c._row_for_r)
    assert "user_task=frozen" in src
    assert "task_from_case" in src
    c2 = json.loads((ROOT / "main/c2_tasks.json").read_text())
    # 这些任务的问题与案例预置问题不同，正是缺陷的触发条件。
    reused = [t for t in c2["tasks"]
              if t["question_id"] != "Q_propagating"
              and t["case_id"] in ("E4_quilcene_event", "Q4_quilcene_quiet")]
    assert reused, "C2 应保留复用其它问题的时窗（缺陷触发条件）"
