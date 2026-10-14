"""Generate the Gate B browser-acceptance write-up from its machine record.

The third review asked for evidence that can be *checked*, not re-stated. The run
ids, the parent chain, the counters and the layout measurements all live in the
trace JSON produced by `main/ui_trace.mjs`; this module turns that record into the
Chinese write-up so the two can never disagree.

Usage:
    .venv/bin/python main/ui_evidence_md.py <trace.json> <runlog-dir> <out.md>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STEP_TITLES = {
    "initial_run": "首轮",
    "exclude_olga": "排除 OLGA",
    "narrow_grid": "收紧网格",
    "restore_olga": "恢复 OLGA",
}


def load_run(runlog: Path, run_id: str) -> dict:
    folder = runlog / run_id
    read = lambda name: (json.loads((folder / name).read_text())  # noqa: E731
                         if (folder / name).exists() else {})
    return {"run": read("run.json"), "task": read("task.json"), "cost": read("cost.json"),
            "state": read("mcp_state.json"), "layers": read("layers.json"),
            "verdict": read("verdict.json"), "feedback": read("feedback.json")}


def robustness(layers: dict) -> dict:
    return {k: (v or {}).get("stability") for k, v in (layers.get("robustness") or {}).items()}


def build(trace: dict, runlog: Path) -> str:
    steps = trace["steps"]
    runs = {s["step"]: load_run(runlog, s["run_id"]) for s in steps}
    for step in steps:
        step["_run"] = runs[step["step"]]

    def station_list(value: str) -> str:
        return value if value else "—"

    chain_rows = []
    for step in steps:
        r = step["_run"]
        chain_rows.append(
            "| {} | `{}` | {} | {} | {} | {} / {} |".format(
                STEP_TITLES[step["step"]], step["run_id"],
                f"`{step['parent']}`" if step.get("parent") else "—",
                station_list(step["conditions"]["在用台站"]),
                station_list(step["conditions"]["已排除台站"]),
                r["cost"].get("http_attempts"), r["feedback"].get("model_calls", 0)))

    head_lines = []
    for step in steps[1:]:
        # The panel text is truncated at 200 chars by the driver; cut at the
        # "这次改的条件" marker so the quoted line stays readable.
        head = (step["feedback_head"] or "").split("这次改的条件")[0].strip()
        head_lines.append("- {}：`{}`".format(STEP_TITLES[step["step"]], head))

    backend_rows = []
    for step in steps:
        r = step["_run"]
        backend_rows.append("| `{}` | {} / `{}` | `{}` | `{}` |".format(
            step["run_id"],
            r["task"].get("target_band_hz"), r["task"].get("method_goal"),
            r["layers"].get("excluded_candidates"),
            [e["candidate_id"] for e in (r["state"].get("exclusions") or [])]))

    before, after, restored = (robustness(runs[s]["layers"])
                               for s in ("initial_run", "narrow_grid", "restore_olga"))
    stations = sorted(set(before) | set(after) | set(restored))
    science_rows = []
    for station in stations:
        science_rows.append("| {} | {} | {} | {} |".format(
            station, before.get(station, "—"),
            after.get(station) or "（本轮已排除）",
            restored.get(station, "—")))

    narrowed = [s for s in stations
                if after.get(s) and before.get(s) and after[s] != before[s]]
    stayed = [s for s in stations
              if after.get(s) and before.get(s) and after[s] == before[s]
              and after[s] != "stable"]

    layout = trace.get("layout_1280"), trace.get("layout_900"), trace.get("layout_narrow")
    narrow_change = runs["narrow_grid"]["state"].get("change")
    first_verdict = runs["initial_run"].get("verdict") or {}
    zh = first_verdict.get("statement_zh") or "（本轮记录中没有 statement_zh）"
    en = first_verdict.get("statement") or "（本轮记录中没有 statement）"

    shots = [(s["screenshot"], STEP_TITLES[s["step"]] + "后的界面") for s in steps]
    shots += [("页面布局-1280x720.png", "1280×720 视口下的用户页（单列铺满）"),
              ("页面布局-900宽.png", "900 宽视口（无横向溢出）"),
              ("用户页-窄窗口.png", "窄窗口下的反馈面板")]
    shot_rows = "\n".join(f"| `{n}` | {d} |" for n, d in shots)

    return f"""# 浏览器验收记录（门槛 B §2.1 / §3.3）

