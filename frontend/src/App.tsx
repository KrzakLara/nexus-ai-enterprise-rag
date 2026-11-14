import type * as React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, ArrowDownToLine, ArrowRight, BookOpen, Check, ChevronDown, ChevronRight,
  CircleAlert, CircleCheck, Clock3, CloudUpload, Database, FileLock2, FileSearch,
  FileText, Fingerprint, LockKeyhole, LogOut, MessageSquareText, Plus,
  Search, Send, Settings2, Shield, ShieldCheck, Sparkles, X,
} from 'lucide-react';
import { api, RETRIEVAL_LABELS, type Answer, type AuditEvent, type Document, type DocumentDetail, type EvaluationSummary, type RetrievalMode, type Source, type User } from './api';

type Section = 'assistant' | 'documents' | 'governance' | 'evaluation';
type Message = { id: number; question: string; result?: Answer; error?: string };

const DEMO_USERS = [
  { email: 'admin@northstar.demo', title: 'Workspace admin', subtitle: 'All Northstar groups', initials: 'AD', color: 'purple' },
  { email: 'finance@northstar.demo', title: 'Finance analyst', subtitle: 'Finance + Legal', initials: 'FA', color: 'blue' },
  { email: 'hr@northstar.demo', title: 'HR specialist', subtitle: 'HR only', initials: 'HR', color: 'orange' },
  { email: 'sales@northstar.demo', title: 'Sales associate', subtitle: 'Sales + Legal', initials: 'SA', color: 'teal' },
  { email: 'finance@otherco.demo', title: 'External tenant', subtitle: 'Otherco · Finance only', initials: 'OC', color: 'rose' },
];
// Guided demo (finance persona): each prompt runs in the retrieval mode it demonstrates.
const QUICK_PROMPTS: { label: string; question: string; mode: RetrievalMode }[] = [
  { label: '1 · Exact identifier (Keyword)', question: 'NS-IT-2026-017', mode: 'lexical' },
  { label: '2 · Paraphrase (Semantic)', question: "How reliable is the cloud provider's service?", mode: 'semantic' },
  { label: '3 · Fusion (Hybrid)', question: '99.9% availability and Atlas notice period', mode: 'hybrid' },
  { label: '4 · Croatian question', question: 'Kolika je naknada za korištenje privatnog automobila?', mode: 'hybrid' },
  { label: '5 · Cross-language (HR → EN)', question: 'Koliko dana unaprijed moramo najaviti raskid ugovora s dobavljačem?', mode: 'hybrid' },
  { label: '6 · Abstains: not in any document', question: 'What is the company car policy for sales managers?', mode: 'hybrid' },
  { label: "7 · Blocked: another tenant's document", question: 'What is the SILVER-IBIS-73 capex forecast?', mode: 'hybrid' },
];

function Logo({ compact = false }: { compact?: boolean }) {
  return <div className="logo-lockup">
    <div className="logo-symbol"><span /><span /><span /><span /></div>
    {!compact && <div className="logo-wordmark">nexus<span>ai</span><small>KNOWLEDGE INTELLIGENCE</small></div>}
  </div>;
}

function Login({ onSuccess }: { onSuccess: (token: string, user: User) => void }) {
  const [selected, setSelected] = useState(DEMO_USERS[1].email);
  const [password, setPassword] = useState('DemoPass123!');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setLoading(true);
    try { const response = await api.login(selected, password); onSuccess(response.access_token, response.user); }
    catch (exception) { setError((exception as Error).message); }
    finally { setLoading(false); }
  }
  return <div className="login-page">
    <div className="login-art">
      <div className="login-top"><Logo /><span className="demo-label">PORTFOLIO PROJECT · V0.2-DEV</span></div>
      <div className="login-center">
        <span className="eyebrow muted-on-dark"><span className="small-pulse" /> AI-POWERED ENTERPRISE SEARCH</span>
        <h1>Knowledge you can <em>trust.</em></h1>
        <p>Ask questions across thousands of documents, inspect grounded evidence, and keep confidential information in the right hands.</p>
        <div className="login-features">
          <div><FileSearch size={19} /><span>Evidence-backed answers</span></div>
          <div><ShieldCheck size={19} /><span>Permission-aware retrieval</span></div>
          <div><Fingerprint size={19} /><span>Auditable by design</span></div>
        </div>
      </div>
      <div className="login-bottom"><span>DEVELOPED AS AN APPLIED AI ENGINEERING CASE STUDY</span><span>© 2026 NEXUS AI</span></div>
      <div className="ambient orb-one" /><div className="ambient orb-two" />
    </div>
    <div className="login-form-pane">
      <form className="login-card" onSubmit={submit}>
        <div className="mini-icon"><LockKeyhole size={23} /></div>
        <div className="eyebrow">INTERACTIVE PRODUCT DEMO</div>
        <h2>Enter your workspace</h2>
        <p className="subtext">Select a demo identity to experience role-based document access.</p>
        <div className="persona-list">
          {DEMO_USERS.map(persona => <button type="button" className={`persona ${selected === persona.email ? 'selected' : ''}`}
            key={persona.email} onClick={() => setSelected(persona.email)}>
            <span className={`avatar ${persona.color}`}>{persona.initials}</span>
            <span className="persona-copy"><strong>{persona.title}</strong><small>{persona.subtitle}</small></span>
            <span className={`radio-dot ${selected === persona.email ? 'active' : ''}`} />
          </button>)}
        </div>
        <label className="field-label" htmlFor="demo-password">Demo password</label>
        <input id="demo-password" className="text-input" type="password" value={password}
          onChange={event => setPassword(event.target.value)} autoComplete="off" />
        {error && <div className="form-error"><CircleAlert size={16} />{error}</div>}
        <button className="button primary full" type="submit" disabled={loading}>
          {loading ? 'Signing in...' : 'Open workspace'} <ArrowRight size={18} />
        </button>
        <p className="form-note"><Shield size={14} /> Synthetic demo data. Never use real confidential documents here.</p>
      </form>
    </div>
  </div>;
}

