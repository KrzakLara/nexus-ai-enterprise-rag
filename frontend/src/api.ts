/**
 * API client. Phase 4: the browser holds ONLY an HttpOnly session cookie (set by the backend);
 * no access / refresh token is ever stored in JavaScript or localStorage. Unsafe requests carry the
 * per-session CSRF token (kept in memory, restored from /api/auth/session after a reload).
 *
 * VITE_API_BASE_URL empty (default in Docker): same-origin requests, /api is proxied by Vite.
 */
export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '';

export type User = { email: string; tenant: string; role: string; groups: string[]; display_name?: string | null };
export type ExtractedField = { value: string; page: number; evidence: string; method: string; status: string };
export type Processing = {
  job_id: string; type: string; status: JobStatus; stage: string | null; attempts: number; max_attempts: number;
  error_code: string | null; error_message: string | null; updated_at: string | null; retry_available: boolean;
} | null;
export type LifecycleState = 'uploaded' | 'processing' | 'ready' | 'failed' | 'deleted' | 'purged';
export type Lifecycle = {
  lifecycle_state: LifecycleState; current_version: number; size_bytes: number | null; content_type: string | null;
  storage_backend: string; retention_until: string | null; updated_at: string | null; processing: Processing;
};
export type Intelligence = { processing_status: string; extraction_status: string | null; review_status: string | null; doc_type: string | null };
export type Document = Lifecycle & {
  id: string; title: string; filename: string; pages: number; groups: string[];
  created_at: string; fields: Record<string, ExtractedField>; intelligence?: Intelligence;
};
export type DocumentDetail = Lifecycle & {
  id: string; title: string; filename: string; page_count: number; owner_is_me: boolean;
  fields: Record<string, ExtractedField>; pages: { page: number; content: string }[];
};
export type JobStatus = 'queued' | 'running' | 'retrying' | 'completed' | 'failed' | 'cancelled';
export type Job = {
  id: string; type: string; status: JobStatus; stage: string | null; document_id: string | null;
  document_version: number | null; attempts: number; max_attempts: number; error_code: string | null;
  error_message: string | null; cancel_requested: boolean; result: Record<string, unknown> | null;
  created_at: string | null; updated_at: string | null; started_at: string | null; finished_at: string | null;
  run_after: string | null; retry_available: boolean;
};
export type Version = { version: number; state: string; filename: string; sha256: string; size_bytes: number | null;
  content_type: string | null; created_by: string; created_at: string };
