// NEXUS AI load test (k6). Real requests against a disposable Compose stack in AUTH_MODE=oidc.
//
//   python scripts/run_load_test.py --profile smoke      (wraps: docker run grafana/k6 ... + docker stats)
//
// Env: PROFILE=smoke|moderate|stress|sustained, BASE (API, default http://api:8000),
//      KC (Keycloak as reached from the k6 container, default http://keycloak:8080),
//      PUBLIC_KC (the issuer host in redirects, default http://localhost:8080),
//      PUBLIC_FRONTEND (redirect_uri host, default http://localhost:5173).
// Users: synthetic realm users of two tenants with different groups and roles. API calls use
// Keycloak access tokens (bearer, aud=nexus-api) obtained with the TEST-ONLY password-grant client;
// the `login` scenario measures the full browser login (Authorization Code + PKCE through the BFF).
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import exec from 'k6/execution';

const BASE = __ENV.BASE || 'http://api:8000';
const KC = __ENV.KC || 'http://keycloak:8080';
const PUBLIC_KC = __ENV.PUBLIC_KC || 'http://localhost:8080';
const PUBLIC_FRONTEND = __ENV.PUBLIC_FRONTEND || 'http://localhost:5173';
const PROFILE = __ENV.PROFILE || 'smoke';
const PASSWORD = 'DemoPass123!'; // synthetic local-only realm credential

const USERS = [
  { email: 'finance@northstar.demo', tenant: 'northstar', groups: ['finance', 'legal'] },
  { email: 'hr@northstar.demo', tenant: 'northstar', groups: ['hr'] },
  { email: 'reviewer@northstar.demo', tenant: 'northstar', groups: ['procurement', 'finance', 'legal'], reviewer: true },
  { email: 'admin@northstar.demo', tenant: 'northstar', groups: ['finance', 'legal', 'sales', 'hr'] },
  { email: 'finance@otherco.demo', tenant: 'otherco', groups: ['finance'] },
];
const QUESTIONS = [
  ['hybrid', 'What is the notice period in the Atlas supplier agreement?'],
  ['hybrid', 'Kolika je naknada za korištenje privatnog automobila?'],
  ['hybrid', '99.9% availability and Atlas notice period'],
  ['semantic', "How reliable is the cloud provider's service?"],
  ['lexical', 'NS-IT-2026-017'],
  ['hybrid', 'What is the company car policy for sales managers?'],
  ['hybrid', 'What is the SILVER-IBIS-73 capex forecast?'],
  ['semantic', 'salary increase budget for next year'],
];

// Per-endpoint latency sub-metrics (k6 computes them only when a threshold references them; the
// bounds are deliberately loose: they are measurements, not pass/fail SLOs).
const ENDPOINTS = ['ask_hybrid', 'ask_semantic', 'ask_lexical', 'list_documents', 'document_detail', 'download',
  'session_validation', 'probe_cross_tenant', 'compare', 'extraction_view', 'upload', 'job_status', 'delete',
  'login_start', 'keycloak_login_page', 'keycloak_credentials', 'login_callback', 'session_cookie', 'keycloak_token'];
const ENDPOINT_TRENDS = Object.fromEntries(ENDPOINTS.map(n => [`http_req_duration{name:${n}}`, ['max>=0']]));

const PROFILES = {
  smoke: { api: { executor: 'constant-vus', vus: 2, duration: '30s' },
           login: { executor: 'per-vu-iterations', vus: 1, iterations: 3, maxDuration: '1m' } },
  moderate: { api: { executor: 'constant-vus', vus: 10, duration: '2m' },
              login: { executor: 'constant-arrival-rate', rate: 6, timeUnit: '1m', duration: '2m', preAllocatedVUs: 2 } },
  stress: { api: { executor: 'ramping-vus', startVUs: 5, stages: [{ duration: '1m', target: 20 }, { duration: '1m', target: 40 },
                                                                  { duration: '1m', target: 40 }, { duration: '30s', target: 0 }] },
            login: { executor: 'constant-arrival-rate', rate: 12, timeUnit: '1m', duration: '3m', preAllocatedVUs: 3 } },
  sustained: { api: { executor: 'constant-vus', vus: 15, duration: '8m' },
               login: { executor: 'constant-arrival-rate', rate: 4, timeUnit: '1m', duration: '8m', preAllocatedVUs: 2 } },
};

