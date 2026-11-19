// Verifies the English demo fixtures against a running stack through the real API (no mocks):
// uploads them as the reviewer (unless 'noupload'), compares every extracted field with
// demo_documents/video/manifest.json, and prints the answers to candidate questions.
//   node verify_demo_pipeline.mjs ../../demo_documents/video '["What is the total due on the Brightline Systems invoice?"]'
//   PROBE_USER=finance@otherco.demo node verify_demo_pipeline.mjs ../../demo_documents/video '[...]' noupload
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
const DIR = process.argv[2]; const questions = JSON.parse(process.argv[3] ?? '[]'); const doUpload = process.argv[4] !== 'noupload';
const browser = await chromium.launch(); const page = await (await browser.newContext()).newPage();
await page.goto('http://localhost:5173');
await page.getByRole('button', { name: /Sign in with SSO/ }).click();
await page.locator('#username').fill(process.env.PROBE_USER ?? 'reviewer@northstar.demo');
await page.locator('#password').fill(process.env.DEMO_PASSWORD ?? 'DemoPass123!');
await page.locator('#kc-login').click(); await page.locator('.sidebar').waitFor();
const call = (url, opts = {}) => page.evaluate(async ([url, opts]) => {
  const s = await (await fetch('/api/auth/session')).json();
  const init = { method: opts.method ?? 'GET', headers: { 'X-CSRF-Token': s.csrf_token } };
  if (opts.json) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
  if (opts.file) { const fd = new FormData(); fd.append('file', new File([Uint8Array.from(atob(opts.file.b64), c => c.charCodeAt(0))], opts.file.name, { type: 'application/pdf' })); fd.append('groups', 'finance'); init.body = fd; }
  const r = await fetch(url, init); return { status: r.status, body: await r.json().catch(() => null) };
}, [url, opts]);
const manifest = JSON.parse(fs.readFileSync(path.join(DIR, 'manifest.json'), 'utf8'));
if (doUpload) for (const m of manifest) {
  const up = await call('/api/documents', { method: 'POST', file: { name: m.file, b64: fs.readFileSync(path.join(DIR, m.file)).toString('base64') } });
  const id = up.body?.document_id ?? up.body?.id; console.log(`\n### ${m.file}: upload ${up.status} id=${id} title=${up.body?.title}`);
  let ex;
  for (let i = 0; i < 90; i++) { ex = await call(`/api/documents/${id}/extraction`); if (ex.status === 200) break; await new Promise(r => setTimeout(r, 2000)); }
  if (ex.status !== 200) { console.log('no extraction', ex.status, JSON.stringify(ex.body).slice(0, 300)); continue; }
  const e = ex.body; console.log(`status=${e.status} provider=${e.provider} issues=${JSON.stringify(e.issues.map(i => `${i.field}:${i.code ?? ''}:${i.message}`))}`);
  let ok = 0, bad = 0;
  for (const [k, v] of Object.entries(m.expected_fields)) {
    const f = e.fields.find(x => x.name === k); const got = f?.value ?? null;
    const match = String(got).replace(/\s/g, '') === String(v).replace(/\s/g, ''); match ? ok++ : bad++;
    console.log(`  ${match ? 'OK ' : 'XX '} ${k}: got=${got} expected=${v} state=${f?.state} grounding=${f?.grounding} ev=p.${f?.evidence?.page}`);
  }
  console.log(`  fields ok=${ok} mismatched=${bad}; line items extracted=${e.fields.filter(f => f.name.startsWith('line_items[')).length}`);
  const st = await call(`/api/documents/${id}/structure`); const p1 = st.body?.pages?.[0];
  console.log(`  page1 method=${p1?.method} ocr=${JSON.stringify(p1?.ocr ?? null)}`);
}
for (const q of questions) {
  const r = await call('/api/ask', { method: 'POST', json: { question: q, top_k: 5, mode: 'hybrid' } });
  console.log(`\n=== ${q}\n${(r.body?.answer ?? JSON.stringify(r.body)).replace(/\n\n/g, '\n').slice(0, 900)}`);
  console.log('sources:', (r.body?.sources ?? []).map(s => `${s.id}:${s.title}`).join(' | '));
}
await browser.close();
