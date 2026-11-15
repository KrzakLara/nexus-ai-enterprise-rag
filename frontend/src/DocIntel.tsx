import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft, Check, CircleAlert, FileSearch, GitCompare, History, Lightbulb, RefreshCw, ScanText, X,
} from 'lucide-react';
import type { User } from './api';
import {
  ApiError, di, STATUS_LABELS, type Block, type Comparison, type Evidence, type Extraction,
  type ExtractionField, type HistoryEvent, type Issue, type LibraryItem, type QueryType, type Structure,
} from './diApi';

const REVIEWER_ROLES = new Set(['admin', 'reviewer']);
const norm = (s: string) => s.replace(/\s+/g, ' ').trim().toLowerCase();
const today = () => new Date().toISOString().slice(0, 10);

function StatusBadge({ status }: { status: string | null }) {
  if (!status) return <span className="di-badge muted">–</span>;
  return <span className={`di-badge ${status}`}>{STATUS_LABELS[status] ?? status.replace(/_/g, ' ')}</span>;
}

export function DocIntelWorkspace({ user }: { user: User }) {
  const [tab, setTab] = useState<'library' | 'compare' | 'insights'>('library');
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const load = useCallback(async () => {
    setLoading(true);
    try { setItems(await di.library()); setError(''); } catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const open = items.find(i => i.id === openId);
  if (open) return <DocumentDetail user={user} item={open} onBack={() => { setOpenId(null); void load(); }} />;
  const toggle = (id: string) => setSelected(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id]);
  return <div className="content-view">
    <div className="page-heading"><div><div className="eyebrow">DOCUMENT INTELLIGENCE</div>
      <h1>Extraction & review<span>.</span></h1>
      <p>Layout-aware parsing (OCR for scans), structured contract and invoice fields with verbatim evidence,
        business validation and human review. Only documents you are authorized to read are listed.</p></div></div>
    <div className="di-tabs">
      <button className={tab === 'library' ? 'active' : ''} onClick={() => setTab('library')}><FileSearch size={15} /> Documents</button>
      <button className={tab === 'compare' ? 'active' : ''} onClick={() => setTab('compare')}><GitCompare size={15} /> Compare ({selected.length})</button>
      <button className={tab === 'insights' ? 'active' : ''} onClick={() => setTab('insights')}><Lightbulb size={15} /> Insights</button>
      <button className="di-refresh" onClick={() => void load()} title="Refresh"><RefreshCw size={15} /></button>
    </div>
    {error && <div className="form-error"><CircleAlert size={16} />{error}</div>}
    {tab === 'library' && <div className="doc-table-wrap"><table className="doc-table di-table">
      <thead><tr><th /><th>Document</th><th>Type</th><th>Format</th><th>Processing</th><th>Extraction</th><th>Review</th></tr></thead>
      <tbody>{items.map(i => <tr key={i.id}>
        <td><input type="checkbox" aria-label={`Select ${i.title}`} checked={selected.includes(i.id)} onChange={() => toggle(i.id)} disabled={!i.extraction_id} /></td>
        <td><button className="link-button" onClick={() => setOpenId(i.id)}>{i.title}</button><small className="di-sub">{i.filename} · {i.pages} page{i.pages === 1 ? '' : 's'}</small></td>
        <td>{i.doc_type ?? '–'}</td>
        <td>{i.format ?? '–'}{i.ocr_pages.length > 0 && <span className="di-chip" title={`OCR on page ${i.ocr_pages.join(', ')}`}><ScanText size={12} /> OCR</span>}</td>
        <td><StatusBadge status={i.processing_status} /></td>
        <td><StatusBadge status={i.extraction_status} /></td>
        <td><StatusBadge status={i.review_status} /></td>
      </tr>)}
      {items.length === 0 && <tr><td colSpan={7} className="di-empty">{loading ? 'Loading documents…' : 'No documents you can read.'}</td></tr>}</tbody></table></div>}
    {tab === 'compare' && <CompareView ids={selected} items={items} />}
    {tab === 'insights' && <InsightsView selected={selected} items={items} />}
    {!REVIEWER_ROLES.has(user.role) && <p className="di-note">You can inspect extractions. Correcting, approving and rejecting require the reviewer or admin role (enforced by the API).</p>}
  </div>;
}

// ------------------------------------------------------------------------------ document detail

function DocumentDetail({ user, item, onBack }: { user: User; item: LibraryItem; onBack: () => void }) {
  const [structure, setStructure] = useState<Structure | null>(null);
  const [extraction, setExtraction] = useState<Extraction | null>(null);
  const [history, setHistory] = useState<HistoryEvent[]>([]);
  const [focus, setFocus] = useState<Evidence | null>(null);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    try {
      const [s, e] = await Promise.all([di.structure(item.id), di.extraction(item.id)]);
      setStructure(s); setExtraction(e);
      setHistory((await di.history(e.id)).events); setError('');
    } catch (err) { setError((err as Error).message); }
  }, [item.id]);
  useEffect(() => { void load(); }, [load]);
  return <div className="content-view">
    <button className="button secondary small di-back" onClick={onBack}><ArrowLeft size={15} /> All documents</button>
    <div className="page-heading"><div><div className="eyebrow">{(extraction?.doc_type ?? 'document').toUpperCase()} · {structure?.parser}</div>
      <h1>{item.title}<span>.</span></h1></div></div>
    {error && <div className="form-error"><CircleAlert size={16} />{error}</div>}
    <div className="di-detail">
      <section className="di-structure">{structure && <StructureView structure={structure} focus={focus} />}</section>
      <section className="di-side">
        {extraction && <ExtractionPanel canReview={REVIEWER_ROLES.has(user.role)} extraction={extraction}
          onFocus={setFocus} onChange={async e => { setExtraction(e); setHistory((await di.history(e.id)).events); }} />}
        <div className="di-card"><h3><History size={16} /> Review history</h3>
          {history.length === 0 ? <p className="di-note">No events.</p> : <ol className="di-history">{history.map(h => <li key={h.id}>
            <strong>{h.action}</strong>{h.field && <> · <code>{h.field}</code></>} <small>v{h.version} · {h.user_id.slice(0, 8)} · {new Date(h.created_at).toLocaleString()}</small>
            {(h.old_value !== null || h.new_value !== null) && h.action === 'correct' && <div className="di-change"><s>{h.old_value ?? '∅'}</s> → {h.new_value ?? '∅'}</div>}
            {h.comment && <div className="di-comment">{h.comment}</div>}</li>)}</ol>}</div>
      </section>
    </div>
  </div>;
}

