# AGH 运行与验证证据索引

路径说明：下文 `main/runlog/<run_id>/` 是研发机原始记录位置；提交包精选的五条记录位于 `replay/runlog/<run_id>/`。按运行指南设置 `RUNLOG_DIR` 后即可只读回放。被拒反馈的历史父记录不在精选回放中，相关事实可核对其归档记录与汇总报告；正常首轮及其反馈父子链均完整包含。

本索引只收录工作区内真实存在、可打开核对的文件。来源严格分为三类，**不混作真实成功率**：

- **真实模型执行**：真实 `deepseek-flash` 经 AGH 执行、真实 MCP 工具与真实观测。
- **规则/离线探针**：确定性验证器与规则路径，`network_or_model_calls=0`，不调用模型、不取数。
- **mock**：仅把上游模型传输替换为 mock，AGH daemon、MCP、预算/工具守卫与资源所有权仍是真实的。

## 0. 场景总览

| 场景 | run id / 证据文件 | 来源 | 一句话预期/实际 |
| --- | --- | --- | --- |
| 正常首轮 | `r-e2e8419bf299785d` | 真实模型 | 取得 1 站、跑专业检查、交付被接受、无规则后备 |
| 条件反馈 | `r-3ae9c6948c2f8967` | 真实模型 | 复用父波形、按新网格实际重算、新取数 0、交付被接受 |
| 合法未知 | `delivery/remediation-2026-10-12/evidence/A_B_C_probe_rerun.json` → `positive_unique_excluded_legal_unknown` | 规则/离线探针 | 排除唯一站后正确报未知并被接受 |
| 受控失败 | `delivery/remediation-2026-10-12/evidence/agh_resource_integration.json` → `scenarios.timeout`（run `r-timeout`） | mock | 会话超时→`status=failed`、`failure_kind=timeout`，资源仍被回收 |
| mock 资源集成 | `delivery/remediation-2026-10-12/evidence/agh_resource_integration.json` | mock | 干净输入 + 真实 daemon/MCP，8/8 通过，其他会话不受影响 |
| 被拒主张→规则后备 | `r-2365883191ac74a8`、`r-8f2eb7e2a2e7333f`、`r-8c01734ee9f1903a` | 真实模型 + 规则后备 | 模型结论与证据冲突→`fallback_used=true`，原主张与拒绝原因保留 |

---

## A. 真实模型执行

### A1. 正常首轮 `r-e2e8419bf299785d`

| 项 | 内容 |
| --- | --- |
| 来源 | 真实模型 `deepseek-flash`（DeepSeek），经 `main/provider_bridge.mjs` 回环桥接；driver `agent_run_optimization.mjs`，profile A1 |
| 问题 | 这段观测能不能用来做 1–12 Hz 的 P/S 拾取？ |
| 条件 | 时窗 2023-12-24T10:00:00–10:02:00 UTC；UW.CHIMA；目标频带 1–12 Hz；方法频带 1–8 Hz；诊断网格 [[1,8],[3,12],[5,20]]；允许取数 |
| 步骤（真实工具时间线） | `task_status(progress)` → `list_candidates(limit=20)` → `acquire_station(409dfad13970)` → `get_evidence()` → `run_onset_check(409dfad13970)` → `run_stability_check(409dfad13970)` → `task_status(finish)` |
| 预期 | 取得观测、形成专业证据，验证器接受交付 |
| 实际 | 取得 1 站（ENE/ENN/ENZ，100 Hz，0 gap）；`run_onset_check` 返回 `onset_utc=null`（无到时）；`run_stability_check` 网格 13 配置仅 1 个有效（ar_aic_3c 约 2.53 s），`stability=insufficient_configurations`；`model_claim_accepted=true`、`fallback_used=false`、`origin=agent_validated`；`status=succeeded`；费用上界 0.100140 元（7 条）；`logical_acquisitions=1`、`http_attempts=0`、`cache_hits=2`；`cleanup` `stop_confirmed=true`、`root_removed=true` |
| 对应文件 | `main/runlog/r-e2e8419bf299785d/`（`task.json`、`run.json`、`validation.json`、`tool_calls.jsonl`、`mcp_state.json`、`cost.json`、`result.json`、`cleanup_status.json`、`native_agh_evidence.json`、`model_response.json`、`prompt_binding.json`）；汇总：`delivery/remediation-2026-10-12/evidence/real_page_chain_v2.json` |

