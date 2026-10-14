# 震迹最新提交状态（2026-10-14）

- 报名与成员信息已由本人确认并填入材料：本科生组，U105，队伍“包小派派派”，王新宇为队长及唯一成员，苏州大学软件工程。
- 当前提交包为 `package/本科生组-U105-包小派派派-王新宇.zip`，已包含最新前后端源码、AGH 构建所需源文件与锁定文件、精选真实核查记录及材料；实际大小、文件数与校验值见包内 `MANIFEST.json` 及包外最终核对记录。
- 2026-10-14 独立解压回放核验 11 项通过，覆盖包内模块隔离、health、demo-guide、样例、5 条记录、首轮和反馈证据及三分量波形、构建源文件、安装产物排除、前端文件与当前源码一致性。网络请求、模型调用、凭据读取均为 0；复用现有 Python 依赖，未执行完整干净依赖安装、AGH 构建或新增收费调用。记录：`package/独立回放核验-2026-10-14.json`。
- 从解压后的 `zhenji-package/` 启动回放时，设置 `RUNLOG_DIR="$PWD/replay/runlog" EQ_STRICT_OFFLINE=1 ./main/start.sh`；脚本需要 `/bin/zsh`，也可保留同样环境变量并改用 `.venv/bin/python main/server.py 8791`。
- 基本报名信息已补齐；声明本人确认、3–5 分钟视频及有效链接、真实公开内容发布平台/链接/页面截图/日期仍待完成。2026-10-14 再核官方指南：成员独立完成确认与来源说明是提交要求，现有文本未明确手签或电子签方式；确认形式按实际提交页面执行。完整干净依赖安装与 AGH 构建仍未复验；本轮没有新增模型费用。

**以下全文是 2026-10-12 的历史交接记录，保留当时的包名、大小、费用快照与待办状态；阅读当前交付状态以本节及最新提交字段清单为准。**

# 震迹参赛收尾 HANDOFF（2026-10-12，M1–M5，不含视频录制）

对象：[参赛收尾执行与验收方案](../../reports/参赛收尾执行与验收方案-2026-10-12.md) 与
[给执行AI的震迹参赛收尾启动指令](../../reports/参赛收尾执行交接指令-2026-10-12.md)。
按要求**排除视频录制**（M4 的视频部分未做），其余 M1/M2/M3/M4(公开稿)/M5 已完成。

## 0. 结论

三视图工作台已落地并通过三尺寸实际渲染检查；M3 提交文档齐备（含同源 PDF）；公开稿与 4 张真实截图已备；候选提交包 6.7 MB 已生成并验证 ≤200MB、无 Key。真实报名信息、视频链接、公开链接按官方要求保留 `待填写/待发布`，未编造。全程未新增模型调用（费用上界仍 17.464664/20 元）。

## 1. M1 展示接线（已验收）

| 缺陷 | 处理 | 证据 |
| --- | --- | --- |
| 反馈区旧文案“不调用模型” | 旧两页签页整页替换为三视图工作台，反馈区明确“真实 Agent 反馈会消耗模型额度”并默认禁止补取 | `main/static/index.html`、`ui_check.json` |
| 默认规则模式、Agent 入口不突出 | 新核查默认 `agent`；“查看已验收 AGH 示例”为一等入口，默认只读回放 | `ui_check` `default_is_workbench_replay` |
| Agent 反馈结论存放 `delivered.verdict` 漏显示 | 前端统一 `normalize(evidence)`，verdict/字段值/接受/后备/审计/停止依据都从 delivered 与 finish_audit 读取 | `ui_check` `conclusion_rendered` |
| 失败轮询未展示真实结果 | 失败也按本次 run 证据渲染失败横幅 + `failure_kind` + cleanup；不用历史结果顶替 | `ui_check` `failure_state_present` |
| 缺字段只在只读接口补 | `/api/evidence` 增 `requested_conditions/actual_conditions/finish_audit/agent_config/condition_applications/cleanup/diagnostic_parameters/native/parent/fixture`；`/api/runs` 增 delivered/fallback/profile；`/api/health` 区分“观测离线/模型可用” | `main/server.py` |

## 2. M2 UI 与视觉（已验收）

- 布局：深蓝品牌栏 + 主导航（核查工作台/运行记录/项目说明）；桌面 320px 任务栏 + 结果区，最大宽度 1440px；窄屏单列。
- Token：一套 `:root` 变量（#F5F7FA/#FFFFFF/#102A43/#142B3C/#52657A/#DCE5EE/#2563EB/#0E7490/#157A55/#A16207/#B42318），正文 16px、结论 26px、按钮 44px、面板 12px 圆角；`prefers-reduced-motion` 处理。
- 真实波形：`/api/waveform/<run_id>/<candidate_id>` 读**同 run** 已落盘 MiniSEED，画 Z/N/E min-max 包络，标“各分量独立归一化”；到时标记只来自该 run 实际结果，未触发时明确说明；已排除站标注。
- 工具时间线：把 `tool_calls.jsonl` 的 start/ok 配对为每工具一行（不再重复），系统条件守卫（`condition_applications`）单独标注“不是模型决策”。
- 条件前后：首轮→本轮改动→当前 三列对比；展示实际诊断网格与 `n_valid/grid_size`，明确“无新增取数、复用同源波形”。
- 无假进度：运行中只显示“真实 AGH 会话执行中…”，完成后加载真实时间线。
- 实际渲染检查：`ui/ui_check.json` **33/33 通过**，含 1280×800、1440×900、390×844 的**无横向溢出**与**无页面错误**；截图在 `ui/screenshots/`（workbench×3、feedback-compare、fallback、failure、legal-unknown、records、about）。

