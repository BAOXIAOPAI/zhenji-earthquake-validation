'use strict';
/* 震迹工作台前端。
   一条小型的展示归一化：首轮、Agent 反馈、规则反馈、持久回放都先转成同一个
   `view` 再渲染，避免每种来源各写一套字段读取。归一化只读已有证据，不改写
   任何结论，也不回填成功字段。 */

const EXAMPLE_FIRST = 'r-e2e8419bf299785d';
const EXAMPLE_FEEDBACK = 'r-3ae9c6948c2f8967';

const VERDICT_LABEL = {
  instrument_artifact: '仪器或通道资料异常',
  no_instrument_anomaly_in_scope: '范围内未见通道异常',
  instrument_unknown: '仪器条件未知',
  coherent_multistation: '多台站共有传播',
  single_station_only: '仅单台站可见',
  not_decidable: '无法判定',
  pick_applicable: '输入条件满足（可做该频带拾取）',
  pick_partially_applicable: '部分台站输入条件满足',
  pick_not_applicable: '输入条件不满足',
  pick_unknown: '输入条件未知（未定）',
  stable: '一致', unstable: '不一致', unstable_bimodal: '不一致（双簇）',
  not_applicable: '不适用（未判断）', no_pick: '无有效拾取',
  no_estimate: '未给出到时（未判断）',
  insufficient_configurations: '有效检查组合不足（未判断）',
  check_failed: '检查失败（未判断）',
};
const TOOL_LABEL = {
  list_candidates: '列出候选台站',
  acquire_station: '获取台站观测',
  run_onset_check: '到时检查（STA/LTA）',
  run_stability_check: '到时稳定性诊断',
  get_evidence: '查看当前观测',
  task_status: '查看核查进展',
  exclude_observation: '排除观测并重算',
};
const GOAL_LABEL = {
  three_component_pick: '三分量 P/S 拾取',
  vertical_onset: '仅垂直分量到时',
};
const CHANGE_LABEL = {
  exclude: '排除台站', include: '恢复台站', band: '目标频带',
  stability_bands: '到时诊断网格', method_goal: '方法目标',
};
const AUDIT_LABEL = {
  determined_answer: '已得到当前问题的确定答案',
  legal_unknown: '当前观测不足以判断',
  strategy_insufficient: '观测不足，或尚未完成必要检查',
};
const STOP_LABEL = {
  public_criterion_satisfied: '已满足判断条件',
  scope_exhausted: '已检查当前范围内全部可用观测',
  observation_budget_reached: '已达到可获取观测数量的上限',
};
const ARM_LABEL = {
  one_shot_model: '单次智能建议', user_feedback_recompute: '按新条件标准核查',
  question: '按问题核查', work: '标准核查',
  adaptive_agent_open: '智能核查',
  adaptive_agent_open_followup: '改条件后的智能核查',
  adaptive_agent_stated: '指定问题的智能核查',
  adaptive_agent_stated_followup: '按新要求智能核查',
  adaptive_agent_a1: '智能核查',
  adaptive_agent_a1_followup: '改条件后的智能核查',
};
const TERMS = {
  'UTC时窗': '要读取的波形开始与结束时间。本页统一用 UTC，按北京时间需减 8 小时；表单中选到的时刻也按 UTC 解释。',
  '台站': '安装地震观测仪器的位置。UW.CHIMA 表示网络 UW 中的台站 CHIMA；多个台站用英文逗号分隔。',
  '命名区域': '一块预先登记的地理范围，系统会在该范围内寻找符合当前时窗和频带的台站。也可以直接指定台站。',
  '目标频带': '你希望使用的振动频率范围，单位 Hz（每秒振动次数）。系统检查采样率和通道能否支持它，不自动保证信号可识别。',
  '到时方法频带': '自动到时检测实际使用的频率范围，可能与目标频带不同；它属于检查条件。',
  '诊断网格': '用多组频带和检查条件重复计算到时，查看结果是否对条件敏感。一致只说明在已检查条件范围内稳定，不证明已识别 P/S 或事件真实。',
  '三分量 P/S 拾取': '三分量记录垂直、南北、东西方向的振动；P/S 拾取是辨认两类地震波到达的时刻。这里先核查输入是否适用，不代表已经识别 P/S。',
  '分析目标': '选择要复核哪种输入条件：三分量 P/S 拾取需要完整三分量，仅垂直到时只要求垂直分量。',
  '核查模式': '智能核查会自动选择观测和分析步骤，产生智能核查费用；其他方式采用标准核查的预设方法检查，免费。开始时会自动检查填写内容；确认条件可选，不产生费用，也不获取观测。',
  '规则后备': '智能建议与观测不符，或缺少必要检查时，当前结果采用观测核查结论。页面会说明当前结果来自哪种方法。',
  '证据不足': '在允许的观测范围或费用限制内仍无法判断。它既不是“没有事件”，也不是正面结果；可以查看本次结束原因和未满足条件。',
  'Agent': '智能分析根据问题选择要查看的观测和分析步骤。每一步的结果都会与当前条件和观测核对，页面展示实际完成的检查。',
  '到时': '检测方法报告的信号起跳时刻。本页自动标记来自 STA/LTA 检查，不应直接当成已确认的 P 波或 S 波。',
  '包络': '每小段波形用最小值和最大值表示，便于在有限画布里观察变化。显示去均值并按分量独立归一化，不能比较各台站的真实幅度。',
  '方法适用性': '只检查当前采样率、通道和任务目标是否满足方法输入条件。满足不代表已经拾取 P/S，也不代表确定有地震。',
  '通道条件': '核对观测分量、采样率与仪器信息等输入条件。异常标记只描述当前证据中的问题，不自动认定仪器故障。',
  '停止依据': '本次为何结束：已满足判断条件，或已检查全部允许的观测，或已达到观测数量和费用限制。后两种情况下仍可能无法判断。',
  '交付来源': '说明当前结果来自智能建议，还是观测核查。智能建议只有与当前观测和条件一致，才会作为本次结论。',
  '新增取数': '是否允许从公开数据源重新获取观测。更改条件时默认使用已有波形；即使没有重新获取观测，智能核查也会产生费用。',
  '当前排除': '被你排除的观测仍保留在记录中，但不参与当前结论；恢复后需要重新复核。',
  'Z/N/E': 'Z 为垂直方向，N 为南北方向，E 为东西方向。图中三行分别归一化，±1 是显示参考，不是物理幅度单位。',
  'STA/LTA': '比较短时间窗与长时间窗内信号强度的检测方法。达到触发条件时报告自动起跳，结果可能受参数或噪声影响。',
  'MCP': '查看本次实际完成的观测获取和分析步骤，以及每一步使用的条件和得到的结果。',
  '交付待验证': '尚未记录完整的检查过程或明确结果，因此还不能确认本次核查完成。',
  '条件变更复核': '更改台站、分析目标或诊断条件后重新核查，保留上一次结果。示例按钮只查看已有结果，不开始新核查。',
};
const TERM_TITLE = {
  Agent: '智能核查', MCP: '分析过程',
  'UTC时窗': '观测时窗', '到时方法频带': '到时检查频带', '诊断网格': '到时检查条件',
  '核查模式': '核查方式',
  '新增取数': '获取新观测',
  '规则后备': '观测核查结果', '停止依据': '结束原因',
  '交付来源': '结果来自哪里', '交付待验证': '结果待确认',
  '条件变更复核': '改条件再核查',
};
const TERM_KEY = {
  'UTC 时窗': 'UTC时窗', '范围': '台站', '允许新增取数': '新增取数',
  '获取新观测': '新增取数',
  '观测时窗': 'UTC时窗', '到时检查频带': '到时方法频带', '到时检查条件': '诊断网格',
  '所问字段': '方法适用性',
  '复核完成 · 当前证据不足': '证据不足', '已使用规则后备': '规则后备',
  'Agent 主动核查': 'Agent', 'Agent 条件变更复核': '条件变更复核',
  '按问题规则核查': '核查模式', '规则反馈复核': '核查模式',
  '按问题自动检查': '核查模式', '按固定规则重新检查': '规则后备',
  '模型自动核查': 'Agent', '模型按新条件重新核查': '条件变更复核',
  '结果来自哪里': '交付来源', '结果尚未确认': '交付待验证',
  '结果待确认': '交付待验证', '观测核查结果': '规则后备',
  '结束原因': '停止依据', '改条件再核查': '条件变更复核',
  '到时检查（STA/LTA）': 'STA/LTA', '到时稳定性诊断': '诊断网格',
  '方法输入条件': '方法适用性',
};
const FIELD_LABEL = {
  method_applicability: '方法输入条件', channel_integrity_flags: '通道异常检查',
  band_support: '频带支持', verdict: '核查结论',
};
const MODE_LABEL = {
  agent: '智能核查（计费）', question: '按问题核查（免费）',
  rule: '多台站传播核查（免费）', fixed: '最多三个台站核查（免费）', full: '全部可用台站核查（免费）',
};
const MODE_NOTE = {
  agent: '智能核查根据问题选择观测和分析步骤，会产生智能核查费用。开始时自动检查填写内容；填入示例和确认条件均免费。',
  question: '按问题对应的预设方法检查，免费；可能获取公开观测。',
  rule: '按固定的多台站传播判据检查，免费；不随问题改变判断条件。',
  fixed: '按固定顺序核查最多三个台站的观测，免费。',
  full: '核查允许范围内全部可用台站的观测，免费。',
};
// Deterministic arms used no model at all, so the honest wording is 未调用/不适用 —
// never 未知 and never a "model failure".
const DET_ARM_RE = /^(question|user_feedback_recompute|work)$/;

const DISPLAY_CODE_LABEL = {
  ...VERDICT_LABEL, ...AUDIT_LABEL, ...STOP_LABEL, ...FIELD_LABEL, ...GOAL_LABEL,
  timeout: '核查超时', discovery_failure: '未能找到符合条件的台站',
  subprocess_exit: '分析中断', no_execution_record: '未保存完整核查过程',
  record_mismatch: '保存结果与当前核查不一致', model_fatal: '模型分析未完成',
  tool_failure: '观测获取或分析未完成', task_mismatch: '分析内容与当前问题不一致',
  cleanup_failed: '分析结束后未能正常停止', cleanup_timeout: '分析结束后停止超时',
  internal_error: '暂时无法完成核查', offline_material_missing: '离线条件下缺少所需观测',
  model_unavailable: '智能核查服务暂不可用，可稍后重试或选择标准核查',
  no_observations: '未取得观测', no_analysable_data: '没有可分析的观测',
  insufficient_stations: '可用台站数量不足', ambiguous_coincidence: '到时一致性不足以判断',
  no_stored_waveform: '未保存可用波形', no_vertical: '缺少垂直分量',
  no_vertical_component: '缺少垂直分量', no_band_data: '缺少频带检查结果',
  nyquist_below_target: '采样率不足以支持目标频带', band_unsupported: '不支持目标频带',
  input_conditions_met: '输入条件满足', input_conditions_not_met: '输入条件不满足',
  three_component_missing: '三分量不完整', three_component_ok: '三分量条件满足',
  vertical_ok: '垂直分量条件满足', vertical_unhealthy: '垂直分量异常',
  observed_channel_flagged: '已发现通道异常', stale_condition: '当前条件已改变，需重新检查',
  gaps_or_overlaps: '波形存在缺口或重叠', non_finite: '波形含无效数值',
  constant: '波形幅度不变', constant_channel: '波形幅度不变',
  acquisition_not_allowed: '当前只允许复用已有观测', candidate_ceiling_reached: '已达到观测数量上限',
  in_scope_candidates_untried: '当前范围内仍有未检查台站', no_new_acquisition: '检查是否只使用已有观测',
  goal_recheck: '重新检查分析目标', diagnostic: '检查到时稳定性',
  resource_limited: '已达到观测数量或费用限制', material_missing: '缺少必要观测',
  unanswered_no_data: '没有观测，无法回答', coherence: '检查多台站传播一致性',
  public_criterion_not_met: '判断条件尚未满足', valid_complete: '核查完成',
  conditions_not_executed: '尚未按当前条件完成检查', obligations_pending: '仍有必要检查未完成',
  wrong_field: '尚未回答当前问题', candidate_ceiling_exceeded: '获取的观测数量超出允许范围',
  forbidden_acquisition: '获取了当前条件不允许使用的新观测',
  scope_not_stated: '尚未说明检查了哪些台站', no_legal_stop_basis: '尚未满足结束检查的条件',
  succeeded: '已完成', failed: '未完成', running: '核查中', unrecorded: '结果尚未记录',
  agent_validated: '模型判断与观测一致', deterministic: '按固定规则检查',
  no_model_used: '未使用模型', window_start: '开始时间（UTC）', window_end: '结束时间（UTC）',
  target_band_hz: '目标频带', method_goal: '分析目标', stations: '台站', region: '台站或区域',
};

