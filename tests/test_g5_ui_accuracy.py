"""review-5 §1/§4.4 界面准确性小修的回归锚。

这些是静态检查（与 test_g2_ui_wiring.py 同一手法）：界面行为的关键分支
出现在源码里、且旧的反模式不再出现。浏览器级验证由既有浏览器链负责，
这里锁住的是“不会再退回误导性表述”这一层。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "main/static/app.js").read_text()


def test_alignment_is_three_way_not_binary():
    """三值对齐在重设计后的工作台里仍然区分 true/false/null。

    旧页面的正则锚定的是具体模板字符串；工作台改为详情表的“分量对齐”列，
    因此这里锚定新的单一渲染函数，保证 null 不会被吞成“通过”。
    """
    m = re.search(r"function alignmentText\(alignment\)\s*\{(.*?)\n\}", APP, re.S)
    assert m, "分量对齐必须有单一渲染函数"
    body = m.group(1)
    assert "未检查（分量不足）" in body, "null 分支必须写明未检查（分量不足）"
    assert "未通过" in body and "通过" in body


def test_alignment_binary_fallback_is_gone():
    """旧的“是/否”二分支不得回归：aligned===false 直接当“未通过”、其它一律“通过”。"""
    assert not re.search(
        r"alignment\.aligned === false\s*\?\s*'未通过[^']*'\s*:\s*'通过'", APP)
    assert "function alignmentText(" in APP


def test_improvement_is_never_asserted_from_a_state_change():
    """条件变更只展示真实执行参数，不自动宣称“改善”。

    重设计后工作台不做改善判定：条件对比显示实际诊断网格、有效配置数与
    “哪些检查重算、哪些观测复用”，因此不存在把 not_applicable→stable 标绿
    的旧反模式；这里同时锁住“不出现自动改善标签”和“确实展示实际参数”。
    """
    assert "改善" not in APP, "工作台不得自动宣称改善"
    assert "到时稳定性检查" in APP and "n_valid" in APP
    # 旧的一刀切改善判定必须不复存在。
    assert not re.search(r"const improved = .*state_after === 'stable'", APP)


def test_bimodal_state_has_a_label():
    """unstable_bimodal 不再以原始键名显示。"""
    assert "unstable_bimodal: '不一致（双簇）'" in APP


def test_deterministic_evidence_area_says_not_called():
    """确定性 run：模型主张/费用/交付来源都写未调用，不写未知或未给出结论。"""
    # 共享的确定性臂判定 helper 存在，并用于交付来源措辞。
    assert "const DET_ARM_RE = /^(question|user_feedback_recompute|work)$/" in APP
    assert "deterministic: DET_ARM_RE.test(" in APP
    assert "本次未使用智能核查" in APP
    assert re.search(r"v\.deterministic \? '本次未使用智能核查'", APP), \
        "标准核查应明确没有使用智能核查"
    # 模型主张 cell 只在有模型主张时显示结论，不再把缺席渲染成“未给出结论”。
    assert "已调用模型，但没有可解析的结论" not in APP
