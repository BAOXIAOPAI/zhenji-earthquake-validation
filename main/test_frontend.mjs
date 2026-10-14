import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('./static/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('./static/index.html', import.meta.url), 'utf8');
const initAt = source.lastIndexOf('(async function init() {');
assert.ok(initAt >= 0, 'The browser startup must be isolated from unit tests.');

// Exercise production functions with a small DOM and an instrumented canvas.
// No browser, network service, model invocation, or real clipboard is needed.
function frontend({fetch, clipboard, dpr = 1, width = 720, height = 320,
  viewportWidth = 1280, viewportHeight = 900} = {}) {
  const nodes = new Map();
  const allNodes = [];
  const canvasCalls = [];
  const listeners = new Map();
  const documentListeners = new Map();
  const historyCalls = [];
  const timers = new Map();
  let timerId = 0;
  const location = {href: 'http://127.0.0.1:8791/', search: '', pathname: '/', hash: ''};
  const setURL = value => {
    const url = new URL(value, location.href);
    Object.assign(location, {href: url.href, search: url.search,
      pathname: url.pathname, hash: url.hash});
  };
  const history = {
    state: null,
    pushState(state, _title, url) {
      this.state = state;
      if (url != null) setURL(url);
      historyCalls.push({method: 'pushState', url: location.href});
    },
    replaceState(state, _title, url) {
      this.state = state;
      if (url != null) setURL(url);
      historyCalls.push({method: 'replaceState', url: location.href});
    },
  };
  const ctx = {};
  for (const name of ['setTransform', 'fillRect', 'clearRect', 'beginPath',
    'moveTo', 'lineTo', 'stroke', 'fillText', 'setLineDash', 'save', 'restore']) {
    ctx[name] = (...args) => canvasCalls.push({name, args});
  }
  ctx.measureText = text => ({width: String(text).length * 6});

  let document;
  const element = (tagName, id = '') => {
    const attrs = new Map();
    const handlers = new Map();
    let value = '';
    const node = {
      tagName: tagName.toUpperCase(), id, className: '', dataset: {}, style: {},
      hidden: false, disabled: false, textContent: '', innerHTML: '',
      children: [], tabIndex: 0, clientWidth: width, clientHeight: height,
      get value() { return value; },
      set value(next) { value = String(next); },
      get options() { return this.children; },
      get classList() {
        const classes = () => new Set(this.className.split(/\s+/).filter(Boolean));
        return {
          contains: name => classes().has(name),
          toggle: (name, force) => {
            const current = classes();
            const enabled = force === undefined ? !current.has(name) : force;
            enabled ? current.add(name) : current.delete(name);
            this.className = [...current].join(' ');
            return enabled;
          },
          add: (...names) => { this.className = [...classes(), ...names].join(' '); },
          remove: (...names) => {
            this.className = [...classes()].filter(name => !names.includes(name)).join(' ');
          },
        };
      },
      appendChild(child) {
        child.parentElement = this; this.children.push(child); return child;
      },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      remove(index) { this.children.splice(index, 1); },
      setAttribute(name, value) { attrs.set(name, String(value)); },
      getAttribute(name) { return attrs.get(name) ?? null; },
      removeAttribute(name) { attrs.delete(name); },
      addEventListener(name, fn) { handlers.set(name, fn); },
      focus() {
        document.activeElement = this;
        document.dispatch('focusin', {target: this});
      },
      scrollIntoView(options) { this.scrolled = options; },
      getBoundingClientRect() {
        return this.rect || {width, height, left: 0, top: 0, right: width, bottom: height};
      },
      matches(selector) {
        if (selector === ':hover') return !!this.hovered;
        const data = /^\[data-([\w-]+)\]$/.exec(selector);
        return data ? this.dataset[data[1]] !== undefined : false;
      },
      closest(selector) {
        return this.matches(selector) ? this : this.parentElement?.closest(selector) || null;
      },
      contains(target) {
        return this === target || this.children.some(child => child.contains(target));
      },
      getContext() { return ctx; },
      querySelectorAll() { return this.children; },
    };
    allNodes.push(node);
    return node;
  };
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*(?:\bid="[^"]+"|\bdata-(?:term|band)="[^"]+")[^>]*)>/g)) {
    const [, tag, attrs] = match;
    const id = /\bid="([^"]+)"/.exec(attrs)?.[1] || '';
    const node = element(tag, id);
    node.hidden = /\bhidden(?:\s|>|$)/.test(attrs);
    node.className = /\bclass="([^"]*)"/.exec(attrs)?.[1] || '';
    node.dataset.view = /\bdata-view="([^"]*)"/.exec(attrs)?.[1];
    for (const attr of attrs.matchAll(/([\w-]+)="([^"]*)"/g)) {
      node.setAttribute(attr[1], attr[2]);
      if (attr[1].startsWith('data-')) node.dataset[attr[1].slice(5)] = attr[2];
    }
    if (id) nodes.set(id, node);
  }
  document = {
    activeElement: null,
    getElementById: id => nodes.get(id) || null,
    createElement: tag => element(tag),
    addEventListener(name, fn) {
      if (!documentListeners.has(name)) documentListeners.set(name, []);
      documentListeners.get(name).push(fn);
    },
    dispatch(name, event = {}) {
      for (const fn of documentListeners.get(name) || []) fn(event);
    },
    querySelectorAll: selector => {
      if (selector === '.navbtn' || selector === '.view') {
        return [...nodes.values()].filter(node => node.classList.contains(selector.slice(1)));
      }
      if (selector === '[data-term]' || selector === '[data-band]') {
        return allNodes.filter(node => node.matches(selector));
      }
      return [];
    },
  };
  for (const id of ['term-popover-title', 'term-popover-text', 'term-close']) {
    nodes.get('term-popover').appendChild(nodes.get(id));
  }
  const window = {
    devicePixelRatio: dpr, location, history,
    scrollCalls: [],
    scrollTo(options) { this.scrollCalls.push(options); },
    innerWidth: viewportWidth, innerHeight: viewportHeight,
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
  };
  const context = vm.createContext({
    document, window, location, history, navigator: {clipboard}, URL, URLSearchParams,
    fetch: fetch || (() => { throw new Error('Unexpected fetch in frontend test'); }),
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id), setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: fn => { fn(); return 0; },
    ResizeObserver: class { observe() {} disconnect() {} }, console,
  });
  vm.runInContext(source.slice(0, initAt) + '\n' +
    'globalThis.front = {app, statusOf, normalize, drawWave, loadRun, loadWave, bind, setView,' +
    'TERMS, term, termPosition, openTerm, closeTerm, bindTerms, utcInput, collectTask,' +
    'taskInputSignature, invalidateTask, fillExampleTask, parseTask, startRun,' +
    'pollRun, pollFeedback, sendFeedback, setTaskBusy,' +
    'loadDemoGuide, showDemoSection};',
    context, {filename: 'app.js'});
  return {front: context.front, context, nodes, canvasCalls, listeners,
    historyCalls, location, setURL, document, allNodes,
    runTimers() {
      const pending = [...timers.values()]; timers.clear();
      for (const fn of pending) fn();
    }};
}