function FieldStatus({ status }: { status: string }) {
  return <span className="field-status"><CircleAlert size={13} />{status.replace('_', ' ')}</span>;
}

function App() {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem('nexus_demo_token'));
  const [user, setUser] = useState<User | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [section, setSection] = useState<Section>('assistant');
  const [docs, setDocs] = useState<Document[]>([]);
  const [searchTerm, setSearchTerm] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [asking, setAsking] = useState(false);
  const [retrievalMode, setRetrievalMode] = useState<RetrievalMode>('bm25');
  const [availableModes, setAvailableModes] = useState<RetrievalMode[]>(['bm25']);
  const [openDoc, setOpenDoc] = useState<DocumentDetail | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [audits, setAudits] = useState<AuditEvent[]>([]);
  const [evaluation, setEvaluation] = useState<EvaluationSummary | null>(null);
  const [evaluationError, setEvaluationError] = useState('');
  const [notice, setNotice] = useState('');
  const chatEnd = useRef<HTMLDivElement>(null);

  const logout = useCallback(() => {
    localStorage.removeItem('nexus_demo_token'); setToken(null); setUser(null);
    setDocs([]); setMessages([]); setOpenDoc(null); setAudits([]);
  }, []);

  useEffect(() => {
    if (!token) { setInitializing(false); return; }
    let alive = true;
    api.me(token).then(current => { if (alive) setUser(current); })
      .catch(() => { if (alive) logout(); })
      .finally(() => { if (alive) setInitializing(false); });
    return () => { alive = false; };
  }, [token, logout]);

  const loadDocuments = useCallback(async () => {
    if (!token) return;
    try { setDocs(await api.documents(token)); }
    catch (exception) { setNotice((exception as Error).message); }
  }, [token]);

  useEffect(() => { if (user) void loadDocuments(); }, [user, loadDocuments]);
  useEffect(() => {
    api.health().then(health => {
      const modes = health.retrieval_modes?.length ? health.retrieval_modes : ['bm25' as RetrievalMode];
      setAvailableModes(modes);
      setRetrievalMode(modes.includes('hybrid') ? 'hybrid' : 'bm25');
    }).catch(() => { /* keep BM25: works on every database */ });
  }, []);
  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);
  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(''), 5000); return () => window.clearTimeout(timer); }, [notice]);

  const persona = DEMO_USERS.find(p => p.email === user?.email);
  const filteredDocs = useMemo(() => docs.filter(doc =>
    `${doc.title} ${doc.filename} ${doc.groups.join(' ')}`.toLowerCase().includes(searchTerm.toLowerCase())),
    [docs, searchTerm]);
  const latest = [...messages].reverse().find(message => message.result?.sources.length)?.result;
  const totalFields = docs.reduce((sum, doc) => sum + Object.keys(doc.fields ?? {}).length, 0);

  async function ask(question: string, modeOverride?: RetrievalMode) {
    const normalized = question.trim();
    const mode = modeOverride && availableModes.includes(modeOverride) ? modeOverride : retrievalMode;
    if (modeOverride && mode === modeOverride) setRetrievalMode(modeOverride);
    if (!normalized || !token || asking) return;
    setSection('assistant'); setDraft(''); setAsking(true);
    const id = Date.now(); setMessages(prev => [...prev, { id, question: normalized }]);
    try {
      const result = await api.ask(token, normalized, mode);
      setMessages(prev => prev.map(item => item.id === id ? { ...item, result } : item));
    } catch (exception) {
      setMessages(prev => prev.map(item => item.id === id ? { ...item, error: (exception as Error).message } : item));
    } finally { setAsking(false); }
  }

  async function openDocument(id: string) {
    if (!token) return;
    try { setOpenDoc(await api.document(token, id)); }
    catch (exception) { setNotice((exception as Error).message); }
  }

  async function showAudits() {
    setSection('governance');
    if (user?.role === 'admin' && token) {
      try { setAudits(await api.audit(token)); } catch (exception) { setNotice((exception as Error).message); }
    }
  }

  async function showEvaluation() {
    setSection('evaluation');
    if (!token || evaluation) return;
    try { setEvaluation(await api.evaluation(token)); setEvaluationError(''); }
    catch (exception) { setEvaluationError((exception as Error).message); }
  }

  async function downloadDoc(doc: Document | DocumentDetail) {
    if (!token) return;
    try { await api.download(token, doc); }
    catch (exception) { setNotice((exception as Error).message); }
  }

  if (initializing) return <div className="loading-screen"><Logo /><div className="loading-bar" /></div>;
  if (!user || !token) return <Login onSuccess={(jwt, loginUser) => {
    localStorage.setItem('nexus_demo_token', jwt); setToken(jwt); setUser(loginUser);
  }} />;

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="sidebar-brand"><Logo /></div>
      <div className="workspace-switch"><div className="workspace-icon">N</div><div><strong>{user.tenant === 'northstar' ? 'Northstar Inc.' : 'Otherco Ltd.'}</strong><small>Enterprise workspace</small></div><ChevronDown size={16} /></div>
      <div className="nav-group-label">WORKSPACE</div>
      <nav className="nav-list" aria-label="Main navigation">
        <button onClick={() => setSection('assistant')} className={`nav-link ${section === 'assistant' ? 'active' : ''}`}><MessageSquareText size={18} /> AI Assistant <span className="nav-beta">AI</span></button>
        <button onClick={() => setSection('documents')} className={`nav-link ${section === 'documents' ? 'active' : ''}`}><BookOpen size={18} /> Document Library <span className="nav-count">{docs.length}</span></button>
        <button onClick={() => void showAudits()} className={`nav-link ${section === 'governance' ? 'active' : ''}`}><ShieldCheck size={18} /> Access & Audit</button>
        <button onClick={() => void showEvaluation()} className={`nav-link ${section === 'evaluation' ? 'active' : ''}`}><Activity size={18} /> Evaluation</button>
      </nav>
      <div className="nav-group-label integrations">PLATFORM</div>
      <div className="nav-static"><Database size={18} /> SQL document index <span className="status-dot" /></div>
      <div className="nav-static"><Activity size={18} /> FTS + pgvector · RRF <span className="status-dot" /></div>
      <div className="sidebar-bottom">
        <div className="demo-warning"><div><LockKeyhole size={16} /> LOCAL DEMO</div><p>Prototype identity, synthetic data and permission-aware retrieval. Not production SSO.</p></div>
        <div className="profile-mini"><span className={`avatar ${persona?.color ?? 'blue'}`}>{persona?.initials ?? 'U'}</span><div><strong>{persona?.title || 'Member'}</strong><small>{user.email}</small></div><button title="Switch demo user" onClick={logout}><LogOut size={17} /></button></div>
      </div>
    </aside>
    <main className="main-pane">
      <header className="topbar"><div className="breadcrumbs">Northstar / <strong>{section === 'assistant' ? 'AI Assistant' : section === 'documents' ? 'Document Library' : section === 'evaluation' ? 'Evaluation' : 'Access & Audit'}</strong></div>
        <div className="topbar-right"><span className="environment"><span className="small-pulse" />Demo environment</span><span className="topbar-separator" /><span className={`avatar tiny ${persona?.color ?? 'blue'}`}>{persona?.initials ?? 'U'}</span></div></header>
      {section === 'assistant' && <div className="assistant-layout">
        <div className="assistant-main">
          <div className="section-heading"><div><span className="eyebrow"><Sparkles size={14} /> AI WORKSPACE</span><h1>Ask your knowledge base<span>.</span></h1><p>Grounded answers from documents you have permission to access.</p></div><button className="button secondary small" onClick={() => setMessages([])}><Plus size={16} /> New chat</button></div>
          <div className="chat-area">
            {messages.length === 0 && <div className="empty-chat"><div className="floating-brand"><Logo compact /></div><div className="eyebrow">YOUR KNOWLEDGE, CONNECTED</div><h2>Good questions deserve<br />verifiable answers.</h2><p>Search contracts, policies, and business records. Every result is traceable to its source.</p><div className="prompts-grid">{QUICK_PROMPTS.map(prompt => <button key={prompt.label} title={`${RETRIEVAL_LABELS[prompt.mode]} · ${prompt.question}`} onClick={() => void ask(prompt.question, prompt.mode)}><FileSearch size={16} /><span><strong>{prompt.label}</strong><br />{prompt.question}</span><ArrowRight size={15} /></button>)}</div></div>}
            {messages.map(message => <div className="conversation" key={message.id}>
              <div className="message-user"><span className={`avatar tiny ${persona?.color ?? 'blue'}`}>{persona?.initials}</span><div>{message.question}</div></div>
              <div className="message-ai"><span className="bot-bubble"><Sparkles size={17} /></span><div className="answer-body">
                {!message.result && !message.error && <div className="thinking"><span /><span /><span /> Searching authorized documents...</div>}
                {message.error && <div className="inline-error"><CircleAlert size={17} />{message.error}</div>}
                {message.result && <><div className="answer-content">{message.result.answer}</div>
                  <div className="answer-metadata"><span className="mini-badge"><ShieldCheck size={13} /> Authorization filtered</span><span>{message.result.sources.length} sources</span><span>{RETRIEVAL_LABELS[message.result.retrieval] ?? message.result.retrieval}</span><span>{message.result.duration_ms} ms total</span>{message.result.diagnostics && <RetrievalTimings diagnostics={message.result.diagnostics} />}{message.result.diagnostics?.evidence?.applied && !message.result.diagnostics.evidence.sufficient && <span className="mini-badge" title="Evidence-sufficiency policy withheld the retrieved candidates">Insufficient evidence · {(message.result.diagnostics.evidence.reasons ?? []).join(', ').replace(/_/g, ' ')}</span>}<span>{message.result.mode === 'llm' ? 'LLM-generated' : 'Offline extractive'}</span></div>
                  {message.result.sources.length > 0 && <div className="inline-sources">{message.result.sources.slice(0, 3).map(source => <button key={source.chunk_id} onClick={() => void openDocument(source.document_id)}><FileText size={14} /><strong>[{source.id}]</strong> {source.title} <span>p.{source.page}</span><ChevronRight size={14} /></button>)}</div>}
                </>}
              </div></div></div>)}
            <div ref={chatEnd} />
          </div>
          <form className="chat-form" onSubmit={event => { event.preventDefault(); void ask(draft); }}><div className="chat-input-wrap"><Sparkles size={19} /><input aria-label="Ask a question" value={draft} onChange={event => setDraft(event.target.value)} placeholder="Ask anything about your documents..." /><button type="submit" disabled={!draft.trim() || asking} title="Send question"><Send size={19} /></button></div><div className="chat-hint"><span><LockKeyhole size={13} /> Results are filtered before retrieval</span><label className="mode-select">Retrieval <select aria-label="Retrieval mode" value={retrievalMode} onChange={event => setRetrievalMode(event.target.value as RetrievalMode)}>{(['bm25', 'lexical', 'semantic', 'hybrid'] as RetrievalMode[]).map(mode => <option key={mode} value={mode} disabled={!availableModes.includes(mode)}>{RETRIEVAL_LABELS[mode]}{availableModes.includes(mode) ? '' : ' (needs PostgreSQL)'}</option>)}</select></label><span>Evidence-first AI · v0.2-dev</span></div></form>
        </div>
        <aside className="evidence-pane"><div className="evidence-top"><div><span className="eyebrow">TRANSPARENCY LAYER</span><h3>Source inspector</h3></div><span className="evidence-live"><span className="status-dot" />LIVE</span></div>
          <p className="evidence-description">Review the exact excerpts used to support each answer.</p>
          {latest ? <div className="evidence-items">{latest.sources.map(source => <Evidence key={source.chunk_id} source={source} onOpen={() => void openDocument(source.document_id)} />)}{latest.sources.length === 0 && <div className="empty-evidence">No matching authorized evidence found.</div>}</div> : <div className="evidence-empty"><div className="evidence-illustration"><FileSearch size={35} /></div><strong>Sources appear here</strong><p>Ask a question to see matched documents, source pages and retrieval scores.</p></div>}
          <div className="evidence-footer"><ShieldCheck size={17} /><span>Permissions enforced in the retrieval query — not just the LLM prompt.</span></div>
        </aside>
      </div>}
      {section === 'documents' && <div className="content-view"><div className="page-heading"><div><div className="eyebrow">DOCUMENT INTELLIGENCE</div><h1>Document library<span>.</span></h1><p>Search, inspect, and manage your authorized business documents.</p></div><button className="button primary" onClick={() => setUploadOpen(true)}><Plus size={18} /> Upload document</button></div>
        <div className="stats-grid"><Stat icon={<BookOpen size={21} />} label="Accessible documents" value={String(docs.length)} detail="Tenant + group filtered" /><Stat icon={<FileText size={21} />} label="Indexed pages" value={String(docs.reduce((sum, d) => sum + d.pages, 0))} detail="Source-aware extraction" /><Stat icon={<Settings2 size={21} />} label="Extracted fields" value={String(totalFields)} detail="Review required" /><Stat icon={<ShieldCheck size={21} />} label="Access policy" value="Enforced" detail="Default deny for other groups" /></div>
        <section className="library-card"><div className="library-header"><div><h3>All accessible files</h3><p>Documents you can read under your current identity.</p></div><div className="search-control"><Search size={17} /><input value={searchTerm} onChange={event => setSearchTerm(event.target.value)} placeholder="Search file names or groups" aria-label="Search documents" /></div></div>
          <div className="doc-table-wrap"><table className="doc-table"><thead><tr><th>DOCUMENT</th><th>ACCESS GROUPS</th><th>FIELDS</th><th>PAGES</th><th /></tr></thead><tbody>{filteredDocs.map(doc => <tr key={doc.id} onClick={() => void openDocument(doc.id)}><td><div className="file-item"><span className="file-icon"><FileText size={20} /></span><div><strong>{doc.title}</strong><small>{doc.filename}</small></div></div></td><td>{doc.groups.map(group => <span className="group-pill" key={group}>{group}</span>)}</td><td><span className="numeric"><Settings2 size={14} /> {Object.keys(doc.fields ?? {}).length}</span></td><td>{doc.pages}</td><td><ChevronRight size={16} /></td></tr>)}</tbody></table>
            {filteredDocs.length === 0 && <div className="no-results"><Search size={25} /><strong>No documents found</strong><p>Try a different term, switch identity or upload a document.</p></div>}
          </div></section>
          <div className="library-footnote"><LockKeyhole size={16} /> You only see documents allowed for <strong>{user.email}</strong>. Switch identities to test isolation.</div>
      </div>}
      {section === 'governance' && <div className="content-view"><div className="page-heading"><div><div className="eyebrow">SECURITY & GOVERNANCE</div><h1>Trust by architecture<span>.</span></h1><p>Permission checks, tenant separation, and a traceable activity log.</p></div><span className="security-highlight"><ShieldCheck size={17} /> Query-time ACL</span></div>
        <div className="governance-grid"><div className="policy-card"><div className="policy-icon"><ShieldCheck size={24} /></div><h3>Permission-aware retrieval</h3><p>Document scope is filtered in SQL <strong>before any chunks are loaded</strong> for ranking or context generation.</p><div className="policy-tag"><Check size={15} /> Implemented</div></div><div className="policy-card"><div className="policy-icon"><Fingerprint size={24} /></div><h3>Tenant isolation</h3><p>All authorized document queries are scoped to the authenticated user’s tenant.</p><div className="policy-tag"><Check size={15} /> Implemented</div></div><div className="policy-card"><div className="policy-icon"><FileLock2 size={24} /></div><h3>Audit trail</h3><p>Uploads, document views, downloads, and searches generate audit events in the database.</p><div className="policy-tag"><Check size={15} /> Implemented</div></div></div>
        <div className="governance-lower"><div className="permissions-panel"><div className="eyebrow">CURRENT IDENTITY</div><h3>Effective permissions</h3><div className="permission-line"><span>User</span><strong>{user.email}</strong></div><div className="permission-line"><span>Tenant</span><strong>{user.tenant}</strong></div><div className="permission-line"><span>Role</span><strong>{user.role}</strong></div><div className="permission-line"><span>Groups</span><div>{user.groups.map(group => <span className="group-pill" key={group}>{group}</span>)}</div></div><div className="divider-soft" /><div className="permission-note"><CircleCheck size={17} /> Other tenants and groups are excluded by default.</div></div><div className="audit-panel"><div className="audit-heading"><div><div className="eyebrow">ACTIVITY</div><h3>Recent audit events</h3></div>{user.role === 'admin' && <button className="link-button" onClick={() => void showAudits()}>Refresh <ArrowRight size={15} /></button>}</div>{user.role !== 'admin' ? <div className="audit-locked"><LockKeyhole size={27} /><strong>Admin access required</strong><p>Switch to the workspace admin to inspect tenant-scoped audit events.</p></div> : audits.length === 0 ? <div className="audit-locked"><Clock3 size={27} /><strong>No events yet</strong><p>Open a file or ask a question, then refresh this panel.</p></div> : <div className="audit-list">{audits.map(event => <div key={event.id} className="audit-row"><span className="audit-dot" /><span><strong>{event.action.replace('_', ' ')}</strong><small>{event.user_id.slice(0, 8)}… {event.document_id ? ` · ${event.document_id.slice(0, 8)}…` : ''}</small></span><time>{new Date(event.created_at).toLocaleString()}</time></div>)}</div>}</div></div>
        <div className="roadmap-note"><CircleAlert size={18} /><span><strong>Roadmap, not implemented:</strong> labeled retrieval evaluation, relevance thresholds, reranking, production OIDC SSO, OCR for scanned PDFs, background workers, hardened storage, and external security review.</span></div>
      </div>}
      {section === 'evaluation' && <EvaluationView data={evaluation} error={evaluationError} />}
    </main>
    {notice && <div className="toast"><CircleAlert size={17} />{notice}<button title="Close" onClick={() => setNotice('')}><X size={17} /></button></div>}
    {openDoc && <div className="overlay" role="presentation" onClick={() => setOpenDoc(null)}><div className="detail-drawer" role="dialog" aria-modal="true" aria-label="Document details" onClick={event => event.stopPropagation()}><div className="drawer-header"><div className="eyebrow">DOCUMENT INSPECTOR</div><button title="Close document" onClick={() => setOpenDoc(null)}><X size={20} /></button></div><div className="drawer-content"><div className="document-hero"><div className="large-file-icon"><FileText size={27} /></div><h2>{openDoc.title}</h2><p>{openDoc.filename}</p><span className="mini-badge"><LockKeyhole size={13} /> Authorized view</span></div><div className="drawer-action"><button className="button secondary full" onClick={() => void downloadDoc(openDoc)}><ArrowDownToLine size={17} /> Download original</button></div><div className="drawer-section"><div className="drawer-section-heading"><h3>Extracted fields</h3><span className="small-counter">{Object.keys(openDoc.fields ?? {}).length}</span></div>{Object.entries(openDoc.fields ?? {}).length === 0 ? <p className="muted-small">No structured contract fields found by the regex baseline.</p> : Object.entries(openDoc.fields).map(([key, field]) => <div className="extracted-field" key={key}><span className="extract-key">{key.replaceAll('_', ' ')}</span><strong>{field.value}</strong><small>Page {field.page} · <FieldStatus status={field.status} /></small><p>{field.evidence}</p></div>)}</div><div className="drawer-section"><div className="drawer-section-heading"><h3>Source text</h3><span className="small-counter">{openDoc.page_count} p.</span></div>{openDoc.pages.map((page, index) => <div className="document-page" key={index}><span>PAGE {page.page}</span><p>{page.content}</p></div>)}</div></div></div></div>}
    {uploadOpen && <UploadModal user={user} token={token} onClose={() => setUploadOpen(false)} onSuccess={() => { setUploadOpen(false); setNotice('Document indexed successfully.'); void loadDocuments(); }} />}
  </div>;
}