export const options = {
  scenarios: {
    api: Object.assign({ exec: 'apiMix' }, PROFILES[PROFILE].api),
    login: Object.assign({ exec: 'browserLogin' }, PROFILES[PROFILE].login),
  },
  thresholds: Object.assign({
    authz_violations: ['count==0'],              // zero tolerance, all profiles
    http_req_failed: ['rate<0.05'],                // 404s of the security probes are expected responses
    checks: ['rate>0.95'],
  }, ENDPOINT_TRENDS),
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'],
};

const authzViolations = new Counter('authz_violations');
const jobQueueWait = new Trend('job_queue_wait_ms', true);
const jobProcessing = new Trend('job_processing_ms', true);
const jobEndToEnd = new Trend('job_end_to_end_ms', true);
const loginDuration = new Trend('bff_login_ms', true);
const jobsFailed = new Rate('jobs_failed');
http.setResponseCallback(http.expectedStatuses(200, 201, 202, 404));

function token(user) {
  const res = http.post(`${KC}/realms/nexus/protocol/openid-connect/token`, {
    grant_type: 'password', client_id: 'nexus-loadtest', client_secret: 'nexus-loadtest-local-dev-secret',
    username: user.email, password: PASSWORD, scope: 'openid',
  }, { tags: { name: 'keycloak_token' } });
  check(res, { 'token issued': r => r.status === 200 });
  const body = res.json();
  return { access: body.access_token, expires: Date.now() + (body.expires_in - 30) * 1000 };
}

export function setup() {
  const sessions = {};
  for (const user of USERS) sessions[user.email] = token(user);
  const reviewer = { headers: { Authorization: `Bearer ${sessions['reviewer@northstar.demo'].access}` } };
  const library = http.get(`${BASE}/api/intelligence/documents`, reviewer).json();
  const procurement = (library || []).filter(d => d.extraction_id).map(d => d.id).slice(0, 4);
  const others = http.get(`${BASE}/api/documents`, { headers: { Authorization: `Bearer ${sessions['finance@otherco.demo'].access}` } }).json();
  const own = {};
  for (const user of USERS) {
    own[user.email] = (http.get(`${BASE}/api/documents`, { headers: { Authorization: `Bearer ${sessions[user.email].access}` } }).json() || []).map(d => d.id);
  }
  return { compareIds: procurement, otherTenantDoc: (others || [])[0]?.id, own };
}

const cache = {};
function headersFor(user) {
  let t = cache[user.email];
  if (!t || Date.now() > t.expires) { t = token(user); cache[user.email] = t; }
  return { headers: { Authorization: `Bearer ${t.access}` } };
}

function pick(list) { return list[Math.floor(Math.random() * list.length)]; }