function StructureView({ structure, focus }: { structure: Structure; focus: Evidence | null }) {
  useEffect(() => {
    if (focus) document.getElementById(`di-page-${focus.page}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [focus]);
  const hit = (text: string, page: number) => !!focus && focus.page === page && norm(text).includes(norm(focus.text).slice(0, 60));
  return <div>
    {structure.warnings.length > 0 && <div className="di-warning">{structure.warnings.join(' · ')}</div>}
    {structure.pages.map(p => <div key={p.number} id={`di-page-${p.number}`} className={`di-page ${focus?.page === p.number ? 'focused' : ''}`}>
      <div className="di-page-head">Page {p.number}{p.label ? ` · ${p.label}` : ''} · <span className="di-chip">{p.method === 'ocr' ? 'OCR' : p.method.replace('_', ' ')}</span>
        {p.ocr && <small> Tesseract {String(p.ocr.version)} · mean word confidence {String(p.ocr.mean_confidence)} (engine score, not a probability) · deskew {String(p.ocr.deskew_degrees)}°</small>}</div>
      {p.blocks.length === 0 && <p className="di-note">No text on this page.</p>}
      {p.blocks.map(b => <BlockView key={b.id} block={b} hit={hit} focus={focus} />)}
      {focus?.page === p.number && focus.bbox && <div className="di-bbox">Evidence location: x {focus.bbox.x0.toFixed(0)}–{focus.bbox.x1.toFixed(0)} pt, y {focus.bbox.top.toFixed(0)}–{focus.bbox.bottom.toFixed(0)} pt (from the parser)</div>}
      {focus?.page === p.number && !focus.bbox && <div className="di-bbox">No coordinates for this format: evidence is located by page and verbatim text.</div>}
    </div>)}
  </div>;
}

function BlockView({ block, hit, focus }: { block: Block; hit: (t: string, p: number) => boolean; focus: Evidence | null }) {
  if (block.table) {
    const t = block.table;
    const rows: (typeof t.cells)[] = Array.from({ length: t.n_rows }, () => []);
    t.cells.forEach(c => rows[c.row].push(c));
    return <div className="di-table-wrap"><table className="di-doc-table"><tbody>{rows.map((cells, r) =>
      <tr key={r} className={focus?.table?.table_id === t.id && focus.table.row === r ? 'hit' : ''}>{cells.sort((a, b) => a.col - b.col).map(c =>
        <td key={c.col} rowSpan={c.row_span} colSpan={c.col_span} className={r === 0 ? 'head' : ''}>{c.text}</td>)}</tr>)}</tbody></table>
      <small className="di-sub">table {t.id} · {t.n_rows}×{t.n_cols} · {t.source.replace('_', ' ')}</small></div>;
  }
  if (block.kind === 'heading') return <div className={`di-heading l${block.level ?? 2} ${hit(block.text, block.page) ? 'hit' : ''}`}>{block.text}</div>;
  const lines = block.lines.length ? block.lines.map(l => l.text) : [block.text];
  return <p className="di-para">{lines.map((l, i) => <span key={i} className={hit(l, block.page) ? 'hit' : ''}>{l}<br /></span>)}</p>;
}

function issueText(i: Issue) {
  return `${i.message}${i.expected ? ` (expected ${i.expected}${i.actual ? `, found ${i.actual}` : ''})` : ''}`;
}

function ExtractionPanel({ extraction, canReview, onFocus, onChange }: {
  extraction: Extraction; canReview: boolean; onFocus: (e: Evidence | null) => void; onChange: (e: Extraction) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [comment, setComment] = useState('');
  const [override, setOverride] = useState('');
  const [blocked, setBlocked] = useState<Issue[] | null>(null);
  const [message, setMessage] = useState('');
  const final = extraction.status === 'approved' || extraction.status === 'rejected';
  const editable = canReview && !final;
  const scalar = extraction.fields.filter(f => !f.name.startsWith('line_items['));
  const items = useMemo(() => {
    const rows: Record<number, Record<string, ExtractionField>> = {};
    extraction.fields.forEach(f => {
      const m = f.name.match(/^line_items\[(\d+)\]\.(\w+)$/);
      if (m) (rows[Number(m[1])] ??= {})[m[2]] = f;
    });
    return Object.entries(rows).sort((a, b) => Number(a[0]) - Number(b[0]));
  }, [extraction.fields]);
  const run = async (fn: () => Promise<Extraction>, done: string) => {
    try { const e = await fn(); onChange(e); setDraft({}); setBlocked(null); setMessage(done); }
    catch (err) {
      const e = err as ApiError;
      const detail = e.detail as { errors?: Issue[] } | undefined;
      if (e.status === 409 && detail?.errors) setBlocked(detail.errors);
      setMessage(e.message);
    }
  };
  const save = () => run(() => di.correct(extraction.id, extraction.version,
    Object.entries(draft).map(([name, value]) => ({ name, value: value.trim() === '' ? null : value }))), 'Corrections saved.');
  const field = (f: ExtractionField) => <tr key={f.name} className={`di-field ${f.state}`}>
    <td><code>{f.name}</code><small className="di-sub">{f.method}</small></td>
    <td>{editable ? <input className="text-input di-input" aria-label={f.name} value={draft[f.name] ?? f.value ?? ''}
      onChange={e => setDraft(d => ({ ...d, [f.name]: e.target.value }))} /> : <span>{f.value ?? <em>missing</em>}</span>}
      {f.corrected && <small className="di-sub">corrected · extracted: {f.extracted_value ?? '∅'}</small>}
      {f.issues.map((i, k) => <div key={k} className={`di-issue ${i.severity}`}>{issueText(i)}</div>)}</td>
    <td><span className={`di-chip ${f.state}`}>{f.state.replace('_', ' ')}</span><small className="di-sub">{f.grounding}</small></td>
    <td>{f.evidence ? <button className="link-button" onClick={() => onFocus(f.evidence)} title={f.evidence.text}>p. {f.evidence.page}</button> : '–'}</td>
  </tr>;
  return <div className="di-card">
    <div className="di-card-head"><h3>Extracted fields</h3><StatusBadge status={extraction.status} /></div>
    <small className="di-sub">{extraction.provider} · version {extraction.version} · {extraction.note}</small>
    {extraction.issues.map((i, k) => <div key={k} className={`di-issue ${i.severity}`}>{i.field}: {issueText(i)}</div>)}
    <table className="di-fields"><thead><tr><th>Field</th><th>Value</th><th>State</th><th>Evidence</th></tr></thead>
      <tbody>{scalar.map(field)}</tbody></table>
    {items.length > 0 && <><h4>Line items</h4><table className="di-fields"><tbody>
      {items.map(([idx, cols]) => Object.values(cols).map(field).concat(<tr key={`sep-${idx}`} className="di-sep"><td colSpan={4} /></tr>))}
    </tbody></table></>}
    {editable && <div className="di-actions">
      <button className="button secondary small" disabled={Object.keys(draft).length === 0} onClick={() => void save()}>Save corrections</button>
      <input className="text-input" placeholder="Comment (required to reject)" value={comment} onChange={e => setComment(e.target.value)} />
      <button className="button primary small" onClick={() => void run(() => di.approve(extraction.id, extraction.version, comment || undefined,
        blocked ? override : undefined), 'Approved.')}><Check size={15} /> Approve</button>
      <button className="button secondary small" disabled={comment.trim().length < 3}
        onClick={() => void run(() => di.reject(extraction.id, extraction.version, comment), 'Rejected.')}><X size={15} /> Reject</button>
    </div>}
    {blocked && <div className="di-blocked"><strong>Approval blocked by {blocked.length} validation error(s).</strong>
      <ul>{blocked.map((i, k) => <li key={k}>{i.field}: {issueText(i)}</li>)}</ul>
      <input className="text-input" placeholder="Override reason (≥ 10 characters, recorded in the history)" value={override} onChange={e => setOverride(e.target.value)} /></div>}
    {message && <p className="di-note">{message}</p>}
    {final && <p className="di-note">This extraction is {extraction.status}{extraction.review_comment ? `: “${extraction.review_comment}”` : ''}. It is final; re-processing requires an explicit force.</p>}
  </div>;
}

// ------------------------------------------------------------------------------ compare / insights

function CompareView({ ids, items }: { ids: string[]; items: LibraryItem[] }) {
  const [result, setResult] = useState<Comparison | null>(null);
  const [error, setError] = useState('');
  const go = async () => {
    try { setResult(await di.compare(ids)); setError(''); } catch (e) { setError((e as Error).message); setResult(null); }
  };
  const title = (id: string) => items.find(i => i.id === id)?.title ?? id;
  return <div className="di-card">
    <p className="di-note">Select 2–10 processed documents in the Documents tab. Values are structured, evidence-backed fields; approved documents use reviewer-corrected values and rejected ones are excluded.</p>
    <button className="button primary small" disabled={ids.length < 2} onClick={() => void go()}><GitCompare size={15} /> Compare {ids.length} documents</button>
    {error && <div className="form-error"><CircleAlert size={16} />{error}</div>}
    {result && <div className="di-table-wrap"><table className="di-doc-table di-compare"><thead><tr><th>Field</th>
      {result.documents.map(d => <th key={d.document_id}>{d.title}<small className="di-sub">{d.doc_type} · {STATUS_LABELS[d.review_status] ?? d.review_status}</small></th>)}<th>Analysis</th></tr></thead>
      <tbody>{result.fields.map(f => <tr key={f.field}><td><code>{f.field}</code></td>
        {f.documents.map(d => <td key={d.document_id}>{d.values.length === 0 ? <em>missing</em> : d.values.map((v, k) =>
          <div key={k} title={v.evidence ? `p. ${v.evidence.page}: ${v.evidence.text}` : ''}>{v.value}{v.evidence && <small className="di-sub">p. {v.evidence.page}</small>}</div>)}</td>)}
        <td>{f.analysis.all_equal ? 'same' : f.analysis.differs ? 'differs' : '–'}
          {f.analysis.lowest && <small className="di-sub">lowest: {title(f.analysis.lowest.document_id)}</small>}
          {f.analysis.missing_in.length > 0 && <small className="di-sub">missing in {f.analysis.missing_in.length}</small>}</td></tr>)}</tbody></table>
      {result.excluded_rejected.length > 0 && <p className="di-note">Excluded (rejected): {result.excluded_rejected.map(d => d.title).join(', ')}</p>}</div>}
  </div>;
}

function InsightsView({ selected, items }: { selected: string[]; items: LibraryItem[] }) {
  const [asOf, setAsOf] = useState(today());
  const [days, setDays] = useState(90);
  const [question, setQuestion] = useState('Which supplier contracts expire within 90 days?');
  const [result, setResult] = useState<{ title: string; data: Record<string, unknown> } | null>(null);
  const [error, setError] = useState('');
  const title = (id: string) => items.find(i => i.id === id)?.title ?? 'document';
  const run = async (label: string, fn: () => Promise<Record<string, unknown>>) => {
    try { setResult({ title: label, data: await fn() }); setError(''); } catch (e) { setError((e as Error).message); setResult(null); }
  };
  const query = (type: QueryType, label: string) => run(label, () => di.query(type, days, asOf));
  const data = (result?.data.result ?? result?.data) as Record<string, unknown> | undefined;
  const matches = (data?.matches ?? []) as Record<string, unknown>[];
  return <div className="di-card">
    <div className="di-insight-controls">
      <label>As of <input type="date" className="text-input" value={asOf} onChange={e => setAsOf(e.target.value)} /></label>
      <label>Days <input type="number" min={1} max={3650} className="text-input" value={days} onChange={e => setDays(Number(e.target.value))} /></label>
      <button className="button secondary small" onClick={() => void query('expiring_within', `Contracts expiring within ${days} days`)}>Expiring contracts</button>
      <button className="button secondary small" onClick={() => void query('auto_renewal', 'Automatic renewal clauses')}>Auto-renewal</button>
      <button className="button secondary small" onClick={() => void query('invalid_invoices', 'Invoices failing validation')}>Invalid invoices</button>
      <button className="button secondary small" onClick={() => void query('conflicts', 'Conflicting information')}>Conflicts</button>
    </div>
    <form className="di-ask" onSubmit={e => { e.preventDefault(); void run(question, () => di.ask(question, asOf, selected)); }}>
      <input className="text-input" value={question} onChange={e => setQuestion(e.target.value)} aria-label="Question about your documents" />
      <button className="button primary small" type="submit">Ask</button>
    </form>
    <p className="di-note">Questions are mapped to deterministic queries over extracted fields (English and Croatian). Unsupported questions are refused rather than guessed.</p>
    {error && <div className="form-error"><CircleAlert size={16} />{error}</div>}
    {result && <div className="di-results"><h4>{result.title}{typeof result.data.interpreted_as === 'string' && <small className="di-sub"> interpreted as {result.data.interpreted_as}</small>}</h4>
      {matches.length === 0 && <p className="di-note">No matching documents.</p>}
      <ul>{matches.map((m, k) => <li key={k}>
        {'document_id' in m && <strong>{String(m.title ?? title(String(m.document_id)))}</strong>}
        {'kind' in m && <strong>{String(m.kind).replace('_', ' ')}: {String(m.key)} · {String(m.field)}</strong>}
        {'expiration_date' in m && <> · expires {String(m.expiration_date)} ({String(m.days_left)} days)</>}
        {'evidence' in m && m.evidence !== null && typeof m.evidence === 'object' && <small className="di-sub">p. {String((m.evidence as Evidence).page)}: “{(m.evidence as Evidence).text}”</small>}
        {Array.isArray(m.issues) && (m.issues as Issue[]).map((i, j) => <div key={j} className="di-issue error">{i.field}: {issueText(i)}</div>)}
        {Array.isArray(m.values) && <div className="di-sub">{(m.values as { title: string; value: string }[]).map(v => `${v.title}: ${v.value}`).join(' · ')}</div>}
      </li>)}</ul>
      {Array.isArray(data?.without_expiration_date) && (data.without_expiration_date as unknown[]).length > 0 &&
        <p className="di-note">{(data.without_expiration_date as unknown[]).length} contract(s) have no extracted expiration date and are not counted.</p>}
    </div>}
  </div>;
}
