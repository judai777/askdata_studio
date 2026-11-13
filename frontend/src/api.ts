import type { AuthUser, LoginResponse, QueryResult, SavedMemory, SchemaField, SchemaTable, WorkspaceConfig } from "./types"

const API_BASE = import.meta.env.VITE_API_BASE ?? ""
const TOKEN_KEY = "askdata_access_token"

function token() {
  return sessionStorage.getItem(TOKEN_KEY)
}

async function request<T>(path: string, options?: RequestInit, timeoutMs = 90_000): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
        ...options?.headers,
      },
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请检查模型接口配置后重试")
    }
    throw new Error("无法连接后端服务，请先启动 FastAPI（127.0.0.1:8000）")
  } finally {
    window.clearTimeout(timeout)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? "服务暂时不可用")
  }
  return response.json() as Promise<T>
}

export const api = {
  hasSession: () => Boolean(token()),
  login: async (username: string, password: string) => {
    const result = await request<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    })
    sessionStorage.setItem(TOKEN_KEY, result.access_token)
    return result.user
  },
  me: () => request<AuthUser>("/api/auth/me"),
  logout: async () => {
    try {
      if (token()) await request<{ logged_out: boolean }>("/api/auth/logout", { method: "POST" })
    } finally {
      sessionStorage.removeItem(TOKEN_KEY)
    }
  },
  clearSession: () => sessionStorage.removeItem(TOKEN_KEY),
  schema: () => request<SchemaTable[]>("/api/schema"),
  query: (query: string, workspace: WorkspaceConfig, sessionId: string) =>
    request<QueryResult>("/api/query", {
      method: "POST",
      body: JSON.stringify({ query, session_id: sessionId, workspace }),
    }),
  clarify: (taskId: string, optionId: string) =>
    request<QueryResult>(`/api/tasks/${taskId}/clarify`, {
      method: "POST",
      body: JSON.stringify({ option_id: optionId }),
    }),
  save: (taskId: string) =>
    request<{ saved: boolean }>("/api/memories", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId }),
    }),
  memories: () => request<SavedMemory[]>("/api/memories"),
  saveField: (tableId: string, field: SchemaField) =>
    request<{ saved: boolean }>("/api/memories/fields", {
      method: "POST",
      body: JSON.stringify({
        table_id: tableId,
        name: field.name,
        label: field.label,
        field_type: field.type,
      }),
    }),
  deleteMemory: (memoryId: string) =>
    request<{ deleted: boolean }>(`/api/memories/${encodeURIComponent(memoryId)}`, {
      method: "DELETE",
    }),
  evalCatalog: () => request<{ version: string; tasks: EvalTask[] }>("/api/eval/catalog"),
  evalCampaigns: () => request<EvalCampaign[]>("/api/eval/campaigns"),
  evalStartCampaign: (payload: { campaign_id: string; task_ids: string[]; mode: "demo" | "live"; trials: 1 | 3; fixture_id: EvalFixtureId; request_limit?: number; model?: string; allow_remote: boolean; retry_interrupted: boolean }) => request<EvalCampaign>("/api/eval/campaigns", { method: "POST", body: JSON.stringify(payload) }),
  evalCampaignDetail: (id: string) => request<EvalCampaign>(`/api/eval/campaigns/${encodeURIComponent(id)}`),
  evalCampaignBaselines: () => request<EvalCampaignBaseline[]>("/api/eval/campaign-baselines"),
  evalApproveCampaignBaseline: (campaignId: string, fingerprint: string, note: string) => request<EvalCampaignBaseline>("/api/eval/campaign-baselines", { method: "POST", body: JSON.stringify({ campaign_id: campaignId, fingerprint, note }) }),
  evalCampaignGate: (baseline: string, candidate: string) => request<EvalCampaignGate>(`/api/eval/campaign-gate?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}`),
  evalBenchmarkReadiness: () => request<EvalBenchmarkReadiness>("/api/eval/benchmark-readiness"),
  evalModels: () => request<EvalModelCatalog>("/api/eval/models"),
  evalReviews: () => request<EvalReviewCatalog>("/api/eval/reviews"),
  evalReviewPreview: (taskId: string) => request<EvalReviewPreview>(`/api/eval/reviews/${encodeURIComponent(taskId)}/preview`),
  evalSubmitReview: (taskId: string, decision: "approved" | "rejected", note: string, fingerprint: string) => request<{ task_id: string; status: string }>(`/api/eval/reviews/${encodeURIComponent(taskId)}`, { method: "POST", body: JSON.stringify({ decision, note, fingerprint }) }),
  evalAudit: () => request<EvalAudit>("/api/eval/audit"),
  evalCheckCandidateSql: (taskId: string, sql: string, fixtureId: EvalFixtureId) => request<EvalCandidateSqlCheck>("/api/eval/candidate-sql/check", { method: "POST", body: JSON.stringify({ task_id: taskId, sql, fixture_id: fixtureId }) }),
  evalCheckCandidateBatch: (candidates: { task_id: string; sql: string }[], fixtureId: EvalFixtureId) => request<EvalCandidateSqlBatch>("/api/eval/candidate-sql/batch", { method: "POST", body: JSON.stringify({ candidates, fixture_id: fixtureId }) }),
  evalCalibration: () => request<EvalCalibration>("/api/eval/calibration"),
  evalRuns: () => request<EvalRunSummary[]>("/api/eval/runs"),
  evalRun: (mode: "demo" | "live", taskIds: string[], trials: 1 | 3, fixtureId: EvalFixtureId = "base", requestLimit?: number, model?: string) => request<EvalRun>("/api/eval/runs", { method: "POST", body: JSON.stringify({ mode, task_ids: taskIds, trials, fixture_id: fixtureId, request_limit: requestLimit, model }) }, 600_000),
  evalStart: (mode: "demo" | "live", taskIds: string[], trials: 1 | 3, fixtureId: EvalFixtureId = "base", requestLimit?: number, model?: string) => request<{ id: string; status: string }>("/api/eval/runs/start", { method: "POST", body: JSON.stringify({ mode, task_ids: taskIds, trials, fixture_id: fixtureId, request_limit: requestLimit, model }) }),
  evalStartCandidate: (baselineId: string, model: string) => request<{ id: string; status: string; baseline_run_id: string }>(`/api/eval/runs/${encodeURIComponent(baselineId)}/candidate`, { method: "POST", body: JSON.stringify({ model }) }),
  evalDetail: (id: string) => request<EvalRun>(`/api/eval/runs/${id}`),
  evalLabels: (id: string) => request<EvalRunLabels>(`/api/eval/runs/${id}/labels`),
  evalSubmitLabel: (id: string, taskId: string, trial: number, fingerprint: string, labels: EvalHumanDimensionLabels, note: string) => request<{ status: string }>(`/api/eval/runs/${id}/labels/${encodeURIComponent(taskId)}/${trial}`, { method: "POST", body: JSON.stringify({ fingerprint, labels, note }) }),
  evalExport: (id: string) => request<Record<string, unknown>>(`/api/eval/runs/${id}/export`),
  evalCancel: (id: string) => request<{ id: string; status: string }>(`/api/eval/runs/${id}/cancel`, { method: "POST" }),
  evalReplay: (id: string) => request<EvalRun>(`/api/eval/runs/${id}/replay`, { method: "POST" }),
  evalCompare: (baseline: string, candidate: string) => request<EvalComparison>(`/api/eval/compare?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}`),
}

