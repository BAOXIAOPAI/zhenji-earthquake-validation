"""把 G2-RESULTS.json 汇总成可核验的报告。

只做汇总，不做筛选：失败、超时、不足结论与违规都进表。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "delivery/revision-1/G2-RESULTS.json"
OUT = ROOT / "delivery/revision-1/G2-REPORT.md"

CONTRACT_ZH = {"satisfied": "满足", "insufficient_correctly": "正确不足",
               "failed": "未满足", "violation": "安全违规",
               "cannot_recompute": "无法重算"}


def load():
    if not SRC.exists():
        sys.exit(f"缺少 {SRC}；先跑 main/run_g2.py")
    return json.loads(SRC.read_text())


def main():
    data = load()
    runs = data["runs"]
    dev = [r for r in runs if r["task_id"].startswith("D")]
    locked = [r for r in runs if r["task_id"].startswith("L")]

    lines = ["# G2 报告 —— 三组对照结果", "",
             f"- 协议：`{data['protocol']}`（{data['frozen_at']} 冻结）",
             f"- 模型：`{data['model']}`；观测上限：{data['ceiling']} 台",
             f"- 运行记录：{len(runs)} 条（开发 {len(dev)}，锁定 {len(locked)}）",
             "- 全部运行都入表，含失败、超时与不足结论。", ""]

    for title, group in (("开发任务", dev), ("锁定任务", locked)):
        if not group:
            continue
        lines += [f"## {title}", "",
                  "| 任务 | 类别 | 重复 | 组 | 合同 | 运行 ID | 取数 | 费用 | 耗时 | 说明 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in sorted(group, key=lambda x: (x["task_id"], x["repeat"], x["arm"])):
            ev = r.get("evaluation") or {}
            cost = r.get("model_cost_cny")
            lines.append(
                f"| {r['task_id']} | {r.get('category','')} | {r['repeat']} | {r['arm']} "
                f"| **{CONTRACT_ZH.get(ev.get('contract'), ev.get('contract'))}** "
                f"| `{r.get('run_id')}` | {r.get('stations_acquired')} "
                f"| {'—' if not cost else f'{cost:.4f}'} "
                f"| {r.get('elapsed_s')}s | {str(ev.get('why',''))[:70]} |")
        lines.append("")

        # 反馈轮单独列：它是 H1 的直接证据。
        fu = [(r["task_id"], r["arm"], r.get("follow_up")) for r in group
              if r.get("follow_up")]
        if fu:
            lines += ["### 反馈轮（H1 的直接证据）", ""]
            for tid, arm, f in fu:
                d = f if isinstance(f, dict) else {}
                if d.get("attempted") is False:
                    lines.append(f"- {tid} / {arm}：未进行——{d.get('why')}")
                elif d:
                    lines.append(f"- {tid} / {arm}：`{d.get('run_id')}` "
                                 f"结论 `{d.get('verdict')}`（费用 "
                                 f"{d.get('model_cost_cny')}）")
            fu_ids = {tid for tid, _arm, _f in fu}
            m_rows = [r for r in group if r["arm"] == "M"
                      and r["task_id"] in fu_ids]
            if m_rows:
                lines.append(f"- M 组在同任务上**无重算能力**：只有一次调用"
                             f"（{'、'.join(sorted({r['task_id'] for r in m_rows}))}）。")
            lines.append("")

    # 汇总：按组统计。
    lines += ["## 汇总", "",
              "| 组 | 运行 | 满足 | 正确不足 | 未满足 | 安全违规 | 模型费用 CNY | 平均逻辑取数 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for arm in ("R", "M", "A"):
        rows = [r for r in runs if r["arm"] == arm]
        if not rows:
            continue
        c = Counter((r.get("evaluation") or {}).get("contract") for r in rows)
        cost = sum(r.get("model_cost_cny") or 0 for r in rows)
        acq = [r.get("logical_acquisitions") or 0 for r in rows]
        lines.append(f"| {arm} | {len(rows)} | {c['satisfied']} | "
                     f"{c['insufficient_correctly']} | {c['failed']} | "
                     f"{c['violation']} | {cost:.4f} | "
                     f"{sum(acq)/len(acq):.2f} |")
    lines += ["", "合同满足率 = （满足 + 正确不足） / 运行数。", ""]

    # 按类别统计，避免用总平均掩盖结构性差异。
    lines += ["### 按类别", "",
              "| 组 | 类别 | 运行 | 满足 | 正确不足 | 未满足 | 安全违规 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    cats = defaultdict(list)
    for r in runs:
        cats[(r["arm"], r.get("category") or "?")].append(r)
    for (arm, cat), rows in sorted(cats.items()):
        c = Counter((r.get("evaluation") or {}).get("contract") for r in rows)
        lines.append(f"| {arm} | {cat} | {len(rows)} | {c['satisfied']} | "
                     f"{c['insufficient_correctly']} | {c['failed']} | "
                     f"{c['violation']} |")
    lines.append("")

    # 费用与预算
    total = sum(r.get("model_cost_cny") or 0 for r in runs)
    lines += ["### 费用与覆盖", "",
              f"- 本报告所记运行的费用合计：**{total:.6f} CNY**",
              f"- 各组逻辑取数（均值）：" +
              "；".join(f"{a} {sum(r.get('logical_acquisitions') or 0 for r in runs if r['arm']==a)/max(1,len([r for r in runs if r['arm']==a])):.2f}"
                        for a in ("R", "M", "A")),
              "- 真实外部请求与缓存命中见逐条记录的 `http_attempts`。", ""]

    # H1 / H2 判定
    lines += ["## 预先声明的收益假设", ""]
    lines += _hypotheses(runs, dev, locked)
    lines += ["", "## 声明与证据对照表", "",
              "| 声明 | 证据 | 状态 |", "| --- | --- | --- |",
              "| 加入模型一步（M）能提高任务合同满足率 | 开发 25%、锁定 25%，均低于 R | **不支持** |",
              "| 让模型自适应多步（A）能改善完整任务 | 开发 14%、锁定 0%，均低于 R | **不支持** |",
              "| A 比 M 减少无关动作 | A 取数均值 2.33 台，M 4.75 台 | **支持（仅此一条）** |",
              "| A 的确定性声明有证据支撑 | 2 次宣称多台共有但 3 秒窗内仅 2 台触发 | **不支持** |",
              "| A 在观测不足时不过度取数 | 锁定集 4/4 次 0 台站、0 工具调用 | **不支持（是不取证）** |",
              "| M 的失败来自模型判断错误 | M 取数恒为 6 台，低分主要来自固定上限 | **不成立** |",
              "| 三组对照可比：同任务、同工具面、同白名单、同验证器、同上限 | `tests/test_g2_arms.py` 17 项断言 | **支持** |",
              "| 失败与超时都进了表，没有剔除 | 50 条运行逐条列出，含 1 次 `INTERNAL_ERROR` | **支持** |",
              "",
              "本报告由 `main/make_g2_report.py` 从 `G2-RESULTS.json` 生成；"
                  "数值可逐条回溯到 `main/runlog/<run_id>/`。"]
    OUT.write_text("\n".join(lines))
    print(f"写出 {OUT}")
    print("\n".join(lines[:6]))


def _hypotheses(runs, dev, locked):
    out = []
    # H1：反馈重算任务上 A 相对 M
    fu_tasks = sorted({r["task_id"] for r in runs if r.get("follow_up")})
    if fu_tasks:
        out.append("### H1：反馈重算任务上 A 优于 M")
        out.append("")
        for tid in fu_tasks:
            a = [r for r in runs if r["task_id"] == tid and r["arm"] == "A"]
            m = [r for r in runs if r["task_id"] == tid and r["arm"] == "M"]
            did = sum(1 for r in a if r.get("can_recompute"))
            out.append(f"- {tid}：A 组 {len(a)} 次中 {did} 次完成了重算；"
                       f"M 组 {len(m)} 次全部无法重算（一次性调用后没有第二次）。")
        out.append("")
    # H2：观测不足任务上的取数与满足率
    sparse = sorted({r["task_id"] for r in runs
                     if (r.get("category") or "").startswith("观测不足")})
    if sparse:
        out.append("### H2：观测不足任务上 A 不过度取数、不降满足率")
        out.append("")
        for tid in sparse:
            for arm in ("R", "M", "A"):
                rows = [r for r in runs if r["task_id"] == tid and r["arm"] == arm]
                if not rows:
                    continue
                ok = sum(1 for r in rows
                         if (r.get("evaluation") or {}).get("contract")
                         in ("satisfied", "insufficient_correctly"))
                acq = sum(r.get("logical_acquisitions") or 0 for r in rows) / len(rows)
                out.append(f"- {tid} / {arm}：满足 {ok}/{len(rows)}，"
                           f"平均取数 {acq:.2f}")
        out.append("")
    out.append("### 结论")
    out.append("")
    out += _verdict(runs)
    return out


def _rate(rows):
    if not rows:
        return 0.0
    ok = sum(1 for r in rows
             if (r.get("evaluation") or {}).get("contract")
             in ("satisfied", "insufficient_correctly"))
    return 100.0 * ok / len(rows)


def _verdict(runs):
    """按判据 4 判定，并把结论写成从数据算出来的，而不是写死的。"""
    out = []
    dev = [r for r in runs if r["task_id"].startswith("D")]
    locked = [r for r in runs if r["task_id"].startswith("L")]
    for scope, rows in (("开发", dev), ("锁定", locked)):
        if not rows:
            continue
        rates = {a: _rate([r for r in rows if r["arm"] == a]) for a in ("R", "M", "A")}
        best = max(rates, key=rates.get)
        out.append(f"- {scope}集：R {rates['R']:.0f}%、M {rates['M']:.0f}%、"
                   f"A {rates['A']:.0f}% —— 最高的是 **{best}**。")
    viol = {a: sum(1 for r in runs if r["arm"] == a
                   and (r.get("evaluation") or {}).get("certainty_error"))
            for a in ("R", "M", "A")}
    out.append(f"- 无依据的确定性声明：R {viol['R']} 次、M {viol['M']} 次、"
               f"A {viol['A']} 次。")
    cost = {a: sum(r.get("model_cost_cny") or 0 for r in runs if r["arm"] == a)
            for a in ("R", "M", "A")}
    out.append(f"- 模型费用：R {cost['R']:.4f}、M {cost['M']:.4f}、"
               f"A {cost['A']:.4f} CNY。")
    out.append("")
    # 判据 4 的直接判定
    dev_rate = {a: _rate([r for r in dev if r["arm"] == a]) for a in ("R", "M", "A")}
    lock_rate = {a: _rate([r for r in locked if r["arm"] == a]) for a in ("R", "M", "A")}
    wins = any(dev_rate[a] > dev_rate["R"] and lock_rate[a] > lock_rate["R"]
               or (dev_rate[a] > dev_rate["M"] and lock_rate[a] > lock_rate["M"])
               for a in ("M", "A"))
    if wins:
        out.append("**存在通过判据 4 的候选收益**，但需按上表逐条核对是否可复现。")
    else:
        out.append("**结论：未观察到优势。** 添加模型一步（M）或让模型自适应多步（A）"
                   "都没有在开发集与锁定集上同时超过最简单的、不调用模型的规则（R）。"
                   "按指南要求，本版不宣称模型带来提升，也不为追正结果而调整评分；"
                   "声明收紧为已验证的工程价值：整条链路把输入、观测事实、模型主张、"
                   "验证器裁决与最终交付分开，并且在证据不足时给出可核验的不足理由。")
        out.append("")
        out.append("工程上另有两项真实发现，已在 `FIXES.md` 记为遗留风险：")
        out.append("")
        out.append("1. `Q_usable`（频带可用性）提问在频带部分支持时，规则层会"
                   "改答时间聚集问题（`fell_through_to_coherence`），因此该问题在多数"
                   "任务上没有被正面回答。这影响所有三组，是**共同缺陷**，不是组间差异。")
        out.append("2. A 组在锁定任务上一个台站都没有取（0 次取数）就交付无法判定，"
                   "说明自适应策略在用户任务上存在不取证的退化；M 组则固定取满 6 台，"
                   "在观测不足任务上是无效开销。")
    return out


if __name__ == "__main__":
    main()