function deferredFetch() {
  const requests = [];
  const fetch = (path, options) => new Promise((resolve, reject) => {
    requests.push({path, options, reject,
      resolve: (body, status = 200) => resolve({
        ok: status >= 200 && status < 300, status,
        json: async () => body,
      }),
    });
  });
  return {fetch, requests};
}

const evidence = (runId, overrides = {}) => ({
  run_id: runId,
  outcome: {status: 'succeeded'},
  scope: {arm: 'adaptive_agent_open'},
  validator: {delivered: {verdict: 'pick_applicable', model_claim_accepted: true}},
  finish_audit: {outcome: 'valid_complete'},
  ...overrides,
});

function waveform(sourceLabel = 'saved observation') {
  const channel = offset => ({
    start_utc: '2023-12-24T10:00:00Z', sampling_rate_hz: 20, n_samples: 1200,
    display_center: offset,
    envelope: [[0, offset - 1, offset + 1], [15, offset - 2, offset + 3],
      [30, offset - 1, offset + 2], [45, offset - 3, offset + 1]],
  });
  return {
    source_label: sourceLabel,
    window: {start: '2023-12-24T10:00:00Z', end: '2023-12-24T10:01:00Z'},
    channels: {Z: channel(8), N: channel(-4), E: channel(2)},
    onset: {seconds: 0, computed: true},
  };
}

function deepFreeze(value) {
  if (value && typeof value === 'object') {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

test('empty probes and missing execution status cannot become completed reviews', () => {
  const {front} = frontend();
  const empty = front.statusOf(evidence('probe', {
    internal: true, acquisition: [], tool_calls: [], validator: {delivered: {}},
  }));
  assert.equal(empty.kind, 'empty');
  assert.notEqual(empty.tone, 'ok');

  for (const status of [undefined, 'unrecorded']) {
    const unrecorded = front.statusOf(evidence('no-execution', {
      outcome: status ? {status} : {}, acquisition: [{candidate_id: 'station'}],
    }));
    assert.equal(unrecorded.kind, 'unverified');
    assert.notEqual(unrecorded.tone, 'ok');
  }
});

test('completion distinguishes accepted model delivery from deterministic review', () => {
  const {front} = frontend();
  const model = front.statusOf(evidence('model'));
  const rule = front.statusOf(evidence('rule', {
    scope: {arm: 'user_feedback_recompute'},
    validator: {delivered: {verdict: 'pick_applicable'}},
  }));
  assert.equal(model.kind, 'ok');
  assert.equal(rule.kind, 'ok');
  assert.notEqual(model.detail, rule.detail);
  assert.notEqual(model.badge, rule.badge);
});

test('failure, fallback, legal unknown, and pending remain distinct states', () => {
  const {front} = frontend();
  const cases = [
    [evidence('failed', {outcome: {status: 'failed', failure_kind: 'network'}}), 'failed'],
    [evidence('rejected', {validator: {delivered: {
      verdict: 'pick_unknown', model_claim_accepted: false,
    }}}), 'fallback'],
    [evidence('fallback', {validator: {delivered: {
      verdict: 'pick_unknown', fallback_used: true,
    }}}), 'fallback'],
    [evidence('unknown', {finish_audit: {outcome: 'legal_unknown'}}), 'unknown'],
    [evidence('pending', {outcome: {status: 'running'}, validator: {delivered: {}}}), 'pending'],
  ];
  for (const [input, expected] of cases) {
    const state = front.statusOf(input);
    assert.equal(state.kind, expected, input.run_id);
    assert.notEqual(state.tone, 'ok', input.run_id);
  }
});

test('Retina drawing scales physical pixels and preserves the original observations', () => {
  const h = frontend({dpr: 2, width: 640, height: 320});
  const data = deepFreeze(waveform());
  const before = JSON.stringify(data);
  h.front.drawWave(data);
  const canvas = h.nodes.get('wave-canvas');
  assert.equal(canvas.width, 1280);
  assert.equal(canvas.height, 640);
  assert.deepEqual(h.canvasCalls.find(call => call.name === 'setTransform').args,
    [2, 0, 0, 2, 0, 0]);
  assert.equal(h.nodes.get('wave-empty').hidden, true);
  assert.equal(JSON.stringify(data), before);
  const coordinates = h.canvasCalls.filter(call => ['moveTo', 'lineTo'].includes(call.name));
  assert.ok(coordinates.length > 20, 'All three component envelopes should be drawn.');
  assert.ok(coordinates.every(call => call.args.every(Number.isFinite)));
  assert.ok(h.canvasCalls.some(call => call.name === 'setLineDash' && call.args[0].length),
    'An onset at zero seconds must still receive a visible marker.');
});

test('a slower earlier run response cannot replace the latest selected run', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.context.rendered = [];
  vm.runInContext('renderWorkbench = view => rendered.push(view.runId);', h.context);
  const old = h.front.loadRun('old');
  const latest = h.front.loadRun('latest');
  pending.requests[1].resolve(evidence('latest'));
  await latest;
  pending.requests[0].resolve(evidence('old'));
  await old;
  assert.equal(h.front.app.runId, 'latest');
  assert.equal(h.front.app.view.runId, 'latest');
  assert.deepEqual(h.context.rendered, ['latest']);
});

test('a stale run failure stays silent after the newer run succeeds', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  vm.runInContext('renderWorkbench = () => {};', h.context);
  const old = h.front.loadRun('old');
  const latest = h.front.loadRun('latest');
  pending.requests[1].resolve(evidence('latest'));
  await latest;
  pending.requests[0].reject(new Error('stale network failure'));
  await old;
  assert.equal(h.front.app.view.runId, 'latest');
  assert.equal(h.nodes.get('app-message').hidden, true);
});

