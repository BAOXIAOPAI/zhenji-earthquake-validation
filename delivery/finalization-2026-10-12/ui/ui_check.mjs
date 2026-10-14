// UI 检查 + 截图（1280×800 / 1440×900 / 390×844）。
// 只读页面：加载示例、切换记录、切换视图都不触发模型调用。
// 输出 JSON 到同目录 ui_check.json；截图到同目录 screenshots/。
import {chromium} from '/Users/baoxiaopai/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs';
import {writeFileSync, mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const shots = join(here, 'screenshots');
mkdirSync(shots, {recursive: true});
const base = process.env.EQ_UI_BASE || 'http://127.0.0.1:8793';
const fixtureBase = process.env.EQ_FIXTURE_BASE || 'http://127.0.0.1:8794';
const report = {schema: 'zhenji-ui-check/v1', base, fixtureBase, model_calls: 0, pages: {}, checks: {}};

const pages = [
  {name: 'workbench-1440x900', path: '/', w: 1440, h: 900, full: false},
  {name: 'workbench-1280x800', path: '/', w: 1280, h: 800, full: false},
  {name: 'workbench-390x844', path: '/', w: 390, h: 844, full: true},
  {name: 'feedback-compare-1440x900', path: '/?run=r-3ae9c6948c2f8967', w: 1440, h: 900, full: false},
  {name: 'fallback-1440x900', path: '/?run=r-2365883191ac74a8', w: 1440, h: 900, full: false},
  {name: 'failure-1440x900', path: '/?run=r-73150d6d3f83d894', w: 1440, h: 900, full: false},
  {name: 'records-1440x900', path: '/?view=records', w: 1440, h: 900, full: false},
  {name: 'about-1440x900', path: '/?view=about', w: 1440, h: 900, full: false},
  {name: 'legal-unknown-1440x900', path: '/?run=r-6366da9f9c81c5c6', w: 1440, h: 900,
   full: false, base: fixtureBase},
];

const browser = await chromium.launch({headless: true});
for (const spec of pages) {
  const ctx = await browser.newContext({viewport: {width: spec.w, height: spec.h}, deviceScaleFactor: 1});
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push('console:' + m.text()); });
  await page.goto((spec.base || base) + spec.path, {waitUntil: 'networkidle', timeout: 30000}).catch(() => {});
  await page.waitForTimeout(1400);
  const dom = await page.evaluate(() => {
    const q = s => document.querySelector(s);
    const qa = s => [...document.querySelectorAll(s)];
    const canvas = q('#wave-canvas');
    let wavePixels = 0;
    if (canvas) {
      try {
        const d = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        for (let i = 0; i < d.length; i += 4) {
          if (d[i] + d[i + 1] + d[i + 2] > 140) wavePixels++;
        }
      } catch (e) { wavePixels = -1; }
    }
    const activeView = qa('.view.on').map(v => v.id);
    return {
      title: document.title,
      brand: (q('.brand .mark') || {}).textContent || '',
      activeViews: activeView,
      verdict: (q('#conclusion-body .verdict-big') || {}).textContent || '',
      statusBadges: qa('#conclusion-body .status-badge').map(b => b.textContent),
      failBanner: !!q('#conclusion-body .failbanner'),
      taskText: (q('#task-body') || {}).textContent || '',
      currentRun: (q('#current-run') || {}).textContent || '',
      timelineItems: qa('#tool-timeline .timeline li').length,
      guardVisible: q('#guard-actions') ? !q('#guard-actions').hidden : false,
      compareCols: qa('#compare-body .cmpcol').length,
      diagnosticNote: (q('#compare-body .note') || {}).textContent || '',
      recordCount: qa('#records-body .record').length,
      recordsHaveExample: qa('#records-body .record').some(r => r.textContent.includes('r-e2e8419bf299785d')),
      aboutHeading: (q('#view-about h2') || {}).textContent || '',
      wavePixels,
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
    };
  });
  await page.screenshot({path: join(shots, spec.name + '.png'), fullPage: spec.full});
  report.pages[spec.name] = {...dom, errors};
  await ctx.close();
}
await browser.close();

const c = report.checks;
const wb = report.pages['workbench-1440x900'];
c.brand_is_zhenji = (wb.brand || '').includes('震迹');
c.default_is_workbench_replay = wb.activeViews.includes('view-workbench')
  && (wb.currentRun || '').startsWith('r-');
c.conclusion_rendered = (wb.verdict || '').length > 2;
c.task_card_has_conditions = /目标频带|分析目标|诊断网格/.test(wb.taskText);
c.waveform_non_blank = wb.wavePixels > 500;
c.tool_timeline_real = wb.timelineItems >= 3;
c.feedback_compare_three_columns = report.pages['feedback-compare-1440x900'].compareCols === 3;
c.feedback_diagnostic_parameter_shown = /诊断参数|网格/.test(
  report.pages['feedback-compare-1440x900'].diagnosticNote);
c.fallback_badge_present = report.pages['fallback-1440x900'].statusBadges
  .some(s => s.includes('规则后备'));
c.failure_state_present = report.pages['failure-1440x900'].failBanner
  && report.pages['failure-1440x900'].statusBadges.some(s => s.includes('失败'));
c.records_view_lists_real_runs = report.pages['records-1440x900'].recordCount > 0
  && report.pages['records-1440x900'].recordsHaveExample;
c.about_view_present = (report.pages['about-1440x900'].aboutHeading || '').includes('震迹');
const legal = report.pages['legal-unknown-1440x900'];
c.legal_unknown_state_present = legal.statusBadges.some(s => s.includes('当前证据不足'));
c.legal_unknown_labelled_fixture = legal.statusBadges.some(s => s.includes('fixture'));
c.legal_unknown_compare = legal.compareCols === 3;
for (const [name, p] of Object.entries(report.pages)) {
  c['no_horizontal_overflow_' + name] = p.overflow <= 1;
  c['no_page_errors_' + name] = p.errors.length === 0;
}
report.summary = {total: Object.keys(c).length,
  passed: Object.values(c).filter(v => v === true).length,
  failed: Object.keys(c).filter(k => c[k] !== true)};
writeFileSync(join(here, 'ui_check.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify({summary: report.summary,
  key: {verdict: wb.verdict, wavePixels: wb.wavePixels, timelineItems: wb.timelineItems,
        compareCols: report.pages['feedback-compare-1440x900'].compareCols,
        recordCount: report.pages['records-1440x900'].recordCount,
        failure: report.pages['failure-1440x900'].statusBadges,
        fallback: report.pages['fallback-1440x900'].statusBadges}}, null, 2));
if (report.summary.failed.length) process.exitCode = 1;