export type Source = {
  id: string; document_id: string; chunk_id: string; title: string;
  page: number; excerpt: string; method: 'bm25' | 'fts' | 'semantic' | 'hybrid'; score: number;
  bm25_score: number | null; fts_rank_cd: number | null; cosine_distance: number | null;
  lexical_rank: number | null; semantic_rank: number | null; rrf_score: number | null;
  /** Query-selected sentences of this chunk, verbatim (quoted with [S#] in offline mode). */
  selected_sentences?: string[];
};
/** bm25 = Python baseline · lexical = PostgreSQL FTS · semantic = pgvector · hybrid = FTS + semantic + RRF */
export type RetrievalMode = 'bm25' | 'lexical' | 'semantic' | 'hybrid';
export const RETRIEVAL_LABELS: Record<RetrievalMode, string> = {
  bm25: 'BM25 (baseline)', lexical: 'Keyword · PostgreSQL FTS',
  semantic: 'Semantic · pgvector', hybrid: 'Hybrid · RRF',
};
/** Answer `mode` values returned by /api/ask (backend/app/rag.py). */
export const ANSWER_MODE_LABELS: Record<string, string> = {
  llm: 'LLM-generated · citations verified',
  extractive: 'Offline extractive · verbatim quotes',
  no_evidence: 'No sufficient evidence · abstained',
  provider_error: 'LLM unavailable · verbatim fallback',
  citation_validation_failed: 'LLM citations rejected · verbatim fallback',
};
/** Real pipeline stages reported by the worker (no percentages are estimated). */
export const STAGE_LABELS: Record<string, string> = {
  queued: 'Queued', downloading: 'Fetching original', parsing: 'Parsing / OCR', indexing: 'Chunking & extracting',
  extracting: 'Extracting', embedding: 'Embedding', purging_index: 'Removing index', purging_objects: 'Removing files',
  completed: 'Completed', cancelled: 'Cancelled',
};
export type UploadResult = {
  id: string; title: string; pages?: number; fields_extracted?: number; embedding_status?: string;
  lifecycle_state: LifecycleState; job: Job | null; duplicate_of?: string | null; deduplicated?: boolean;
};
export type Diagnostics = {
  mode: RetrievalMode; source_count: number; retrieval_ms: Record<string, number>;
  branch_candidates: Record<string, number>; score_note: string;
  parameters: { candidates?: number; rrf_k?: number; weights?: Record<string, number> };
  evidence?: { applied: boolean; sufficient?: boolean; reasons?: string[];
    signals?: { semantic_top_similarity: number | null; sentence_top_similarity: number | null;
      term_coverage: number | null; identifiers: string[]; identifiers_found: string[];
      lexical_candidates?: number; branch_agreement?: boolean; candidate_count?: number };
    withheld_candidates?: number };
};
export type MetricSet = Record<string, number>;
export type AbstentionMetrics = {
  n_answerable: number; n_unanswerable: number; answerable_recall: number; unanswerable_detection: number;
  false_abstention_rate: number | null; false_answerability_rate: number | null; balanced_accuracy: number;
};
export type EvaluationSummary = {
  generated_at: string;
  dataset: { total: number; dev: number; test: number; answerable: number; unanswerable: number; sha256: string };
  corpus: { documents: number; chunks: number; tenants: string[] };
  model: { name: string; revision: string };
  modes: Record<RetrievalMode, { test: MetricSet; all: MetricSet; latency_ms: { p50: number; p95: number };
    by_category: Record<string, number> }>;
  significance: { n_cases: number; hybrid_minus?: Record<string, Record<string, { mean_diff: number; ci95: [number, number]; ci_excludes_zero: boolean }>> } | null;
  ablation: { grid_size: number; 'test_delta_ndcg@5': number;
    default: { config: { candidates: number; rrf_k: number; weights: Record<string, number> }; test: MetricSet };
    selected: { config: { candidates: number; rrf_k: number; weights: Record<string, number> }; test: MetricSet } } | null;
  abstention: Record<string, { selected: { family: string; policy: { min_semantic_similarity: number };
    dev: AbstentionMetrics; test: AbstentionMetrics }; no_policy_test: AbstentionMetrics }>;
  security: Record<string, number | boolean> | null;
};
export type Answer = {
  answer: string; mode: string; retrieval: RetrievalMode; duration_ms: number;
  diagnostics: Diagnostics; sources: Source[];
};
export type Health = { database: string; retrieval_modes: RetrievalMode[]; auth_mode?: string; processing_mode?: string };
export type AuditEvent = { id: number; action: string; user_id: string; document_id: string | null; created_at: string };
export type AuthConfig = { auth_mode: 'demo' | 'oidc'; login_url: string | null; password_login: boolean; csrf_header: string };
export type SessionInfo = { authenticated: boolean; auth_mode: string; method: string; user: User;
  csrf_token?: string; expires_at?: string; idle_expires_at?: string };