function displayText(value) {
  return String(value == null ? '' : value)
    .replace(/\b[a-z][a-z0-9_]*\b/gi, key => DISPLAY_CODE_LABEL[key] ||
      (key.includes('_') ? '当前无法确认' : key))
    .replace(/原始模型主张|模型原本主张|模型主张|模型判断|智能分析判断/g, '智能建议')
    .replace(/验证器/g, '结果检查').replace(/生产 validate/g, '结果检查')
    .replace(/状态\s*fixture|fixture/gi, '演示样例')
    .replace(/内部探针/g, '服务检查').replace(/\bAGH\b/gi, '智能核查')
    .replace(/\bMCP\b/gi, '分析过程').replace(/\bAgent\b/gi, '智能核查')
    .replace(/\brun(?:_id)?\b/gi, '核查记录')
    .replace(/字段/g, '检查项目').replace(/交付/g, '结果')
    .replace(/验收/g, '检查确认').replace(/回放/g, '查看已保存结果')
    .replace(/守卫/g, '条件检查').replace(/规则后备/g, '观测核查结果')
    .replace(/父轮/g, '上一次核查').replace(/子轮/g, '本次核查')
    .replace(/父子记录/g, '前后两次记录')
    .replace(/模型费用|模型额度/g, '智能核查费用').replace(/模型核查/g, '智能核查')
    .replace(/模型/g, '智能分析').replace(/元数据/g, '仪器信息')
    .replace(/运行/g, '核查').replace(/模式/g, '方式').replace(/参数/g, '条件')
    .replace(/配置/g, '检查组合')
    .replace(/到时诊断网格|诊断网格/g, '到时检查频带')
    .replace(/新增取数/g, '获取新观测').replace(/已取数/g, '已取得观测');
}

function userError(message) {
  const raw = String(message || '').trim();
  if (/未知运行|run.not.found/i.test(raw)) return '这条核查记录暂时无法找到，请刷新记录列表后重试。';
  if (DISPLAY_CODE_LABEL[raw]) return displayText(DISPLAY_CODE_LABEL[raw]);
  if (/failed to fetch|networkerror|econn|enotfound|dns|getaddrinfo|connection refused/i.test(raw)) {
    return '暂时无法连接核查服务，请检查连接后重试。';
  }
  if (/api[_ -]?key|credential|unauthori[sz]ed|authentication|\b40[13]\b|密钥/i.test(raw)) {
    return '智能核查服务暂不可用，可稍后重试或选择标准核查。';
  }
  if (/budget|ledger|cost.{0,20}(?:limit|stop)|预算|台账|额度|余额|费用/i.test(raw)) {
    return /exhausted|exceeded|limit.{0,20}reached|cost.{0,20}stop|已达.*上限|达到.*上限|超出.*上限|预算不足|额度不足|余额不足/i.test(raw)
      ? '已达到当前费用上限，暂时无法继续智能核查。'
      : '费用信息暂时无法确认，请稍后重试。';
  }
  if (/依赖|未安装|dependencies|not installed|模型.{0,20}(?:配置|连接|服务|不可用)/i.test(raw)) {
    return '智能核查服务暂不可用，可稍后重试或选择标准核查。';
  }
  if (/timeout|timed out|abort|超时/i.test(raw)) return '核查超时，请稍后重试。';
  const reason = raw.replace(/^[A-Za-z]+Error:\s*/, '');
  if (/traceback|\b[A-Za-z]+Error\b|\b(?:stderr|stdout|stack|node_modules|token|secret|key|json|schema|exception)\b|\/(?:Users|home|tmp|private|var|usr)\/|[{}]/i.test(reason)) {
    return '暂时无法完成这一步，请稍后重试。';
  }
  const readable = displayText(reason);
  return /[\u3400-\u9fff]/.test(reason)
    ? readable : '暂时无法完成这一步，请检查填写内容或连接后重试。';
}

const $ = id => document.getElementById(id);
const headers = {'Content-Type': 'application/json', 'X-EQ-Request': '1'};
const app = {
  view: null,           // 当前归一化后的 run
  runId: null,
  replay: true,
  waveform: null,
  candidateId: null,
  poll: null,
  mode: 'agent',
  parsed: null,
  samples: {},
  activeView: 'workbench',
  runRequest: 0,
  waveRequest: 0,
  records: null,
  recordsLoading: null,
  recordLimit: 12,
  guide: null,
  parseRequest: 0,
  parsedInput: null,
  taskRunning: false,
  activeTrackId: null,
  pollInFlight: false,
  health: null,
  termTrigger: null,
  termPinned: false,
  termTimer: null,
  suppressTermFocus: false,
};

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(userError(body.error || ('HTTP ' + res.status)));
    error.code = body.code;
    error.missing = body.missing;
    error.status = res.status;
    throw error;
  }
  return body;
}
const txt = v => (v === null || v === undefined || v === '') ? '—' : String(v);
const escapeHtml = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function showMessage(message, failed = false) {
  const box = $('app-message');
  box.textContent = message;
  box.classList.toggle('bad', failed);
  box.hidden = !message;
}
function armText(arm) {
  return ARM_LABEL[arm] || (String(arm || '').startsWith('adaptive_agent')
    ? '智能核查' : arm ? '已保存核查' : '核查方式未记录');
}

function termHelp(key, label = key) {
  const title = displayText(TERM_TITLE[key] || key);
  const visible = displayText(label === key ? title : label);
  return TERMS[key] ? '<button type="button" class="term-help" data-term="' + key +
    '" aria-label="了解' + escapeHtml(title) + '" aria-haspopup="dialog" aria-controls="term-popover" aria-expanded="false">' +
    escapeHtml(visible) + '</button>' : '';
}
function term(label, key = TERM_KEY[label] || label) {
  return termHelp(key, label) || escapeHtml(displayText(label));
}
function termPosition(rect, size, viewport) {
  const width = Math.min(260, viewport.width - 24);
  const left = Math.max(12, Math.min(rect.left, viewport.width - width - 12));
  const below = rect.bottom + 8;
  const top = below + size.height <= viewport.height - 12 ? below
    : Math.max(12, rect.top - size.height - 8);
  return {left, top, width};
}
function closeTerm(restoreFocus = false) {
  clearTimeout(app.termTimer);
  const trigger = app.termTrigger;
  if (trigger) {
    trigger.setAttribute('aria-expanded', 'false');
    trigger.removeAttribute('aria-describedby');
  }
  $('term-popover').hidden = true;
  app.termTrigger = null;
  app.termPinned = false;
  if (restoreFocus && trigger) {
    app.suppressTermFocus = true;
    trigger.focus();
    app.suppressTermFocus = false;
  }
}
function openTerm(trigger, pinned = false) {
  const key = trigger.dataset.term;
  if (!TERMS[key]) return;
  clearTimeout(app.termTimer);
  if (app.termTrigger && app.termTrigger !== trigger) {
    app.termTrigger.setAttribute('aria-expanded', 'false');
    app.termTrigger.removeAttribute('aria-describedby');
  }
  app.termTrigger = trigger;
  app.termPinned = pinned;
  trigger.setAttribute('aria-expanded', 'true');
  trigger.setAttribute('aria-describedby', 'term-popover-text');
  $('term-popover-title').textContent = displayText(TERM_TITLE[key] || key);
  $('term-popover-text').textContent = displayText(TERMS[key]);
  const popup = $('term-popover');
  popup.hidden = false;
  const pos = termPosition(trigger.getBoundingClientRect(), popup.getBoundingClientRect(),
    {width: window.innerWidth, height: window.innerHeight});
  popup.style.width = pos.width + 'px';
  popup.style.left = pos.left + 'px';
  popup.style.top = pos.top + 'px';
}
function bindTerms() {
  const popup = $('term-popover');
  for (const trigger of document.querySelectorAll('[data-term]')) {
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-controls', 'term-popover');
    trigger.setAttribute('aria-expanded', 'false');
  }
  document.addEventListener('click', event => {
    const trigger = event.target.closest('[data-term]');
    if (trigger) {
      if (app.termTrigger === trigger && app.termPinned) closeTerm();
      else {
        openTerm(trigger, true);
        if (event.detail === 0) popup.focus({preventScroll: true});
      }
    } else if (!popup.contains(event.target)) closeTerm();
  });
  document.addEventListener('focusin', event => {
    if (app.suppressTermFocus) return;
    const trigger = event.target.closest('[data-term]');
    if (trigger) openTerm(trigger, false);
    else if (!popup.contains(event.target)) closeTerm();
  });
  document.addEventListener('pointerover', event => {
    if (event.pointerType === 'touch' || app.termPinned) return;
    const trigger = event.target.closest('[data-term]');
    if (trigger) openTerm(trigger);
    if (popup.contains(event.target)) clearTimeout(app.termTimer);
  });
  document.addEventListener('pointerout', event => {
    if (app.termPinned || !app.termTrigger) return;
    if (event.target.closest('[data-term]') || popup.contains(event.target)) {
      app.termTimer = setTimeout(() => {
        if (!popup.matches(':hover') && !popup.contains(document.activeElement) &&
            document.activeElement !== app.termTrigger) closeTerm();
      }, 180);
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !popup.hidden) {
      event.preventDefault(); closeTerm(true);
    }
  });
  $('term-close').onclick = () => closeTerm(true);
  window.addEventListener('resize', () => closeTerm());
  window.addEventListener('scroll', () => closeTerm(), {capture: true, passive: true});
}

/* ------------------------------------------------------------ 归一化 */
function statusOf(ev) {
  const delivered = (ev.validator || {}).delivered || {};
  const outcome = ev.outcome || {};
  const audit = ev.finish_audit || {};
  if (ev.record_state === 'empty' || ((!outcome.status ||
      ['succeeded', 'unrecorded'].includes(outcome.status)) &&
      !(ev.acquisition || []).length && !(ev.tool_calls || []).length && !delivered.verdict)) {
    return {kind: 'empty', badge: ev.internal ? '服务检查记录' : '暂无核查内容',
            tone: 'replay', detail: '没有观测、分析过程或明确结果，不能确认核查完成。'};
  }
  if (outcome.status === 'running') {
    return {kind: 'pending', badge: '核查中', tone: 'unknown', detail: '结果尚未保存。'};
  }
  if (outcome.status && !['succeeded', 'unrecorded'].includes(outcome.status)) {
    const reasons = [userError(outcome.failure_kind || '核查未完成'),
      outcome.failure_detail ? userError(outcome.failure_detail) : ''];
    return {kind: 'failed', badge: '本次核查未完成', tone: 'failed',
            detail: [...new Set(reasons.filter(Boolean))].join('：')};
  }
  if (outcome.status !== 'succeeded' || !delivered.verdict) {
    return {kind: 'unverified', badge: '结果待确认', tone: 'unknown',
            detail: '尚未保存完整检查过程和明确结果，不能确认核查完成。'};
  }
  if (delivered.model_claim_accepted === false || delivered.fallback_used === true) {
    return {kind: 'fallback', badge: '已采用观测核查结果', tone: 'fallback',
            detail: delivered.rejection_reason ? userError(delivered.rejection_reason)
              : '智能建议未能与当前观测和条件对应，当前显示观测核查结果。'};
  }
  if ((audit.outcome || delivered.audit_outcome) === 'legal_unknown') {
    return {kind: 'unknown', badge: '核查结束 · 观测不足以判断', tone: 'unknown',
            detail: displayText(audit.stop_basis || delivered.stop_reason_code || '当前观测不足以判断') +
                    (audit.unmet && audit.unmet.length ? ' · 尚需完成：' +
                      audit.unmet.map(u => displayText(u.kind)).join('、') : '')};
  }
  if (delivered.model_claim_accepted === true) {
    return {kind: 'ok', badge: '核查完成', tone: 'ok',
            detail: '智能建议已与本次观测和条件核对。'};
  }
  if (DET_ARM_RE.test(String((ev.scope || {}).arm || ''))) {
    return {kind: 'ok', badge: '标准核查完成', tone: 'ok',
            detail: '已按预设方法完成检查，本次未使用智能分析。'};
  }
  return {kind: 'unverified', badge: '结果待确认', tone: 'unknown',
          detail: '已有分析过程，尚未确认智能建议是否与当前观测一致。'};
}

