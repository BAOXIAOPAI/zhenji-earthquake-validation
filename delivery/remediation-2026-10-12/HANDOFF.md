# 参赛主体整改 HANDOFF（2026-10-12，含 review-9 补齐）

对象：`reports/参赛主体最终整改方案-2026-10-12.md` 的 A/B/C/D 四包，以及
`delivery/review-9-2026-10-12/主体验收结论与最后补齐事项.md` 的§3/§4/§5。

## 0. 结论

review-9 的唯一功能阻塞（0 取证被当作范围耗尽）已关闭，§5 的四项同步/证据修正已完成，§4 的真实首轮＋一次反馈闭环已跑通：两条 run 均由真实模型经 AGH 执行工具、**model_claim_accepted=true、fallback_used=false、cleanup 正常**。免费回归与独立正反例保持通过。剩余仅是本机遗留的旧驱动残留进程与本轮 mock 排练产生的极小台账条目（已披露，未删历史）。

## 1. §3 停止判断漏洞：关闭

**故障**：请求范围内仍有可取候选、`acquisition_allowed=true`、一次取证都没做时，空 scope + `scope_exhausted` 被接受为合法结束。

**修改**
- `main/execution_conditions.py`：新增 `remaining_scope_candidates(requested, state)`——按请求范围、已尝试集合、已取得集合、已排除集合与当前许可，计算“范围内仍未尝试且允许取得”的候选；并区分 `acquisition_not_allowed`、`candidate_ceiling_reached`、`in_scope_candidates_untried`。
- `main/finish_audit.py`：`_supported_stop_bases` 仅在 `remaining_scope_candidates` 为空时才支持 `scope_exhausted`；仍有可取候选时不再支持，结果为 `strategy_insufficient`、`can_finish=false`。
- `main/run_agent_arm.py`：`_scope_report` 的空 scope 仅在“无参与对象且无可取候选”时合法；仍可行动时返回 `empty_scope_but_actions_remain`，`scope_valid=false`。

**关闭证据**（`evidence/A_B_C_probe_rerun.json`，review-9 同一脚本复跑）
- 0 取证反例：`accepted=False`、`fallback_used=True`、`outcome=strategy_insufficient`、`can_finish=False`、`remaining_acquirable=['55881be1192c']`（关闭条件 1）。
- 正常质量/适用性正例仍 `accepted=True`（关闭条件 2）。
- 唯一站排除、真实 3/3 上限正例仍 `accepted=True`、`legal_unknown`（关闭条件 3）。

## 2. §5 同步与证据修正

| 项 | 修改 | 关闭证据 |
| --- | --- | --- |
| 子 task 候选上限 | `main/prepare_agent.py` `make_task(... max_unique_candidates_episode=child_requested[...])` | `C_child_task_cap_matches_state`；review-9 脚本 `feedback_default_and_cap` 现为 3/3/3 |
| watchdog 身份 | `main/run_agent_arm.py` `_stop_owned_daemon` 校验 `runtime-owner.run_id` 与本 run 一致，不一致返回 `runtime_owner_run_mismatch` 且不执行停止 | `D_watchdog_rejects_owner_run_mismatch` |
| 干净资源探针 | `evidence/agh_resource_integration.mjs` 重写：每场景**新种**干净输入（fresh task/state/discovery），核本次 `runtime-owner.run_id`、本次 driver/daemon PID 退出、本次 socket 消失；sentinel 先记录原 PID/socket 再核同身份 | `agh_resource_integration.json` 8/8：`normal_owner_is_this_run`、`driver_pid_exited`、`daemon_pid_exited`、`socket_removed`、`other_session_unaffected`（same_pid/same_socket） |
| 原生 tool 事件 | `main/agent_run_optimization.mjs` 清理前用 `main/native_archive.mjs` 对 `data/sessions.db` 做一致性快照（`VACUUM INTO`，含 WAL 行），并复制 `data/audit/host.jsonl`；从快照提取一对 `tool/call`＋`tool/result`（session_key、seq、origin）；桥侧摘要改名 `bridge_request_summary.json` 并标注“非原生” | `native_tool_pair`：`call_seq`/`result_seq`/`result_origin=tool:mcp_seismic_review_..._task_status`；`mcp_tool_pair` 为 run 自身 MCP 记录；`tests/test_native_archive.py` 证明归档非空 |

## 3. §4 真实首轮＋一次反馈闭环（真跑）

入口：真实 `main/server.py` 的 `/api/review` 与 `/api/followup`（页面同一 HTTP 合同），真实模型，无 mock。

