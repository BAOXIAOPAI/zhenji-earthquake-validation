// Free integration probe for remediation package D: failure terminal state, the
// session timeout, and reclamation of THIS run's resources.
//
// The AGH daemon, the MCP stdio child, the provider bridge guards and the owned
// resource records are all the real product code. ONLY the upstream model HTTP
// call is mocked (evidence/mock_provider.mjs), so no network request and no model
// spend occur. A second AGH daemon in a separate home is started as a sentinel;
// its PID and socket are captured BEFORE the runs and must be unchanged after, so a
// CLI auto-restart cannot be mistaken for "unaffected".
//
// Each scenario gets a CLEAN input run directory (fresh task/state/discovery), so
// the runtime-owner, PID and socket it verifies are this run's own — never a copied
// historical owner.
//
// Run from the project root:
//   node delivery/remediation-2026-10-12/evidence/agh_resource_integration.mjs
import {execFile} from 'node:child_process';
import {existsSync,readFileSync,writeFileSync,mkdirSync,cpSync,rmSync} from 'node:fs';
import {mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {dirname,resolve,join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const project = resolve(here, '../../..');
const tsx = join(project, 'vendor/agnes-harness/node_modules/.bin/tsx');
const cliEntry = join(project, 'vendor/agnes-harness/packages/cli/dist/local/agnes.mjs');
const OUT = join(here, 'agh_resource_integration.json');

const report = {schema: 'remediation-d-integration/v2',
  network_or_model_calls: 0, upstream_is_mock: true,
  input_run_dirs: 'freshly seeded per scenario (no copied historical owner)',
  scenarios: {}, checks: {}};

const execFileP = (bin, args, options = {}) => new Promise((resolvePromise, reject) => {
  const child = execFile(bin, args, {maxBuffer: 2 ** 23, ...options},
    (error, stdout, stderr) => {
      if (error && error.code === undefined && error.killed) return reject(error);
      resolvePromise({code: error?.code ?? 0, stdout: stdout ?? '', stderr: stderr ?? ''});
    });
  child.stdin?.end?.();
});

const readJson = path => JSON.parse(readFileSync(path, 'utf8'));

function makeHome(home) {
  mkdirSync(join(home, 'profiles/local-dev'), {recursive: true, mode: 0o700});
  writeFileSync(join(home, 'profiles/local-dev/profile.yaml'),
    'name: local-dev\ncomputerUse:\n  enabled: false\n');
}

const cliEnv = home => ({...process.env, TMPDIR: '/tmp', AGH_HOME: home,
  AGNES_PROFILE: 'local-dev'});

// A clean, self-consistent run directory: no inherited owner, socket, tool log or
// acquisition history. Only the sample candidate the mock may act on.
function seedCleanRun(runId, runDir) {
  mkdirSync(runDir, {recursive: true});
  const windowStart = '2025-03-03T13:02:20', windowEnd = '2025-03-03T13:04:20';
  const candidate = {candidate_id: 'probe-cand-01', network: 'UW',
    station: 'PROBE', distance_km: 12.5, family: 'HH?',
    analysis_eligible: true, three_component: true, vertical_only: false,
    reason: 'probe', band_support: {supported: true, nyquist_hz: 50.0,
      target_band_hz: [1.0, 12.0], sampling_rate_hz: 100.0,
      required_rate_hz: 24.0, reason: 'ok'}};
  const discovery = {station_query_url: 'probe://clean', station_rows: 1,
    candidates: [candidate], eligible: [candidate], n_eligible: 1,
    target_band_hz: [1.0, 12.0], method_band_hz: [1.0, 8.0]};
  const requested = {
    schema: 'seismic-execution-conditions/v1', question_id: 'Q_usable',
    required_field: 'method_applicability',
    scope: {scope_kind: 'all_allowed', stations: [], candidate_ids: []},
    method_goal: 'three_component_pick', target_band_hz: [1.0, 12.0],
    onset_method_band_hz: [1.0, 8.0], diagnostic_bands_hz: null,
    acquisition_allowed: true, excluded_candidate_ids: [],
    max_unique_candidates_episode: 6,
    condition_id: 'probe-condition-' + runId};
  const task = {schema: 'seismic-task/v1', task_id: 't-' + runId,
    created_at: new Date().toISOString(),
    user_question: '这个探针窗口能否用于 1–12 Hz 的 P/S 拾取？',
    window_start: windowStart, window_end: windowEnd,
    region: {kind: 'box', min_lat: 47, max_lat: 49, min_lon: -124, max_lon: -121},
    target_band_hz: [1.0, 12.0], method_band_hz: [1.0, 8.0],
    method_goal: 'three_component_pick', question_id: 'Q_usable',
    scope: requested.scope, diagnostic_bands_hz: null, acquisition_allowed: true,
    excluded_candidate_ids: [], max_unique_candidates_episode: 6,
    operational_standard: null, demo_case_ref: null,
    acceptance: {kind: 'probe', split: 'probe'},
    requested_conditions: requested};
  const state = {run_id: runId, task_id: task.task_id, user_question: task.user_question,
    window_start: windowStart, window_end: windowEnd, target_band_hz: [1.0, 12.0],
    method_band_hz: [1.0, 8.0], method_goal: 'three_component_pick',
    method_applicability_goal: 'three_component_pick', question_id: 'Q_usable',
    scope: requested.scope, requested_conditions: requested,
    diagnostics_bands_hz: null, acquisition_allowed: true, excluded_candidate_ids: [],
    max_unique_candidates_episode: 6, analysis_level: 'quality_only',
    exclusions: [], attempted_candidate_ids_episode: [], condition_applications: [],
    stability_checks: {}, derived_evidence_checks: {}, discovery: {n_eligible: 1,
      eligible: [candidate]}, acquired: [], requests: 0, http_attempts: 0,
    logical_acquisitions: 0, cache_hits: 0, bytes: 0};
  writeFileSync(join(runDir, 'task.json'), JSON.stringify(task, null, 2));
  writeFileSync(join(runDir, 'discovery.json'), JSON.stringify(discovery, null, 2));
  writeFileSync(join(runDir, 'mcp_state.initial.json'), JSON.stringify(state, null, 2));
  writeFileSync(join(runDir, 'mcp_state.json'), JSON.stringify(state, null, 2));
  writeFileSync(join(runDir, 'evaluator.json'), JSON.stringify({
    task_id: task.task_id, note: 'probe only'}, null, 2));
}

async function sentinelStart(home) {
  makeHome(home);
  const shown = await execFileP(process.execPath, [cliEntry, 'sessions', '--json'],
    {cwd: project, env: cliEnv(home), timeout: 120000});
  if (shown.code !== 0) throw Error('sentinel daemon failed: ' + shown.stderr.slice(0, 200));
  const ownerPath = join(home, 'data/daemon/owner.json');
  const owner = existsSync(ownerPath) ? readJson(ownerPath) : null;
  const socket = owner?.socketPath;
  return {home, owner, pid: owner?.pid ?? null, socket_path: socket,
    socket_existed_at_start: socket ? existsSync(socket) : null};
}

async function sentinelIdentity(sentinel) {
  const socketExists = sentinel.socket_path ? existsSync(sentinel.socket_path) : null;
  let reachable = false;
  try {
    const shown = await execFileP(process.execPath, [cliEntry, 'sessions', '--json'],
      {cwd: project, env: cliEnv(sentinel.home), timeout: 120000});
    reachable = shown.code === 0;
  } catch { reachable = false; }
  const ownerPath = join(sentinel.home, 'data/daemon/owner.json');
  const nowOwner = existsSync(ownerPath) ? readJson(ownerPath) : null;
  return {reachable, socket_exists: socketExists,
    pid: nowOwner?.pid ?? null, socket_path: nowOwner?.socketPath ?? null,
    same_pid: (nowOwner?.pid ?? null) === sentinel.pid,
    same_socket: (nowOwner?.socketPath ?? null) === sentinel.socket_path};
}

async function main() {
  const temp = await mkdtemp(join(tmpdir(), 'eq-resource-'));
  const runlogRoot = join(temp, 'runlog');
  mkdirSync(runlogRoot, {recursive: true});

  const sentinelHome = join(temp, 'sentinel-home');
  let sentinel = null;
  try {
    sentinel = await sentinelStart(sentinelHome);
  } catch (error) {
    report.sentinel = {error: String(error?.message ?? error).slice(0, 300)};
  }

  async function runDriver(name, {hang, sessionTimeoutMs, graceMs}) {
    const runId = 'r-' + name;
    const runDir = join(runlogRoot, runId);
    seedCleanRun(runId, runDir);
    const env = {
      ...process.env, RUNLOG_DIR: runlogRoot,
      EQ_CACHE_DIR: join(temp, 'cache'), EQ_OUTPUT_DIR: join(temp, 'out'),
      EQ_STRICT_OFFLINE: '0',
      EQ_MOCK_PROVIDER: join(here, 'mock_provider.mjs'), EQ_MOCK_RUN_DIR: runDir,
      EQ_MOCK_HANG: hang ? '1' : '0',
      EQ_SESSION_TIMEOUT_MS: String(sessionTimeoutMs),
      EQ_CLEANUP_GRACE_MS: String(graceMs),
      EQ_BUDGET_LEDGER: join(temp, 'ledger.json'),
      EQ_REQUEST_LOG_DIR: join(temp, 'live-requests'),
      MPLCONFIGDIR: join(temp, 'mpl'), XDG_CACHE_HOME: join(temp, 'xdg'), TMPDIR: '/tmp',
    };
    const started = Date.now();
    const proc = await execFileP(tsx, ['main/agent_run_optimization.mjs', runId, 'A1',
      'deepseek-flash'], {cwd: project, env, timeout: 240000});
    const recordPath = join(runDir, `agent_execution_A1_${runId}.json`);
    const record = existsSync(recordPath) ? readJson(recordPath) : null;
    const cleanupPath = join(runDir, 'cleanup_status.json');
    const cleanup = existsSync(cleanupPath) ? readJson(cleanupPath) : null;
    const ownerPath = join(runDir, 'runtime-owner.json');
    const owner = existsSync(ownerPath) ? readJson(ownerPath) : null;
    const socket = owner?.socket_path ?? null;
    const nativePath = join(runDir, 'native_agh_evidence.json');
    const native = existsSync(nativePath) ? readJson(nativePath) : null;
    // runtime-owner.json.pid is the DRIVER process that wrote it; the AGH daemon
    // pid is the owner captured in native_agh_evidence.json (data/daemon/owner.json
    // before cleanup). They are different processes and are reported separately.
    const driverPid = owner?.pid ?? null;
    const daemonPid = native?.owner?.pid ?? null;
    return {
      name, run_id: runId, exit_code: proc.code, elapsed_ms: Date.now() - started,
      stdout: proc.stdout.trim().slice(-300), stderr: proc.stderr.trim().slice(-300),
      record_status: record?.status ?? null, failure_kind: record?.failure_kind ?? null,
      cleanup_ok: record?.cleanup_ok ?? null, cleanup_needed: record?.cleanup_needed ?? null,
      stop_confirmed: record?.cleanup?.stop_confirmed ?? cleanup?.stop_confirmed ?? null,
      root_removed: record?.root_removed ?? cleanup?.root_removed ?? null,
      owned_home: record?.owned_home ?? cleanup?.owned_home ?? null,
      owned_home_removed: record?.owned_home ? !existsSync(record.owned_home) : null,
      owner_run_id: owner?.run_id ?? null, owner_run_matches: owner?.run_id === runId,
      driver_pid: driverPid, driver_pid_exited: driverPid ? !pidAlive(driverPid) : null,
      daemon_pid: daemonPid, daemon_pid_exited: daemonPid ? !pidAlive(daemonPid) : null,
      socket_path: socket, socket_removed: socket ? !existsSync(socket) : null,
      session_timed_out: record?.session_timed_out ?? null,
      model_requests: record?.model_requests ?? null,
      tool_proposals: record?.tool_proposals ?? null,
      cleanup_errors: record?.cleanup?.cleanup_errors ?? cleanup?.errors ?? null,
      native_evidence_present: existsSync(nativePath),
      native_tool_pair: native?.native_tool_pair ?? null,
      native_sessions_db: native?.native_sessions_db ?? null,
      native_host_audit: native?.native_host_audit ?? null,
      mcp_tool_pair: native?.mcp_tool_pair ?? null,
    };
  }

  const normal = await runDriver('normal', {hang: false, sessionTimeoutMs: 120000,
    graceMs: 45000});
  report.scenarios.normal = normal;
  report.checks.normal_succeeded = normal.exit_code === 0
    && normal.record_status === 'succeeded' && normal.cleanup_ok === true;
  report.checks.normal_resources_reclaimed = normal.stop_confirmed === true
    && normal.root_removed === true && normal.owned_home_removed === true
    && normal.socket_removed === true && normal.driver_pid_exited === true
    && normal.daemon_pid_exited === true;
  report.checks.normal_owner_is_this_run = normal.owner_run_matches === true;
  report.checks.normal_native_evidence_saved = normal.native_evidence_present === true
    && normal.native_sessions_db === 'native_sessions.db'
    && normal.native_tool_pair !== null;
  report.checks.normal_real_tool_call = (normal.tool_proposals ?? 0) > 0
    && normal.mcp_tool_pair !== null;

  const timeout = await runDriver('timeout', {hang: true, sessionTimeoutMs: 4000,
    graceMs: 45000});
  report.scenarios.timeout = timeout;
  report.checks.timeout_failed = timeout.exit_code === 3
    && timeout.record_status === 'failed' && timeout.failure_kind === 'timeout';
  report.checks.timeout_still_reclaimed = timeout.stop_confirmed === true
    && timeout.root_removed === true && timeout.owned_home_removed === true
    && timeout.socket_removed === true && timeout.driver_pid_exited === true
    && timeout.daemon_pid_exited === true;

  if (sentinel) {
    const alive = await sentinelIdentity(sentinel);
    report.sentinel = {home: sentinel.home, pid_at_start: sentinel.pid,
      socket_at_start: sentinel.socket_path,
      socket_existed_at_start: sentinel.socket_existed_at_start, after: alive};
    report.checks.other_session_unaffected = alive.reachable === true
      && alive.socket_exists === true && alive.same_pid === true
      && alive.same_socket === true && sentinel.socket_existed_at_start === true;
  } else {
    report.checks.other_session_unaffected = null;
  }

  const artifactRoot = join(here, 'artifacts');
  for (const [name, scenario] of Object.entries({normal, timeout})) {
    const src = join(runlogRoot, scenario.run_id);
    const dst = join(artifactRoot, name);
    rmSync(dst, {recursive: true, force: true});
    mkdirSync(dst, {recursive: true});
    for (const file of ['native_agh_evidence.json', 'native_sessions.db',
      'native_host_audit.jsonl', 'cleanup_status.json', 'runtime-owner.json',
      'prompt_binding.json', 'tool_calls.jsonl', 'model_response.json']) {
      if (existsSync(join(src, file))) cpSync(join(src, file), join(dst, file));
    }
    const record = join(src, `agent_execution_A1_${scenario.run_id}.json`);
    if (existsSync(record)) cpSync(record, join(dst, 'agent_execution.json'));
  }

  const checks = report.checks;
  report.summary = {total: Object.keys(checks).length,
    passed: Object.values(checks).filter(v => v === true).length,
    failed: Object.keys(checks).filter(k => checks[k] !== true)};

  if (sentinel) {
    await execFileP(process.execPath, [cliEntry, 'daemon', 'stop'],
      {cwd: project, env: cliEnv(sentinel.home), timeout: 120000});
  }
  rmSync(temp, {recursive: true, force: true});
  writeFileSync(OUT, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  if (report.summary.failed.length) process.exitCode = 1;
}

function pidAlive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

await main();