function normalize(ev, {replay = true} = {}) {
  const task = ev.task || {};
  const requested = ev.requested_conditions || {};
  const actual = ev.actual_conditions || {};
  const delivered = (ev.validator || {}).delivered || {};
  const audit = ev.finish_audit || {};
  const acq = ev.acquisition || [];
  const excludedIds = new Set(acq.filter(a => a.excluded).map(a => a.candidate_id));
  // Pair each tool `start` with its own later `ok`/`error` so the timeline shows
  // one row per real call, not two (the JSONL stores both phases).
  const rawCalls = ev.tool_calls || [];
  const tools = [];
  const pending = [];
  for (const call of rawCalls) {
    if (call.phase === 'start') {
      const row = {tool: call.tool, arguments: call.arguments, at: call.at,
                   phase: 'start', result: null, resultPhase: null, resultAt: null};
      tools.push(row); pending.push(row);
    } else {
      const idx = pending.findIndex(r => r.tool === call.tool);
      const row = idx >= 0 ? pending.splice(idx, 1)[0] : null;
      if (row) { row.result = call.result || null; row.resultPhase = call.phase;
                 row.resultAt = call.at; }
    }
  }
  return {
    runId: ev.run_id,
    parentRunId: ev.parent_run_id,
    replay,
    fixture: !!ev.fixture,
    internal: !!ev.internal,
    sourceKind: ev.source_kind,
    recordState: ev.record_state,
    fixtureNote: ev.fixture_note,
    arm: (ev.scope || {}).arm,
    deterministic: DET_ARM_RE.test(String((ev.scope || {}).arm || '')),
    task: {
      question: task.user_question,
      followUp: task.follow_up,
      windowStart: task.window_start, windowEnd: task.window_end,
      region: task.region, scope: requested.scope || task.scope,
      goal: requested.method_goal || task.method_goal,
      targetBand: requested.target_band_hz || task.target_band_hz,
      methodBand: requested.onset_method_band_hz || task.method_band_hz,
      diagnosticGrid: requested.diagnostic_bands_hz,
      acquisitionAllowed: requested.acquisition_allowed,
      excluded: requested.excluded_candidate_ids || [],
      changes: requested.requested_changes || [],
    },
    status: statusOf(ev),
    conclusion: {
      verdict: delivered.verdict,
      label: VERDICT_LABEL[delivered.verdict] || delivered.verdict,
      statement: delivered.statement_zh || delivered.statement,
      reason: (ev.validator || {}).reason || '',
      field: delivered.field, fieldValue: delivered.field_value,
      accepted: delivered.model_claim_accepted,
      fallback: delivered.fallback_used,
      origin: delivered.origin,
      auditOutcome: audit.outcome || delivered.audit_outcome,
      stopBasis: audit.stop_basis || delivered.stop_reason_code,
      stopBases: audit.stop_bases,
      rejection: delivered.rejection_reason,
      limitations: delivered.limitations || [],
      determined: delivered.result_determined,
      modelConclusion: (ev.model_claim || {}).conclusion,
    },
    audit,
    modelClaim: ev.model_claim || {},
    acquisition: acq,
    excludedIds,
    candidates: acq.map(a => ({candidate_id: a.candidate_id, station: a.station,
                                excluded: !!a.excluded, status: a.status,
                                flags: a.channel_integrity_flags || [],
                                band: a.band_support || null})),
    tools,
    guardActions: ev.condition_applications || [],
    diagnostics: ev.diagnostic_parameters || [],
    stability: ev.stability_checks || {},
    config: ev.agent_config || {},
    cleanup: ev.cleanup || null,
    native: ev.native || {},
    cost: ev.cost || {},
    outcome: ev.outcome || {},
    parent: ev.parent || null,
    taskHashes: {task: task.task_sha256, prompt: task.prompt_sha256},
    requested, actual,
  };
}

/* ------------------------------------------------------------- 渲染 */
function dl(rows) {
  return '<dl>' + rows.filter(r => r && r[1] !== undefined && r[1] !== null)
    .map(r => '<dt>' + term(r[0]) + '</dt><dd>' +
      (r[2] ? r[1] : escapeHtml(r[1])) + '</dd>').join('') + '</dl>';
}
const band = b => (Array.isArray(b) && b.length === 2) ? (b[0] + '–' + b[1] + ' Hz') : '—';
// 分量对齐是三值：true 通过 / false 未通过 / null 未检查（分量不足）。
// 把 null 吞进二分支会把它显示成“通过”，因此单独成函数。
function alignmentText(alignment) {
  if (!alignment || alignment.aligned === null || alignment.aligned === undefined) {
    return '未检查（分量不足）';
  }
  return alignment.aligned === true ? '通过' : '未通过';
}
const gridTxt = g => (Array.isArray(g) && g.length)
  ? g.map(band).join('、') : '全部诊断参数';

function scopeText(scope) {
  if (!scope) return '—';
  const kind = scope.scope_kind || scope.kind;
  if (kind === 'explicit_candidates') {
    return '指定台站：' + ((scope.stations || []).join('、') || '尚未找到符合条件的台站');
  }
  if (kind === 'observed_scope') return '已获取观测的台站';
  return '全部允许使用的台站';
}

function renderTask(v) {
  const t = v.task;
  const excludedStations = (t.excluded || []).map(id =>
    (v.acquisition.find(a => a.candidate_id === id) || {}).station).filter(Boolean);
  $('task-body').innerHTML = dl([
    ['问题', t.question],
    ['观测时窗', txt(t.windowStart) + ' → ' + txt(t.windowEnd)],
    ['范围', scopeText(t.scope)],
    ['分析目标', GOAL_LABEL[t.goal] || '未记录'],
    ['目标频带', band(t.targetBand)],
    ['到时检查频带', band(t.methodBand)],
    ['到时检查条件', gridTxt(t.diagnosticGrid)],
    ['获取新观测', t.acquisitionAllowed === false ? '否（使用已保存观测）' : '是'],
    ['当前排除', excludedStations.length ? excludedStations.join('、')
      : (t.excluded || []).length ? t.excluded.length + ' 项观测' : '无'],
  ]);
  $('current-run').textContent = v.runId ? '已保存' : '暂无记录';
}

function renderConclusion(v) {
  const c = v.conclusion;
  const box = $('conclusion-body');
  const replayTag = v.fixture
    ? '<span class="status-badge status-replay">演示样例（未使用智能分析）</span>'
    : v.internal ? '<span class="status-badge status-replay">服务检查记录（非实际核查）</span>'
      : v.replay ? '<span class="status-badge status-replay">' +
        (v.deterministic ? '已保存的标准核查结果'
          : v.tools.length && v.native.agh_session_id ? '已保存的智能核查结果'
            : '已保存记录（智能分析使用情况尚未确认）') + '</span>' : '';
  let html = '<div class="statusline">' +
    '<span class="status-badge status-' + v.status.tone + '">' +
      term(v.status.badge, v.status.kind === 'fallback' ? '规则后备' :
        v.status.kind === 'unknown' ? '证据不足' : v.status.kind === 'unverified' ? '交付待验证' : '') + '</span>' +
    replayTag +
    (v.arm ? '<span class="tag replay">' +
      term(armText(v.arm), v.deterministic ? '核查模式' : 'Agent') + '</span>' : '') +
    '</div>';
  if (v.status.kind === 'failed') {
    html += '<div class="failbanner"><b>本次核查未完成</b>：' + escapeHtml(userError(v.status.detail)) +
      '<br>已取得的观测和已发生的费用仍保留。</div>';
  }
  if (v.fixture) {
    html += '<div class="failbanner" style="background:var(--panel-2);color:var(--dim);' +
      'border-color:var(--line)">这是用于演示界面的样例，并未发起实际智能核查。</div>';
  }
  html += '<div class="verdict-big">' + escapeHtml(displayText(c.label || '—')) + '</div>';
  if (c.field && c.field !== 'verdict' && c.fieldValue !== undefined && c.fieldValue !== null) {
    html += '<div class="verdict-sub">' + term(FIELD_LABEL[c.field] || '检查项目',
      c.field === 'method_applicability' ? '方法适用性' :
        c.field === 'channel_integrity_flags' ? '通道条件' : c.field === 'band_support' ? '目标频带' : '') +
      '：<span class="fieldvalue">' + escapeHtml(fieldValueText(c.field, c.fieldValue)) +
      '</span></div>';
  }
  if (c.statement) html += '<p class="note">结论适用范围：' + escapeHtml(displayText(c.statement)) + '</p>';
  html += '<div class="basis">' +
    '<div><span class="k">当前说明</span>' + escapeHtml(displayText(v.status.detail)) + '</div>' +
    '<div><span class="k">复核结果</span>' +
      escapeHtml(AUDIT_LABEL[c.auditOutcome] || '核查结果尚未确认') +
      (typeof c.determined === 'boolean' ? ' · ' + (c.determined ? '结论确定' : '结论未知') : '') + '</div>' +
    '<div><span class="k">' + term('结束原因') + '</span>' +
      escapeHtml(STOP_LABEL[c.stopBasis] || (c.stopBasis ? displayText(c.stopBasis) : '—')) +
      '</div>' +
    '<div><span class="k">' + term('结果来自哪里') + '</span>' +
      (c.accepted === true ? '智能建议已与当前观测核对'
        : c.accepted === false ? '观测核查结果（智能建议未采用）'
          : v.deterministic ? '标准核查（未使用智能分析）' : '智能分析使用情况尚未确认') + '</div>' +
    '</div>';
  if (c.rejection) {
    html += '<p class="note bad" style="color:var(--fail)">未采用智能建议的原因：' +
      escapeHtml(userError(c.rejection)) + '</p>';
  }
  if (c.modelConclusion && c.accepted === false) {
    html += '<p class="note">最初的智能建议：' + escapeHtml(displayText(c.modelConclusion)) + '</p>';
  }
  if (v.parent) {
    html += '<p class="note">上一次核查：' +
      escapeHtml(VERDICT_LABEL[(v.parent.delivered || {}).verdict] || '结论尚未记录') + '</p>';
  }
  box.innerHTML = html;
}

function fieldValueText(field, value) {
  if (value === null || value === undefined || value === 'unknown') return '未知';
  if (field === 'method_applicability') return value === true ? '满足' : value === false ? '不满足' : '见核查结论';
  if (field === 'channel_integrity_flags') {
    return Array.isArray(value) ? value.length ? '已发现通道异常（详见证据）' : '当前范围未发现通道异常'
      : typeof value === 'boolean' ? value ? '已发现通道异常' : '当前范围未发现通道异常' : '见核查结论';
  }
  return typeof value === 'boolean' ? value ? '是' : '否' : '见核查结论';
}