| | 首轮 | 反馈 |
| --- | --- | --- |
| run_id | `r-e2e8419bf299785d` | `r-3ae9c6948c2f8967` |
| 父 run | — | `r-e2e8419bf299785d`（`parent_run_id` 一致） |
| 状态 | succeeded | succeeded |
| 模型交付 | accepted=true，`fallback_used=false`，`origin=agent_validated` | accepted=true，`fallback_used=false`，`origin=agent_validated` |
| 实际配置 | profile=A1，driver=`agent_run_optimization.mjs`，analysis_level=quality_only | 继承父配置：`inherited_from_parent=True`，`inheritance_decision=inherited_from_parent_config` |
| 条件 | scope=explicit CHIMA，band 1–12，grid 全网格 | grid=`[[3,12]]`，`acquisition_allowed=false`，change=stability_bands |
| 真实工具 | acquire_station / run_onset_check / run_stability_check / get_evidence / task_status | run_stability_check(candidate=`409dfad13970`) **使用请求网格 [[3,12]]** / get_evidence / task_status；无新增取数（logical_acquisitions=0，http_attempts=0） |
| 引用 | quality/applicability/onset/stability | quality/applicability/stability（未重算 onset） |
| 费用上限 | 0.10014 | 0.081032 |
| 原生证据 | 会话 id + tool/call(17)/tool/result(22) pair | 会话 id + tool/call(18)/tool/result(23) pair |
| cleanup | stop_confirmed=true，root_removed=true | 同左 |

报告：`evidence/real_page_chain_v2.json`（含原始模型答案、验收、cost、native pair）。

**如实记录的过程**：
- 反馈条件首次在父 run `r-40183b72e23b844f` 上试了 3 次（`r-2365883191ac74a8`、`r-8f2eb7e2a2e7333f`、`r-8c01734ee9f1903a`）。三次都**真实执行了请求的网格工具**、未新增取数，但模型把“诊断网格不稳定”误当作适用性结论，分别答 `pick_unknown`/`pick_partially_applicable`/`pick_unknown` 而被拒。报告分别在 `evidence/real_page_chain.json`、`real_page_feedback_retry.json`、`real_page_feedback_retry2.json`，未删除。
- 据此在 `main/prompts/agent_policy_v1.txt` 增加一条契约说明（诊断网格结果本身不构成方法适用性判定，按输入条件回答所问字段，把网格不稳定作为 limitation 报告），随后重跑得到上表的通过链。该改动**未提示任何具体标签**，只是把验证器既有语义写进合同。
- 另有一条真实首轮 `r-2c9c465f11a1fa7b`（一次调试误触的真实会话）同样 `accepted=true`，用 quality/applicability 引用、未跑无关到时，已留存于 `evidence/artifacts/real-first-round-standalone/`，可作为“质量/适用性问题不必计算 onset”的真实佐证。

## 4. 预算

- 起始累计上限口径 16.903876 元；真实闭环结束后 **17.464664 元**（20 元硬守卫，未删除历史预留、未改额度、未绕过守卫）。
- 全部付费请求都逐请求 reserve/settle；期间没有出现 `Budget exhausted`。mock 排练误写了极少量台账条目（约 1e-5 元级，provider 记为 deepseek、case_id 为 mock run），未删除，此处披露。
- 免费复现与测试未计费。

## 5. 最终免费验证

| 检查 | 结果 | 报告 |
| --- | --- | --- |
| A/B/C/D 免费反例 | 20/20 | `evidence/remediation_probe.json` |
| review-9 A/B/C 独立脚本复跑 | 阻塞关闭、3 正例通过、3 反例被拒、cap 同步 | `evidence/A_B_C_probe_rerun.json` |
| 干净 AGH 资源集成（真实 daemon+MCP，mock 上游） | 8/8 | `evidence/agh_resource_integration.json` |
| 既有回归 | 407 passed | pytest |
| 优化门 G01–G16 | `all_required_pass: true` | `/tmp/gates-final3.json` |

复跑命令：

```sh
cd earthquake-validation
.venv/bin/python -B delivery/remediation-2026-10-12/evidence/remediation_probe.py
.venv/bin/python -B delivery/remediation-2026-10-12/evidence/A_B_C_probe_rerun.py
node delivery/remediation-2026-10-12/evidence/agh_resource_integration.mjs
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -B main/agent_optimization_gates.py --output /tmp/gates.json
# 真实闭环（会消耗预算）
.venv/bin/python -B delivery/remediation-2026-10-12/evidence/real_page_chain.py
```

## 6. 本轮修改文件

`main/execution_conditions.py`、`main/finish_audit.py`、`main/run_agent_arm.py`、`main/prepare_agent.py`、`main/agent_run_optimization.mjs`、`main/provider_bridge.mjs`（仅测试用重定向）、`main/prompts/agent_policy_v1.txt`（契约说明一条）；探针 `evidence/remediation_probe.py`、`evidence/A_B_C_probe_rerun.py`、`evidence/agh_resource_integration.mjs`、`evidence/mock_provider.mjs`、`evidence/real_page_chain.py`。未改旧交付 `delivery/review-8*`、`delivery/review-9*`。

## 7. 未完成 / 环境注意

- 本机遗留：旧 `main/agent_run.mjs` 产生的 `/tmp/eq-main-*` 目录与 daemon/worker 仍在（约 157 目录、136 daemon），是本轮之前的残留；新路径（`eq-opt-*`）在本轮真跑与集成中均为零残留。建议演示前按各自 home 官方 `daemon stop` 清理，本轮未擅自全局杀进程。
- review-9 §5 提到的“大规模原生档案”未重建：本轮按最小要求保留 `sessions.db`、`host.jsonl` 与一对原生 tool/call+result，足以对应演示 run。
- 主体待接收判定权在评审；本文件只列功能与证据状态。