export interface EvalTask { id: string; category: string; query: string; route: string; database?: string; kind?: string; required_tables?: string[] }
export interface EvalCampaign { id: string; created_at: string; status: string; plan_hash: string; mode: "demo" | "live"; trials: number; task_ids: string[]; request_limit: number | null; model: string | null; evaluation_users?: Record<string, string>; fixture_id: string; task_version: string; summary: { total_tasks: number; completed_tasks: number; passed: number; completed_shards: number; total_shards: number; remote_requests_used: number; remote_requests_uncertain: boolean; schema_fields?: EvalSchemaSummary; sql_outcome?: EvalSqlSummary; model_metrics?: EvalMetrics | null } | null; shards: { task_ids: string[]; status: string; run_ids: string[] }[] }
export interface EvalCampaignBaseline { campaign_id: string; plan_hash: string; approved_at: string; reviewer: string; note: string; task_count: number; passed: number; passed_task_ids: string[] }
export interface EvalCampaignGate { baseline_id: string; candidate_id: string; passed: boolean; task_count: number; baseline_passed: number; candidate_passed: number; regressed: string[]; improved: string[] }
export interface EvalCandidateSqlCheck { task_id: string; fixture_id: EvalFixtureId; fixture_hash: string; execution_success: boolean; execution_error: string | null; result_correct: boolean; grade: { passed: boolean; checks: Record<string, boolean>; dimensions: Record<string, boolean> }; candidate_row_count: number; expected_row_count: number; candidate_preview: Record<string, unknown>[]; expected_preview: Record<string, unknown>[] }
export interface EvalCandidateSqlBatch { task_version: string; fixture_id: EvalFixtureId; fixture_hash: string; submission_hash: string; summary: { submitted: number; executed: number; result_correct: number; passed: number }; cases: { task_id: string; execution_success: boolean; execution_error: string | null; result_correct: boolean; passed: boolean; checks: Record<string, boolean>; candidate_row_count: number; expected_row_count: number }[] }
export interface EvalBenchmarkReadiness { task_version: string; existing: { total: number; approved: number; sql_tasks: number; with_field_constraints: number }; target: { database: string; development: number; development_goal: number; holdout: number; holdout_goal: number; total_goal: number } }
export interface EvalReviewSpec { intent: string; result: string; process: string; pitfall: string }
export interface EvalReviewRecord { decision: "approved" | "rejected"; note: string; reviewer: string; reviewed_at: string; fingerprint: string }
export interface EvalReviewItem { task_id: string; query: string; category: string; spec: EvalReviewSpec; fingerprint: string; status: "approved" | "rejected" | "pending" | "stale"; latest: EvalReviewRecord | null }
export interface EvalReviewCatalog { version: string; task_version: string; counts: { approved: number; rejected: number; pending: number; stale: number }; tasks: EvalReviewItem[] }
export interface EvalReviewPreview extends EvalReviewItem { task: EvalTask; oracle_sql: string | null; expected_rows: Record<string, Record<string, unknown>[]>; history: EvalReviewRecord[] }
export type EvalFixtureId = "base" | "edge_v1" | "edge_v2" | "edge_v3"
export interface EvalProvenance { kind: "demo" | "live"; prompt_hash: string | null; workflow_hash: string | null; config: Record<string, string | number | boolean> | null }
export interface EvalAudit { task_version: string; mutant_version: string; mutant_hash: string; oracle_hash: string; fixture_hashes: Record<string, string>; counts: Record<string, { killed: number; survived: number; invalid: number }>; cases: { id: string; task_id: string; fault: string; sql: string; suggestion?: string | null; status: "killed" | "survived" | "invalid"; fixtures: Record<string, { status: "killed" | "survived" | "invalid"; error?: string }> }[] }
export interface EvalMetrics { elapsed_ms: number; model_calls: number; failed_calls: number; usage_calls: number; successful_calls: number; total_tokens: number | null }
export interface EvalSchemaFields { target_count: number; retrieved_count: number; matched_count: number; recall: number; precision: number; missing?: string[]; extra?: string[] }
export interface EvalSchemaSummary { labeled_tasks: number; unlabeled_tasks: number; macro_recall: number | null; macro_precision: number | null }
export interface EvalSqlOutcome { executed: boolean; result_correct: boolean }
export interface EvalSqlSummary { attempts: number; executed: number; result_correct: number }
export interface EvalSqlLatency { sql_attempts: number; end_to_end: { count: number; mean_ms: number | null; p95_ms: number | null }; database_tool: { count: number; mean_ms: number | null; p95_ms: number | null } }
export interface EvalStage { stage: string; tool?: string | null; elapsed_ms: number | null; success: boolean; failure_type: string | null }
export interface EvalStageMetric { stage: string; calls: number; failures: number; elapsed_ms: number | null }
export interface EvalFailureAttribution { kind: "stage" | "grader" | "runner"; stage: string | null; failed_checks: string[]; trial?: number }
export interface EvalFailureCount { kind: EvalFailureAttribution["kind"]; stage: string | null; count: number }
export interface EvalRequestBudget { limit: number; used: number; exhausted: boolean }
export interface EvalModelCatalog { default: string; models: string[]; api_key_configured: boolean }
export type EvalHumanLabel = "pass" | "fail" | "unsure"
export interface EvalHumanDimensionLabels { completion: EvalHumanLabel; safety: EvalHumanLabel; process: EvalHumanLabel; report_quality: EvalHumanLabel }
export interface EvalHumanLabelRecord { fingerprint: string; labels: EvalHumanDimensionLabels; note: string; reviewer: string; reviewed_at: string }
export interface EvalRunLabels { run_id: string; mode: string; items: { task_id: string; trial: number; fingerprint: string; status: "unlabeled" | "valid" | "stale"; latest: EvalHumanLabelRecord | null; history: EvalHumanLabelRecord[] }[] }
export interface EvalCalibrationCounts { tp: number; fp: number; fn: number; tn: number; unsure: number; decided: number; agreement: number | null }
export interface EvalCalibration { live_attempts: number; labeled_attempts: number; stale_labels: number; demo_practice: number; groups: { grader_hash: string; attempts: number; dimensions: Record<"completion" | "safety" | "process", EvalCalibrationCounts>; report_quality: Record<EvalHumanLabel, number> }[] }
export interface EvalModelCall { kind: string; model: string; stage?: string | null; elapsed_ms: number; attempts: number; success: boolean; usage: Record<string, number> | null }
export interface EvalAttempt { trial: number; result: Record<string, unknown>; grade: { passed: boolean; checks: Record<string, boolean>; dimensions: Record<string, boolean> }; error: string | null; metrics?: EvalMetrics; schema_fields?: EvalSchemaFields | null; sql_outcome?: EvalSqlOutcome | null; model_calls?: EvalModelCall[]; stages?: EvalStage[]; failure_attribution?: EvalFailureAttribution | null }
export interface EvalCase { task_id: string; category: string; query: string; kind: string; attempts: EvalAttempt[]; incomplete?: boolean; metrics?: EvalMetrics | null; stage_metrics?: EvalStageMetric[] | null; failure_attribution?: EvalFailureAttribution | null; pass3: boolean; result: Record<string, unknown>; grade: EvalAttempt["grade"]; error: string | null }
export interface EvalRunSummary { id: string; mode: string; model: string; fixture_id?: string; started_at: string; status?: string; grader_hash?: string; baseline_run_id?: string; request_budget?: EvalRequestBudget | null; progress?: { completed_attempts: number; total_attempts: number; current_task: string | null; current_trial?: number }; summary: { total: number; passed: number; completion: number; safety: number; process: number; pass3: number | null; metrics?: EvalMetrics | null; schema_fields?: EvalSchemaSummary | null; sql_outcome?: EvalSqlSummary | null; sql_latency?: EvalSqlLatency | null; stage_metrics?: EvalStageMetric[] | null; failure_summary?: EvalFailureCount[] | null } }
export interface EvalRun extends EvalRunSummary { task_version: string; fixture_hash: string; task_ids?: string[]; provenance?: EvalProvenance; review?: { total: number; approved: number; rejected: number; pending: number; stale: number }; finished_at: string; trials: number; model_task_count?: number; cases: EvalCase[]; replayed_at?: string; replay_grader_hash?: string; error?: string }
export interface EvalComparison { baseline: { id: string; model: string }; candidate: { id: string; model: string }; mode: string; trials: number; grader_comparison?: { status: "same" | "unknown"; baseline_hash: string | null; candidate_hash: string | null }; provenance?: { status: "same" | "changed" | "unknown"; baseline: EvalProvenance | null; candidate: EvalProvenance | null; changes: { field: string; before: string | number | boolean | null; after: string | number | boolean | null }[] }; budget_comparison?: { status: "same" | "changed" | "unknown"; baseline_limit: number | null; candidate_limit: number | null }; counts: { common: number; regressed: number; improved: number; still_passed: number; still_failed: number }; only_baseline: string[]; only_candidate: string[]; cases: { task_id: string; query: string; kind: string; state: string; before_passed: boolean; after_passed: boolean; check_changes: { name: string; before: boolean | null; after: boolean | null }[]; cost_change?: { elapsed_ms_delta: number | null; model_calls_delta: number | null; total_tokens_delta: number | null }; stage_changes?: { stage: string; elapsed_ms_delta: number | null; calls_delta: number; failures_delta: number }[]; attribution_change?: { before: EvalFailureAttribution | null; after: EvalFailureAttribution | null } | null }[] }
