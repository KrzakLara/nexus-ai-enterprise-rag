// Records the NEXUS AI product demo against a locally running stack (see docs/DEMO.md, section 2).
//
// Real browser, real interactions: Chromium is driven by Playwright and every frame comes from the
// Chrome DevTools screencast of the actual page. Nothing is mocked. The script writes JPEG frames
// with their capture timestamps plus a markers.json file; build_video.py turns them into the MP4.
//
//   cd scripts/demo_video && npm install playwright@1.57.0 && npx playwright install chromium
//   node record_demo.mjs <output-dir>
//
// Accounts are the synthetic Keycloak demo users from docs/DEMO.md. The password is read from
// DEMO_PASSWORD (default: the documented local-only demo password) and is never shown on screen
// in clear text (the Keycloak password field is masked).
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = process.env.NEXUS_REPO ?? path.resolve(HERE, '..', '..');
const OUT = path.resolve(process.argv[2] ?? path.join(HERE, 'out'));
const APP = process.env.APP_URL ?? 'http://localhost:5173';
const GRAFANA = process.env.GRAFANA_URL ?? 'http://localhost:3001';
const PASSWORD = process.env.DEMO_PASSWORD ?? 'DemoPass123!';
const GRAFANA_AUTH = 'Basic ' + Buffer.from(`${process.env.GRAFANA_ADMIN ?? 'grafana-local-admin'}:${process.env.GRAFANA_ADMIN_PASSWORD ?? 'grafana-local-admin-secret'}`).toString('base64');
const UPLOAD_FILE = path.join(REPO, 'eval', 'docintel', 'documents', 'di-i02.pdf');
const RAG_QUESTION = 'Servis Plus R-2026-0153: datum dospijeća i ukupno za platiti';
const VIEW = { width: 1440, height: 810 };   // CSS pixels; scale 4/3 => 1920x1080 frames
const SCALE = 4 / 3;

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(path.join(OUT, 'frames'), { recursive: true });
const markers = [];
const frames = [];
// Deliberate pauses (>= 1 s) are stretched so captions can be read in the edited video.
const HOLD = Number(process.env.DEMO_HOLD ?? 1.6);
const sleep = ms => new Promise(r => setTimeout(r, ms >= 1000 ? ms * HOLD : ms));
const mark = (name) => { markers.push({ name, t: Date.now() / 1000 }); console.log(`[mark] ${name}`); };

// ---------------------------------------------------------------- visible cursor (recording aid)
const CURSOR_SCRIPT = `(() => {
  if (window.__demoCursor) return; window.__demoCursor = true;
  const install = () => {
    const c = document.createElement('div');
    c.id = '__demo_cursor';
    c.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24"><path d="M4 2 L4 19 L8.5 14.8 L11.6 21.6 L14.4 20.4 L11.3 13.7 L17.5 13.7 Z" fill="#111827" stroke="#ffffff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, { position: 'fixed', left: '0px', top: '0px', zIndex: 2147483647, pointerEvents: 'none',
      transform: 'translate(-3px,-2px)', filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.35))' });
    const pos = JSON.parse(sessionStorage.getItem('__demo_cursor_pos') || '[720,420]');
    c.style.left = pos[0] + 'px'; c.style.top = pos[1] + 'px';
    document.documentElement.appendChild(c);
    addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px';
      try { sessionStorage.setItem('__demo_cursor_pos', JSON.stringify([e.clientX, e.clientY])); } catch {} }, true);
    addEventListener('mousedown', e => {
      const r = document.createElement('div');
      Object.assign(r.style, { position: 'fixed', left: (e.clientX - 18) + 'px', top: (e.clientY - 18) + 'px', width: '36px', height: '36px',
        borderRadius: '50%', border: '3px solid rgba(99,102,241,.85)', background: 'rgba(99,102,241,.18)', zIndex: 2147483646,
        pointerEvents: 'none', transition: 'transform .45s ease-out, opacity .45s ease-out', transform: 'scale(.4)', opacity: '1' });
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(1.5)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 600);
    }, true);
  };
  if (document.documentElement) install(); else addEventListener('DOMContentLoaded', install);
})();`;

