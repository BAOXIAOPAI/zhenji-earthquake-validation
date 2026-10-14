/**
 * Real-browser acceptance trace for the user page (review-3, 2026-10-10).
 *
 * Drives Google Chrome over the DevTools Protocol — no test framework, no browser
 * download. It performs the guide's required chain on the actual page:
 *
 *     event sample -> run -> exclude OLGA -> narrow the onset grid -> restore OLGA
 *
 * and records, per step, the run id, the parent id, the conditions actually in
 * force and a screenshot. The point is to prove that a feedback round continues
 * from the LATEST run (review P1-1) and that the page lays out at 1280x720, not
 * that a route exists (review 3.3).
 *
 * Usage:
 *   node main/ui_trace.mjs <base-url> <out-dir>
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:8799";
const OUT = resolve(process.argv[3] || "delivery/review-3-2026-10-10/evidence");
const CHROME = process.env.CHROME_BIN ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DEBUG_PORT = Number(process.env.CHROME_DEBUG_PORT || 9333);
const PROFILE = join(tmpdir(), "eq-ui-trace-" + Date.now());

mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.id = 0;
    this.pending = new Map();
    this.events = [];
    this.ready = new Promise((res, rej) => {
      this.ws.addEventListener("open", res);
      this.ws.addEventListener("error", rej);
    });
    this.ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve: ok, reject: bad } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? bad(new Error(JSON.stringify(msg.error))) : ok(msg.result);
      } else if (msg.method) {
        this.events.push(msg);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((ok, bad) => {
      this.pending.set(id, { resolve: ok, reject: bad });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression: `(() => { ${expression} })()`,
      returnByValue: true, awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw new Error("page exception: " + JSON.stringify(res.exceptionDetails));
    }
    return res.result.value;
  }
  close() { try { this.ws.close(); } catch { /* already closed */ } }
}

async function fetchJson(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return await r.json();
    } catch { /* chrome not up yet */ }
    await sleep(250);
  }
  throw new Error("chrome devtools endpoint never came up: " + url);
}

async function waitFor(cdp, expression, label, timeoutMs = 240000, diagExpr = null) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await cdp.eval(`return !!(${expression});`)) return true;
    await sleep(400);
  }
  let diag = "";
  if (diagExpr) {
    try { diag = " :: " + (await cdp.eval(diagExpr)); }
    catch (e) { diag = " :: diag failed " + e.message; }
  }
  throw new Error(`timed out waiting for ${label}${diag}`);
}

const FEEDBACK_DIAG = `
  return JSON.stringify({
    fb_result: document.getElementById('fb-result').textContent.slice(0, 400),
    conditions: document.getElementById('fb-conditions').textContent.slice(0, 400),
    warn: document.getElementById('fb-warn').textContent,
    chain: (() => {
      const box = document.getElementById('fb-conditions');
      if (!box) return null;
      const dts = [...box.querySelectorAll('dt')].map(e => e.textContent.trim());
      const dds = [...box.querySelectorAll('dd')].map(e => e.textContent.trim());
      const i = dts.findIndex(t => t === '分支链');
      return i >= 0 ? dds[i] : null;
    })(),
  });
`;

const READ_CONDITIONS = `
  const box = document.getElementById('fb-conditions');
  if (!box) return null;
  const dts = [...box.querySelectorAll('dt')].map(e => e.textContent.trim());
  const dds = [...box.querySelectorAll('dd')].map(e => e.textContent.trim());
  const out = {};
  dts.forEach((k, i) => { out[k] = dds[i]; });
  return out;
`;

function runIdsIn(text) {
  return [...new Set(String(text || "").match(/r-[0-9a-f]{16}/g) || [])];
}