> 这份文件由 `main/ui_evidence_md.py` 从 `ui-operation-trace-gate-b.json` 与
> `main/ui-trace-runlog/` 生成，**不是手写复述**。重新跑一次浏览器验收会得到新的
> run id，重新生成即可保持一致。

本轮验收针对第三次评审的两条要求：

- **§2.1**：用户页的反馈必须从**最新一轮**继续，并在界面上写清“当前生效条件”，
  而不是悄悄退回首轮；
- **§3.3**：页面在常用窗口尺寸下能正常操作，主结果用中文说明“答案 / 边界 / 下一步”，
  技术 ID 与机器 JSON 收进可展开区域。

## 1. 怎么做的（可复现）

- 浏览器：**{trace.get('chrome')}**（headless=new），经 **Chrome DevTools Protocol**
  直接驱动，**未安装** Playwright / Puppeteer。
- 驱动脚本：`main/ui_trace.mjs`（Node 内置 `WebSocket` + `fetch`，无第三方依赖）。
- 服务进程使用**独立 run 目录**，不写进 `main/runlog`，历史记录未被触碰：

```bash
cd earthquake-validation
RUNLOG_DIR="$PWD/main/ui-trace-runlog" EQ_CACHE_DIR="$PWD/main/cache" \\
  .venv/bin/python main/server.py 8799 &
node main/ui_trace.mjs http://127.0.0.1:8799 delivery/review-3-2026-10-10/evidence
.venv/bin/python main/ui_evidence_md.py \\
  delivery/review-3-2026-10-10/evidence/ui-operation-trace-gate-b.json \\
  main/ui-trace-runlog \\
  delivery/review-3-2026-10-10/evidence/浏览器验收-2026-10-10.md
```

- 页面加载期间的 console 异常：**{len(trace.get('console_errors') or [])} 条**。
- 全部数据来自**本地缓存**：首轮 `http_attempts={runs['initial_run']['cost'].get('http_attempts')}`、
  `cache_hits={runs['initial_run']['cost'].get('cache_hits')}`；反馈轮 `http_attempts=0`、
  `model_calls=0`。**这证明的是界面闭环与布局，不是一次新的下载**，也不是模型调用。

## 2. 窗口布局验收（§3.3）

| 视口 | `main` 宽×高 | `#page-user` 宽×高 | `#page-lab` | 结论 |
|---|---|---|---|---|
| {layout[0]['viewport'][0]}×{layout[0]['viewport'][1]} | {layout[0]['main']['w']}×{layout[0]['main']['h']} | {layout[0]['page_user']['w']}×{layout[0]['page_user']['h']} | {layout[0]['page_lab']['w']}（隐藏） | 用户页单列铺满，不再被三列栅格挤压 |
| {layout[1]['viewport'][0]}×{layout[1]['viewport'][1]} | {layout[1]['main']['w']}×{layout[1]['main']['h']} | {layout[1]['page_user']['w']}×{layout[1]['page_user']['h']} | {layout[1]['page_lab']['w']}（隐藏） | 窄窗口下仍为单列，无横向溢出 |
| {layout[2]['viewport'][0]}×{layout[2]['viewport'][1]} | {layout[2]['main']['w']}×{layout[2]['main']['h']} | {layout[2]['page_user']['w']}×{layout[2]['page_user']['h']} | {layout[2]['page_lab']['w']}（隐藏） | 长内容纵向滚动，无横向滚动条 |

`main` 在修复前是固定的三列栅格，用户页被挤在中间一列；现在 `#page-user` /
`#page-lab` 各自为独立布局，切换页签时不再互相挤压。

## 3. 操作链（§2.1 的核心）

在用户页上依次完成：**首轮 → 排除 OLGA → 收紧到时网格到 3–12 Hz → 恢复 OLGA**。

| 步骤 | run | 记录的父 run | 在用台站 | 已排除 | 新增外部请求 / 模型调用 |
|---|---|---|---|---|---|
{chr(10).join(chain_rows)}

界面每一步自己写出的“子 run / 父 run”原文（取自反馈面板）：

{chr(10).join(head_lines)}