// ---------------------------------------------------------------- screencast
let cdp = null;
async function startScreencast(page) {
  if (cdp) { try { await cdp.send('Page.stopScreencast'); await cdp.detach(); } catch { /* target gone */ } }
  cdp = await page.context().newCDPSession(page);
  const session = cdp;
  session.on('Page.screencastFrame', async ({ data, metadata, sessionId }) => {
    const file = `f${String(frames.length).padStart(6, '0')}.jpg`;
    frames.push({ file, t: metadata.timestamp ?? Date.now() / 1000 });
    fs.writeFileSync(path.join(OUT, 'frames', file), Buffer.from(data, 'base64'));
    try { await session.send('Page.screencastFrameAck', { sessionId }); } catch { /* detached */ }
  });
  await session.send('Page.startScreencast', { format: 'jpeg', quality: 92, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1 });
}

// ---------------------------------------------------------------- human-like input
let cursor = { x: 720, y: 420 };
async function glide(page, x, y, ms = 650) {
  const from = { ...cursor };
  const steps = Math.max(12, Math.round(ms / 16));
  for (let i = 1; i <= steps; i++) {
    const k = i / steps; const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    await page.mouse.move(from.x + (x - from.x) * e, from.y + (y - from.y) * e);
    await sleep(16);
  }
  cursor = { x, y };
}
async function hover(page, locator, ms) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error('element not visible');
  await glide(page, box.x + Math.min(box.width / 2, 60), box.y + box.height / 2, ms);
}
async function click(page, locator, { pause = 250, ms } = {}) {
  await locator.waitFor({ state: 'visible' });
  await hover(page, locator, ms);
  await sleep(pause);
  await page.mouse.down(); await sleep(70); await page.mouse.up();
}
async function type(page, locator, text, delay = 38) {
  await click(page, locator);
  await locator.pressSequentially(text, { delay });
}
async function smoothScroll(page, dy, ms = 900, selector = null) {
  const steps = Math.round(ms / 16);
  for (let i = 0; i < steps; i++) {
    if (selector) await page.evaluate(([s, d]) => { const el = document.querySelector(s); if (el) el.scrollBy(0, d); }, [selector, dy / steps]);
    else await page.mouse.wheel(0, dy / steps);
    await sleep(16);
  }
}