async function main() {
  const step = (name) => console.error("[ui-trace] " + name);
  step("launching chrome on port " + DEBUG_PORT);
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-sandbox",
    "--disable-dev-shm-usage", "--no-first-run",
    "--no-default-browser-check", "--hide-scrollbars",
    `--remote-debugging-port=${DEBUG_PORT}`,
    `--user-data-dir=${PROFILE}`,
    "--window-size=1280,720", "about:blank",
  ], { stdio: ["ignore", "ignore", "ignore"] });

  const trace = { base: BASE, started_at: new Date().toISOString(), steps: [] };
  let cdp;
  try {
    const version = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/version`);
    trace.chrome = version.Browser;
    const list = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
    const page = list.find((t) => t.type === "page");
    cdp = new CDP(page.webSocketDebuggerUrl);
    await cdp.ready;
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride",
      { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });

    await cdp.send("Page.navigate", { url: BASE + "/" });
    await waitFor(cdp, "document.getElementById('u-run')", "page load", 30000);
    await sleep(800);
    step("page loaded: " + BASE);

    const layout = (w, h, name) => cdp.eval(`
      const el = document.querySelector('main');
      const u = document.getElementById('page-user');
      const l = document.getElementById('page-lab');
      const r = e => e ? {w: Math.round(e.getBoundingClientRect().width),
                          h: Math.round(e.getBoundingClientRect().height)} : null;
      return JSON.stringify({viewport:[${w},${h}], main:r(el), page_user:r(u),
                             page_lab:r(l),
                             page_user_active: u.classList.contains('active')});
    `).then((s) => { trace[name] = JSON.parse(s); });

    const shot = async (name, selector) => {
      // Scroll the panel that this step is about into view first: the user page is
      // taller than the viewport, so a plain capture would show only the form and
      // every step would look identical.
      if (selector) {
        await cdp.eval(`
          const el = document.querySelector(${JSON.stringify(selector)});
          if (el) el.scrollIntoView({ block: 'start' });
          window.scrollBy(0, -12);
          return true;
        `);
        await sleep(500);
      }
      const res = await cdp.send("Page.captureScreenshot", { format: "png" });
      writeFileSync(join(OUT, name), Buffer.from(res.data, "base64"));
      return name;
    };


    const stepSnapshot = () => cdp.eval(`
      return JSON.stringify({
        scroll_y: Math.round(window.scrollY),
        result_head: (document.getElementById('u-result').textContent || '')
          .replace(/\\s+/g, ' ').trim().slice(0, 200),
        feedback_head: (document.getElementById('fb-result').textContent || '')
          .replace(/\\s+/g, ' ').trim().slice(0, 200),
        verdict_tag: (document.querySelector('#u-result .verdict') || {}).textContent || null,
      });
    `).then((v) => JSON.parse(v));

    // --- 1. layout at the reviewed viewport ---------------------------------
    await layout(1280, 720, "layout_1280");
    await shot("页面布局-1280x720.png", "#page-user");
    await cdp.send("Emulation.setDeviceMetricsOverride",
      { width: 900, height: 900, deviceScaleFactor: 1, mobile: false });
    await sleep(400);
    await layout(900, 900, "layout_900");
    await shot("页面布局-900宽.png", "#page-user");
    await cdp.send("Emulation.setDeviceMetricsOverride",
      { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });
    await sleep(400);

    // --- 2. first run on the verified real-event sample ---------------------
    // `ustate` is a lexical const, not a window property, so poll by applying the
    // preset until it takes effect (it no-ops until /api/samples has loaded).
    let presetOk = false;
    let presetDiag = "";
    for (let i = 0; i < 80; i++) {
      presetDiag = await cdp.eval(`
        const before = document.getElementById('u-start').value;
        window.uPreset('event');
        return JSON.stringify({
          called: typeof window.uPreset,
          before, after: document.getElementById('u-start').value,
          warn: document.getElementById('u-warn').textContent,
          note: document.getElementById('u-preset-note').textContent.slice(0, 80),
          has_btn: !!document.getElementById('u-preset-event'),
          ready: document.readyState,
        });
      `);
      presetOk = JSON.parse(presetDiag).after.length > 0;
      if (presetOk) break;
      await sleep(400);
    }
    if (!presetOk) throw new Error("event sample never became available: " + presetDiag);
    await sleep(600);
    await cdp.eval("document.getElementById('u-parse').click(); return true;");
    await sleep(2500);
    const parseDiag = await cdp.eval(`
      return JSON.stringify({
        run_disabled: document.getElementById('u-run').disabled,
        warn: document.getElementById('u-warn').textContent,
        region_value: document.getElementById('u-region').value,
        start: document.getElementById('u-start').value,
        question: document.getElementById('u-question').value,
        parse_body: document.getElementById('u-parse-body').textContent.slice(0, 300),
      });
    `);
    step("parse diag: " + parseDiag);
    if (JSON.parse(parseDiag).run_disabled) {
      throw new Error("parse never became ready: " + parseDiag);
    }
    await cdp.eval("document.getElementById('u-run').click(); return true;");
    step("first run started");
    await waitFor(cdp,
      "!document.getElementById('u-feedback-panel').hidden && " +
      "document.getElementById('fb-conditions').textContent.includes('当前 run')",
      "first run finished", 300000);
    await sleep(600);
    let cond = JSON.parse(JSON.stringify(await cdp.eval(READ_CONDITIONS)));
    trace.steps.push({ step: "initial_run", parent: null, conditions: cond, ...(await stepSnapshot()),
                       run_id: runIdsIn(cond["当前 run（下次反馈的父轮）"])[0] || null,
                       screenshot: await shot("用户页-首轮结果.png", "#u-feedback-panel") });

    // --- 3. exclude OLGA -> recompute --------------------------------------
    const pickStation = (name) => cdp.eval(`
      const sel = document.getElementById('fb-station');
      const opt = [...sel.options].find(o => o.dataset.station === ${JSON.stringify(name)});
      if (!opt) return false;
      sel.value = opt.value;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    `);
    const CURRENT_RUN = `
      (() => {
        const box = document.getElementById('fb-conditions');
        if (!box) return '';
        const dts = [...box.querySelectorAll('dt')].map(e => e.textContent.trim());
        const dds = [...box.querySelectorAll('dd')].map(e => e.textContent.trim());
        const i = dts.findIndex(t => t.startsWith('当前 run'));
        return i >= 0 ? dds[i] : '';
      })()
    `;
    const currentRun = () => cdp.eval(`return (${CURRENT_RUN});`);
    const before = await currentRun();
    if (!(await pickStation("OLGA"))) throw new Error("OLGA was not in the station list");
    await cdp.eval("document.getElementById('fb-exclude').click(); return true;");
    step("exclude OLGA clicked (prev run " + before + ")");
    await waitFor(cdp, `(${CURRENT_RUN}) !== ${JSON.stringify(before)}`,
      "exclude recompute", 90000, FEEDBACK_DIAG);
    await sleep(500);
    cond = JSON.parse(JSON.stringify(await cdp.eval(READ_CONDITIONS)));
    trace.steps.push({ step: "exclude_olga", parent: before, conditions: cond, ...(await stepSnapshot()),
                       run_id: runIdsIn(cond["当前 run（下次反馈的父轮）"])[0] || null,
                       screenshot: await shot("用户页-排除OLGA.png", "#u-feedback-panel") });

    // --- 4. narrow the onset grid (the P1-1 regression step) ---------------
    const afterExclude = await currentRun();
    await cdp.eval(`
      const b = [...document.querySelectorAll('button')]
        .find(x => x.textContent.includes('收紧到 3–12 Hz'));
      if (!b) return false;
      b.click();
      return true;
    `);
    await waitFor(cdp, `(${CURRENT_RUN}) !== ${JSON.stringify(afterExclude)}`,
      "narrow-grid recompute", 90000, FEEDBACK_DIAG);
    await sleep(500);
    cond = JSON.parse(JSON.stringify(await cdp.eval(READ_CONDITIONS)));
    step("narrow grid done; chain=" + cond["分支链"]);
    trace.steps.push({ step: "narrow_grid", parent: afterExclude, conditions: cond, ...(await stepSnapshot()),
                       run_id: runIdsIn(cond["当前 run（下次反馈的父轮）"])[0] || null,
                       screenshot: await shot("用户页-收紧网格.png", "#u-result") });

    // --- 5. restore OLGA ----------------------------------------------------
    const afterNarrow = await currentRun();
    await pickStation("OLGA");
    await cdp.eval("document.getElementById('fb-include').click(); return true;");
    await waitFor(cdp, `(${CURRENT_RUN}) !== ${JSON.stringify(afterNarrow)}`,
      "restore recompute", 90000, FEEDBACK_DIAG);
    await sleep(500);
    cond = JSON.parse(JSON.stringify(await cdp.eval(READ_CONDITIONS)));
    trace.steps.push({ step: "restore_olga", parent: afterNarrow, conditions: cond, ...(await stepSnapshot()),
                       run_id: runIdsIn(cond["当前 run（下次反馈的父轮）"])[0] || null,
                       screenshot: await shot("用户页-恢复OLGA.png", "#u-feedback-panel") });

    // --- 6. narrow viewport behaviour, for the acceptance condition --------
    await cdp.send("Emulation.setDeviceMetricsOverride",
      { width: 900, height: 1000, deviceScaleFactor: 1, mobile: false });
    await sleep(400);
    await layout(900, 1000, "layout_narrow");
    await shot("用户页-窄窗口.png", "#u-feedback-panel");

    trace.console_errors = cdp.events
      .filter((e) => e.method === "Runtime.exceptionThrown")
      .map((e) => e.params?.exceptionDetails?.text || "exception");
    trace.finished_at = new Date().toISOString();
  } finally {
    if (cdp) cdp.close();
    chrome.kill("SIGKILL");
    try { rmSync(PROFILE, { recursive: true, force: true }); } catch { /* ignore */ }
  }

  const target = join(OUT, "ui-operation-trace-gate-b.json");
  writeFileSync(target, JSON.stringify(trace, null, 2));
  console.log(JSON.stringify(trace, null, 2));
  console.log("wrote " + target);
}

main().catch((err) => {
  console.error("UI TRACE FAILED:", err.message);
  process.exit(1);
});