test('run loading failure is visible while the records view is active', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.app.records = [];
  h.front.setView('records');
  const loading = h.front.loadRun('unavailable');
  pending.requests[0].reject(new Error('latest network failure'));
  await loading;
  const message = h.nodes.get('app-message');
  assert.equal(message.hidden, false);
  assert.match(message.textContent, /核查记录.*暂时无法/);
  assert.doesNotMatch(message.textContent, /latest network failure/);
  assert.equal(h.nodes.get('view-records').classList.contains('on'), true);
});

test('malformed evidence fails visibly instead of silently rendering a completed run', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.context.rendered = [];
  vm.runInContext('renderWorkbench = view => rendered.push(view.runId);', h.context);
  const loading = h.front.loadRun('malformed');
  pending.requests[0].resolve(evidence('malformed', {acquisition: {invalid: true}}));
  const loaded = await loading;
  assert.equal(loaded, false);
  assert.equal(h.nodes.get('app-message').hidden, false);
  assert.equal(h.front.app.view, null);
  assert.deepEqual(h.context.rendered, []);
});

test('waveform requests arriving out of order keep the latest station observation', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.app.runId = 'run';
  const old = h.front.loadWave('run', 'old-station');
  const latest = h.front.loadWave('run', 'latest-station');
  const data = waveform('latest source');
  pending.requests[1].resolve(data);
  await latest;
  pending.requests[0].resolve(waveform('stale source'));
  await old;
  assert.equal(h.front.app.candidateId, 'latest-station');
  assert.equal(h.front.app.waveform, data);
  assert.equal(h.nodes.get('wave-empty').hidden, true);
  assert.match(h.nodes.get('wave-note').innerHTML, /latest source/);
  assert.doesNotMatch(h.nodes.get('wave-note').innerHTML, /stale source/);
});

test('an old waveform error cannot cover a successfully loaded newer station', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.app.runId = 'run';
  const old = h.front.loadWave('run', 'old-station');
  const latest = h.front.loadWave('run', 'latest-station');
  const data = waveform();
  pending.requests[1].resolve(data);
  await latest;
  pending.requests[0].reject(new Error('stale waveform failure'));
  await old;
  assert.equal(h.front.app.waveform, data);
  assert.equal(h.nodes.get('wave-empty').hidden, true);
});

test('a waveform response from a previously selected run is discarded', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.app.runId = 'old-run';
  const loading = h.front.loadWave('old-run', 'station');
  h.front.app.runId = 'new-run';
  pending.requests[0].resolve(waveform());
  await loading;
  assert.equal(h.front.app.waveform, null);
});

test('clipboard denial and an unavailable clipboard provide manual-copy feedback', async () => {
  for (const clipboard of [undefined, {
    writeText: async () => { throw new Error('clipboard denied'); },
  }]) {
    const h = frontend({clipboard});
    h.front.app.runId = 'r-copy-manually';
    h.front.bind();
    await h.nodes.get('copy-run').onclick();
    const message = h.nodes.get('app-message');
    assert.equal(message.hidden, false);
    assert.match(message.textContent, /复制浏览器地址栏中的链接/);
    assert.doesNotMatch(message.textContent, /r-copy-manually/);
    assert.notEqual(h.nodes.get('copy-run').textContent, '链接已复制');
  }
});

test('an invalid routed view falls back to an accessible workbench tab', () => {
  const h = frontend();
  h.setURL('/?view=missing');
  h.front.setView('missing');
  assert.equal(h.front.app.activeView, 'workbench');
  assert.equal(h.nodes.get('view-workbench').classList.contains('on'), true);
  assert.equal(h.nodes.get('tab-workbench').getAttribute('aria-selected'), 'true');
  const selected = [...h.nodes.values()].filter(node =>
    node.dataset.view && node.getAttribute('aria-selected') === 'true');
  assert.equal(selected.length, 1);
  assert.equal(new URL(h.location.href).searchParams.get('view'), 'workbench');
});