// ---------------------------------------------------------------- scenes
async function ssoLogin(page, email) {
  await page.goto(APP);
  await page.getByRole('button', { name: /Sign in with SSO/ }).waitFor();
  await sleep(1800);
  mark(`login_page:${email}`);
  await click(page, page.getByRole('button', { name: /Sign in with SSO/ }));
  await page.waitForURL(/:8080\//);
  await page.locator('#username').waitFor();
  await startScreencast(page);            // cross-origin navigation: re-attach the screencast
  await sleep(700);
  mark(`keycloak:${email}`);
  await type(page, page.locator('#username'), email, 45);
  await click(page, page.locator('#password'), { pause: 120 });
  await page.locator('#password').fill(PASSWORD);   // masked field; filled at once, never shown in clear text
  await sleep(300);
  await click(page, page.locator('#kc-login, input[type=submit], button[type=submit]').first());
  await page.waitForURL(u => u.toString().startsWith(APP));
  await startScreencast(page);
  await page.locator('.sidebar').waitFor();
  mark(`logged_in:${email}`);
  await sleep(2200);
}

async function logout(page) {
  await click(page, page.locator('.profile-mini button[title="Sign out"]'));
  await page.waitForURL(u => u.toString().startsWith(APP) && !u.toString().includes('8080'), { timeout: 20000 }).catch(() => {});
  await page.getByRole('button', { name: /Sign in with SSO/ }).waitFor({ timeout: 20000 });
  await startScreencast(page);
  mark('logged_out');
}

async function main() {
  const browser = await chromium.launch({ args: ['--hide-scrollbars', '--force-color-profile=srgb', '--font-render-hinting=none'] });
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: SCALE, locale: 'en-US', timezoneId: 'Europe/Zagreb', colorScheme: 'light' });
  await context.addInitScript(CURSOR_SCRIPT);
  // Grafana (local admin, synthetic stack): authenticate with a request header so no login form is recorded.
  await context.route(`${GRAFANA}/**`, route => route.continue({ headers: { ...route.request().headers(), authorization: GRAFANA_AUTH } }));
  const page = await context.newPage();
  page.setDefaultTimeout(60000);
  await page.goto('about:blank');
  await startScreencast(page);

  // ---- 1. SSO login as the document reviewer
  await ssoLogin(page, 'reviewer@northstar.demo');

  // ---- 2. Upload a synthetic invoice and watch the real background job
  await click(page, page.getByRole('button', { name: /Document Library/ }));
  await sleep(1800);
  mark('library');
  await click(page, page.getByRole('button', { name: /Upload document/ }));
  await page.locator('.upload-modal').waitFor();
  await sleep(700);
  mark('upload_modal');
  const chooser = page.waitForEvent('filechooser');
  await click(page, page.locator('.drop-zone'));
  await (await chooser).setFiles(UPLOAD_FILE);
  await sleep(700);
  const groupBox = name => page.locator('.access-group-list label').filter({ hasText: new RegExp(`^${name}$`) }).locator('input');
  if (!(await groupBox('finance').isChecked())) await click(page, groupBox('finance'));
  if (await groupBox('procurement').isChecked()) await click(page, groupBox('procurement'));
  await sleep(500);
  await click(page, page.locator('.upload-modal button[type=submit]'));
  mark('upload_submitted');
  const row = page.locator('.doc-table tbody tr').filter({ hasText: 'di-i02.pdf' }).first();
  await row.waitFor();
  await sleep(400);
  mark('upload_row_visible');
  await row.locator('.proc-badge.ready').waitFor({ timeout: 180000 });
  mark('upload_ready');
  await hover(page, row.locator('.proc-badge').first());
  await sleep(2600);

  // ---- 3. Lifecycle panel (real job history)
  await click(page, row.locator('.file-item'));
  await page.locator('.detail-drawer').waitFor();
  await page.locator('.version-list').first().waitFor({ timeout: 15000 }).catch(() => {});
  await sleep(1200);
  mark('lifecycle');
  await smoothScroll(page, 260, 900, '.drawer-content');
  await sleep(2200);
  await click(page, page.locator('.detail-drawer button[title="Close document"]'));
  await sleep(500);

  // ---- 4. Document Intelligence: extraction with evidence, then approval
  await click(page, page.getByRole('button', { name: /Document Intelligence/ }));
  await page.locator('.di-table tbody tr').first().waitFor();
  await sleep(1500);
  mark('di_library');
  const diRow = page.locator('.di-table tbody tr').filter({ hasText: 'di-i02.pdf' }).first();
  await click(page, diRow.locator('button.link-button'));
  await page.locator('.di-fields').first().waitFor();
  await sleep(1500);
  mark('di_detail');
  const ev = page.locator('.di-fields tr').filter({ has: page.locator('code', { hasText: /^total_amount$/ }) }).locator('button.link-button').first();
  if (await ev.count()) { await click(page, ev); await sleep(1800); mark('di_evidence'); }
  await smoothScroll(page, 380, 1200, '.content-view');
  await sleep(2000);
  await smoothScroll(page, 420, 1200, '.content-view');
  await sleep(1500);
  const approve = page.locator('.di-actions button', { hasText: 'Approve' });
  await click(page, approve);
  await page.locator('.di-note', { hasText: /approved/i }).first().waitFor({ timeout: 20000 });
  await sleep(500);
  mark('di_approved');
  await page.locator('.di-history li', { hasText: 'approve' }).first().scrollIntoViewIfNeeded().catch(() => {});
  await sleep(2600);

  // ---- 5. Scanned invoice: OCR + validation blocks approval
  await click(page, page.locator('button.di-back'));
  await page.locator('.di-table tbody tr').first().waitFor();
  await sleep(900);
  const scanRow = page.locator('.di-table tbody tr').filter({ hasText: 'di-i07' }).first();
  await click(page, scanRow.locator('button.link-button'));
  await page.locator('.di-page-head').first().waitFor();
  await sleep(1600);
  mark('ocr_detail');
  await hover(page, page.locator('.di-page-head').first(), 600);
  await sleep(1800);
  const approve2 = page.locator('.di-actions button', { hasText: 'Approve' });
  await click(page, approve2);
  await page.locator('.di-blocked').waitFor({ timeout: 20000 });
  await page.locator('.di-blocked').scrollIntoViewIfNeeded();
  await sleep(400);
  mark('ocr_blocked');
  await hover(page, page.locator('.di-blocked strong'), 500);
  await sleep(3200);

  // ---- 6. Hybrid RAG with citations about the uploaded invoice
  await click(page, page.getByRole('button', { name: /AI Assistant/ }));
  await page.locator('.chat-input-wrap input').waitFor();
  await sleep(1200);
  mark('assistant');
  await page.locator('select[aria-label="Retrieval mode"]').selectOption('hybrid');
  await type(page, page.locator('.chat-input-wrap input'), RAG_QUESTION, 32);
  await sleep(400);
  await click(page, page.locator('.chat-input-wrap button[type=submit]'));
  mark('rag_asked');
  await page.locator('.answer-content').last().waitFor({ timeout: 60000 });
  await sleep(600);
  mark('rag_answer');
  await sleep(1500);
  await hover(page, page.locator('.evidence-card').first(), 700);
  await sleep(2600);

  // Multi-document hybrid question (guided prompt 3): verbatim quotes from several contracts
  await click(page, page.getByRole('button', { name: /New chat/ }));
  await sleep(900);
  await click(page, page.locator('.prompts-grid button').filter({ hasText: '3 ·' }));
  mark('fusion_asked');
  await page.locator('.answer-content').last().waitFor({ timeout: 60000 });
  await sleep(600);
  mark('fusion_answer');
  await sleep(1800);
  await hover(page, page.locator('.evidence-card').nth(1), 700);
  await sleep(2200);
  await hover(page, page.locator('.answer-metadata').last(), 700);
  await sleep(2000);

  // ---- 7. Same tenant, another tenant's identifier: nothing is returned
  await click(page, page.getByRole('button', { name: /New chat/ }));
  await sleep(1000);
  const blocked = page.locator('.prompts-grid button').filter({ hasText: '7 ·' });
  await click(page, blocked);
  mark('blocked_asked');
  await page.locator('.answer-content').last().waitFor({ timeout: 60000 });
  await sleep(500);
  mark('blocked_answer');
  await hover(page, page.locator('.answer-metadata .mini-badge').last(), 700);
  await sleep(3000);

  // ---- 8. Cross-tenant: an Otherco user cannot see the Northstar invoice
  await logout(page);
  await ssoLogin(page, 'finance@otherco.demo');
  await click(page, page.getByRole('button', { name: /Document Library/ }));
  await sleep(800);
  mark('otherco_library');
  await hover(page, page.locator('.doc-table tbody tr').first(), 700);
  await sleep(2600);
  await click(page, page.getByRole('button', { name: /AI Assistant/ }));
  await page.locator('.chat-input-wrap input').waitFor();
  await sleep(600);
  await page.locator('select[aria-label="Retrieval mode"]').selectOption('hybrid');
  await type(page, page.locator('.chat-input-wrap input'), 'What is the total of invoice R-2026-0153 from Servis Plus?', 32);
  await click(page, page.locator('.chat-input-wrap button[type=submit]'));
  mark('otherco_asked');
  await page.locator('.answer-content').last().waitFor({ timeout: 60000 });
  await sleep(500);
  mark('otherco_answer');
  await hover(page, page.locator('.answer-metadata').last(), 700);
  await sleep(3200);

  // ---- 9. Evaluation (stored benchmark results)
  await click(page, page.getByRole('button', { name: /Evaluation/ }));
  await page.locator('.stats-grid').waitFor();
  await sleep(1500);
  mark('evaluation');
  await smoothScroll(page, 330, 1300, '.content-view');
  await sleep(2800);

  // ---- 10. Grafana operations dashboard (real metrics from this session)
  await page.goto(`${GRAFANA}/?orgId=1&from=now-15m&to=now&kiosk`);
  await startScreencast(page);
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(6000);
  mark('grafana');
  await glide(page, 520, 360, 900);
  await sleep(2500);
  await glide(page, 1100, 520, 900);
  await sleep(3000);
  mark('end');

  await cdp.send('Page.stopScreencast').catch(() => {});
  await sleep(300);
  fs.writeFileSync(path.join(OUT, 'frames.json'), JSON.stringify(frames));
  fs.writeFileSync(path.join(OUT, 'markers.json'), JSON.stringify(markers, null, 1));
  await browser.close();
  console.log(`frames: ${frames.length}, markers: ${markers.length}, out: ${OUT}`);
}

main().catch(async error => {
  console.error(error);
  fs.writeFileSync(path.join(OUT, 'frames.json'), JSON.stringify(frames));
  fs.writeFileSync(path.join(OUT, 'markers.json'), JSON.stringify(markers, null, 1));
  process.exit(1);
});