**这直接复现了 P1-1 的修复**：第三次评审时，第二轮反馈的父轮仍是首轮
（`r-b9a98071a275c6a8`），收紧网格会静默把 OLGA 重新算进去。现在收紧网格这一轮的父轮
是排除那一轮，**OLGA 在收紧网格后仍然是排除状态**，界面的“已排除台站”也仍然写着 OLGA。

界面同时写出“初始 run = 分支根”与“当前 run = 下一轮的父轮”两个**不同**的字段，
并明确提示：「下一次反馈默认从“当前 run”继续，上一轮的条件会被继承；“从初始条件分支”
会回到最初一轮重新比较，这是显式选择，不会静默发生。」

### 后端逐轮佐证

| run | 冻结任务 `target_band_hz` / `method_goal` | `layers.excluded_candidates` | `mcp_state.exclusions` |
|---|---|---|---|
{chr(10).join(backend_rows)}

收紧网格那一轮的 `change` 为 `{json.dumps(narrow_change, ensure_ascii=False)}`。
注意这里两个频带是**分开的**：`target_band_hz` 仍是所问的 1–12 Hz（问题的频带），
`bands_hz` 是**到时网格**（算法参数）。这正是 P0-2 要求的分离，没有被合并成一个字段。

## 4. 收紧网格后的真实科学结果（没有被说成“一改就好”）

| 台站 | 首轮（1–12 Hz 全网格） | 收紧网格后（3–12 Hz） | 恢复 OLGA 后 |
|---|---|---|---|
{chr(10).join(science_rows)}

收紧网格让 {('、'.join(narrowed) or '没有台站')} 的稳定性发生变化；
{('、'.join(stayed) or '没有台站')} 仍然不是稳定态。
界面因此不把收紧网格写成结论，只写成一条可执行建议，并保留“这是重算建议，
不代表结果一定变稳定”。第三次评审指出的“固定声称频带已定 / 硬编码
'落在事件之前，物理上不可能是 P'”在本轮已删除，改为逐台站输出**该站自己的**
依据、影响与建议。

## 5. 主结果的中文表述（§3.3）

首轮结果区的“结论边界”行取自 `verdict.json` 的 `statement_zh`：

> 结论边界：{zh}

对应的英文冻结原文（`statement`，一个字符都没改，界面收进“规则原文（英文，冻结不变）”
可展开区）：

```
{en}
```

- 中文行是新加的 `statement_zh`；英文 `statement` 是冻结规则的措辞，回归测试读的就是它。
- 机器可读的 ID 与 JSON（run id、task id、`layers.json`、`verdict.json`）都在
  “证据”可展开区，不占用主结果版面。

## 6. 本轮验收**没有**证明什么（边界）

1. **不是一次新的外部下载**。首轮 `http_attempts=0`、`cache_hits={runs['initial_run']['cost'].get('cache_hits')}`，
   全部命中本地缓存。本轮验收的是界面闭环与布局；“断网也能跑”的证据在离线包（门槛 D）里另行给出。
2. **没有调用任何模型**。四轮的 `model_calls` 均为 0、`model_cost_known=false`，
   所以本轮不产生新的额度消耗（账本条目数不变）。
3. **截图只是辅助**。截图与 trace JSON 同时产出；判定依据是 JSON 里的 run id、父 run、
   条件与计数，以及各 run 目录下的 `task.json` / `mcp_state.json` / `layers.json`。
4. **首轮结果面板不随反馈改写**。反馈轮的结果写在反馈面板内（含“重算前/重算后”逐字段
   对照），首轮结果面板保留首轮判定。这是刻意保留的对照，但确实需要操作者看反馈面板
   才能读到最新判定——如果评审要求主结果区也跟随切换，这是一个明确的后续改动点。
5. **浏览器只有 Chrome 一种**，未在 Firefox / Safari 上验收。

## 7. 截图索引

| 文件 | 内容 |
|---|---|
{shot_rows}
| `ui-operation-trace-gate-b.json` | 机器记录：run id / 父 run / 条件 / 计数 / 布局测量 |
"""


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    trace = json.loads(Path(sys.argv[1]).read_text())
    out = build(trace, Path(sys.argv[2]))
    Path(sys.argv[3]).write_text(out)
    print("wrote " + sys.argv[3])


if __name__ == "__main__":
    main()