校验事实：`acceptance_checks` 全部为 true；证据引用 `quality/applicability/onset/stability` 全部 `resolved`；原生 AGH 事件对 `native_tool_pair.call_seq=17`、`result_seq=22`，`result_origin=tool:mcp_seismic_review_..._task_status`；策略 SHA `83e61308df2fcfe76994ed3ef1fc2a621f3ceee7261ab0e6b289186a132d6531`。

### A2. 条件反馈 `r-3ae9c6948c2f8967`

| 项 | 内容 |
| --- | --- |
| 来源 | 真实模型 `deepseek-flash`，继承父配置（`inherited_from_parent=true`，`inheritance_decision=inherited_from_parent_config`） |
| 父 run | `r-e2e8419bf299785d`（`run.json` 的 `parent_run_id` 一致） |
| 条件变更 | 诊断网格收紧为 [[3,12]] Hz，`acquisition_allowed=false`，`requested_changes=[{kind: stability_bands, ...}]`；`condition_applications` 记录 `actor=deterministic_guard` |
| 步骤 | `task_status(progress)` → `get_evidence()` → `run_stability_check(409dfad13970)` → `get_evidence()` → `task_status(finish)` |
| 预期 | 复用父观测、不新增取数、按新网格实际重算 |
| 实际 | 使用同候选与同波形 SHA-256（`1670a2e99a5bb723dbe9ee2870ba716e430fe9e189919d429031fd67cb223f15`）；`logical_acquisitions=0`、`http_attempts=0`、`cache_hits=0`、`bytes_total=0`；稳定性工具实际在 [[3,12]] 上运行（4 个 stalta_z 配置无到时，ar_aic_3c 约 2.53 s，`n_valid=1`，`stability=insufficient_configurations`）；证据引用 quality/applicability/stability，**未**重算 onset；`model_claim_accepted=true`、`fallback_used=false`；`status=succeeded`；费用上界 0.081032 元（5 条）；`cleanup` 正常 |
| 对应文件 | `main/runlog/r-3ae9c6948c2f8967/`（同上清单）；汇总 `real_page_chain_v2.json` 的 `feedback_*` 字段；原生事件对 `call_seq=18`、`result_seq=23` |

**注意**：`输入适用性`（`delivered.verdict=pick_applicable`，输入条件满足）与 `到时诊断稳定性`（`stability=insufficient_configurations`）是两个字段，不能合并叙述；`spread_s=0.0` 不构成“稳定”，因为有效配置少于 3 个。

### A3. 被拒主张→规则后备（保留的真实失败反馈）

来源：真实模型，三条均为对父 run `r-40183b72e23b844f` 的真实反馈尝试；均真实执行了请求的网格工具、未新增取数，但模型把“诊断网格不稳定”误当作方法适用性结论，被验证器拒绝。

| run id | 模型主张 | 停止依据 | 验证结果 | 费用上界 |
| --- | --- | --- | --- | --- |
| `r-2365883191ac74a8` | `pick_unknown` | `unanswered_no_data` | `model_claim_accepted=false`、`origin=fallback`、`fallback_used=true`；拒绝原因“the model stated `pick_unknown` but the evidence supports `pick_applicable` (input_conditions_met)” | 0.080088 元（5 条） |
| `r-8f2eb7e2a2e7333f` | `pick_partially_applicable` | `public_criterion_satisfied` | 同上被拒，`origin=fallback` | 0.063608 元（4 条） |
| `r-8c01734ee9f1903a` | `pick_unknown` | `scope_exhausted` | 同上被拒，`origin=fallback` | 0.09698 元（6 条） |