test('browser history restores an invalid view safely without adding another entry', async () => {
  const h = frontend();
  h.front.app.runId = 'r-retained';
  h.front.bind();
  h.front.setView('about');
  assert.equal(h.historyCalls.length, 1);
  assert.equal(new URL(h.location.href).searchParams.get('run'), 'r-retained');
  h.setURL('/?view=missing&run=r-retained');
  await h.listeners.get('popstate')();
  assert.equal(h.front.app.activeView, 'workbench');
  assert.equal(h.nodes.get('tab-workbench').tabIndex, 0);
  assert.equal(h.nodes.get('tab-about').tabIndex, -1);
  assert.equal(h.historyCalls.length, 1);
});

test('switching views starts at the top while browser history retains its own scroll restoration', () => {
  const h = frontend();
  h.front.app.records = [];
  h.front.setView('about');
  assert.equal(h.context.window.scrollCalls.length, 1);
  assert.equal(h.context.window.scrollCalls[0].top, 0);
  h.front.setView('workbench', {historyMode: 'none'});
  assert.equal(h.context.window.scrollCalls.length, 1);
});

const demoGuide = () => ({
  status: 'ok',
  form: {
    window_start: '2023-12-24T10:00:00Z', window_end: '2023-12-24T10:02:00Z',
    question: '这段观测能不能用于 1–12 Hz 的 P/S 拾取？',
    stations: ['UW.CHIMA'], target_band_hz: [1, 12],
    method_goal: 'three_component_pick', region_name: '',
  },
  examples: {initial_run_id: 'saved-initial', feedback_run_id: 'saved-feedback'},
  stats: {run_count: 2, feedback_chain_count: 1},
});
const plain = value => JSON.parse(JSON.stringify(value));
function readyTask(h) {
  h.front.app.guide = demoGuide();
  h.front.fillExampleTask();
  h.front.app.parsed = {ready: true};
  h.front.app.parsedInput = h.front.taskInputSignature();
  h.nodes.get('u-run').disabled = false;
}

test('terminology explains the scientific limits of applicability and stability', () => {
  const {front} = frontend();
  assert.match(front.TERMS['方法适用性'], /不代表已经拾取 P\/S/);
  assert.match(front.TERMS['诊断网格'], /不证明.*P\/S.*事件真实/);
  assert.match(front.TERMS['到时'], /不应直接当成.*P 波.*S 波/);
  assert.match(front.TERMS['核查模式'], /其他方式.*(?:规则|标准).*检查/);
  assert.match(front.TERMS['核查模式'], /确认条件.*(?:免费|费用).*观测/);
});

test('filling the verified example copies physical inputs and never starts a request', () => {
  const h = frontend();
  h.front.bind();
  readyTask(h);
  h.nodes.get('u-fill-example').disabled = false;
  h.nodes.get('u-fill-example').onclick();
  const form = demoGuide().form;
  assert.deepEqual(plain(h.front.collectTask()), {
    question: form.question, window_start: form.window_start, window_end: form.window_end,
    stations: form.stations, target_band_hz: form.target_band_hz,
    method_goal: form.method_goal,
  });
  assert.equal(h.nodes.get('u-start').value, '2023-12-24T10:00:00');
  assert.equal(h.front.app.parsed, null);
  assert.equal(h.front.app.parsedInput, null);
  assert.equal(h.nodes.get('u-run').disabled, false);
  assert.match(h.nodes.get('u-parse-body').textContent, /已填入示例条件/);
});

test('UTC form values preserve their displayed clock instead of shifting to browser local time', () => {
  const h = frontend();
  readyTask(h);
  h.nodes.get('u-start').value = '2026-10-13T13:14';
  h.nodes.get('u-end').value = '2026-10-13T13:16:17';
  assert.equal(h.front.collectTask().window_start, '2026-10-13T13:14Z');
  assert.equal(h.front.collectTask().window_end, '2026-10-13T13:16:17Z');
  assert.equal(h.front.utcInput('2026-10-13T13:14:00+08:00'),
    '2026-10-13T13:14:00+08:00');
  assert.equal(h.front.utcInput('2026-10-13T13:14:00Z'), '2026-10-13T13:14:00Z');
  assert.equal(h.front.utcInput(' '), undefined);
  assert.equal(h.nodes.get('u-start').getAttribute('type'), 'datetime-local');
});

test('optional parameter preview only calls the deterministic parser', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  readyTask(h);
  const parsing = h.front.parseTask();
  assert.equal(pending.requests.length, 1);
  assert.equal(pending.requests[0].path, '/api/task');
  assert.equal(pending.requests[0].options.method, 'POST');
  assert.equal(JSON.parse(pending.requests[0].options.body).window_start,
    '2023-12-24T10:00:00Z');
  assert.equal(h.nodes.get('u-run').disabled, true);
  pending.requests[0].resolve({ready: true, structured: {
    user_question: demoGuide().form.question,
    window_start: demoGuide().form.window_start,
    window_end: demoGuide().form.window_end,
    region: {kind: 'stations', stations: ['UW.CHIMA']},
    target_band_hz: [1, 12], method_goal: 'three_component_pick',
  }});
  await parsing;
  assert.equal(h.nodes.get('u-run').disabled, false);
  assert.equal(h.front.app.parsed.ready, true);
  assert.equal(h.front.app.taskRunning, false);
  assert.equal(pending.requests.length, 1);
  assert.match(h.nodes.get('u-parse-body').innerHTML, /条件已确认.*可以开始核查/);
});