/** Session problems the UI handles globally (sign in again). */
export const SESSION_CODES = new Set(['session_expired', 'session_revoked', 'session_invalid', 'not_authenticated',
  'revalidation_unavailable', 'session_mode_mismatch']);

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === 'string' ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`);
  }
  get code(): string | undefined { return (this.detail as { code?: string } | null)?.code; }
  get isSessionProblem(): boolean { return this.status === 401 && (!this.code || SESSION_CODES.has(this.code)); }
}

let csrfToken: string | null = null;
let onSessionLost: ((error: ApiError) => void) | null = null;
export const auth = {
  setCsrf(token: string | null) { csrfToken = token; },
  onSessionLost(handler: ((error: ApiError) => void) | null) { onSessionLost = handler; },
};

export async function request<T>(path: string, init: RequestInit = {}, opts: { silent401?: boolean } = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers: Record<string, string> = { ...(init.body instanceof FormData || !init.body ? {} : { 'Content-Type': 'application/json' }),
    ...(init.headers as Record<string, string> | undefined) };
  if (!['GET', 'HEAD'].includes(method) && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: 'include' });
  } catch {
    throw new ApiError(0, `Cannot connect to the API${API_BASE ? ` at ${API_BASE}` : ''}. Start the backend first.`);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new ApiError(response.status, (body as { detail?: unknown }).detail ?? `HTTP ${response.status}`);
    if (error.isSessionProblem && !opts.silent401) onSessionLost?.(error);
    throw error;
  }
  return body as T;
}

const json = (method: string, body?: unknown): RequestInit => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  authConfig: () => request<AuthConfig>('/api/auth/config'),
  session: () => request<SessionInfo>('/api/auth/session', {}, { silent401: true }),
  login: (email: string, password: string) => request<{ csrf_token: string; user: User; expires_at: string }>(
    '/api/auth/login', json('POST', { email, password }), { silent401: true }),
  logout: () => request<{ logged_out: boolean; logout_url: string | null }>('/api/auth/logout', json('POST')),
  oidcLoginUrl: (returnTo: string) => `${API_BASE}/api/auth/oidc/login?return_to=${encodeURIComponent(returnTo)}`,
  health: () => request<Health>('/api/health'),
  documents: () => request<Document[]>('/api/documents'),
  document: (id: string) => request<DocumentDetail>(`/api/documents/${id}`),
  ask: (question: string, mode: RetrievalMode) => request<Answer>('/api/ask', json('POST', { question, top_k: 5, mode })),
  audit: () => request<AuditEvent[]>('/api/audit'),
  evaluation: () => request<EvaluationSummary>('/api/evaluation/summary'),
  upload: (file: File, groups: string[]) => {
    const body = new FormData(); body.append('file', file); body.append('groups', groups.join(','));
    return request<UploadResult>('/api/documents', { method: 'POST', body });
  },
  uploadVersion: (id: string, file: File) => {
    const body = new FormData(); body.append('file', file);
    return request<{ document_id: string; version: number; job: Job }>(`/api/documents/${id}/versions`, { method: 'POST', body });
  },
  versions: (id: string) => request<{ current_version: number; versions: Version[] }>(`/api/documents/${id}/versions`),
  deleteDocument: (id: string) => request<{ deleted: boolean; purge_job: Job }>(`/api/documents/${id}`, { method: 'DELETE' }),
  reindex: (id: string) => request<{ job: Job; deduplicated: boolean }>(`/api/documents/${id}/reindex`, json('POST', { force: false })),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  documentJobs: (id: string) => request<Job[]>(`/api/documents/${id}/jobs`),
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, json('POST')),
  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, json('POST')),
  download: async (path: string, filename: string) => {
    const response = await fetch(`${API_BASE}${path}`, { credentials: 'include' });
    if (!response.ok) throw new ApiError(response.status, `Download blocked (${response.status})`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = filename;
    link.click(); URL.revokeObjectURL(url);
  },
};

export const ACTIVE_JOB = new Set<JobStatus>(['queued', 'running', 'retrying']);
export const isInFlight = (d: Lifecycle) => d.lifecycle_state === 'uploaded' || d.lifecycle_state === 'processing'
  || (!!d.processing && ACTIVE_JOB.has(d.processing.status));