三条 `result.json` 均为 `status=succeeded`（指进程/会话成功），`logical_acquisitions=0`、`http_attempts=0`；对应文件 `main/runlog/<run_id>/validation.json`，报告 `delivery/remediation-2026-10-12/evidence/real_page_chain.json`、`real_page_feedback_retry.json`、`real_page_feedback_retry2.json`。**这些失败记录未被删除，也未改记为成功。**

### A4. 补充真实首轮 `r-2c9c465f11a1fa7b`

| 项 | 内容 |
| --- | --- |
| 来源 | 真实模型 `deepseek-flash`，profile A1，`analysis_level=quality_only` |
| 步骤 | `task_status(progress)` → `list_candidates` → `acquire_station(23c5d4ea56c2)` → `get_evidence()` → `task_status(finish)`（**未**调用 onset/stability） |
| 实际 | `can_finish=true`；`cleanup` `stop_confirmed=true`、`root_removed=true`；据 `delivery/remediation-2026-10-12/HANDOFF.md` §3，该轮 `accepted=true`、仅用 quality/applicability 引用 |
| 对应文件 | `delivery/remediation-2026-10-12/evidence/artifacts/real-first-round-standalone/`（`agent_config.json`、`task.json`、`tool_calls.jsonl`、`mcp_state.json`、`cleanup_status.json`、`native_agh_evidence.json`） |

---

## B. 规则 / 离线探针（无模型、无网络）

来源统一为确定性验证器与规则路径，证据文件 `network_or_model_calls=0`。

### B1. 合法未知 `positive_unique_excluded_legal_unknown`

| 项 | 内容 |
| --- | --- |
| 来源 | 规则/离线探针（`delivery/remediation-2026-10-12/evidence/A_B_C_probe_rerun.json`） |
| 步骤 | 排除范围内唯一参与站，禁止新增取数，提交空范围 `pick_unknown` + `scope_exhausted` |
| 预期 | 接受为“正确未知”，可结束 |
| 实际 | `accepted=true`、`fallback_used=false`、`audit_outcome=legal_unknown`、`can_finish=true`、`stop_bases=["scope_exhausted"]`、`scope_valid=true`、`evidence.all_valid=true`（空范围无引用） |

### B2. 受控提前停止被拦截 `unstarted_explicit_scope_unknown`

| 项 | 内容 |
| --- | --- |
| 来源 | 规则/离线探针（同上 JSON） |
| 步骤 | 指定 CHIMA、候选可用且允许取数，但尚未取证即提交空范围 + `scope_exhausted` |
| 预期 | 拒绝；仍有可取候选时不得结束 |
| 实际 | `accepted=false`、`outcome=strategy_insufficient`、`can_finish=false`、`remaining_acquirable=["55881be1192c"]`、`scope_valid=false`、原因 `empty_scope_but_actions_remain` |

### B3. 其它规则反例（均被拒）

| 场景键 | 预期 | 实际 |
| --- | --- | --- |
| `negative_uncomputed_onset` | 引用未计算的到时应被拒 | `evidence_refs_resolve=false`，`unverified=["onset:55881be1192c"]` |
| `negative_fabricated_scope` | 虚构范围应被拒 | `scope_valid=false` |
| `negative_missing_stop_code` | 缺停止依据应被拒 | `stop_basis_legal=false` |
| `negative_excluded_reference` | 引用已排除观测应被拒 | `all_valid=false`，`unverified=["quality:55881be1192c"]` |

### B4. 上限与子任务一致性（规则）

| 场景键 | 实际 |
| --- | --- |
| `positive_real_three_of_three_stop` | 真实累计 3 次、上限 3：`outcome=legal_unknown`、`can_finish=true`、`stop_basis=resource_limited`（源 run `r-c8f9ca0979c35163`） |
| `feedback_default_and_cap` | parent/child task/state cap 均为 3；默认反馈禁止新增取数，越界候选 `http_issued=false` |
| `feedback_cap_with_explicit_permission` | 显式开启取数后第 4 个候选仍被候选上限拒绝（`reason=candidate_ceiling`，`http_issued=false`） |
| `product_launch_parameters_no_driver_launched` | 启动参数正确（driver `agent_run_optimization.mjs`，A1；反馈继承父配置），**未**因启动而发起真实运行 |