test('missing fields explain corrective action without exposing schema keys or executable markup', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  readyTask(h);
  const parsing = h.front.parseTask();
  pending.requests[0].resolve({ready: false, missing: [
    {field: 'window_start', why: 'window_start：没有可识别的 UTC 时窗起点'},
    {field: 'region', why: 'region：没有给出区域或台站'},
    {field: 'question', why: '<script>question</script> 尚不明确'},
  ]});
  await parsing;
  const message = h.nodes.get('u-parse-body').innerHTML;
  assert.match(message, /开始时间.*使用示例条件/);
  assert.match(message, /命名区域.*UW.CHIMA/);
  assert.doesNotMatch(message, /window_start|window_end|target_band_hz|method_goal|\bregion\b|\bquestion\b/);
  assert.doesNotMatch(message, /<script>/);
  assert.match(message, /&lt;script&gt;/);
  assert.equal(h.nodes.get('u-run').disabled, false);
});

test('an invalid filled window explains why its end needs correction', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  readyTask(h);
  h.nodes.get('u-end').value = '2023-12-24T09:59:00';
  const parsing = h.front.parseTask();
  pending.requests[0].resolve({ready: false, missing: [
    {field: 'window_end', why: '终点不晚于起点', example: '终点需大于起点'},
  ]});
  await parsing;
  assert.match(h.nodes.get('u-parse-body').innerHTML, /终点不晚于起点|结束时间.*(?:晚于|早于|大于).*开始/);
  assert.equal(h.nodes.get('u-run').disabled, false);
});

test('editing every task input or mode invalidates a previous ready result', () => {
  const h = frontend();
  h.front.bind();
  for (const id of ['u-question', 'u-start', 'u-end', 'u-stations', 'u-region',
    'u-band-lo', 'u-band-hi', 'u-goal', 'u-mode']) {
    for (const event of ['oninput', 'onchange']) {
      readyTask(h);
      h.nodes.get(id)[event]();
      assert.equal(h.front.app.parsed, null, id + ' ' + event);
      assert.equal(h.front.app.parsedInput, null, id + ' ' + event);
      assert.equal(h.nodes.get('u-run').disabled, false, id + ' ' + event);
      assert.equal(h.nodes.get('u-parse-body').hidden, true, id + ' ' + event);
    }
  }
});

test('time, station, and band presets invalidate readiness without making requests', () => {
  const h = frontend();
  h.front.bind();
  readyTask(h);
  h.nodes.get('u-start').value = '';
  h.nodes.get('u-period').value = 'example';
  h.nodes.get('u-period').onchange();
  assert.equal(h.front.collectTask().window_start, demoGuide().form.window_start);
  assert.equal(h.front.app.parsed, null);

  readyTask(h);
  h.nodes.get('u-region').value = 'orcas';
  h.nodes.get('u-station-preset').value = 'UW.CHIMA';
  h.nodes.get('u-station-preset').onchange();
  assert.equal(h.nodes.get('u-region').value, '');
  assert.deepEqual(plain(h.front.collectTask().stations), ['UW.CHIMA']);
  assert.equal(h.front.app.parsed, null);

  readyTask(h);
  const chip = h.document.querySelectorAll('[data-band]').find(b => b.dataset.band === '3,12');
  chip.onclick();
  assert.deepEqual(plain(h.front.collectTask().target_band_hz), [3, 12]);
  assert.equal(chip.getAttribute('aria-pressed'), 'true');
  assert.equal(h.front.app.parsed, null);
  assert.equal(h.nodes.get('u-run').disabled, false);
});

test('clearing removes parsed readiness as well as physical task inputs', () => {
  const h = frontend();
  h.front.bind();
  readyTask(h);
  h.nodes.get('u-reset').onclick();
  assert.equal(h.front.app.parsed, null);
  assert.equal(h.front.app.parsedInput, null);
  assert.equal(h.nodes.get('u-run').disabled, false);
  assert.equal(h.nodes.get('u-parse-body').hidden, true);
  for (const id of ['u-question', 'u-start', 'u-end', 'u-stations', 'u-region',
    'u-band-lo', 'u-band-hi', 'u-goal']) assert.equal(h.nodes.get(id).value, '', id);
  assert.equal(h.front.collectTask().window_start, undefined);
});

test('a parse response for edited inputs cannot validate the changed task', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.bind();
  readyTask(h);
  const old = h.front.parseTask();
  h.nodes.get('u-band-hi').value = '20';
  h.nodes.get('u-band-hi').oninput();
  pending.requests[0].resolve({ready: true});
  await old;
  assert.equal(h.front.app.parsed, null);
  assert.equal(h.nodes.get('u-run').disabled, false);
  const latest = h.front.parseTask();
  pending.requests[1].resolve({ready: true});
  await latest;
  assert.equal(h.front.app.parsed.ready, true);
  assert.equal(h.nodes.get('u-run').disabled, false);
});

test('older parse errors cannot replace a newer successfully parsed task', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  readyTask(h);
  const old = h.front.parseTask();
  const latest = h.front.parseTask();
  pending.requests[1].resolve({ready: true});
  await latest;
  pending.requests[0].reject(new Error('obsolete parser failure'));
  await old;
  assert.equal(h.front.app.parsed.ready, true);
  assert.equal(h.nodes.get('u-run').disabled, false);
  assert.equal(h.nodes.get('u-warn').textContent, '');
});