/* ------------------------------------------------------------ 波形 */
function renderWaveToolbar(v) {
  const bar = $('wave-toolbar');
  bar.innerHTML = '';
  const withWave = v.candidates;
  if (!withWave.length) {
    app.waveRequest++;
    app.waveform = null;
    app.candidateId = null;
    $('wave-note').textContent = '';
    drawWave(null);
    waveEmpty('暂无观测数据', '这次核查尚未取得可显示的波形。');
    return;
  }
  const preferred = withWave.find(c => !c.excluded && c.status === 'analysed') || withWave[0];
  if (withWave.length === 1) {
    bar.textContent = '台站 ' + (preferred.station || '未命名台站') +
      (preferred.excluded ? '（已排除）' : '');
    loadWave(v.runId, preferred.candidate_id);
    return;
  }
  const sel = document.createElement('select');
  for (const c of withWave) {
    const o = document.createElement('option');
    o.value = c.candidate_id;
    o.textContent = (c.station || '未命名台站') + (c.excluded ? '（已排除）' : '');
    sel.appendChild(o);
  }
  sel.value = preferred.candidate_id;
  sel.onchange = () => loadWave(v.runId, sel.value);
  const label = document.createElement('span');
  label.className = 'hint';
  label.textContent = '台站';
  bar.append(label, sel);
  loadWave(v.runId, preferred.candidate_id);
}

function waveEmpty(title, detail) {
  $('wave-empty').hidden = false;
  $('wave-empty-title').textContent = title;
  $('wave-empty-detail').textContent = detail || '';
}

async function loadWave(runId, candidateId) {
  const request = ++app.waveRequest;
  app.candidateId = candidateId;
  app.waveform = null;
  const note = $('wave-note');
  note.textContent = '';
  drawWave(null);
  waveEmpty('正在加载波形…', '显示这次核查对应的观测。');
  try {
    const data = await api('/api/waveform/' + encodeURIComponent(runId) + '/' +
                           encodeURIComponent(candidateId));
    if (request !== app.waveRequest || runId !== app.runId) return;
    if (data.error) {
      const title = data.code === 'waveform_file_missing' ? '波形暂时无法查看'
        : data.code === 'no_observations' ? '暂无观测数据'
          : '波形加载失败';
      waveEmpty(title, userError(data.reason || data.error));
      return;
    }
    app.waveform = data;
    drawWave(data);
    const bits = [];
    bits.push('<span class="legend-dot" style="background:#0E7490"></span>' +
      term('观测包络', '包络') + '（各分量幅度分别缩放） · ' + term('Z/N/E'));
    if (data.onset && data.onset.seconds !== null && data.onset.seconds !== undefined) {
      bits.push('<span class="legend-dot" style="background:#A16207"></span>' + term('检测到的起跳时刻', '到时') + ' ' +
        data.onset.seconds + ' s');
    } else if (data.onset && data.onset.computed) {
      bits.push('未检测到符合条件的信号起跳');
    }
    if ((data.channel_integrity_flags || []).includes('gaps_or_overlaps')) {
      bits.push('观测含缺口或重叠，按实际时间分段显示，缺口未补齐');
    }
    for (const component of ['Z', 'N', 'E']) {
      const rates = ((data.channels || {})[component] || {}).sampling_rates_hz || [];
      if (rates.length > 1) bits.push(component + ' 分段采样率：' + escapeHtml(rates.join(' / ')) + ' Hz');
    }
    if (data.excluded) bits.push('<span style="color:var(--fail)">该台站已排除，不参与当前结论</span>');
    note.innerHTML = bits.join(' · ') + '<br>' +
      escapeHtml(displayText(data.source_label || '观测数据')
        .replace(/原始下载波形/g, '台站观测波形')
        .replace(/派生副本（人工注入或改写，已披露）/g, '经人工调整的波形')) +
      ' · 各分量用于观察波形形态，不作跨站幅值比较。<br>观测时窗（UTC）：' +
      escapeHtml(txt((data.window || {}).start)) + ' → ' +
      escapeHtml(txt((data.window || {}).end));
  } catch (e) {
    if (request !== app.waveRequest || runId !== app.runId) return;
    waveEmpty('波形暂时无法查看', userError(e.message));
  }
}

function drawWave(data) {
  const canvas = $('wave-canvas');
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const W = rect.width, H = rect.height;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#F0F4F8';
  ctx.fillRect(0, 0, W, H);
  if (!data || data.error || !data.channels) return;
  const comps = ['Z', 'N', 'E'].filter(c =>
    data.channels[c] && (data.channels[c].envelope || []).length);
  if (!comps.length) {
    waveEmpty('暂无可显示的分量', '这段波形没有可用的 Z/N/E 分量。');
    return;
  }
  $('wave-empty').hidden = true;
  const left = 66, right = W - 16, top = 24, bottom = H - 38;
  const rowH = (bottom - top) / comps.length;
  const utcTime = value => Date.parse(value &&
    !/Z$|[+-]\d{2}:?\d{2}$/.test(value) ? value + 'Z' : value);
  const windowStart = utcTime((data.window || {}).start);
  const offset = ch => {
    const traceStart = utcTime(ch.start_utc);
    return Number.isFinite(windowStart) && Number.isFinite(traceStart)
      ? (traceStart - windowStart) / 1000 : 0;
  };
  const t0 = Math.min(...comps.map(c => data.channels[c].envelope[0][0] + offset(data.channels[c])));
  const t1 = Math.max(...comps.map(c => {
    const ch = data.channels[c];
    return (ch.duration_seconds ?? ch.n_samples / ch.sampling_rate_hz) + offset(ch);
  }));
  const xAt = t => left + (right - left) * (t - t0) / (t1 - t0 || 1);
  const tickStep = Math.max(1, Math.ceil((t1 - t0) / (W < 500 ? 4 : 8) / 15) * 15);
  ctx.font = '11px system-ui';
  ctx.lineWidth = 1;
  for (let t = Math.ceil(t0 / tickStep) * tickStep; t <= t1; t += tickStep) {
    const x = xAt(t);
    ctx.strokeStyle = '#D1DDE8';
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.fillStyle = '#52657A';
    ctx.textAlign = 'center';
    ctx.fillText(String(Math.round(t * 10) / 10), x, bottom + 17);
  }
  ctx.textAlign = 'left';
  comps.forEach((c, idx) => {
    const ch = data.channels[c];
    const env = ch.envelope || [];
    if (!env.length) return;
    const y0 = top + idx * rowH + 8, y1 = top + (idx + 1) * rowH - 8;
    const mid = (y0 + y1) / 2, half = (y1 - y0) / 2;
    const center = Number.isFinite(ch.display_center) ? ch.display_center
      : env.reduce((sum, row) => sum + (row[1] + row[2]) / 2, 0) / env.length;
    let amp = 0;
    for (const row of env) amp = Math.max(amp, Math.abs(row[1] - center), Math.abs(row[2] - center));
    if (!amp) amp = 1;
    ctx.strokeStyle = '#9BACBC';
    ctx.beginPath(); ctx.moveTo(left, mid); ctx.lineTo(right, mid); ctx.stroke();
    ctx.strokeStyle = '#0E7490';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const [t, mn, mx] of env) {
      const x = xAt(t + offset(ch));
      const ya = mid - ((mx - center) / amp) * half * 0.95;
      const yb = mid - ((mn - center) / amp) * half * 0.95;
      ctx.moveTo(x, ya); ctx.lineTo(x, yb);
    }
    ctx.stroke();
    ctx.fillStyle = '#142B3C';
    ctx.font = '12px system-ui';
    ctx.fillText(c, 8, mid - 12);
    ctx.font = '10px system-ui';
    ctx.fillText(ch.sampling_rate_hz == null
      ? '多采样率'
      : ch.sampling_rate_hz + ' Hz', 8, mid + 3);
    ctx.fillStyle = '#52657A';
    ctx.fillText('+1', left - 19, y0 + 9);
    ctx.fillText('0', left - 14, mid + 3);
    ctx.fillText('−1', left - 19, y1);
  });
  // 到时标记
  if (data.onset && data.onset.seconds !== null && data.onset.seconds !== undefined &&
      data.onset.seconds >= t0 && data.onset.seconds <= t1) {
    const x = xAt(data.onset.seconds);
    ctx.strokeStyle = '#A16207';
    ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#A16207';
    ctx.font = '12px system-ui';
    ctx.fillText('到时 ' + data.onset.seconds + ' s', Math.min(x + 4, W - 80), 16);
  }
  ctx.fillStyle = '#52657A';
  ctx.font = '11px system-ui';
  ctx.textAlign = 'center';
  ctx.fillText('相对 UTC 窗口起点（秒） · 幅度独立归一化', W / 2, H - 7);
  ctx.textAlign = 'left';
}

/* ------------------------------------------------------------ 时间线 */
function changeText(change, candidates = []) {
  const station = candidates.find(c => c.candidate_id === change.candidate_id);
  const target = station ? station.station || '指定台站' : change.station || '指定台站';
  if (change.kind === 'exclude' || change.kind === 'include') {
    return CHANGE_LABEL[change.kind] + '：' + txt(target);
  }
  if (change.kind === 'method_goal') return '方法目标：' +
    (GOAL_LABEL[change.method_goal] || '已调整');
  if (change.kind === 'band' || change.kind === 'stability_bands') {
    if (change.kind === 'stability_bands') return '复查频带：' +
      gridTxt(change.diagnostic_bands_hz || change.bands_hz);
    return CHANGE_LABEL[change.kind] + '：' + band(change.target_band_hz || change.band_hz);
  }
  return CHANGE_LABEL[change.kind] || '已调整核查条件';
}

function toolSummary(entry) {
  if (entry.resultPhase === 'error') return '此步骤未能完成，请根据错误提示重试。';
  if (entry.resultPhase !== 'ok') return '尚未记录此步骤的完成结果。';
  const r = entry.result || {};
  switch (entry.tool) {
    case 'list_candidates':
      return r.shown == null ? '已查看可用台站。' : '找到 ' + r.shown + ' 个候选台站。';
    case 'acquire_station':
      return txt(r.station) + ' · ' + (r.status === 'analysed' ? '已取得观测并分析。'
        : ['failed', 'metadata_unavailable', 'waveform_unavailable', 'fetch_error'].includes(r.status)
          ? '未能取得可分析的观测。'
          : '尚未确认观测是否可用于分析。');
    case 'get_evidence':
      return r.stations_acquired == null ? '已查看当前分析依据。'
        : '当前分析依据包含 ' + r.stations_acquired + ' 个台站的观测。';
    case 'run_onset_check':
      return typeof r.onset_utc === 'string' ? '检测到的到时：' + r.onset_utc
        : '本步未给出可确认的到时，请结合台站观测与稳定性检查判断。';
    case 'run_stability_check':
      return (VERDICT_LABEL[r.stability] || '尚未得到稳定性判断') +
        (r.bands_evaluated ? ' · 检查频带 ' + gridTxt(r.bands_evaluated) : '');
    case 'task_status':
      return r.can_finish === true ? '当前分析依据已足够，可以结束核查。'
        : r.can_finish === false ? '当前分析依据还不足以结束核查。'
          : '已查看核查进度；尚未记录是否可以结束。';
    case 'exclude_observation':
      return '已处理排除要求，请查看调整后的结果。';
    default:
      return '此步骤已完成。';
  }
}

