// Test-only mock model transport for the free AGH lifecycle integration probe.
//
// It replaces ONLY the upstream DeepSeek HTTP call. Everything else — the AGH
// daemon, the MCP stdio child, the provider bridge's budget/tool guards and the
// resource ownership records — is the real product code. No network request and no
// model spend happen here.
//
// Behaviour, driven by env:
//   EQ_MOCK_RUN_DIR   the run directory, read to pick a real tool name/candidate
//   EQ_MOCK_HANG=1    never answer (used to force the session timeout)
//   EQ_MOCK_TURNS     number of tool-calling turns before the final answer (default 1)
import {readFileSync} from 'node:fs';
import {join} from 'node:path';

const hang = process.env.EQ_MOCK_HANG === '1';
const fixedRunDir = process.env.EQ_MOCK_RUN_DIR || null;
const runlogRoot = process.env.EQ_MOCK_RUNLOG_DIR || null;
const turns = Number(process.env.EQ_MOCK_TURNS || '1');

// Resolve the run directory dynamically when the caller (the real page/server)
// assigns the run id: the prompt the driver sends carries `SEISMIC_REVIEW_RUN=<id>`.
function runDirFor(body) {
  if (fixedRunDir) return fixedRunDir;
  if (!runlogRoot) return null;
  const match = JSON.stringify(body.messages || []).match(/SEISMIC_REVIEW_RUN=([A-Za-z0-9_-]+)/);
  return match ? join(runlogRoot, match[1]) : null;
}

function state(runDir) {
  if (!runDir) return {};
  try {
    return JSON.parse(readFileSync(join(runDir, 'mcp_state.initial.json'), 'utf8'));
  } catch {
    return {};
  }
}

function pickTool(body, snapshot) {
  const names = (body.tools || []).map(t => t.function?.name).filter(Boolean);
  const pick = suffix => names.find(n => n.endsWith(suffix));
  const acquired = snapshot.acquired || [];
  const changes = (snapshot.requested_conditions || {}).requested_changes || [];
  const wantsGrid = changes.some(c => c && c.kind === 'stability_bands');
  // A user-narrowed diagnostic grid is a real obligation: run the check with the
  // current grid so the rehearsal exercises the same tool the real model must call.
  if (acquired.length && wantsGrid) {
    const stability = pick('run_stability_check');
    if (stability) return {name: stability, arguments: {candidate_id: acquired[0].candidate_id}};
  }
  if (acquired.length) {
    const candidate = acquired[0].candidate_id;
    const onset = pick('run_onset_check');
    if (onset) return {name: onset, arguments: {candidate_id: candidate}};
  }
  const status = pick('task_status');
  if (status) return {name: status, arguments: {stage: 'progress'}};
  const list = pick('list_candidates');
  if (list) return {name: list, arguments: {limit: 3}};
  return null;
}

function finalAnswer(snapshot) {
  const requested = snapshot.requested_conditions || {};
  const acquired = snapshot.acquired || [];
  return JSON.stringify({
    field: requested.required_field || 'verdict',
    conclusion: 'not_decidable',
    condition_id: requested.condition_id || 'mock-condition',
    scope_candidate_ids: acquired.map(r => r.candidate_id),
    evidence_refs: [],
    stop_reason_code: 'unanswered_no_data',
    limitations: ['mock provider: lifecycle probe only, no scientific claim'],
  });
}

export async function transport(url, options) {
  const body = JSON.parse(options.body);
  const snapshot = state(runDirFor(body));
  const messages = body.messages || [];
  const toolTurns = messages.filter(m => m.role === 'tool').length;
  const assistantTurns = messages.filter(m => m.role === 'assistant').length;
  const answer = (message, finish) => ({
    ok: true,
    status: 200,
    json: async () => ({
      id: 'mock-' + assistantTurns,
      object: 'chat.completion',
      created: 0,
      model: body.model || 'mock',
      choices: [{index: 0, message, finish_reason: finish}],
      usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2},
    }),
  });
  if (hang) {
    // Hold the request open with a promise that owns no timer handle: the driver's
    // session timer must be what ends this run, and the process must still be able
    // to exit once cleanup has closed every real resource.
    await new Promise(() => {});
  }
  if (toolTurns < turns && assistantTurns <= turns) {
    const tool = pickTool(body, snapshot);
    if (tool) {
      return answer({
        role: 'assistant',
        content: null,
        tool_calls: [{id: 'mock-tc-' + assistantTurns, type: 'function',
                      function: {name: tool.name, arguments: JSON.stringify(tool.arguments)}}],
      }, 'tool_calls');
    }
  }
  return answer({role: 'assistant', content: finalAnswer(snapshot)}, 'stop');
}