test('one start automatically validates changed inputs and submits only once', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  readyTask(h);
  h.nodes.get('u-band-hi').value = '20';
  const started = h.front.startRun();
  await h.front.startRun();
  assert.equal(pending.requests.length, 1);
  assert.equal(pending.requests[0].path, '/api/task');
  assert.deepEqual(JSON.parse(pending.requests[0].options.body).target_band_hz, [1, 20]);
  assert.equal(h.nodes.get('u-question').disabled, true);
  pending.requests[0].resolve({ready: true, structured: {target_band_hz: [1, 20]}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pending.requests.length, 2);
  assert.equal(pending.requests[1].path, '/api/review');
  pending.requests[1].resolve({run_id: 'stub-only-no-model'}, 202);
  await started;
  assert.equal(h.front.app.taskRunning, true);
  assert.equal(h.nodes.get('u-run').disabled, true);
  assert.equal(h.nodes.get('u-parse').disabled, true);
});

test('invalid automatic validation keeps saved results and never starts a review', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.nodes.get('conclusion-body').innerHTML = 'saved result';
  const started = h.front.startRun();
  pending.requests[0].resolve({ready: false, missing: [
    {field: 'window_start', why: '没有可识别的 UTC 时窗起点'},
  ]});
  await started;
  assert.equal(pending.requests.length, 1);
  assert.equal(h.front.app.taskRunning, false);
  assert.match(h.nodes.get('u-warn').textContent, /开始时间/);
  assert.equal(h.nodes.get('conclusion-body').innerHTML, 'saved result');
});

test('unavailable models explain the cause while rule mode stays available', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  readyTask(h);
  h.front.app.health = {model_available: false, model_unavailable_reason: '模型预算不足'};
  h.front.setTaskBusy(false);
  assert.equal(h.nodes.get('u-run').disabled, true);
  assert.match(h.nodes.get('u-mode-note').textContent, /费用上限/);
  await h.front.startRun();
  assert.equal(pending.requests.length, 0);
  h.nodes.get('u-mode').value = 'question';
  h.front.setTaskBusy(false);
  assert.equal(h.nodes.get('u-run').disabled, false);
  assert.match(h.nodes.get('u-mode-note').textContent, /免费/);
});

test('backend validation errors are humanized when a start request is rejected', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  readyTask(h);
  const started = h.front.startRun();
  pending.requests[0].resolve({error: '任务信息不完整，请先补齐', missing: [
    {field: 'window_start', why: 'window_start 不正确'},
    {field: 'method_goal', why: 'method_goal 不支持'},
  ]}, 400);
  await started;
  const message = h.nodes.get('u-warn').textContent;
  assert.match(message, /开始时间.*分析目标|开始时间.*三分量/);
  assert.doesNotMatch(message, /window_start|method_goal/);
  assert.equal(h.front.app.taskRunning, false);
});

test('missing tracking jobs explain the lost status and release the busy controls', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.setTaskBusy(true);
  h.front.app.activeTrackId = 'lost-job';
  h.front.app.poll = 71;
  const polling = h.front.pollRun('lost-job');
  pending.requests[0].resolve({error: '未知运行'}, 404);
  await polling;
  assert.match(h.nodes.get('u-warn').textContent, /核查记录.*查看本次结果/);
  assert.equal(h.front.app.taskRunning, false);
  assert.equal(h.front.app.activeTrackId, null);
  assert.equal(h.front.app.poll, null);
  assert.equal(h.nodes.get('u-question').disabled, false);
});

test('temporary polling errors remain visible and recover without duplicate polls', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.setTaskBusy(true);
  h.front.app.activeTrackId = 'busy-job';
  const first = h.front.pollRun('busy-job');
  await h.front.pollRun('busy-job');
  assert.equal(pending.requests.length, 1);
  pending.requests[0].reject(new Error('connection lost'));
  await first;
  assert.match(h.nodes.get('u-warn').textContent, /正在重新连接/);
  assert.equal(h.front.app.taskRunning, true);
  const recovered = h.front.pollRun('busy-job');
  pending.requests[1].resolve({status: 'running', log: [{message: '正在获取观测'}]});
  await recovered;
  assert.equal(h.nodes.get('u-warn').textContent, '正在获取观测');
  assert.equal(h.nodes.get('run-state').textContent, '核查中');
});

test('an obsolete tracking response cannot clear a newer job or its interval', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.setTaskBusy(true);
  h.front.app.activeTrackId = 'old-job';
  const old = h.front.pollRun('old-job');
  h.front.app.activeTrackId = 'new-job';
  h.front.app.poll = 92;
  pending.requests[0].resolve({status: 'done'});
  await old;
  assert.equal(h.front.app.activeTrackId, 'new-job');
  assert.equal(h.front.app.poll, 92);
  assert.equal(h.front.app.taskRunning, true);
});

test('feedback and new starts share one busy lock', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.app.view = h.front.normalize(evidence('parent', {acquisition: [
    {candidate_id: 'station', station: 'CHIMA', status: 'analysed'},
  ]}));
  const feedback = h.front.sendFeedback({kind: 'exclude', candidate_id: 'station'});
  await h.front.sendFeedback({kind: 'exclude', candidate_id: 'station'});
  await h.front.startRun();
  assert.equal(pending.requests.length, 1);
  assert.equal(pending.requests[0].path, '/api/followup');
  assert.equal(h.nodes.get('fb-exclude').disabled, true);
  pending.requests[0].resolve({run_id: 'feedback-job'}, 202);
  await feedback;
  assert.equal(h.front.app.activeTrackId, 'feedback-job');
});