function renderTimeline(v) {
  const box = $('tool-timeline');
  const guard = $('guard-actions');
  if (v.guardActions.length) {
    const last = v.guardActions[v.guardActions.length - 1];
    guard.hidden = false;
    guard.innerHTML = '<h4>已应用你的调整</h4>' +
      v.guardActions.map(a => '<div>' + escapeHtml(changeText(a.change || {}, v.candidates)) +
        '</div>').join('') +
      (last.recomputed ? '<div class="note">已重新分析 ' + last.recomputed + ' 条观测</div>' : '');
  } else {
    guard.hidden = true;
  }
  const calls = v.tools || [];
  if (!calls.length) {
    box.innerHTML = '<p class="hint">本次没有逐步分析记录。</p>';
    return;
  }
  const ol = document.createElement('ol');
  ol.className = 'timeline';
  for (const entry of calls) {
    const li = document.createElement('li');
    const name = entry.tool || entry.kind || 'event';
    const bad = entry.resultPhase === 'error';
    li.innerHTML = '<div class="tname' + (bad ? ' bad' : '') + '">' +
      term(TOOL_LABEL[name] || '分析步骤') +
      (bad ? '（未能完成）' : entry.resultPhase === 'ok' ? '' : '（未完成）') + '</div>' +
      '<div class="tresult">' + escapeHtml(toolSummary(entry)) + '</div>' +
      (entry.at ? '<div class="note">' + escapeHtml(new Date(entry.at)
        .toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false})) + '</div>' : '');
    ol.appendChild(li);
  }
  box.innerHTML = '';
  box.appendChild(ol);
  const note = document.createElement('p');
  note.className = 'note';
  note.textContent = '以上为已记录的核查经过，未完成的步骤会保留标记。';
  box.appendChild(note);
}

/* ------------------------------------------------------- 条件前后比较 */
function renderCompare(v) {
  const box = $('compare-body');
  const fb = $('fb-controls');
  const agent = String(v.config.driver || '').includes('agent_run_optimization') ||
    String(v.arm || '').startsWith('adaptive_agent');
  const canFeedback = !!v.runId && !v.fixture && !v.internal &&
    ['ok', 'unknown', 'fallback'].includes(v.status.kind) && v.acquisition.length > 0 && agent;
  fb.hidden = !canFeedback;
  for (const control of fb.querySelectorAll('button, select')) control.disabled = !canFeedback;
  if (!v.runId) { box.innerHTML = '<p class="hint">打开已有核查记录后，可查看条件对比。</p>';
    return; }
  const t = v.task;
  const cur = [
    ['目标频带', band(t.targetBand)],
    ['分析目标', GOAL_LABEL[t.goal] || '未记录'],
    ['复查频带', gridTxt(t.diagnosticGrid)],
    ['获取新观测', t.acquisitionAllowed === false ? '否' :
      t.acquisitionAllowed === true ? '是' : '未记录'],
    ['参与台站', v.candidates.filter(c => !c.excluded)
      .map(c => c.station || '未命名台站').join('、') || '（无）'],
  ];
  const column = (title, rows, className = '') => '<div class="cmpcol ' + className +
    '"><h4>' + title + '</h4>' + rows.map(r =>
      '<div class="cmprow"><span class="k">' + r[0] + '</span>' +
      escapeHtml(txt(r[1])) + '</div>').join('') + '</div>';
  let html = '';
  if (v.parent) {
    const p = v.parent;
    const pr = p.requested_conditions || {};
    const before = [
      ['目标频带', band(pr.target_band_hz)],
      ['分析目标', GOAL_LABEL[pr.method_goal] || '未记录'],
      ['复查频带', gridTxt(pr.diagnostic_bands_hz)],
      ['获取新观测', pr.acquisition_allowed === false ? '否' :
        pr.acquisition_allowed === true ? '是' : '未记录'],
      ['参与台站', Array.isArray(p.acquisition) ? p.acquisition.filter(a => !a.excluded)
        .map(a => a.station || '未命名台站').join('、') || '（无）' : '上次未记录台站清单'],
    ];
    const changes = (v.task.changes || []).map(c => changeText(c, v.candidates));
    html += '<div class="cmpgrid">' +
      column('上一次核查', before) +
      '<div class="cmpcol"><h4>本次调整</h4>' +
        (changes.length ? changes.map(c => '<div class="cmprow">' + escapeHtml(c) +
          '</div>').join('') : '<div class="cmprow">—</div>') +
        '<div class="cmprow"><span class="k">使用观测</span>沿用上次保存的数据</div></div>' +
      column('本次复核', cur, 'now') +
      '</div>';
    const diag = v.diagnostics[0];
    if (diag) {
      html += '<p class="note">本次已检查频带：' + escapeHtml(gridTxt(diag.bands_hz)) +
        ' · 结果 ' + escapeHtml(VERDICT_LABEL[diag.stability] || '未记录') +
        ' · 有效检测 ' + txt(diag.n_valid) + '/' + txt(diag.n_configurations) + ' 组</p>';
    }
  } else {
    html += '<div class="cmpgrid">' + column('初次核查', cur) +
      '<div class="cmpcol cmp-placeholder"><h4>本次调整</h4>' +
        '<p class="hint">尚未修改条件</p><p class="note">提交反馈后将在此显示改动。</p></div>' +
      column('当前条件', cur, 'now') + '</div>' +
      '<p class="note">调整后会单独保存新的核查结果，并保留这次结果供对比。</p>';
  }
  box.innerHTML = html;
  // 每次切 run 重建选项，避免不支持反馈的记录残留上一轮台站。
  const sel = $('fb-station');
  sel.innerHTML = '';
  if (canFeedback) {
    for (const c of v.candidates) {
      const o = document.createElement('option');
      o.value = c.candidate_id;
      o.textContent = (c.station || '未命名台站') + (c.excluded ? '（已排除）' : '');
      sel.appendChild(o);
    }
    $('fb-goal').value = t.goal || 'three_component_pick';
  }
  $('fb-result').innerHTML = canFeedback
    ? '<p class="hint">调整后将使用已保存的观测重新核查，默认不获取新数据。</p>'
    : '<p class="hint">此记录暂不支持调整后重新核查，请选择有观测数据的模型核查记录。</p>';
}

/* -------------------------------------------------------------- 详情 */
function renderDetails(v) {
  const box = $('detail-body');
  const c = v.conclusion;
  const observationStatus = {
    analysed: '已分析', metadata_unavailable: '缺少仪器信息',
    waveform_unavailable: '缺少波形数据', fetch_error: '获取观测失败',
  };
  let html = '<section class="evidence-section"><h3>观测数据</h3><p class="hint">' +
    v.acquisition.length + ' 个台站记录 · 当前参与 ' +
    v.acquisition.filter(a => !a.excluded).length + ' 个。展开台站可查看数据条件。</p>';
  if (!v.acquisition.length) html += '<p class="hint">本次没有保存观测数据。</p>';
  for (const a of v.acquisition) {
    html += '<details class="ev-station"><summary><b>' + escapeHtml(a.network) + '.' +
      escapeHtml(a.station) + '</b> · ' + escapeHtml(observationStatus[a.status] || '状态未确认') +
      (a.excluded ? ' · <span class="flag">已排除</span>' : '') + '</summary>' +
      dl([
        ['通道', (a.channels || []).join('、')],
        ['距离(km)', a.distance_km], ['窗口', txt((a.window || {}).utc_start) + ' → ' +
          txt((a.window || {}).utc_end)],
        ['自动到时', typeof a.onset_utc === 'string' ? a.onset_utc : '未记录可确认的到时'],
        ['采样率', a.sampling_rate_hz == null ? '未记录' : a.sampling_rate_hz + ' Hz'],
        ['目标频带', band(a.target_band_hz)],
        ['频带支持', a.band_support && a.band_support.supported === true ? '支持'
          : a.band_support && a.band_support.supported === false ? '不支持' : '未确认'],
        ['通道条件', (a.channel_integrity_flags || []).length
          ? displayText(a.channel_integrity_flags.join('、'))
          : a.status === 'analysed' ? '未发现异常' : '尚未确认'],
        ['波形来源', (a.hashes || {}).derived_waveform_sha256 || (a.hashes || {}).derivation
          ? '波形经过人工调整' : (a.source || {}).waveform_url ? '公开台站观测' : '未记录'],
      ]) + '</details>';
  }
  if (v.diagnostics.length) {
    html += '<h4>到时稳定性检查</h4><div class="tablewrap"><table class="grid">' +
      '<tr><th>台站</th><th>检查频带</th><th>结果</th><th>有效 / 已检查组数</th>' +
      '<th>分量对齐</th><th>说明</th></tr>' +
      v.diagnostics.map(d => '<tr><td>' + escapeHtml(d.station) + '</td><td>' +
        escapeHtml(gridTxt(d.bands_hz)) + '</td><td>' +
        escapeHtml(VERDICT_LABEL[d.stability] || '未确认') + '</td><td>' +
        txt(d.n_valid) + '/' + txt(d.n_configurations) + '</td><td>' +
        escapeHtml(alignmentText(d.alignment)) + '</td><td>' +
        escapeHtml(displayText(d.statement || d.reason || '').replace(/配置/g, '检查组合')) + '</td></tr>').join('') +
      '</table></div>';
  }
  html += '</section><section class="evidence-section"><h3>分析依据</h3>';
  const acceptance = v.fixture ? '展示内容，未实际执行'
    : c.accepted === true ? '相符' : c.accepted === false ? '未获支持'
      : v.deterministic ? '本次未使用智能核查' : '未确认';
  html += '<p>' + (v.fixture ? '这是展示示例，其中的分析结果不代表实际完成了核查。'
    : c.accepted === true ? '智能建议与观测证据一致。观测核查结果：' +
      escapeHtml(displayText(c.label || '—')) : c.accepted === false ?
      '智能建议未获观测证据支持；请以当前显示的观测核查结果为准。' :
      v.deterministic ? '本次按所选规则分析观测，没有使用智能核查。'
        : '本次未记录可确认的智能建议。') + '</p>';
  if (c.rejection) {
    const reason = String(c.rejection);
    const issues = [
      [/field_matches/, '分析内容与这次问题不一致'],
      [/condition_matches/, '分析采用的条件与这次要求不一致'],
      [/scope_declared/, '未说明结论适用的台站范围'],
      [/evidence_refs/, '缺少可核对的观测依据'],
      [/stop_basis_legal/, '没有提供充分的结束理由'],
    ].filter(([pattern]) => pattern.test(reason)).map(([, label]) => label);
    const stated = reason.match(/the model stated `([^`]+)` but the evidence supports `([^`]+)`/);
    const explanation = issues.length ? issues.join('；')
      : stated && VERDICT_LABEL[stated[1]] && VERDICT_LABEL[stated[2]]
        ? '智能建议为“' + VERDICT_LABEL[stated[1]] + '”，观测核查结果为“' +
          VERDICT_LABEL[stated[2]] + '”，两者不一致。'
        : /no parsable conclusion/.test(reason) ? '没有得到可读的分析结论。'
          : /asked field/.test(reason) ? '已取得的观测还不足以回答这次问题。'
            : /strategy_insufficient/.test(reason) ? '现有分析不足以支持结束核查。'
              : /[\u4e00-\u9fff]/.test(reason) ? displayText(reason)
                : '现有证据不足以确认智能建议。';
    html += '<p class="flag">尚不能采纳的原因：' + escapeHtml(displayText(explanation)) + '</p>';
  }
  if (c.modelConclusion && !v.fixture) {
    html += '<p>智能建议：' + escapeHtml(VERDICT_LABEL[c.modelConclusion] ||
      '未得到可确认的结论') + '</p>';
  }
  if (c.statement) html += '<p class="note">判断范围：' + escapeHtml(displayText(c.statement)) + '</p>';
  html += '<p class="note">结果仅适用于所列台站和时窗。' +
    (c.field === 'method_applicability' ? '数据条件满足，不代表已经识别出 P 波或 S 波。' : '') +
    (v.diagnostics.length ? '到时稳定性只反映已检查的频带与方法，不能单独证明存在地震。' : '') + '</p>';
  const validationRow = (label, value, tone) => '<div class="validation-row"><span>' +
    label + '</span><span class="status-badge status-' + tone + '">' +
    escapeHtml(value) + '</span></div>';
  html += '</section><section class="evidence-section"><h3>检查结果</h3>' +
    validationRow('证据核对', acceptance, v.fixture ? 'replay'
      : c.accepted === true ? 'ok' : c.accepted === false ? 'failed' : 'replay') +
    validationRow('结束条件', v.fixture ? '展示内容，未实际执行'
      : v.audit.can_finish === true ? '满足' :
      v.audit.can_finish === false ? '未满足' : '未记录',
      v.fixture ? 'replay' : v.audit.can_finish === true ? 'ok' : v.audit.can_finish === false ? 'failed' : 'replay') +
    validationRow('核查状态', v.fixture ? '展示示例' : v.status.kind === 'failed' ? '未完成'
      : AUDIT_LABEL[c.auditOutcome] || '未确认',
      v.fixture ? 'replay' : v.status.kind === 'failed' ? 'failed' : c.auditOutcome === 'determined_answer' ? 'ok' :
        c.auditOutcome === 'legal_unknown' ? 'unknown' : 'replay') +
    dl([
      [FIELD_LABEL[c.field] || '本次问题', v.fixture ? '展示内容'
        : fieldValueText(c.field, c.fieldValue)],
      ['结束原因', STOP_LABEL[c.stopBasis] || '未记录可读的结束原因'],
      ['结果说明', v.status.kind === 'failed' ? userError(v.status.detail) : displayText(v.status.detail)],
    ]) + '</section><section class="evidence-section"><h3>数据来源与费用</h3>' + dl([
      ['数据来源', v.fixture ? '展示示例' : v.acquisition.length
        ? 'EarthScope 公开台站波形与仪器信息' : '本次未取得观测数据'],
      ['分析方式', v.fixture ? '展示示例，未实际执行' : v.deterministic ? '按所选规则核查'
        : c.accepted === false || c.fallback === true ? '智能建议尚不能采纳，采用观测核查结果'
          : c.accepted === true ? '智能分析，并核对观测证据' : '尚未确认'],
      ['本次智能核查费用（估算上限）', v.fixture ? '展示内容，费用不适用'
        : v.deterministic || v.outcome.model_started === false ? '本次未使用智能核查'
          : v.cost.model_cost_known && v.cost.model_cost_upper_cny != null
            ? v.cost.model_cost_upper_cny + ' 元' : '尚未确认费用'],
    ]);
  if (v.replay) html += '<p class="note">查看保存记录不会产生新的智能核查费用。</p>';
  html += '</section>';
  box.innerHTML = html;
}