function Evidence({ source, onOpen }: { source: Source; onOpen: () => void }) {
  return <button className="evidence-card" onClick={onOpen}><div className="source-head"><span className="source-number">{source.id}</span><span className="source-page">PAGE {source.page}</span></div><strong>{source.title}</strong>{source.selected_sentences?.length ? <p title="Sentences selected for this question (verbatim from the cited chunk)">{source.selected_sentences.map(sentence => `“${sentence}”`).join(' ')}</p> : <p>{source.excerpt.slice(0, 240)}{source.excerpt.length > 240 ? '…' : ''}</p>}<div className="source-bottom"><span title={scoreTitle(source)}><Database size={13} /> {scoreLabel(source)}</span><span>Inspect <ArrowRight size={13} /></span></div></button>;
}

function scoreLabel(source: Source): string {
  if (source.method === 'hybrid') {
    const ranks = [`KW ${source.lexical_rank ? `#${source.lexical_rank}` : '–'}`,
      `SEM ${source.semantic_rank ? `#${source.semantic_rank}` : '–'}`];
    return `${ranks.join(' · ')} · RRF ${(source.rrf_score ?? source.score).toFixed(4)}`;
  }
  if (source.method === 'semantic') return `SEM #${source.semantic_rank ?? '?'} · cosine ${source.score.toFixed(3)}`;
  if (source.method === 'fts') return `KW #${source.lexical_rank ?? '?'} · ts_rank ${source.score.toFixed(3)}`;
  return `BM25 ${source.score.toFixed(2)}`;
}

function scoreTitle(source: Source): string {
  if (source.method === 'hybrid') return 'Reciprocal Rank Fusion: sum of 1/(60 + rank) over the keyword and semantic branches. A rank statistic, not a confidence.';
  if (source.method === 'semantic') return 'Cosine similarity (1 - cosine distance); not a probability.';
  if (source.method === 'fts') return 'PostgreSQL ts_rank_cd; comparable only within this query.';
  return 'Python BM25 baseline score; comparable only within this query.';
}

function RetrievalTimings({ diagnostics }: { diagnostics: Answer['diagnostics'] }) {
  const t = diagnostics.retrieval_ms ?? {};
  if (diagnostics.mode !== 'hybrid') return <span>retrieval {t.total ?? '?'} ms</span>;
  const c = diagnostics.branch_candidates ?? {};
  return <span title={diagnostics.score_note}>keyword {c.lexical ?? 0} cand. / {t.lexical ?? '?'} ms · semantic {c.semantic ?? 0} cand. / {t.semantic ?? '?'} ms · fusion {t.fusion ?? '?'} ms</span>;
}

const pct = (v: number | null | undefined, d = 3) => (v === null || v === undefined ? '–' : v.toFixed(d));

function EvaluationView({ data, error }: { data: EvaluationSummary | null; error: string }) {
  if (error) return <div className="content-view"><div className="library-card"><p>{error}</p></div></div>;
  if (!data) return <div className="content-view"><div className="library-card"><p>Loading stored benchmark results…</p></div></div>;
  const modes = Object.keys(data.modes) as RetrievalMode[];
  const metrics = ['recall@1', 'recall@5', 'recall@10', 'precision@5', 'mrr@10', 'ndcg@5'];
  const categories = Object.keys(data.modes[modes[0]].by_category);
  const sec = data.security ?? {};
  return <div className="content-view">
    <div className="page-heading"><div><div className="eyebrow">RETRIEVAL EVALUATION</div><h1>Measured, not claimed<span>.</span></h1>
      <p>Stored results of <code>scripts/evaluate.py</code> from {new Date(data.generated_at).toLocaleString()} · {data.dataset.total} labeled questions ({data.dataset.dev} dev / {data.dataset.test} test) over {data.corpus.documents} synthetic documents ({data.corpus.chunks} chunks) · {data.model.name}.</p></div></div>
    <div className="stats-grid">
      <Stat icon={<ShieldCheck size={21} />} label="Authorization violations" value={String(sec.total_violations ?? '–')} detail="candidates, sources, citations, prompts" />
      <Stat icon={<FileSearch size={21} />} label="Answerable / unanswerable" value={`${data.dataset.answerable} / ${data.dataset.unanswerable}`} detail="incl. unauthorized-source questions" />
      <Stat icon={<Database size={21} />} label="Test cases (answerable)" value={String(data.modes.hybrid?.test.n ?? '–')} detail="metrics below: held-out test split" />
    </div>
    <section className="library-card"><div className="library-header"><div><h3>Retrieval comparison · held-out test split</h3><p>Macro-averaged over answerable questions; top_k = 10; latency = warm wall time per query.</p></div></div>
      <div className="doc-table-wrap"><table className="doc-table"><thead><tr><th>MODE</th>{metrics.map(m => <th key={m}>{m.toUpperCase()}</th>)}<th>P50 MS</th><th>P95 MS</th></tr></thead>
        <tbody>{modes.map(m => <tr key={m}><td><strong>{RETRIEVAL_LABELS[m]}</strong></td>{metrics.map(k => <td key={k}>{pct(data.modes[m].test[k])}</td>)}<td>{pct(data.modes[m].latency_ms.p50, 1)}</td><td>{pct(data.modes[m].latency_ms.p95, 1)}</td></tr>)}</tbody></table></div>
      {data.significance?.hybrid_minus && <p className="library-footnote">Hybrid − other modes, nDCG@5 (bootstrap 95 % CI, n = {data.significance.n_cases}): {Object.entries(data.significance.hybrid_minus['ndcg@5']).map(([m, d]) => `${RETRIEVAL_LABELS[m as RetrievalMode]} ${d.mean_diff >= 0 ? '+' : ''}${d.mean_diff.toFixed(3)} [${d.ci95[0].toFixed(3)}, ${d.ci95[1].toFixed(3)}]${d.ci_excludes_zero ? ' *' : ''}`).join(' · ')}. A difference only counts if its CI excludes 0.</p>}
    </section>
    <section className="library-card"><div className="library-header"><div><h3>nDCG@5 by question category (all answerable)</h3><p>Where each strategy is strong or weak.</p></div></div>
      <div className="doc-table-wrap"><table className="doc-table"><thead><tr><th>CATEGORY</th>{modes.map(m => <th key={m}>{RETRIEVAL_LABELS[m].toUpperCase()}</th>)}</tr></thead>
        <tbody>{categories.map(c => <tr key={c}><td>{c.replace(/_/g, ' ')}</td>{modes.map(m => <td key={m}>{pct(data.modes[m].by_category[c])}</td>)}</tr>)}</tbody></table></div></section>
    <section className="library-card"><div className="library-header"><div><h3>Evidence sufficiency (abstention)</h3><p>Policy chosen on the dev split, reported on test. Cosine thresholds are heuristics, not probabilities.</p></div></div>
      <div className="doc-table-wrap"><table className="doc-table"><thead><tr><th>MODE</th><th>POLICY</th><th>ANSWERABLE RECALL</th><th>UNANSWERABLE DETECTED</th><th>FALSE ABSTENTION</th><th>WITHOUT POLICY: UNANSWERABLE DETECTED</th></tr></thead>
        <tbody>{Object.entries(data.abstention).map(([m, a]) => <tr key={m}><td>{RETRIEVAL_LABELS[m as RetrievalMode]}</td><td>{a.selected.family} @ {a.selected.policy.min_semantic_similarity}</td><td>{pct(a.selected.test.answerable_recall)}</td><td>{pct(a.selected.test.unanswerable_detection)}</td><td>{pct(a.selected.test.false_abstention_rate)}</td><td>{pct(a.no_policy_test.unanswerable_detection)}</td></tr>)}</tbody></table></div></section>
    {data.ablation && <section className="library-card"><div className="library-header"><div><h3>Hybrid RRF ablation</h3><p>{data.ablation.grid_size} configurations, selected on dev nDCG@5, reported on test.</p></div></div>
      <div className="doc-table-wrap"><table className="doc-table"><thead><tr><th>CONFIG</th><th>CANDIDATES</th><th>K</th><th>WEIGHTS (KW / SEM)</th><th>TEST NDCG@5</th><th>TEST RECALL@5</th></tr></thead>
        <tbody>{(['default', 'selected'] as const).map(k => { const row = data.ablation![k]; return <tr key={k}><td>{k === 'default' ? 'default' : 'selected on dev'}</td><td>{row.config.candidates}</td><td>{row.config.rrf_k}</td><td>{row.config.weights.lexical} / {row.config.weights.semantic}</td><td>{pct(row.test['ndcg@5'])}</td><td>{pct(row.test['recall@5'])}</td></tr>; })}</tbody></table></div></section>}
    <div className="library-footnote"><LockKeyhole size={16} /> All numbers come from <code>eval/reports/latest.json</code> (dataset sha256 {data.dataset.sha256.slice(0, 12)}…). Nothing on this page is hardcoded.</div>
  </div>;
}

function Stat({ icon, label, value, detail }: { icon: React.ReactNode; label: string; value: string; detail: string }) {
  return <div className="stat"><div className="stat-upper"><span>{label}</span><div className="stat-icon">{icon}</div></div><strong>{value}</strong><small>{detail}</small></div>;
}

function UploadModal({ token, user, onClose, onSuccess }: { token: string; user: User; onClose: () => void; onSuccess: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [groups, setGroups] = useState<string[]>(user.groups.slice(0, 1));
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!file || !groups.length) return;
    setWorking(true); setError('');
    try { await api.upload(token, file, groups); onSuccess(); }
    catch (exception) { setError((exception as Error).message); }
    finally { setWorking(false); }
  }
  return <div className="overlay modal-overlay" role="presentation" onClick={onClose}><form className="upload-modal" role="dialog" aria-modal="true" aria-label="Upload document" onClick={event => event.stopPropagation()} onSubmit={submit}><div className="drawer-header"><div className="eyebrow">DOCUMENT INGESTION</div><button type="button" title="Close" onClick={onClose}><X size={20} /></button></div><h2>Upload a document</h2><p className="subtext">Extract, chunk, index, and share with allowed groups.</p><input type="file" accept=".txt,.md,.pdf,.docx" hidden ref={input} onChange={event => setFile(event.target.files?.[0] ?? null)} /><button type="button" className="drop-zone" onClick={() => input.current?.click()}><CloudUpload size={34} /><strong>{file?.name ?? 'Choose a document'}</strong><span>{file ? `${(file.size / 1024).toFixed(1)} KB selected` : 'PDF (text), DOCX, TXT or MD · max 5 MB'}</span></button><label className="field-label">Authorized groups</label><div className="access-group-list">{user.groups.map(group => <label key={group}><input type="checkbox" checked={groups.includes(group)} onChange={event => setGroups(previous => event.target.checked ? [...previous, group] : previous.filter(item => item !== group))} /><span>{group}</span></label>)}</div><p className="permission-helper"><Shield size={15} /> Only group members and the uploader will be able to retrieve it.</p>{error && <div className="form-error"><CircleAlert size={16} />{error}</div>}<button className="button primary full" type="submit" disabled={working || !file || groups.length === 0}>{working ? 'Processing document...' : 'Index document'} <ArrowRight size={17} /></button></form></div>;
}

export default App;
