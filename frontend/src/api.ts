export const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

export type User = { email: string; tenant: string; role: string; groups: string[] };
export type ExtractedField = { value: string; page: number; evidence: string; method: string; status: string };
export type Document = {
  id: string; title: string; filename: string; pages: number; groups: string[];
  created_at: string; fields: Record<string, ExtractedField>;
};
export type DocumentDetail = {
  id: string; title: string; filename: string; page_count: number;
  fields: Record<string, ExtractedField>; pages: { page: number; content: string }[];
};
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
export type UploadResult = { id: string; title: string; pages: number; fields_extracted: number; embedding_status: string };
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
export type Health = { database: string; retrieval_modes: RetrievalMode[] };
export type AuditEvent = { id: number; action: string; user_id: string; document_id: string | null; created_at: string };

async function request<T>(path: string, token?: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init?.headers },
    });
  } catch {
    throw new Error(`Cannot connect to API at ${API_BASE}. Start the backend first.`);
  }
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { const body = await response.json(); message = body.detail ?? message; } catch { /* ignore */ }
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
  }
  return response.json() as Promise<T>;
}

export const api = {
  login: (email: string, password: string) => request<{ access_token: string; user: User }>(
    '/api/auth/login', undefined, { method: 'POST', body: JSON.stringify({ email, password }) }),
  health: () => request<Health>('/api/health'),
  me: (token: string) => request<User>('/api/me', token),
  documents: (token: string) => request<Document[]>('/api/documents', token),
  document: (token: string, id: string) => request<DocumentDetail>(`/api/documents/${id}`, token),
  ask: (token: string, question: string, mode: RetrievalMode) => request<Answer>('/api/ask', token,
    { method: 'POST', body: JSON.stringify({ question, top_k: 5, mode }) }),
  audit: (token: string) => request<AuditEvent[]>('/api/audit', token),
  evaluation: (token: string) => request<EvaluationSummary>('/api/evaluation/summary', token),
  upload: (token: string, file: File, groups: string[]) => {
    const body = new FormData(); body.append('file', file); body.append('groups', groups.join(','));
    return request<UploadResult>('/api/documents', token, { method: 'POST', body });
  },
  download: async (token: string, doc: Document | DocumentDetail) => {
    const response = await fetch(`${API_BASE}/api/documents/${doc.id}/source`,
      { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error(`Download blocked (${response.status})`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = doc.filename;
    link.click(); URL.revokeObjectURL(url);
  },
};