function renderWorkbench(v) {
  renderTask(v);
  renderConclusion(v);
  renderWaveToolbar(v);
  renderTimeline(v);
  renderCompare(v);
  renderDetails(v);
}

/* --------------------------------------------------------- 加载 run */
async function loadRun(runId, {replay = true, navigate = true, historyMode = 'push'} = {}) {
  const request = ++app.runRequest;
  const note = $('run-load-note');
  note.hidden = false;
  note.textContent = '正在打开核查记录…';
  showMessage('');
  try {
    const ev = await api('/api/evidence/' + encodeURIComponent(runId));
    if (ev.error) throw new Error(ev.error);
    if (request !== app.runRequest) return;
    const view = normalize(ev, {replay});
    app.runId = runId;
    app.replay = replay;
    app.view = view;
    $('run-state').textContent = '查看记录';
    renderWorkbench(app.view);
    note.textContent = replay ? '已打开核查记录。查看已有结果免费。'
      : '核查已结束，下方是本次结果与分析依据。';
    if (navigate) setView('workbench', {historyMode});
    return true;
  } catch (e) {
    if (request !== app.runRequest) return;
    note.textContent = '这条记录暂时无法打开。' + (app.runId
      ? '当前显示上一次打开的记录。' : '请稍后重试。');
    showMessage('核查记录暂时无法查看：' + userError(e.message), true);
    return false;
  }
}

/* ------------------------------------------------------------ 记录表 */
function recordStatus(run) {
  if (run.status && !['succeeded', 'unrecorded', 'running'].includes(run.status)) {
    return {tone: 'bad', label: '核查未完成'};
  }
  if (run.fixture) return {tone: 'replay', label: '演示案例'};
  if (run.record_state === 'empty') return {tone: '', label: '暂无结果'};
  if (run.status !== 'succeeded' || !run.delivered_verdict) {
    return {tone: 'warn', label: run.status === 'running' ? '核查中' : '结果待确认'};
  }
  if (run.fallback_used || run.model_claim_accepted === false) {
    return {tone: 'warn', label: '观测核查结果'};
  }
  if (run.audit_outcome === 'legal_unknown') return {tone: 'warn', label: '已核查 · 证据不足'};
  if (run.model_claim_accepted === true) return {tone: 'ok', label: '复核完成'};
  if (DET_ARM_RE.test(String(run.arm || ''))) return {tone: 'ok', label: '复核完成'};
  return {tone: 'warn', label: '结果待确认'};
}

function renderRecords() {
  const box = $('records-body');
  if (!app.records) return;
  const query = $('record-search').value.trim().toLowerCase();
  const filter = $('record-filter').value;
  const demo = run => [EXAMPLE_FIRST, EXAMPLE_FEEDBACK].includes(run.run_id);
  const runs = app.records.filter(run => !run.internal &&
    (filter === 'all' || (filter === 'demo' && demo(run)) ||
      (filter === 'real' && run.source_kind === 'real') ||
      (filter === 'failed' && recordStatus(run).tone === 'bad')) &&
    (!query || [run.run_id, run.user_question, run.follow_up, run.parent_run_id]
      .some(v => String(v || '').toLowerCase().includes(query))));
  runs.sort((a, b) => Number(demo(b)) - Number(demo(a)));
  const groups = new Map();
  for (const run of runs) {
    const key = run.user_question || run.run_id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(run);
  }
  const visible = [...groups.values()].slice(0, app.recordLimit);
  box.innerHTML = '';
  const makeRow = run => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'record' + (run.parent_run_id ? ' child' : '');
    const state = recordStatus(run);
    row.innerHTML =
      '<span class="rid">' + (run.parent_run_id ? '条件复核' : '首次核查') + '</span>' +
      '<span class="rq">' + escapeHtml(displayText(run.user_question || '未填写核查问题')) +
        (run.follow_up ? '<br><span class="rmeta">反馈：' +
          escapeHtml(displayText(run.follow_up)) + '</span>' : '') + '</span>' +
      '<span><span class="tag ' + state.tone + '">' + escapeHtml(state.label) + '</span>' +
        (demo(run) ? '<br><span class="tag replay">示例核查</span>' : '') + '</span>' +
      '<span class="rmeta">' + escapeHtml(VERDICT_LABEL[run.delivered_verdict] ||
        displayText(run.delivered_verdict || '尚无结论')) + '<br>' + escapeHtml(armText(run.arm)) + '</span>' +
      '<span class="rmeta">' + escapeHtml(run.started_at ?
        new Date(run.started_at).toLocaleString('zh-CN', {hour12: false}) : '时间未记录') + '</span>';
    row.onclick = () => loadRun(run.run_id, {replay: true});
    return row;
  };
  for (const group of visible) {
    const block = document.createElement('div');
    block.className = 'record-group';
    block.appendChild(makeRow(group[0]));
    if (group.length > 1) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = '同一问题的其他 ' + (group.length - 1) + ' 条记录（含条件变更与重复核查）';
      details.appendChild(summary);
      for (const run of group.slice(1)) details.appendChild(makeRow(run));
      block.appendChild(details);
    }
    box.appendChild(block);
  }
  if (!runs.length) box.innerHTML = '<p class="hint">没有符合条件的核查记录。</p>';
  $('records-note').textContent = '最近 ' + app.records.length + ' 条核查记录 · 找到 ' +
    runs.length + ' 条 · 当前显示 ' + visible.length + '/' + groups.size + ' 组。';
  $('records-more').hidden = visible.length >= groups.size;
}

async function loadRecords({refresh = false} = {}) {
  if (app.recordsLoading) return app.recordsLoading;
  if (app.records && !refresh) return;
  const box = $('records-body');
  $('records-refresh').disabled = true;
  $('records-note').textContent = '正在加载核查记录…';
  if (!app.records) box.innerHTML = '<p class="hint">加载中…</p>';
  app.recordsLoading = (async () => {
  try {
    const data = await api('/api/runs?limit=200');
    if (!Array.isArray(data.runs)) throw new Error('暂时无法读取记录');
    app.records = data.runs;
    renderRecords();
  } catch (e) {
    $('records-note').textContent = '暂时无法读取核查记录，请点击刷新重试。';
    showMessage(userError(e.message), true);
  } finally {
    $('records-refresh').disabled = false;
  }
  })();
  try { await app.recordsLoading; } finally { app.recordsLoading = null; }
}

/* ------------------------------------------------------------ 健康/选项 */
async function loadHealth() {
  try {
    const h = await api('/api/health');
    app.health = h;
    $('budget').textContent = '查看已有记录与确认条件免费；智能核查按实际用量计费，标准核查免费。';
    $('budget').hidden = false;
    if (!app.taskRunning) $('run-state').textContent = h.active_run ? '核查中' : app.runId ? '查看记录' : '待核查';
    updateTaskOptions();
  } catch (e) { $('budget').hidden = true; }
}

async function loadOptions() {
  const [regions, goals, modes, samples] = await Promise.all([
    api('/api/regions'), api('/api/goals').catch(() => ({goals: []})),
    api('/api/modes'), api('/api/samples').catch(() => ({samples: {}})),
  ]);
  app.samples = samples.samples || {};
  const refill = (sel, rows, make) => {
    while (sel.options.length > 1) sel.remove(1);
    for (const row of rows) sel.appendChild(make(row));
  };
  refill($('u-region'), regions.regions || [], r => {
    const o = document.createElement('option'); o.value = r.name;
    o.textContent = displayText(r.label); return o; });
  refill($('u-goal'), goals.goals || [], g => {
    const o = document.createElement('option'); o.value = g.id;
    o.textContent = GOAL_LABEL[g.id] || displayText(g.label); return o; });
  refill($('u-mode'), modes.modes || [], m => {
    const o = document.createElement('option'); o.value = m.id;
    o.textContent = MODE_LABEL[m.id] || displayText(m.label);
    if (m.id === 'agent') o.selected = true; return o; });
  updateTaskOptions();
}

async function loadDemoGuide() {
  $('u-fill-example').disabled = true;
  try {
    const guide = await api('/api/demo-guide');
    if (guide.status !== 'ok' || !guide.form) throw new Error('示例条件暂时无法读取');
    app.guide = guide;
    $('u-fill-example').disabled = app.taskRunning;
    const s = guide.stats || {};
    $('about-stats').innerHTML =
      '<div class="about-stat"><strong>' + escapeHtml(txt(s.run_count)) +
        '</strong><span>已完成的示例核查</span></div>' +
      '<div class="about-stat"><strong>' + escapeHtml(txt(s.feedback_chain_count)) +
        '</strong><span>条件复核对比</span></div>';
    $('about-guide-note').textContent = '以下数字来自下方示例核查记录。查看示例免费。';
    const sel = $('u-station-preset');
    while (sel.options.length > 1) sel.remove(1);
    for (const station of guide.form.stations || []) {
      const option = document.createElement('option');
      option.value = station; option.textContent = '示例台站 ' + station;
      sel.appendChild(option);
    }
  } catch (e) {
    app.guide = null;
    const sel = $('u-station-preset');
    while (sel.options.length > 1) sel.remove(1);
    $('about-stats').textContent = '示例信息暂时无法查看。';
    $('about-guide-note').textContent = '可通过下方按钮查看示例核查记录。';
    $('u-warn').textContent = '示例条件暂时无法读取，请稍后重试或自行填写。';
  }
}