export function apiMix(data) {
  const user = USERS[exec.vu.idInTest % USERS.length];
  const h = headersFor(user);
  const roll = Math.random();
  if (roll < 0.45) {
    const [mode, question] = pick(QUESTIONS);
    const res = http.post(`${BASE}/api/ask`, JSON.stringify({ question, mode }),
      Object.assign({ tags: { name: `ask_${mode}` } }, { headers: Object.assign({ 'Content-Type': 'application/json' }, h.headers) }));
    check(res, { 'ask 200': r => r.status === 200 });
    if (res.status === 200) {
      const body = res.json();
      const visible = new Set(data.own[user.email] || []);
      for (const s of body.sources || []) {
        // documents uploaded during the run carry their tenant in the title (load-<tenant>-...)
        const loadDoc = (s.title || '').startsWith('load-');
        if (loadDoc ? !(s.title || '').startsWith(`load-${user.tenant}-`) : !visible.has(s.document_id)) authzViolations.add(1, { kind: 'source' });
      }
      if (user.tenant === 'northstar' && (body.answer || '').includes('SILVER-IBIS-73')) authzViolations.add(1, { kind: 'answer' });
    }
  } else if (roll < 0.62) {
    const res = http.get(`${BASE}/api/documents`, Object.assign({ tags: { name: 'list_documents' } }, h));
    check(res, { 'list 200': r => r.status === 200 });
    for (const d of res.status === 200 ? res.json() : []) {
      const allowed = d.title.startsWith('load-') ? d.title.startsWith(`load-${user.tenant}-`) : (data.own[user.email] || []).includes(d.id);
      if (!allowed) authzViolations.add(1, { kind: 'list' });
    }
  } else if (roll < 0.70) {
    const id = pick(data.own[user.email] || ['none']);
    check(http.get(`${BASE}/api/documents/${id}`, Object.assign({ tags: { name: 'document_detail' } }, h)), { 'detail 200': r => r.status === 200 });
  } else if (roll < 0.76) {
    const id = pick(data.own[user.email] || ['none']);
    check(http.get(`${BASE}/api/documents/${id}/source`, Object.assign({ tags: { name: 'download' } }, h)), { 'download 200': r => r.status === 200 });
  } else if (roll < 0.82) {
    check(http.get(`${BASE}/api/auth/session`, Object.assign({ tags: { name: 'session_validation' } }, h)), { 'session 200': r => r.status === 200 });
  } else if (roll < 0.87 && data.otherTenantDoc && user.tenant === 'northstar') {
    // security probe under load: another tenant's document must be indistinguishable from a missing one
    const res = http.get(`${BASE}/api/documents/${data.otherTenantDoc}`, Object.assign({ tags: { name: 'probe_cross_tenant' } }, h));
    if (res.status !== 404) authzViolations.add(1, { kind: 'cross_tenant' });
  } else if (roll < 0.92 && user.reviewer && data.compareIds.length >= 2) {
    const res = http.post(`${BASE}/api/intelligence/compare`, JSON.stringify({ document_ids: data.compareIds.slice(0, 2) }),
      { headers: Object.assign({ 'Content-Type': 'application/json' }, h.headers), tags: { name: 'compare' } });
    check(res, { 'compare 200': r => r.status === 200 });
    const ext = http.get(`${BASE}/api/documents/${data.compareIds[0]}/extraction`, Object.assign({ tags: { name: 'extraction_view' } }, h));
    check(ext, { 'extraction 200': r => r.status === 200 });
  } else {
    uploadAndWait(user, h);
  }
  sleep(Math.random() * 0.5);
}

function uploadAndWait(user, h) {
  const marker = `LOAD-${exec.vu.idInTest}-${exec.vu.iterationInScenario}-${Date.now()}`;
  const body = `Load test record ${marker}.\nCounterparty: Synthetic Partner d.o.o.\nAnnual value: EUR 12,500\nNotice period: 30 days\n`;
  const t0 = Date.now();
  const res = http.post(`${BASE}/api/documents`, { file: http.file(body, `load-${user.tenant}-${marker}.txt`, 'text/plain'), groups: user.groups[0] },
    Object.assign({ tags: { name: 'upload' } }, h));
  if (!check(res, { 'upload 202': r => r.status === 202 })) return;
  const jobId = res.json().job.id;
  for (let i = 0; i < 120; i++) {
    sleep(0.5);
    const job = http.get(`${BASE}/api/jobs/${jobId}`, Object.assign({ tags: { name: 'job_status' } }, h)).json();
    if (['completed', 'failed', 'cancelled'].includes(job.status)) {
      jobsFailed.add(job.status !== 'completed');
      if (job.started_at && job.created_at) jobQueueWait.add(Date.parse(job.started_at) - Date.parse(job.created_at));
      if (job.started_at && job.finished_at) jobProcessing.add(Date.parse(job.finished_at) - Date.parse(job.started_at));
      jobEndToEnd.add(Date.now() - t0);
      if (job.status === 'completed') {   // keep the dataset stable: remove the synthetic document again
        http.del(`${BASE}/api/documents/${res.json().id}`, null, Object.assign({ tags: { name: 'delete' } }, h));
      }
      return;
    }
  }
  jobsFailed.add(true);
}

