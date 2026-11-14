import { API_BASE } from './api';

/** Phase 3 document-intelligence API (backend/app/docintel_api.py). */
export type BBox = { x0: number; top: number; x1: number; bottom: number };
export type Issue = { field: string; code: string; severity: 'error' | 'warning'; message: string;
  expected?: string | null; actual?: string | null };
export type Evidence = { document_id: string; page: number; text: string; block_id?: string | null;
  bbox?: BBox | null; table?: { table_id: string; row: number; col: number | null } | null };
export type FieldState = 'ok' | 'missing' | 'needs_review' | 'invalid';
export type ExtractionField = {
  name: string; value_type: string; value: string | null; extracted_value: string | null; raw: string | null;
  method: string; evidence: Evidence | null; extracted_evidence: Evidence | null; grounding: string;
  issues: Issue[]; state: FieldState; corrected: boolean; corrected_by: string | null; corrected_at: string | null;
};
export type ExtractionStatus = 'pending' | 'needs_review' | 'approved' | 'rejected';
export type Extraction = {
  id: string; document_id: string; doc_type: 'contract' | 'invoice' | 'unknown'; provider: string;
  status: ExtractionStatus; version: number; issues: Issue[]; fields: ExtractionField[];
  reviewed_by: string | null; reviewed_at: string | null; review_comment: string | null; note: string;
  can_review?: boolean;
};
export type LibraryItem = {
  id: string; title: string; filename: string; pages: number; created_at: string;
  processing_status: string; format: string | null; ocr_pages: number[]; doc_type: string | null;
  extraction_id: string | null; extraction_status: ExtractionStatus | null; review_status: string | null;
};
export type Cell = { row: number; col: number; text: string; row_span: number; col_span: number; bbox: BBox | null };
export type Table = { id: string; page: number; n_rows: number; n_cols: number; cells: Cell[]; source: string; bbox: BBox | null };
export type Line = { text: string; bbox: BBox | null; ocr_confidence: number | null };
export type Block = { id: string; kind: 'heading' | 'paragraph' | 'table'; page: number; text: string;
  lines: Line[]; level: number | null; section: string | null; bbox: BBox | null; table: Table | null };
export type Page = { number: number; method: string; width: number | null; height: number | null;
  label: string | null; blocks: Block[]; ocr: Record<string, unknown> | null };
export type Structure = { document_id: string; title: string; format: string; parser: string; pages: Page[]; warnings: string[] };
export type HistoryEvent = { id: number; action: string; user_id: string; field: string | null;
  old_value: string | null; new_value: string | null; comment: string | null; version: number; created_at: string };
export type CompareValue = { field: string; value: string; value_type: string; grounding: string; state: string;
  evidence: { page: number; text: string } | null };
export type Comparison = {
  documents: { document_id: string; title: string; doc_type: string; review_status: string }[];
  excluded_rejected: { document_id: string; title: string }[];
  fields: { field: string; documents: { document_id: string; title: string; values: CompareValue[] }[];
    analysis: { missing_in: string[]; all_equal: boolean; differs: boolean;
      lowest?: { document_id: string; value: string }; highest?: { document_id: string; value: string } } }[];
  note: string;
};
export type QueryType = 'expiring_within' | 'auto_renewal' | 'invalid_invoices' | 'conflicts';

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === 'string' ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`);
  }
}

async function call<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } });
  } catch {
    throw new ApiError(0, `Cannot connect to API at ${API_BASE}.`);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, (body as { detail?: unknown }).detail ?? `HTTP ${response.status}`);
  return body as T;
}

const post = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) });

export const di = {
  library: (t: string) => call<LibraryItem[]>(t, '/api/intelligence/documents'),
  structure: (t: string, id: string) => call<Structure>(t, `/api/documents/${id}/structure`),
  extraction: (t: string, id: string) => call<Extraction>(t, `/api/documents/${id}/extraction`),
  process: (t: string, id: string, body: { force?: boolean; doc_type?: string }) =>
    call<Extraction>(t, `/api/documents/${id}/process`, post(body)),
  history: (t: string, eid: string) => call<{ events: HistoryEvent[] }>(t, `/api/extractions/${eid}/history`),
  correct: (t: string, eid: string, expected_version: number,
            corrections: { name: string; value: string | null; comment?: string }[]) =>
    call<Extraction>(t, `/api/extractions/${eid}/fields`, { method: 'PATCH', body: JSON.stringify({ expected_version, corrections }) }),
  approve: (t: string, eid: string, expected_version: number, comment?: string, override_reason?: string) =>
    call<Extraction>(t, `/api/extractions/${eid}/approve`, post({ expected_version, comment, override_reason })),
  reject: (t: string, eid: string, expected_version: number, comment: string) =>
    call<Extraction>(t, `/api/extractions/${eid}/reject`, post({ expected_version, comment })),
  compare: (t: string, document_ids: string[]) => call<Comparison>(t, '/api/intelligence/compare', post({ document_ids })),
  query: (t: string, type: QueryType, days: number, as_of: string) =>
    call<Record<string, unknown>>(t, '/api/intelligence/query', post({ type, days, as_of })),
  ask: (t: string, question: string, as_of: string, document_ids: string[]) =>
    call<{ interpreted_as: string; parameters: Record<string, unknown>; result: Record<string, unknown>; method: string }>(
      t, '/api/intelligence/ask', post({ question, as_of, document_ids: document_ids.length ? document_ids : null })),
};

export const STATUS_LABELS: Record<string, string> = {
  pending: 'Awaiting approval', needs_review: 'Needs review', approved: 'Approved', rejected: 'Rejected',
  awaiting_review: 'Awaiting review', processed: 'Processed', parsed: 'Parsed', failed: 'Failed',
  not_processed: 'Not processed',
};