async function showDemoSection(section) {
  const feedback = section === 'feedback';
  const ids = (app.guide || {}).examples || {};
  const runId = feedback ? ids.feedback_run_id || EXAMPLE_FEEDBACK : ids.initial_run_id || EXAMPLE_FIRST;
  const loaded = await loadRun(runId, {replay: true});
  if (!loaded) return;
  const anchor = feedback ? 'feedback-card' : section === 'evidence' ? 'wave-card' : 'conclusion-card';
  const card = $(anchor);
  card.scrollIntoView({behavior: 'auto', block: 'start'});
  card.tabIndex = -1;
  card.focus({preventScroll: true});
}

/* --------------------------------------------- 历史实验资料（次级，只读） */
let legacyLoaded = false;
async function loadLegacyCases() {
  if (legacyLoaded) return;
  legacyLoaded = true;
  try {
    const data = await api('/api/cases');
    const box = $('case-list');
    box.innerHTML = '';
    for (const c of data.cases) {
      const b = document.createElement('button');
      b.className = 'ghost';
      b.style.margin = '2px 4px 2px 0';
      b.textContent = (c.case_id.startsWith('E') ? '事件窗口' :
        c.case_id.startsWith('Q') ? '静默窗口' : '诊断窗口') +
        ' ' + (data.cases.indexOf(c) + 1) + (c.split === 'locked' ? '（锁定集）' : '');
      b.onclick = () => loadLegacyCompare(c.case_id);
      box.appendChild(b);
    }
    if (data.cases.length) loadLegacyCompare(data.cases[0].case_id);
  } catch (e) {
    $('compare').innerHTML = '<p class="hint">历史对照不可用。</p>';
  }
}

async function loadLegacyCompare(caseId) {
  try {
    const data = await api('/api/baseline?case_id=' + encodeURIComponent(caseId));
    const box = $('compare');
    if (!data.arms.length) {
      box.innerHTML = '<p class="hint">该时窗没有冻结记录。</p>';
      return;
    }
    const max = Math.max(...data.arms.map(a => a.requests_to_verdict || 0), 1);
    box.innerHTML = '<p class="hint">时窗 ' + escapeHtml(caseId) + '</p>' +
      data.arms.map(a => '<div class="cmprow">' + escapeHtml(a.label) + '：' +
        (a.requests_to_verdict == null ? '—' : a.requests_to_verdict) + ' 次请求' +
        '<span class="chip">' + Math.round(100 * (a.requests_to_verdict || 0) / max) +
        '%</span></div>').join('');
  } catch (e) {
    $('compare').innerHTML = '<p class="hint">历史对照不可用。</p>';
  }
}

/* --------------------------------------------------------------- 视图 */
function setView(name, {historyMode = 'push'} = {}) {
  closeTerm();
  if (!['workbench', 'records', 'about'].includes(name)) name = 'workbench';
  const changed = app.activeView !== name;
  app.activeView = name;
  for (const b of document.querySelectorAll('.navbtn')) {
    const selected = b.dataset.view === name;
    b.classList.toggle('on', selected);
    b.setAttribute('aria-selected', String(selected));
    b.tabIndex = selected ? 0 : -1;
  }
  for (const v of document.querySelectorAll('.view')) {
    v.classList.toggle('on', v.id === 'view-' + name);
  }
  if (changed && historyMode !== 'none') window.scrollTo({top: 0, behavior: 'auto'});
  if (historyMode !== 'none') {
    const url = new URL(location.href);
    url.searchParams.set('view', name);
    if (app.runId) url.searchParams.set('run', app.runId);
    if (url.href !== location.href) {
      history[historyMode === 'replace' ? 'replaceState' : 'pushState']({}, '', url);
    }
  }
  if (name === 'records') loadRecords();
  if (name === 'workbench') requestAnimationFrame(() => drawWave(app.waveform));
}

/* ------------------------------------------------------- 新任务与解析 */
function utcInput(value) {
  const text = String(value || '').trim();
  return text ? /Z$|[+-]\d{2}:?\d{2}$/.test(text) ? text : text + 'Z' : undefined;
}
function collectTask() {
  const body = {
    question: $('u-question').value.trim(),
    window_start: utcInput($('u-start').value),
    window_end: utcInput($('u-end').value),
  };
  const stations = $('u-stations').value.trim();
  const region = $('u-region').value;
  if (stations) body.stations = stations.split(',').map(s => s.trim()).filter(Boolean);
  else if (region) body.region = region;
  const lo = $('u-band-lo').value, hi = $('u-band-hi').value;
  if (lo || hi) body.target_band_hz = [Number(lo || 1), Number(hi || 12)];
  if ($('u-goal').value) body.method_goal = $('u-goal').value;
  return body;
}

function taskInputSignature() {
  return JSON.stringify({task: collectTask(), mode: $('u-mode').value || 'agent'});
}
function updateBandButtons() {
  const value = $('u-band-lo').value + ',' + $('u-band-hi').value;
  for (const b of document.querySelectorAll('[data-band]')) {
    b.setAttribute('aria-pressed', String(b.dataset.band === value));
  }
}
function updateTaskOptions() {
  const mode = $('u-mode').value || 'agent';
  const unavailable = mode === 'agent' && app.health?.model_available === false;
  $('u-mode-note').textContent = unavailable
    ? '智能核查暂不可用：' + userError(app.health.model_unavailable_reason || '请稍后重试。')
    : MODE_NOTE[mode] || '请选择已有的核查模式。';
  $('u-goal-note').textContent = ($('u-goal').value || 'three_component_pick') === 'vertical_onset'
    ? '只检查垂直分量的到时输入条件，不判断已确认的 P/S 震相。'
    : '检查三分量 P/S 拾取的输入条件；满足不代表已经识别 P/S。';
  if (!app.taskRunning) $('u-run').textContent = mode === 'agent' ? '开始智能核查（计费）' : '开始标准核查（免费）';
  $('u-run').disabled = app.taskRunning || unavailable;
  updateBandButtons();
}
function setTaskBusy(busy) {
  app.taskRunning = busy;
  for (const id of ['u-question', 'u-start', 'u-end', 'u-stations', 'u-region',
    'u-band-lo', 'u-band-hi', 'u-goal', 'u-mode', 'u-period', 'u-station-preset',
    'u-parse', 'u-reset', 'fb-exclude', 'fb-include', 'fb-goal-apply', 'fb-grid-apply']) {
    $(id).disabled = busy;
  }
  $('u-fill-example').disabled = busy || !app.guide;
  for (const button of document.querySelectorAll('[data-band]')) button.disabled = busy;
  updateTaskOptions();
}
function invalidateTask() {
  app.parseRequest++;
  app.parsed = null;
  app.parsedInput = null;
  $('u-parse-body').hidden = true;
  $('u-warn').textContent = '';
  updateTaskOptions();
}
function fillExampleTask() {
  const form = (app.guide || {}).form;
  if (!form) { $('u-warn').textContent = '示例条件暂不可用，请刷新页面或自行填写。'; return; }
  $('u-question').value = form.question;
  $('u-start').value = form.window_start.replace(/Z$/, '');
  $('u-end').value = form.window_end.replace(/Z$/, '');
  $('u-stations').value = form.stations.join(',');
  $('u-region').value = form.region_name || '';
  $('u-goal').value = form.method_goal;
  $('u-band-lo').value = form.target_band_hz[0];
  $('u-band-hi').value = form.target_band_hz[1];
  $('u-mode').value = 'agent';
  $('u-period').value = 'example';
  $('u-station-preset').value = form.stations.length === 1 ? form.stations[0] : 'custom';
  invalidateTask();
  $('u-parse-body').hidden = false;
  $('u-parse-body').textContent = '已填入示例条件。可修改后开始核查，或先免费确认条件。';
}
function humanTaskMessage(message) {
  const labels = {window_start: '开始时间（UTC）', window_end: '结束时间（UTC）',
    target_band_hz: '目标频带', method_goal: '分析目标', stations: '台站',
    region: '台站或区域', question: '核查问题',
    three_component_pick: '三分量 P/S 拾取', vertical_onset: '仅垂直分量到时'};
  return displayText(String(message || '').replace(/\b(window_start|window_end|target_band_hz|method_goal|stations|region|question|three_component_pick|vertical_onset)\b/g,
    key => labels[key]));
}
function missingTaskText(missing) {
  const fallback = {
    window_start: '请选择开始时间（UTC），也可点“使用示例条件”。',
    window_end: '请选择结束时间（UTC），也可点“使用示例条件”。',
    region: '请选命名区域，或填写网络.台站（如 UW.CHIMA）。',
    stations: '请填写至少一个台站，多个用英文逗号分隔。',
    target_band_hz: '频带应为两个正数，且下限小于上限。',
    method_goal: '请选择三分量 P/S 拾取或仅垂直分量到时。',
    question: '请写一个具体问题，例如“这段观测能不能用于 1–12 Hz 的 P/S 拾取？”',
  };
  return (missing || []).map(m => {
    const reason = humanTaskMessage(m.why);
    if (fallback[m.field] && /没有可识别的|没有给出区域或台站|没有给出疑点/.test(reason)) return fallback[m.field];
    return [reason, fallback[m.field]].filter(Boolean).join(' ') || '请补全核查信息。';
  }).join('；');
}

async function parseTask({forStart = false} = {}) {
  if (app.taskRunning && !forStart) return null;
  const request = ++app.parseRequest;
  const signature = taskInputSignature();
  app.parsed = null;
  app.parsedInput = null;
  $('u-warn').textContent = '';
  $('u-parse').disabled = true;
  $('u-run').disabled = true;
  try {
    const parsed = await api('/api/task', {method: 'POST', headers,
      body: JSON.stringify(collectTask())});
    if (request !== app.parseRequest || signature !== taskInputSignature()) return;
    app.parsed = parsed;
    app.parsedInput = signature;
    const box = $('u-parse-body');
    box.hidden = false;
    const s = parsed.structured || {};
    box.innerHTML = '<p class="' + (parsed.ready ? 'note' : 'note bad') + '">' +
      escapeHtml(parsed.ready ? '条件已确认，可以开始核查。' : '请补齐或更正 ' +
        (parsed.missing || []).length + ' 项：') +
      '</p>' +
      (parsed.ready ? dl([['核查问题', s.user_question],
          ['观测时窗（UTC）', txt(s.window_start) + ' → ' + txt(s.window_end)],
          ['台站或区域', (s.region || {}).kind === 'stations' ?
            (s.region.stations || []).join('、') : (s.region || {}).label || '未选择'],
          ['目标频带', band(s.target_band_hz || [1,12])],
          ['分析目标', GOAL_LABEL[s.method_goal] || s.method_goal]]) :
        '<ul>' + (parsed.missing || []).map(m => '<li>' +
          escapeHtml(missingTaskText([m])) + '</li>').join('') + '</ul>') +
      (parsed.notes || []).map(note => '<p class="note">' + escapeHtml(displayText(note)) + '</p>').join('') +
      (parsed.unsupported || []).map(item => '<p class="note bad">' + escapeHtml(displayText(item.statement)) + '</p>').join('') +
      '<p class="note">确认条件免费。开始核查后，将按你选择的方式分析。</p>';
    updateTaskOptions();
    return parsed;
  } catch (e) {
    if (request === app.parseRequest) $('u-warn').textContent = '暂时无法确认条件：' +
      (e.missing ? missingTaskText(e.missing) : userError(e.message));
    return null;
  } finally {
    if (request === app.parseRequest) {
      $('u-parse').disabled = app.taskRunning;
      updateTaskOptions();
    }
  }
}