## 3. M3 文档与证据（已交付）

目录 `documents/`：`项目说明.md`(+`项目说明.pdf` 5 页同源)、`README.md`（并覆盖仓库根 README）、`运行与复现指南.md`、`AGH运行与验证证据索引.md`、`模型与第三方来源清单.md`、`独立完成声明-待确认.md`、`提交字段与链接清单.md`。

- SQLite 归档修正：`main/native_archive.mjs` 用 `VACUUM INTO`（退回 `wal_checkpoint(FULL)`+复制）生成一致 `native_sessions.db` 快照；`tests/test_native_archive.py` 2 项通过，证明归档包含 WAL 中未 checkpoint 的行。
- 资源报告标签修正：`driver_pid_exited` 与 `daemon_pid_exited` 分列；集成复跑 8/8。
- 文档中的 UI 描述已按新工作台更新（删除“仍是旧两页签”的待确认项），归档缺口说明改为“已修 + 历史旧链如实注明”。

## 4. M4 公开稿（已备，视频未录制）

- `public/公开发布稿.md`：标题、约 520 字正文、三项能力、4 张真实截图、AGH 结构图、入口/链接表、发布记录（平台/链接/日期待填写）、边界声明。
- `public/screenshots/`：结论+波形、AGH 工具与证据、证据详情、条件前后对比。
- **未录制 3–5 分钟视频**（按要求排除）；字段清单保留“待录制/待发布”。

## 5. M5 提交包（候选已生成）

- `package/build_package.py` → `package/震迹-提交包-待填写命名.zip`：**6.7 MB 压缩 / 14.5 MB 解压 / 1450 文件**，≤200MB。
- 含可运行源码、AGH 源码+lockfile+溯源、文档、公开与 UI 证据、`replay/runlog/` 只读回放（首轮/反馈/三条被拒）、脱敏 `费用快照.json`；不含 `.venv`/`node_modules`/全量 runlog/缓存/视频/Key（已用 `sk-[0-9a-f]{24,}` 正则核查无泄漏）。
- `package/提交包说明与打包指南.md`：最终命名（组别-参赛编号-队伍名称-队长姓名）、提交前三件事、校验命令。

## 6. 验证命令与结果

```sh
cd earthquake-validation
node delivery/finalization-2026-10-12/ui/ui_check.mjs          # 需先起服务（见下）
.venv/bin/python -m pytest tests/ -q                            # 409 passed
.venv/bin/python -B main/agent_optimization_gates.py --output /tmp/gates.json   # all_required_pass
.venv/bin/python -B delivery/finalization-2026-10-12/package/build_package.py   # 生成候选包
```

| 检查 | 结果 |
| --- | --- |
| UI 渲染检查（含三尺寸、四状态、对比、无溢出） | 33/33 |
| 既有回归 | 409 passed |
| 优化门 G01–G16 | all_required_pass |
| 候选包 | 6.7 MB ≤ 200 MB，无 Key |
| 项目说明 PDF | 5 页 |

## 7. 本会话修改/新增文件

- 改：`main/static/index.html`、`main/static/style.css`、`main/static/app.js`、`main/server.py`、`README.md`。
- 新增：`delivery/finalization-2026-10-12/` 下 `ui/`（检查脚本+JSON+截图）、`documents/`、`public/`、`fixtures/`（legal-unknown fixture）、`package/`（构建脚本+候选包+指南）、本 HANDOFF。
- 并行子任务（同目录）：`main/native_archive.mjs`、`tests/test_native_archive.py`、`tests/native_archive_probe.mjs`、`delivery/remediation-2026-10-12/evidence/agh_resource_integration.*` 的 PID 标签修正。

## 8. 模型费用

未新增任何模型调用：加载示例、切换记录/视图、免费解析、截图与检查都不触发模型；UI 检查中的 fixture 为生产 `validate` 的手工主张。台账仍 1317 条、累计上界 17.464664/20 元。

## 9. 剩余项（需用户/后续）

1. 真实报名字段（组别/学校/专业/参赛编号/队伍名称/队长/成员）—— 待填写。
2. 3–5 分钟视频（含链接）—— 按要求本轮未录制。
3. 公开内容平台/链接/截图/发布日期 —— 待发布。
4. 用真实命名重命名候选 ZIP。
5. 独立拷贝中从零启动 + 完整回放的复现记录 —— 待确认。
6. 历史旧驱动 `/tmp/eq-main-*` 残留 daemon/worker 建议按各自 home 官方 `daemon stop` 清理（未全局杀进程）。