test('failed feedback opens its saved failed evidence instead of losing the result', async () => {
  const requests = [];
  const h = frontend({fetch: async path => {
    requests.push(path);
    const body = path === '/api/review/feedback-job'
      ? {status: 'failed', result: {run_id: 'failed-child'}}
      : path === '/api/evidence/failed-child'
        ? evidence('failed-child', {outcome: {status: 'failed', error: 'tool failed'},
            acquisition: [], validator: {delivered: {}}})
        : path === '/api/health'
          ? {cumulative_cost_upper_cny: 0, total_limit_cny: 20, model_available: true}
          : {runs: []};
    return {ok: true, status: 200, json: async () => body};
  }});
  h.front.setTaskBusy(true);
  h.front.app.activeTrackId = 'feedback-job';
  await h.front.pollFeedback('feedback-job');
  assert.ok(requests.includes('/api/evidence/failed-child'));
  assert.equal(h.front.app.runId, 'failed-child');
  assert.equal(h.front.app.view.status.kind, 'failed');
  assert.equal(h.front.app.taskRunning, false);
});

test('terms work with keyboard focus, tap pinning, outside clicks, and Escape focus restoration', () => {
  const h = frontend();
  h.front.bindTerms();
  const trigger = h.document.querySelectorAll('[data-term]')[0];
  const popup = h.nodes.get('term-popover');
  trigger.focus();
  assert.equal(popup.hidden, false);
  assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  assert.equal(trigger.getAttribute('aria-describedby'), 'term-popover-text');
  h.document.dispatch('click', {target: trigger});
  assert.equal(h.front.app.termPinned, true);
  h.document.dispatch('click', {target: h.nodes.get('term-popover-text')});
  assert.equal(popup.hidden, false, 'Readable popup text must not close on interaction.');
  let prevented = false;
  h.document.dispatch('keydown', {key: 'Escape', preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(popup.hidden, true);
  assert.equal(h.document.activeElement, trigger);
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(trigger.getAttribute('aria-describedby'), null);
  h.document.dispatch('click', {target: trigger});
  h.document.dispatch('click', {target: h.nodes.get('u-question')});
  assert.equal(popup.hidden, true);
  h.document.dispatch('click', {target: trigger});
  h.document.dispatch('click', {target: trigger});
  assert.equal(popup.hidden, true, 'A second tap should toggle the pinned popup closed.');
});

test('hovered glossary text stays open while the pointer moves into its readable popup', () => {
  const h = frontend();
  h.front.bindTerms();
  const trigger = h.document.querySelectorAll('[data-term]')[0];
  const popup = h.nodes.get('term-popover');
  h.document.dispatch('pointerover', {target: trigger, pointerType: 'touch'});
  assert.equal(popup.hidden, true);
  h.document.dispatch('pointerover', {target: trigger, pointerType: 'mouse'});
  h.document.dispatch('pointerout', {target: trigger});
  h.document.dispatch('pointerover', {target: popup, pointerType: 'mouse'});
  h.runTimers();
  assert.equal(popup.hidden, false);
  h.document.dispatch('pointerout', {target: popup});
  h.runTimers();
  assert.equal(popup.hidden, true);
});

test('glossary popup clamps to a 390px mobile viewport and opens above a bottom-edge trigger', () => {
  const h = frontend({viewportWidth: 390, viewportHeight: 700});
  const trigger = h.document.querySelectorAll('[data-term]')[0];
  trigger.rect = {left: 370, right: 389, top: 660, bottom: 682, width: 19, height: 22};
  const popup = h.nodes.get('term-popover');
  popup.rect = {left: 0, top: 0, right: 260, bottom: 120, width: 260, height: 120};
  h.front.openTerm(trigger, true);
  const left = parseFloat(popup.style.left);
  const top = parseFloat(popup.style.top);
  const popupWidth = parseFloat(popup.style.width);
  assert.equal(popupWidth, 260);
  assert.ok(left >= 12 && left + popupWidth <= 378);
  assert.ok(top >= 12 && top + 120 < trigger.rect.top);
  const narrow = h.front.termPosition(trigger.rect, popup.rect, {width: 240, height: 700});
  assert.ok(narrow.left >= 12 && narrow.left + narrow.width <= 228);
});

test('verified guide statistics stay scoped and every demo navigation reads saved evidence only', async () => {
  const requests = [];
  const guide = demoGuide();
  const h = frontend({fetch: async (path, options) => {
    requests.push({path, method: options?.method || 'GET'});
    const body = path === '/api/demo-guide' ? guide :
      path.startsWith('/api/evidence/') ? evidence(path.split('/').at(-1), {
        acquisition: [{candidate_id: 'saved-station', station: 'CHIMA', network: 'UW',
          status: 'analysed'}],
      }) : path.startsWith('/api/waveform/') ? waveform() : null;
    assert.ok(body, 'Guide must not access a mutation endpoint: ' + path);
    return {ok: true, status: 200, json: async () => body};
  }});
  await h.front.loadDemoGuide();
  assert.equal(h.nodes.get('u-fill-example').disabled, false);
  assert.match(h.nodes.get('about-stats').innerHTML, /<strong>2<\/strong>/);
  assert.match(h.nodes.get('about-stats').innerHTML, /<strong>1<\/strong>/);
  assert.match(h.nodes.get('about-guide-note').textContent, /数字来自.*示例核查记录/);
  for (const [section, runId, anchor] of [
    ['conclusion', 'saved-initial', 'conclusion-card'],
    ['evidence', 'saved-initial', 'wave-card'],
    ['feedback', 'saved-feedback', 'feedback-card'],
  ]) {
    await h.front.showDemoSection(section);
    assert.equal(h.front.app.runId, runId);
    assert.equal(h.front.app.activeView, 'workbench');
    assert.equal(h.front.app.replay, true);
    assert.equal(h.document.activeElement.id, anchor);
    assert.equal(h.nodes.get(anchor).scrolled.block, 'start');
  }
  assert.equal(requests.length, 7);
  assert.ok(requests.every(request => request.method === 'GET'));
  assert.equal(requests.filter(request => request.path.startsWith('/api/waveform/')).length, 3);
});

test('unverified guide metadata leaves example filling disabled and does not invent statistics', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  const loading = h.front.loadDemoGuide();
  pending.requests[0].resolve({status: 'unverified', stats: {run_count: 999}});
  await loading;
  assert.equal(h.front.app.guide, null);
  assert.equal(h.nodes.get('u-fill-example').disabled, true);
  assert.match(h.nodes.get('about-stats').textContent, /示例信息.*无法查看/);
  assert.doesNotMatch(h.nodes.get('about-stats').textContent, /999/);
  h.front.fillExampleTask();
  assert.match(h.nodes.get('u-warn').textContent, /示例条件暂不可用/);
  assert.equal(pending.requests.length, 1);
});

test('glossary controls underline the actual term without a separate question mark', () => {
  const h = frontend();
  const labelled = h.front.term('观测包络', '包络');
  assert.match(labelled, /<button[^>]+data-term="包络"[^>]+>观测包络<\/button>/);
  assert.equal((labelled.match(/观测包络/g) || []).length, 1);
  assert.doesNotMatch(labelled, />\?<\/button>/);
  assert.doesNotMatch(html, /class="term-help"[^>]*>\?<\/button>/);
  assert.equal(h.front.term('<未解释的条件>'), '&lt;未解释的条件&gt;');
});

test('the accepted-example button opens the saved conclusion with persistent readable feedback', async () => {
  const pending = deferredFetch();
  const h = frontend({fetch: pending.fetch});
  h.front.app.guide = demoGuide();
  h.front.bind();
  vm.runInContext('renderWorkbench = () => {};', h.context);
  const opening = h.nodes.get('btn-example').onclick();
  const note = h.nodes.get('run-load-note');
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /正在打开核查记录/);
  assert.equal(h.nodes.get('app-message').hidden, true);
  assert.equal(pending.requests.length, 1);
  assert.equal(pending.requests[0].path, '/api/evidence/saved-initial');
  pending.requests[0].resolve(evidence('saved-initial'));
  await opening;
  h.runTimers();
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /已打开核查记录.*查看已有结果免费/);
  assert.doesNotMatch(note.textContent, /saved-initial/);
  assert.equal(h.document.activeElement.id, 'conclusion-card');
  assert.equal(h.nodes.get('conclusion-card').scrolled.block, 'start');
  assert.equal(h.front.app.activeView, 'workbench');
  assert.equal(pending.requests.length, 1);
  assert.equal(pending.requests[0].options, undefined);
});