function cookieHeader(res, existing) {
  const jar = Object.assign({}, existing || {});
  for (const [name, values] of Object.entries(res.cookies || {})) if (values.length) jar[name] = values[0].value;
  return jar;
}
const serialize = jar => Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');

export function browserLogin() {
  const user = pick(USERS);
  const t0 = Date.now();
  const start = http.get(`${BASE}/api/auth/oidc/login?return_to=/`, { redirects: 0, tags: { name: 'login_start' } });
  if (!check(start, { 'login redirect': r => r.status === 302 })) return;
  let apiJar = cookieHeader(start);
  const authUrl = start.headers.Location.replace(PUBLIC_KC, KC);
  const page = http.get(authUrl, { redirects: 0, tags: { name: 'keycloak_login_page' } });
  const match = /<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"/.exec(page.body || '');
  if (!check(page, { 'login page': () => !!match })) return;
  const action = match[1].replace(/&amp;/g, '&').replace(PUBLIC_KC, KC);
  const kcJar = cookieHeader(page);
  const posted = http.post(action, { username: user.email, password: PASSWORD, credentialId: '' },
    { redirects: 0, headers: { Cookie: serialize(kcJar) }, tags: { name: 'keycloak_credentials' } });
  if (!check(posted, { 'idp redirect': r => r.status === 302 })) return;
  const callback = posted.headers.Location.replace(PUBLIC_FRONTEND, BASE);
  const done = http.get(callback, { redirects: 0, headers: { Cookie: serialize(apiJar) }, tags: { name: 'login_callback' } });
  apiJar = cookieHeader(done, apiJar);
  const session = http.get(`${BASE}/api/auth/session`, { headers: { Cookie: serialize(apiJar) }, tags: { name: 'session_cookie' } });
  if (check(session, { 'bff session': r => r.status === 200 && r.json().method === 'session' })) loginDuration.add(Date.now() - t0);
}

export function handleSummary(data) {
  const pickMetric = name => data.metrics[name] ? data.metrics[name].values : null;
  const byName = {};
  for (const [key, metric] of Object.entries(data.metrics)) {
    const m = /^http_req_duration\{name:(.+)\}$/.exec(key);
    if (m) byName[m[1]] = metric.values;
  }
  const compact = {
    profile: PROFILE, state: data.state, thresholds_ok: Object.values(data.metrics).every(m => !m.thresholds
      || Object.values(m.thresholds).every(t => t.ok)),
    http_reqs: pickMetric('http_reqs'), http_req_failed: pickMetric('http_req_failed'),
    http_req_duration: pickMetric('http_req_duration'), iterations: pickMetric('iterations'),
    authz_violations: pickMetric('authz_violations') || { count: 0 }, checks: pickMetric('checks'),
    job_queue_wait_ms: pickMetric('job_queue_wait_ms'), job_processing_ms: pickMetric('job_processing_ms'),
    job_end_to_end_ms: pickMetric('job_end_to_end_ms'), jobs_failed: pickMetric('jobs_failed'),
    bff_login_ms: pickMetric('bff_login_ms'), by_endpoint: byName,
  };
  return { stdout: JSON.stringify(compact, null, 2) + '\n', '/out/summary.json': JSON.stringify(compact, null, 2) };
}