async function startRun() {
  if (app.taskRunning) return;
  const mode = $('u-mode').value || 'agent';
  if (mode === 'agent' && app.health?.model_available === false) {
    $('u-warn').textContent = userError(app.health.model_unavailable_reason || '智能核查暂不可用，请稍后重试。');
    return;
  }
  setTaskBusy(true);
  $('u-run').textContent = '正在检查条件…';
  const signature = taskInputSignature();
  const parsed = app.parsed?.ready && app.parsedInput === signature
    ? app.parsed : await parseTask({forStart: true});
  if (!parsed?.ready || signature !== taskInputSignature()) {
    setTaskBusy(false);
    if (parsed && !parsed.ready) $('u-warn').textContent = missingTaskText(parsed.missing);
    else if (signature !== taskInputSignature()) $('u-warn').textContent = '条件已变更，请按当前条件重新开始核查。';
    return;
  }
  $('u-warn').textContent = '条件已确认，正在开始' + (mode === 'agent' ? '智能核查…' : '标准核查…');
  $('u-run').textContent = '正在提交…';
  try {
    const started = await api('/api/review', {method: 'POST', headers,
      body: JSON.stringify({task: collectTask(), mode})});
    app.activeTrackId = started.run_id;
    $('u-run').textContent = '核查中…';
    $('run-state').textContent = '核查中';
    $('u-warn').textContent = (mode === 'agent' ? '智能核查' : '标准核查') + '已开始，正在分析观测。';
    $('run-load-note').hidden = false;
    $('run-load-note').textContent = '新核查正在进行，完成后将显示本次结果。下方为上一次打开的记录。';
    $('conclusion-card').scrollIntoView({behavior: 'smooth', block: 'start'});
    app.poll = setInterval(() => pollRun(started.run_id), 800);
  } catch (e) {
    $('u-warn').textContent = '暂时无法开始：' + (e.missing ? missingTaskText(e.missing) : userError(e.message));
    setTaskBusy(false);
  }
}

async function pollRun(trackId, feedback = false) {
  if (trackId !== app.activeTrackId || app.pollInFlight) return;
  app.pollInFlight = true;
  let data;
  const notice = $(feedback ? 'fb-warn' : 'u-warn');
  try {
    data = await api('/api/review/' + trackId);
  } catch (e) {
    if (trackId !== app.activeTrackId) return;
    notice.textContent = '暂时无法获取核查进展：' + userError(e.message) +
      (e.status === 404 ? '请在核查记录中查看本次结果。'
        : '正在重新连接，请稍候。');
    $('run-state').textContent = '正在重新连接';
    if (e.status === 404) {
      clearInterval(app.poll); app.poll = null;
      app.activeTrackId = null;
      setTaskBusy(false);
    }
    return;
  } finally { app.pollInFlight = false; }
  if (trackId !== app.activeTrackId) return;
  if (data.status === 'running') {
    const log = data.log || [];
    const latest = log[log.length - 1];
    notice.textContent = latest?.message ? humanTaskMessage(latest.message) : '核查正在进行，请稍候。';
    $('run-state').textContent = '核查中';
    return;
  }
  clearInterval(app.poll); app.poll = null;
  app.activeTrackId = null;
  setTaskBusy(false);
  notice.textContent = data.status === 'done' ? '本轮核查已完成，可查看对应记录。'
    : '本次核查未完成：' + userError(data.error || '请查看核查记录中的原因。');
  if ((data.status === 'done' || data.status === 'failed') && data.result) {
    // 失败的 job 也带着真实结果：直接展示本次 run 的证据。
    const rid = data.result.run_id;
    if (rid) await loadRun(rid, {replay: false});
    else renderResultFallback(data.result);
  } else {
    $('conclusion-body').innerHTML = '<p class="note bad">核查未完成：' +
      escapeHtml(userError(data.error || '请稍后重试。')) + '</p>';
  }
  if (feedback) $('fb-result').textContent = notice.textContent;
  loadHealth(); loadRecords({refresh: true});
}

function renderResultFallback(result) {
  const box = $('conclusion-body');
  const d = result.delivered || {};
  box.innerHTML = '<div class="statusline"><span class="status-badge status-failed">' +
    '核查未完成</span></div>' +
    '<div class="verdict-big">' + escapeHtml(VERDICT_LABEL[d.verdict] || displayText(d.verdict || '暂无结论')) + '</div>' +
    '<p class="note">' + escapeHtml(userError(result.statement_zh || result.error || '请稍后重试。')) + '</p>';
}

/* ----------------------------------------------------------- 反馈 */
function currentAgentParent() {
  const v = app.view;
  if (!v || !v.runId) return null;
  const agent = String(v.config.driver || '').includes('agent_run_optimization') ||
    String(v.arm || '').startsWith('adaptive_agent');
  return agent && !v.fixture && !v.internal &&
    ['ok', 'unknown', 'fallback'].includes(v.status.kind) && v.acquisition.length ? v.runId : null;
}

async function sendFeedback(change, instruction) {
  if (app.taskRunning) return;
  const parent = currentAgentParent();
  if (!parent) { $('fb-warn').textContent = '当前记录无法继续智能复核，请选择有观测数据的已完成记录。'; return; }
  $('fb-warn').textContent = '';
  setTaskBusy(true);
  $('fb-result').innerHTML = '<p class="hint">正在按新条件复核…</p>';
  $('run-state').textContent = '复核中';
  try {
    const started = await api('/api/followup', {method: 'POST', headers,
      body: JSON.stringify({parent_run_id: parent, change, instruction,
                            engine: 'agent'})});
    app.activeTrackId = started.run_id;
    app.poll = setInterval(() => pollFeedback(started.run_id), 800);
  } catch (e) {
    $('fb-warn').textContent = '暂时无法复核：' + userError(e.message);
    $('fb-result').textContent = '本次复核尚未开始。';
    setTaskBusy(false);
    $('run-state').textContent = app.runId ? '查看记录' : '待核查';
  }
}

async function pollFeedback(trackId) {
  return pollRun(trackId, true);
}

/* ------------------------------------------------------------ 绑定 */
function bind() {
  bindTerms();
  for (const b of document.querySelectorAll('.navbtn')) {
    b.onclick = () => setView(b.dataset.view);
    b.onkeydown = event => {
      const tabs = [...document.querySelectorAll('.navbtn')];
      const index = tabs.indexOf(b);
      const target = event.key === 'ArrowRight' ? (index + 1) % tabs.length
        : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length
          : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
      if (target < 0) return;
      event.preventDefault();
      setView(tabs[target].dataset.view);
      tabs[target].focus();
    };
  }
  $('btn-example').onclick = () => showDemoSection('conclusion');
  $('btn-new').onclick = () => {
    setView('workbench');
    const card = $('newtask-card');
    card.open = true;
    card.scrollIntoView({behavior: 'smooth', block: 'start'});
  };
  $('copy-run').onclick = async () => {
    if (!app.runId) return;
    try {
      if (!navigator.clipboard) throw new Error('剪贴板不可用');
      const link = new URL(location.href);
      link.searchParams.set('view', 'workbench');
      link.searchParams.set('run', app.runId);
      await navigator.clipboard.writeText(link.href);
      $('copy-run').textContent = '链接已复制';
      setTimeout(() => { $('copy-run').textContent = '复制链接'; }, 1200);
    } catch (e) {
      showMessage('无法自动复制，请复制浏览器地址栏中的链接。', true);
    }
  };
  $('records-refresh').onclick = () => loadRecords({refresh: true});
  $('record-filter').onchange = () => { app.recordLimit = 12; renderRecords(); };
  $('record-search').oninput = () => { app.recordLimit = 12; renderRecords(); };
  $('records-more').onclick = () => { app.recordLimit += 12; renderRecords(); };
  for (const detail of document.querySelectorAll('#view-records > details')) {
    detail.ontoggle = () => { if (detail.open) loadLegacyCases(); };
  }
  window.addEventListener('popstate', async () => {
    const params = new URLSearchParams(location.search);
    const view = params.get('view') || 'workbench';
    const runId = params.get('run') || EXAMPLE_FIRST;
    setView(view, {historyMode: 'none'});
    if (runId !== app.runId) await loadRun(runId, {navigate: false});
  });
  $('u-parse').onclick = parseTask;
  $('u-run').onclick = startRun;
  $('u-fill-example').onclick = fillExampleTask;
  $('u-fill-example').disabled = true;
  $('about-show-conclusion').onclick = () => showDemoSection('conclusion');
  $('about-show-evidence').onclick = () => showDemoSection('evidence');
  $('about-show-feedback').onclick = () => showDemoSection('feedback');
  for (const id of ['u-question', 'u-start', 'u-end', 'u-stations', 'u-region',
                    'u-band-lo', 'u-band-hi', 'u-goal', 'u-mode']) {
    const control = $(id);
    const changed = () => {
      if (id === 'u-start' || id === 'u-end') $('u-period').value = 'custom';
      if (id === 'u-stations') {
        $('u-station-preset').value = 'custom';
        if (control.value.trim()) $('u-region').value = '';
      }
      if (id === 'u-region' && control.value) {
        $('u-stations').value = ''; $('u-station-preset').value = 'custom';
      }
      invalidateTask();
    };
    control.oninput = changed;
    control.onchange = changed;
  }
  $('u-period').onchange = () => {
    invalidateTask();
    if ($('u-period').value === 'example') {
      const form = (app.guide || {}).form;
      if (!form) {
        $('u-period').value = 'custom';
        $('u-warn').textContent = '示例时段暂不可用，请填写 UTC 时间。'; return;
      }
      $('u-start').value = form.window_start.replace(/Z$/, '');
      $('u-end').value = form.window_end.replace(/Z$/, '');
    }
    updateTaskOptions();
  };
  $('u-station-preset').onchange = () => {
    if ($('u-station-preset').value !== 'custom') {
      $('u-stations').value = $('u-station-preset').value;
      $('u-region').value = '';
    }
    invalidateTask();
  };
  for (const button of document.querySelectorAll('[data-band]')) {
    button.onclick = () => {
      const values = button.dataset.band.split(',');
      $('u-band-lo').value = values[0]; $('u-band-hi').value = values[1];
      invalidateTask();
    };
  }
  $('u-reset').onclick = () => {
    for (const id of ['u-question', 'u-start', 'u-end', 'u-stations',
                      'u-band-lo', 'u-band-hi']) $(id).value = '';
    $('u-region').value = ''; $('u-goal').value = ''; $('u-mode').value = 'agent';
    $('u-period').value = 'custom'; $('u-station-preset').value = 'custom';
    invalidateTask();
  };
  $('fb-exclude').onclick = () => sendFeedback(
    {kind: 'exclude', candidate_id: $('fb-station').value},
    '排除该台站，复用已存波形，不新增取数');
  $('fb-include').onclick = () => sendFeedback(
    {kind: 'include', candidate_id: $('fb-station').value},
    '恢复该台站并复核');
  $('fb-goal-apply').onclick = () => sendFeedback(
    {kind: 'method_goal', method_goal: $('fb-goal').value},
    '切换方法目标后复核，不新增取数');
  $('fb-grid-apply').onclick = () => {
    const parts = JSON.parse($('fb-grid').value);
    sendFeedback({kind: 'stability_bands', target_band_hz: parts},
                 '把到时诊断网格收紧到 ' + parts[0] + '–' + parts[1] + ' Hz，不新增取数');
  };
}

(async function init() {
  bind();
  loadHealth();
  loadDemoGuide();
  await loadOptions().catch(() => {
    $('u-warn').textContent = '核查选项暂时无法读取，请刷新页面重试。';
  });
  setInterval(loadHealth, 20000);
  // 默认打开已验收真实示例（只读回放，不调用模型）。
  // 支持深链 /?run=<id> 与 /?view=records|about。
  const params = new URLSearchParams(location.search);
  const view = params.get('view');
  const runId = params.get('run');
  await loadRun(runId || EXAMPLE_FIRST, {replay: true, navigate: false});
  setView(view || 'workbench', {historyMode: 'replace'});
  // A deep-linked view wins over the workbench that loadRun defaults to.
  if (view) setView(view, {historyMode: 'none'});
  const observer = new ResizeObserver(() => drawWave(app.waveform));
  observer.observe($('wave-canvas'));
})();