test('segmented waveforms retain gap width and finite axes when sample rates differ', () => {
  const h = frontend({width: 720});
  const data = {
    window: {start: '1970-01-01T00:00:10Z'},
    channels: {Z: {
      start_utc: '1970-01-01T00:00:10', n_samples: 6, display_center: 106 / 6,
      sampling_rate_hz: null, sampling_rates_hz: [2, 4], duration_seconds: 6,
      envelope: [[0, 2, 2], [0.5, 4, 4], [5, 10, 10], [5.25, 20, 20],
        [5.5, 30, 30], [5.75, 40, 40]],
    }},
  };
  const ctx = h.nodes.get('wave-canvas').getContext('2d');
  const segments = [];
  let from;
  ctx.moveTo = (x, y) => { from = [x, y]; };
  ctx.lineTo = (x, y) => {
    if (ctx.strokeStyle === '#0E7490') segments.push([from, [x, y]]);
  };
  h.front.drawWave(data);
  assert.equal(segments.length, 6);
  assert.ok(segments.flat(2).every(Number.isFinite));
  assert.ok(segments.every(([a, b]) => a[0] === b[0]), 'Each bucket is vertical; gaps must not be connected.');
  assert.ok(segments[2][0][0] - segments[1][0][0] > 450,
    'The later segment must be drawn at its true time, leaving the four-second gap blank.');
  assert.ok(segments.every(([a]) => a[0] >= 66 && a[0] <= 704));
  assert.ok(h.canvasCalls.some(call => call.name === 'fillText' && call.args[0] === '多采样率'));
});

test('waveform notes disclose saved gap flags and varying segment rates', async () => {
  const data = waveform();
  data.channel_integrity_flags = ['gaps_or_overlaps'];
  data.channels.Z.sampling_rate_hz = null;
  data.channels.Z.sampling_rates_hz = [20, 40];
  data.channels.Z.duration_seconds = 60;
  const h = frontend({fetch: async () => ({ok: true, json: async () => data})});
  h.front.app.runId = 'saved';
  await h.front.loadWave('saved', 'station');
  assert.match(h.nodes.get('wave-note').innerHTML, /缺口或重叠.*缺口未补齐/);
  assert.match(h.nodes.get('wave-note').innerHTML, /Z 分段采样率：20 \/ 40 Hz/);
});