### B5. A/B/C/D 免费反例总表

| 证据文件 | 来源 | 结果 |
| --- | --- | --- |
| `delivery/remediation-2026-10-12/evidence/remediation_probe.json` | 规则/离线探针（`network_or_model_calls=0`） | 20/20 通过 |
| `delivery/remediation-2026-10-12/evidence/A_B_C_probe_rerun.json` | 规则/离线探针（`network_or_model_calls=0`） | 阻塞关闭、3 正例通过、3 反例被拒、cap 同步 |

---

## C. mock（上游模型替换；AGH daemon/MCP 真实）

### C1. mock 资源集成 `agh_resource_integration.json`

| 项 | 内容 |
| --- | --- |
| 来源 | mock 上游（`upstream_is_mock=true`、`network_or_model_calls=0`）；真实 AGH daemon + MCP + 预算/工具守卫 |
| 输入 | 每场景**新种干净输入**（fresh task/state/discovery），不复制历史 owner；sentinel 记录另一会话原 PID/socket |
| 步骤 | 运行 `normal`：会话执行、真实 MCP `task_status` 工具调用、官方停止确认、资源移除；运行 `timeout`：会话超时、官方停止确认、资源移除 |
| 预期 | 正常成功且资源全回收；超时失败但资源仍回收；其他会话 PID/socket 不变 |
| 实际 | `checks` 8/8：`normal_succeeded`、`normal_resources_reclaimed`、`normal_owner_is_this_run`、`normal_native_evidence_saved`、`normal_real_tool_call`、`timeout_failed`、`timeout_still_reclaimed`、`other_session_unaffected` 全 true；sentinel `same_pid=true`、`same_socket=true` |
| 对应文件 | `delivery/remediation-2026-10-12/evidence/agh_resource_integration.json`；逐场景原生材料 `delivery/remediation-2026-10-12/evidence/artifacts/normal/`、`artifacts/timeout/` |

正常场景细节：run `r-normal`，`record_status=succeeded`，`owner_pid_exited=true`，`socket_removed=true`，原生工具对 `call_seq=17`/`result_seq=21`（`result_origin=tool:mcp_seismic_review_..._task_status`）。

### C2. 受控失败（超时）

| 项 | 内容 |
| --- | --- |
| 来源 | mock 上游，真实 AGH 生命周期 |
| 步骤 | run `r-timeout`：会话被 4000 ms 超时取消（`session_timeout_ms=4000`、`session_timed_out=true`），随后执行完整清理 |
| 预期 | 明确失败状态 + 资源被回收，不用旧结果顶替 |
| 实际 | `status=failed`、`failure_kind=timeout`、`answer=null`、`model_requests=1`、`tool_proposals=0`；`cleanup_ok=true`、`stop_confirmed=true`、`root_removed=true`、`cleanup_errors=[]`；`owned_home_removed=true` |
| 对应文件 | `delivery/remediation-2026-10-12/evidence/agh_resource_integration.json` → `scenarios.timeout`；`delivery/remediation-2026-10-12/evidence/artifacts/timeout/agent_execution.json`、`cleanup_status.json` |

---

## D. 对应关系与边界说明

- 本索引中只有 A 组（含 A1–A4）是真实模型执行；B 组为规则/离线探针；C 组为 mock 上游（AGH 与 MCP 真实）。**三条被拒的真实反馈属于真实模型执行 + 规则后备**，不是 mock，也不是规则生成。
- `legal_unknown` 的规则正例使用匿名候选 `55881be1192c`，与真实链的 CHIMA 候选 `409dfad13970` 不同，不能互相替换叙述。
- 现有记录不能用于宣称总体成功率、准确率或“Agent 优于规则”；本索引只说明各场景的身份、来源、步骤与预期/实际是否一致。
