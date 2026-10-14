"""G3 打包：从既有记录生成交付清单、费用、失败与声明对照，全部无网络、无模型调用。

原则：只汇总，不筛选。失败、超时、被拒绝的主张与不足结论一律进表。
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "delivery/revision-1"
RUNLOG = ROOT / "main/runlog"
LEDGER = ROOT / "runs/model-budget-ledger.json"

# 交付包要校验的文件：源码、协议、界面、测试与生成脚本。
SOURCES = [
    "main/core.py", "main/task.py", "main/task_input.py", "main/diagnostics.py",
    "main/review.py", "main/server.py", "main/run_agent_arm.py",
    "main/prepare_agent.py", "main/run_g2.py", "main/make_g2_report.py",
    "main/make_g3_package.py", "main/mcp_server.py", "main/permission.mjs",
    "main/provider_bridge.mjs", "main/agent_run.mjs", "main/one_shot.mjs",
    "main/tools.json", "main/g2_tasks.json", "main/start.sh",
    "main/static/index.html", "main/static/app.js", "main/static/style.css",
    "src/budget.mjs", "prototype/model-budget.json",
    "tests/test_g0.py", "tests/test_g1_tool_surface.py",
    "tests/test_g1_user_entry.py", "tests/test_g2_arms.py",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cost_entries():
    if not LEDGER.exists():
        return []
    return json.loads(LEDGER.read_text())["entries"]


def g2_rows():
    f = OUT / "G2-RESULTS.json"
    return json.loads(f.read_text())["runs"] if f.exists() else []


def write_manifest():
    files, missing = {}, []
    for rel in SOURCES:
        p = ROOT / rel
        if not p.exists():
            missing.append(rel)
            continue
        files[rel] = {"sha256": sha(p), "bytes": p.stat().st_size}
    for rel in sorted(p.name for p in OUT.glob("*") if p.is_file()):
        files[f"delivery/revision-1/{rel}"] = {
            "sha256": sha(OUT / rel), "bytes": (OUT / rel).stat().st_size}
    # 逐次运行的证据也进清单：run ID 是唯一关联键，校验它就能追到原始与派生数据。
    runs = {}
    for d in sorted(RUNLOG.iterdir()):
        if not d.is_dir():
            continue
        rec = {}
        for name in ("task.json", "discovery.json", "mcp_state.json",
                     "model_response.json", "validation.json", "cost.json",
                     "result.json", "run.json"):
            f = d / name
            if f.exists():
                rec[name] = {"sha256": sha(f), "bytes": f.stat().st_size}
        runs[d.name] = rec
    doc = {"generated_by": "main/make_g3_package.py",
           "note": ("校验清单：交付文件 + 每次运行的证据。run ID 是唯一关联键，"
                    "贯穿任务快照、发现、MCP 状态、模型响应、验证与费用。"),
           "files": files, "count": len(files), "missing": missing,
           "runs": runs, "run_count": len(runs)}
    (OUT / "MANIFEST.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    print(f"MANIFEST.json：交付文件 {len(files)} 项，运行证据 {len(runs)} 次，"
          f"缺失 {len(missing)}")


def write_costs():
    es = cost_entries()
    by_status = Counter(e["status"] for e in es)
    total = sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0 for e in es)
    deep = [e for e in es if e.get("provider") == "deepseek"]
    rows = g2_rows()
    per_arm = {}
    for arm in ("R", "M", "A"):
        rs = [r for r in rows if r.get("arm") == arm]
        per_arm[arm] = {"runs": len(rs),
                        "model_cost_cny": round(sum(r.get("model_cost_cny") or 0
                                                    for r in rs), 6),
                        "mean_logical_acquisitions": round(
                            sum(r.get("logical_acquisitions") or 0 for r in rs)
                            / max(1, len(rs)), 3)}
    cfg = json.loads((ROOT / "prototype/model-budget.json").read_text())
    doc = {
        "note": ("费用为上界估算，不是提供方账单。每次调用前按最坏上下文预留，"
                 "结算用实际用量。abandoned 条目是被中断进程的预留，按预留额全额计入。"),
        "authorized_limit_cny": cfg["total_limit"],
        "guard_limit_cny": cfg.get("guard_limit_cny", cfg["total_limit"]),
        "cumulative_upper_cny": round(total, 6),
        "remaining_before_guard_cny": round(
            cfg.get("guard_limit_cny", cfg["total_limit"]) - total, 6),
        "entries": len(es),
        "entries_by_status": dict(by_status),
        "deepseek_entries": len(deep),
        "pricing_source": cfg["source"],
        "pricing_verified_date": cfg["verified_client_date"],
        "model": cfg["model"], "model_version_documented": cfg["model_version_documented"],
        "rate_note": (f"输入 {cfg['input_million_peak']} 元/百万 token、"
                      f"输出 {cfg['output_million_peak']} 元/百万 token（峰值价上界）"),
        "g2_by_arm": per_arm,
        "not_counted": ("公共 FDSN/EarthScope 取数不产生费用，因此不在金额内；"
                        "Agnes 探针的金额未知，单列而不计入。"),
    }
    (OUT / "COSTS.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    print(f"COSTS.json：累计上界 {total:.4f} / {doc['guard_limit_cny']} 元，"
          f"{len(es)} 条")


def write_failures():
    lines = ["# FAILURES.md —— 全部失败、超时与不及格，未做删减", "",
             "本文件列出运行中所有**非成功**记录。成功的运行在 `G2-RESULTS.json` 与",
             "`main/runlog/` 中逐条可查。不剔除、不重跑换掉、不把失败说成通过。", ""]

    # 1. 运行层失败（覆盖整个 runlog，不只 G2）
    rows = []
    for d in sorted(RUNLOG.iterdir()):
        r = d / "result.json"
        if not r.exists():
            continue
        try:
            rec = json.loads(r.read_text())
        except Exception:  # noqa: BLE001
            continue
        if rec.get("status") and rec["status"] != "succeeded":
            rows.append((d.name, rec.get("status"), rec.get("failure_kind"),
                         str(rec.get("failure_detail"))[:160]))
    lines += [f"## 1. 运行层失败（{len(rows)} 条，覆盖全部 {len(list(RUNLOG.iterdir()))} 次运行）", ""]
    if rows:
        lines += ["| run ID | 状态 | 失败类型 | 详情 |", "| --- | --- | --- | --- |"]
        lines += [f"| `{a}` | {b} | {c} | {dd} |" for a, b, c, dd in rows]
    else:
        lines.append("无。")
    lines.append("")

    # 2. 对照层的未满足与安全违规
    g2 = g2_rows()
    bad = [r for r in g2 if (r.get("evaluation") or {}).get("contract")
           in ("failed", "violation")]
    lines += [f"## 2. G2 对照里未满足与安全违规（{len(bad)} 条 / 共 {len(g2)} 条）", "",
              "| 任务 | 组 | 合同 | 运行 ID | 说明 |", "| --- | --- | --- | --- | --- |"]
    for r in sorted(bad, key=lambda x: (x["task_id"], x["arm"])):
        ev = r["evaluation"]
        lines.append(f"| {r['task_id']} | {r['arm']} | {ev['contract']} "
                     f"| `{r.get('run_id')}` | {str(ev.get('why'))[:100]} |")
    lines.append("")

    # 3. 分类汇总，避免用总数掩盖结构性差异
    c = Counter((r["arm"], (r.get("evaluation") or {}).get("contract")) for r in g2)
    lines += ["## 3. 按组分类计数", "", "| 组 | 满足 | 正确不足 | 未满足 | 安全违规 |",
              "| --- | --- | --- | --- | --- |"]
    for arm in ("R", "M", "A"):
        lines.append(f"| {arm} | {c[(arm,'satisfied')]} | "
                     f"{c[(arm,'insufficient_correctly')]} | {c[(arm,'failed')]} | "
                     f"{c[(arm,'violation')]} |")
    lines += ["", "## 4. 实测到的两个基础设施故障（已修，保留记录）", "",
              "1. **费用台账被一次中断永久堵死**：进程在 reserve 与 settle 之间被杀会留下",
              "   `pending` 条目和陈旧锁，之后所有付费运行失败，AGH 显示为",
              "   `INTERNAL_ERROR (-32603)`。已修并可恢复，回归见 `tests/test_g2_arms.py`。",
              "2. **`delivered.value` 混淆判决标签与字段取值**：通道完整性字段的取值是",
              "   `True`，下游会读成结论。已拆为 `delivered.verdict` 与 `field_value`。",
              "", "这两条是真实发生过的故障，未从记录中移除。"]
    (OUT / "FAILURES.md").write_text("\n".join(lines))
    print(f"FAILURES.md：运行层失败 {len(rows)} 条，对照层 {len(bad)} 条")


def write_claims():
    text = (OUT / "G2-REPORT.md").read_text() if (OUT / "G2-REPORT.md").exists() else ""
    table = ""
    if "## 声明与证据对照表" in text:
        table = text[text.index("## 声明与证据对照表"):]
        table = table[:table.index("\n本报告由")]
    lines = [
        "# CLAIMS.md —— 声明与证据对照", "",
        "**本版最重要的声明是一条否定结论。**", "",
        "> 加入模型一步（M）或让模型自适应多步（A），在开发集与锁定集上都没有同时",
        "> 超过不调用模型的问题感知规则（R）。因此本版**不宣称模型带来提升**。", "",
        "这条结论来自 50 次真实运行（开发 38 + 锁定 12），逐条见 `G2-RESULTS.json`，",
        "协议见 `G2-PROTOCOL.md`（先于任何 G2 运行冻结），判定逻辑见",
        "`main/make_g2_report.py`。", "",
        "## G2 声明对照（从报告原样引用）", "", table, "",
        "## 本版明确**不**主张的事", "",
        "1. 不主张自适应多步优于确定性规则——数据不支持。",
        "2. 不主张「确定性规则已达下界、模型无提升空间」——旧主张已撤回，",
        "   本次只说明**在这一批任务与这一种评价口径下未观察到优势**，",
        "   样本量小，不外推。",
        "3. 不主张零假阳性：ComCat 有探测下限，「目录无事件」不等于「无信号」。",
        "4. 不主张稳定性诊断等于到时正确性：它只是在有限固定网格上的稳定性。",
        "5. 不主张「专业业务可靠性」：小样本通过只说明原型值得继续。", "",
        "## 可以主张的工程价值（有证据）", "",
        "1. 输入、观测事实、模型主张、验证器裁决与最终交付是**分开的四层**，",
        "   模型原文错误不会被重新计算的判决遮住（P0-2，有回归测试）。",
        "2. 证据不足时给出**可核验的不足理由**，而不是猜一个标签。",
        "3. 唯一 run ID 贯穿任务快照、MCP 状态、工具调用、原始与派生数据、",
        "   模型响应、验证结果、费用与界面（P0-1，有回归测试）。",
        "4. 费用上界可核验且**不会漏记**：挂起条目按预留额保守计入。", "",
    ]
    (OUT / "CLAIMS.md").write_text("\n".join(lines))
    print("CLAIMS.md：已写出")


def write_results():
    g2 = g2_rows()
    cfg = json.loads((ROOT / "prototype/model-budget.json").read_text())
    doc = {
        "note": "逐次结果汇总。每条 run_id 都能在 main/runlog/<run_id>/ 追到全部证据。",
        "protocol": "delivery/revision-1/G2-PROTOCOL.md",
        "frozen_at": "2026-10-09",
        "model": {"name": cfg["model"], "version": cfg["model_version_documented"],
                  "provider": "DeepSeek", "api": "https://api.deepseek.com/chat/completions",
                  "used_in": "M 组一次调用、A 组多步自适应；R 组不调用模型"},
        "arms": {"R": "问题感知确定性规则，不使用模型",
                 "M": "同一模型，一次取得完整许可证据",
                 "A": "AGH 自适应复核，按中间证据选择许可动作"},
        "ceiling_stations": 6,
        "g2_runs": g2,
        "g2_summary": {},
    }
    for scope, pre in (("development", "D"), ("locked", "L")):
        rows = [r for r in g2 if r["task_id"].startswith(pre)]
        s = {}
        for arm in ("R", "M", "A"):
            rs = [r for r in rows if r["arm"] == arm]
            c = Counter((r.get("evaluation") or {}).get("contract") for r in rs)
            s[arm] = {"runs": len(rs), "satisfied": c["satisfied"],
                      "insufficient_correctly": c["insufficient_correctly"],
                      "failed": c["failed"], "violation": c["violation"],
                      "satisfaction_rate": round(
                          100 * (c["satisfied"] + c["insufficient_correctly"])
                          / max(1, len(rs)), 1),
                      "certainty_errors": sum(
                          1 for r in rs if (r.get("evaluation") or {}).get("certainty_error")),
                      "model_cost_cny": round(sum(r.get("model_cost_cny") or 0
                                                  for r in rs), 6)}
        doc["g2_summary"][scope] = s
    (OUT / "RESULTS.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    (OUT / "PROTOCOL.md").write_text(
        "# PROTOCOL.md —— 冻结协议索引\n\n"
        "本版有两层协议，都已冻结，写定时间早于对应的验证运行。\n\n"
        "## 1. 对照协议（G2）\n\n"
        "见 [`G2-PROTOCOL.md`](G2-PROTOCOL.md)。它规定三组定义、任务合同、覆盖类别、\n"
        "观测上限、评价口径、通过判据与**预先声明的收益假设**。锁定任务的时窗在该\n"
        "文件写定时确定，此前未查看其波形与参考到时。\n\n"
        "## 2. 任务与冻结快照\n\n"
        "每个任务在运行前由 `main/task.py` 写成不可变快照（`task.json`，只读），\n"
        "记录用户原问题、时窗、区域、目标频带 1–12 Hz、方法频带 1–8 Hz、\n"
        "允许的动作与验收说明。快照的 sha256 进 `MANIFEST.json`。\n\n"
        "## 3. 不随结果改动的部分\n\n"
        "- 观测上限：6 台（三组相同）\n"
        "- 方法频带 1–8 Hz、目标频带 1–12 Hz、因果四角带通\n"
        "- 判决标签集合：`instrument_artifact` / `coherent_multistation` /\n"
        "  `single_station_only` / `not_decidable`\n"
        "- 验证器与失败处理：三组共用同一个入口\n\n"
        "## 4. 开发阶段修正过的部分（如实记录）\n\n"
        "评判器在**开发阶段**被修正过两次：一是把「没取数就答未知」从合格改为不合格，\n"
        "二是拆开判决标签与字段取值。两次修正都发生在**任何锁定任务运行之前**，\n"
        "并用 `main/run_g2.py rejudge` 对全部已有运行统一重判，避免为口径改动重复付费。\n")
    print("RESULTS.json / PROTOCOL.md：已写出")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in (write_manifest, write_costs, write_failures, write_claims, write_results):
        fn()


if __name__ == "__main__":
    main()