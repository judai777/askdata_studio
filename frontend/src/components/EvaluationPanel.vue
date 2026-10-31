<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { api } from "../api"
import type { EvalAudit, EvalComparison, EvalFailureAttribution, EvalFixtureId, EvalModelCatalog, EvalReviewCatalog, EvalReviewPreview, EvalRun, EvalRunSummary, EvalTask } from "../api"

const tasks = ref<EvalTask[]>([])
const selected = ref<string[]>([])
const runs = ref<EvalRunSummary[]>([])
const current = ref<EvalRun | null>(null)
const busy = ref(false)
const cancelBusy = ref(false)
const exportBusy = ref(false)
const error = ref("")
const mode = ref<"demo" | "live">("demo")
const trials = ref<1 | 3>(1)
const requestLimit = ref<20 | 50 | 100>(50)
const modelCatalog = ref<EvalModelCatalog | null>(null)
const selectedModel = ref("")
const fixtureId = ref<EvalFixtureId>("base")
const auditFixtures: { id: EvalFixtureId; label: string }[] = [{ id: "base", label: "原始" }, { id: "edge_v1", label: "边界 v1" }, { id: "edge_v2", label: "边界 v2" }]
const expanded = ref("")
const baselineId = ref("")
const candidateId = ref("")
const comparison = ref<EvalComparison | null>(null)
const compareError = ref("")
const audit = ref<EvalAudit | null>(null)
const auditBusy = ref(false)
const auditError = ref("")
const reviews = ref<EvalReviewCatalog | null>(null)
const reviewPreview = ref<EvalReviewPreview | null>(null)
const reviewNote = ref("")
const reviewBusy = ref(false)
const reviewError = ref("")
const auditCases = computed(() => [...(audit.value?.cases || [])].sort((a, b) => ({ survived: 0, invalid: 1, killed: 2 })[a.status] - ({ survived: 0, invalid: 1, killed: 2 })[b.status]))
let timer: number | null = null
let polling = false

onUnmounted(() => { if (timer !== null) window.clearInterval(timer) })

async function pollRun() {
  if (!current.value || polling) return
  polling = true
  try {
    current.value = await api.evalDetail(current.value.id)
    if (!['queued', 'running', 'cancel_requested'].includes(current.value.status || 'completed')) {
      if (timer !== null) window.clearInterval(timer)
      timer = null
      runs.value = await api.evalRuns()
      if (current.value.status === "completed" && current.value.baseline_run_id) {
        baselineId.value = current.value.baseline_run_id
        candidateId.value = current.value.id
        await compareRuns()
      }
    }
  } catch (caught) { error.value = String(caught) }
  finally { polling = false }
}

function watchRun() {
  if (timer !== null) window.clearInterval(timer)
  if (current.value && ['queued', 'running', 'cancel_requested'].includes(current.value.status || '')) timer = window.setInterval(() => { void pollRun() }, 1500)
}

onMounted(async () => {
  try {
    const [catalog, history, reviewCatalog, models] = await Promise.all([api.evalCatalog(), api.evalRuns(), api.evalReviews(), api.evalModels()])
    tasks.value = catalog.tasks
    selected.value = catalog.tasks.slice(0, 5).map(task => task.id)
    runs.value = history
    reviews.value = reviewCatalog
    modelCatalog.value = models
    selectedModel.value = models.default
    const completed = history.filter(run => !run.status || run.status === "completed")
    baselineId.value = completed[1]?.id || ""
    candidateId.value = completed[0]?.id || ""
  } catch (caught) { error.value = String(caught) }
})

function toggle(id: string) {
  selected.value = selected.value.includes(id) ? selected.value.filter(x => x !== id) : [...selected.value, id]
}

async function start() {
  error.value = ""
  if (!selected.value.length || selected.value.length > 5 || (mode.value === "live" && trials.value === 3 && selected.value.length > 3)) { error.value = "请选择 1–5 题；三次实时试验最多 3 题"; return }
  if (mode.value === "live" && !window.confirm(`Chat 模型：${selectedModel.value}。将调用远程 Chat、Embedding 和 Rerank API，最多执行 ${selected.value.length * trials.value} 次任务试验、发送 ${requestLimit.value} 次 HTTP 请求（重试也计入）。继续吗？`)) return
  busy.value = true
  try {
    const started = await api.evalStart(mode.value, selected.value, trials.value, fixtureId.value, mode.value === "live" ? requestLimit.value : undefined, mode.value === "live" ? selectedModel.value : undefined)
    current.value = await api.evalDetail(started.id)
    watchRun()
    runs.value = await api.evalRuns()
  } catch (caught) { error.value = String(caught) }
  finally { busy.value = false }
}

async function openRun(id: string) {
  try {
    current.value = await api.evalDetail(id)
    error.value = ""
    if (current.value.mode === "live") mode.value = "live"
    if (current.value.baseline_run_id) {
      baselineId.value = current.value.baseline_run_id
      candidateId.value = current.value.id
      if (current.value.status === "completed") await compareRuns()
    }
    watchRun()
  }
  catch (caught) { error.value = String(caught) }
}

async function startCandidate() {
  const baseline = current.value
  if (!baseline || !baseline.task_ids?.length || !baseline.request_budget || !selectedModel.value) return
  const attempts = baseline.task_ids.length * baseline.trials
  if (!window.confirm(`以运行 ${baseline.id} 为基线（${baseline.model}），使用候选 Chat 模型 ${selectedModel.value}。固定 ${baseline.task_ids.length} 题、${attempts} 次任务试验、${baseline.fixture_id || 'base'} 数据和 ${baseline.request_budget.limit} 次远程 HTTP 请求上限（含重试）；可能产生 API 费用。继续吗？`)) return
  busy.value = true
  error.value = ""
  try {
    const started = await api.evalStartCandidate(baseline.id, selectedModel.value)
    baselineId.value = baseline.id
    candidateId.value = started.id
    comparison.value = null
    current.value = await api.evalDetail(started.id)
    watchRun()
    runs.value = await api.evalRuns()
  } catch (caught) { error.value = String(caught) }
  finally { busy.value = false }
}

async function replay() {
  if (!current.value) return
  busy.value = true
  try { current.value = await api.evalReplay(current.value.id); error.value = "" }
  catch (caught) { error.value = String(caught) }
  finally { busy.value = false }
}

async function exportRun() {
  if (!current.value) return
  exportBusy.value = true
  error.value = ""
  try {
    const report = await api.evalExport(current.value.id)
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }))
    const link = document.createElement("a")
    link.href = url
    link.download = `askdata-eval-${current.value.id}.json`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (caught) { error.value = String(caught) }
  finally { exportBusy.value = false }
}

async function cancelRun() {
  if (!current.value) return
  cancelBusy.value = true
  error.value = ""
  try {
    await api.evalCancel(current.value.id)
    current.value = await api.evalDetail(current.value.id)
    watchRun()
  } catch (caught) { error.value = String(caught) }
  finally { cancelBusy.value = false }
}

async function compareRuns() {
  compareError.value = ""
  comparison.value = null
  if (!baselineId.value || !candidateId.value) { compareError.value = "请选择两次运行"; return }
  try { comparison.value = await api.evalCompare(baselineId.value, candidateId.value) }
  catch (caught) { compareError.value = String(caught) }
}

async function runAudit() {
  auditBusy.value = true
  auditError.value = ""
  try { audit.value = await api.evalAudit() }
  catch (caught) { auditError.value = String(caught) }
  finally { auditBusy.value = false }
}

async function openReview(taskId: string) {
  reviewBusy.value = true
  reviewError.value = ""
  try { reviewPreview.value = await api.evalReviewPreview(taskId); reviewNote.value = "" }
  catch (caught) { reviewError.value = String(caught) }
  finally { reviewBusy.value = false }
}

async function submitReview(decision: "approved" | "rejected") {
  if (!reviewPreview.value) return
  if (!reviewNote.value.trim()) { reviewError.value = "请先填写人工审核意见"; return }
  reviewBusy.value = true
  reviewError.value = ""
  try {
    const taskId = reviewPreview.value.task_id
    await api.evalSubmitReview(taskId, decision, reviewNote.value, reviewPreview.value.fingerprint)
    const [catalog, preview] = await Promise.all([api.evalReviews(), api.evalReviewPreview(taskId)])
    reviews.value = catalog
    reviewPreview.value = preview
    reviewNote.value = ""
  } catch (caught) { reviewError.value = String(caught) }
  finally { reviewBusy.value = false }
}

function reviewLabel(status: string) {
  return ({ approved: "已通过", rejected: "已退回", pending: "待审核", stale: "审核已过期" } as Record<string, string>)[status] || status
}

function taskReviewStatus(taskId: string) { return reviews.value?.tasks.find(item => item.task_id === taskId)?.status || "pending" }

function auditLabel(status: string) {
  return ({ killed: "识别", survived: "漏检", invalid: "无效" } as Record<string, string>)[status] || status
}

function comparisonLabel(state: string) {
  return ({ regressed: "退化", improved: "改善", still_passed: "保持通过", still_failed: "保持失败" } as Record<string, string>)[state] || state
}

function formatDuration(value: number) { return `${(value / 1000).toFixed(2)} 秒` }
function stageLabel(stage: string) {
  return ({ preprocess: "请求预处理", retrieve_schema: "Schema 检索", prepare_single_database: "单库 Agent", mcp_tool_call: "MCP 工具", execute_single_database: "SQL 执行与整理", run_multi_database: "多库执行", answer_qa: "数据问答", respond_directly: "直接回复" } as Record<string, string>)[stage] || stage
}
function stageDuration(value: number | null) { return value === null ? "未知" : `${value.toFixed(2)} 毫秒` }
function signedDuration(value: number | null) { return value === null ? "未知" : `${value > 0 ? "+" : ""}${value.toFixed(2)} 毫秒` }
function failureLabel(value: EvalFailureAttribution | null) {
  if (!value) return "无失败归因"
  if (value.kind === "stage") return `阶段异常：${stageLabel(value.stage || "未知")}`
  if (value.kind === "runner") return "评测执行异常，未记录到工作流阶段"
  return `判分未通过${value.failed_checks.length ? `：${value.failed_checks.join("、")}` : ""}；运行阶段无明确异常`
}
</script>

<template>
  <div class="eval-page">
    <header class="eval-heading"><div><small>ASKDATA EVALUATION</small><h1>Agent 评测台</h1><p>选任务、运行、判分、查看轨迹和重放。</p></div><span>管理员</span></header>
    <div class="eval-grid">
      <section class="eval-card">
        <h2>1 · 任务集</h2>
        <p>固定数据集上的可复现问题，单次最多 5 题。</p>
        <label v-for="task in tasks" :key="task.id" class="eval-task">
          <input type="checkbox" :checked="selected.includes(task.id)" @change="toggle(task.id)">
          <span><strong>{{ task.query }}</strong><small>{{ task.category }} · {{ task.id }} · {{ reviewLabel(taskReviewStatus(task.id)) }}</small></span>
        </label>
        <h2>2 · 运行方式</h2>
        <label class="eval-option"><input v-model="mode" type="radio" value="demo"> 离线演示 · 无需模型</label>
        <label class="eval-option"><input v-model="mode" type="radio" value="live"> 实时评测 · 调用远程 API</label>
        <label v-if="mode === 'live'" class="eval-option">Chat 模型 <select v-model="selectedModel"><option v-for="model in modelCatalog?.models || []" :key="model" :value="model">{{ model }}{{ model === modelCatalog?.default ? '（默认）' : '' }}</option></select></label>
        <label class="eval-option">独立试验 <select v-model.number="trials"><option :value="1">1 次</option><option :value="3">3 次（Pass³）</option></select></label>
        <label v-if="mode === 'live'" class="eval-option">远程请求上限 <select v-model.number="requestLimit"><option :value="20">20 次</option><option :value="50">50 次</option><option :value="100">100 次</option></select></label>
        <label class="eval-option">数据集 <select v-model="fixtureId"><option value="base">原始数据</option><option value="edge_v1">边界数据 v1</option><option value="edge_v2">边界数据 v2</option></select></label>
        <p v-if="fixtureId !== 'base'" class="eval-notice">边界数据在临时目录加入折扣、非已支付和日期端点记录；v2 再加入不同订单的同额实付记录。原始 CSV 不会改变。</p>
        <p v-if="mode === 'demo'" class="eval-notice">演示结果由固定 oracle 生成，用于体验流程，不代表模型准确率。</p>
        <p v-else class="eval-notice">模型列表由服务端登记；Embedding、Rerank 和其他配置沿用当前服务。顺序执行，可能产生 API 费用。请求上限包含重试，不是费用上限；权限护栏题不调用模型。{{ modelCatalog?.api_key_configured ? '' : '尚未配置远程密钥。' }}</p>
        <button class="eval-primary" :disabled="busy || ['queued', 'running', 'cancel_requested'].includes(current?.status || '') || !selected.length" @click="start">{{ busy ? '正在启动…' : '开始评测' }}</button>
        <p v-if="error" class="eval-error">{{ error }}</p>
      </section>
      <section class="eval-card">
        <h2>3 · 运行报告</h2>
        <template v-if="current">
          <div class="eval-meta">{{ current.mode === 'demo' ? '离线演示' : '实时评测' }} · {{ current.model }} · {{ current.id }}</div>
          <p v-if="['queued', 'running', 'cancel_requested'].includes(current.status || '')" class="eval-notice">{{ current.status === 'cancel_requested' ? '正在等待当前试验结束，随后取消' : '运行中' }}：{{ current.progress?.completed_attempts || 0 }} / {{ current.progress?.total_attempts || 0 }} 次试验完成 <span v-if="current.progress?.current_task">· 当前 {{ current.progress.current_task }}</span></p>
          <p v-if="current.status === 'cancelled'" class="eval-notice">已取消后续试验；保留已完成的 {{ current.progress?.completed_attempts || 0 }} 次试验。部分完成的题不计入通过数。</p>
          <p v-if="current.status === 'budget_exhausted'" class="eval-notice">远程请求预算已耗尽，后续试验已停止；已有轨迹保留，部分完成的题不计入通过数。</p>
          <p v-if="current.status === 'failed' || current.status === 'interrupted'" class="eval-error">{{ current.error || '运行未完成' }}</p>
          <div class="eval-score"><strong>{{ current.summary.passed }} / {{ current.summary.total }}</strong><span>任务通过</span></div>
          <div class="eval-metrics"><span>完成 {{ current.summary.completion }}/{{ current.summary.total }}</span><span>安全 {{ current.summary.safety }}/{{ current.summary.total }}</span><span>过程 {{ current.summary.process }}/{{ current.summary.total }}</span></div>
          <p v-if="current.summary.metrics" class="eval-meta">试验总耗时 {{ formatDuration(current.summary.metrics.elapsed_ms) }} · 远程调用 {{ current.summary.metrics.model_calls }} 次 · Token {{ current.summary.metrics.total_tokens ?? '未知' }} <span v-if="current.summary.metrics.successful_calls">（用量覆盖 {{ current.summary.metrics.usage_calls }}/{{ current.summary.metrics.successful_calls }} 次成功调用）</span></p>
          <p v-if="current.request_budget" class="eval-meta">远程 HTTP 请求 {{ current.request_budget.used }}/{{ current.request_budget.limit }} 次（含重试）{{ current.request_budget.exhausted ? ' · 已耗尽' : '' }}；这是调用上限，不代表费用。</p>
          <details v-if="current.summary.stage_metrics?.length" class="eval-provenance"><summary>阶段汇总（{{ current.summary.stage_metrics.length }} 类）</summary><p class="eval-meta">阶段累计耗时用于定位；MCP 工具耗时已包含在单库 Agent 阶段中。</p><p v-for="stage in current.summary.stage_metrics" :key="stage.stage" class="eval-meta">{{ stageLabel(stage.stage) }}：{{ stageDuration(stage.elapsed_ms) }} · {{ stage.calls }} 次 · 失败 {{ stage.failures }} 次</p></details>
          <p v-if="current.summary.failure_summary?.length" class="eval-notice">失败归因（按失败试验计）：<span v-for="item in current.summary.failure_summary" :key="`${item.kind}-${item.stage}`">{{ failureLabel({ kind: item.kind, stage: item.stage, failed_checks: [] }) }} {{ item.count }} 次　</span></p>
          <p v-if="current.trials === 3" class="eval-meta">{{ current.mode === 'live' ? '模型任务 Pass³' : '演示流程 Pass³' }}：{{ current.summary.pass3 }} / {{ current.model_task_count ?? current.cases.filter(item => item.kind !== 'security_probe').length }}。权限护栏题单独运行一次。</p>
          <p class="eval-meta">任务集 v{{ current.task_version }} · {{ current.fixture_id || 'base' }} · 数据指纹 {{ current.fixture_hash }}</p>
          <p v-if="current.baseline_run_id" class="eval-meta">候选运行 · 基线 {{ current.baseline_run_id }} · 固定题目、数据、试验次数和请求上限</p>
          <p v-if="current.task_ids?.length" class="eval-meta">本次选题：{{ current.task_ids.join('、') }}</p>
          <p class="eval-meta">原始判分器 {{ current.grader_hash || '未知（旧运行）' }}<span v-if="current.replay_grader_hash"> · 本次重放判分器 {{ current.replay_grader_hash }}</span></p>
          <p v-if="current.replay_grader_hash && current.grader_hash && current.replay_grader_hash !== current.grader_hash" class="eval-notice">本次重放使用了不同的判分器，重判结果可能与原始报告不同；原始记录未被覆盖。</p>
          <p v-if="current.review" class="eval-meta">运行时人工审核：{{ current.review.approved }}/{{ current.review.total }} 题已通过 <span v-if="current.review.pending || current.review.stale || current.review.rejected">· 其余题分数仅供开发验证</span></p>
          <p v-if="current.mode === 'live' && current.provenance" class="eval-meta">提示词相关源码 {{ current.provenance.prompt_hash }} · 工作流源码 {{ current.provenance.workflow_hash }}</p>
          <p v-else-if="current.mode === 'live'" class="eval-meta">旧运行缺少实现版本信息。</p>
          <details v-if="current.mode === 'live' && current.provenance?.config" class="eval-provenance"><summary>查看本次非敏感模型配置</summary><pre>{{ JSON.stringify(current.provenance.config, null, 2) }}</pre></details>
          <button class="eval-secondary" :disabled="busy || (current.status !== undefined && current.status !== 'completed')" @click="replay">重放并重新判分</button>
          <button v-if="current.mode === 'live' && current.status === 'completed' && current.task_ids?.length && current.request_budget && current.grader_hash && !current.baseline_run_id" class="eval-secondary" :disabled="busy || !selectedModel" @click="startCandidate">以此为基线运行候选（{{ selectedModel }}）</button>
          <button class="eval-secondary" :disabled="exportBusy || !['completed', 'cancelled', 'budget_exhausted'].includes(current.status || 'completed')" @click="exportRun">{{ exportBusy ? '正在导出…' : '下载评测摘要' }}</button>
          <button v-if="['queued', 'running', 'cancel_requested'].includes(current.status || '')" class="eval-secondary eval-cancel" :disabled="cancelBusy || current.status === 'cancel_requested'" @click="cancelRun">{{ cancelBusy ? '正在请求…' : '停止后续试验' }}</button>
          <article v-for="item in current.cases" :key="item.task_id" class="eval-result">
            <button @click="expanded = expanded === item.task_id ? '' : item.task_id"><b :class="item.incomplete ? 'fail' : item.grade.passed ? 'pass' : 'fail'">{{ item.incomplete ? '未完成' : item.grade.passed ? '通过' : '失败' }}</b><span>{{ item.query }}</span><span>⌄</span></button>
            <p v-if="item.failure_attribution" class="eval-notice">主要排查位置 · 试验 {{ item.failure_attribution.trial }}：{{ failureLabel(item.failure_attribution) }}</p>
            <div v-if="expanded === item.task_id" class="eval-trace">
              <p v-if="item.error" class="eval-error">{{ item.error }}</p>
              <p>判分：<span v-for="(ok, name) in item.grade.checks" :key="name" :class="ok ? 'pass' : 'fail'">{{ name }} {{ ok ? '✓' : '×' }}　</span></p>
              <p>SQL：<code>{{ item.result.sql || '无' }}</code></p>
              <details v-if="item.stage_metrics?.length" class="eval-provenance"><summary>本题阶段归因</summary><p v-for="stage in item.stage_metrics" :key="stage.stage" class="eval-meta">{{ stageLabel(stage.stage) }}：{{ stageDuration(stage.elapsed_ms) }} · {{ stage.calls }} 次 · 失败 {{ stage.failures }} 次</p></details>
              <div v-for="attempt in item.attempts" :key="attempt.trial"><p>试验 {{ attempt.trial }}：<span :class="attempt.grade.passed ? 'pass' : 'fail'">{{ attempt.grade.passed ? '通过' : '失败' }}</span> {{ attempt.error || '' }} <small v-if="attempt.metrics">· {{ formatDuration(attempt.metrics.elapsed_ms) }} · 调用 {{ attempt.metrics.model_calls }} 次 · Token {{ attempt.metrics.total_tokens ?? '未知' }}</small></p><p v-if="attempt.failure_attribution" class="eval-notice">{{ failureLabel(attempt.failure_attribution) }}</p><details v-if="attempt.stages?.length" class="eval-provenance"><summary>阶段轨迹</summary><p v-for="(stage, index) in attempt.stages" :key="index" class="eval-meta"><span :class="stage.success ? 'pass' : 'fail'">{{ stage.success ? '✓' : '×' }}</span> {{ stageLabel(stage.stage) }}<span v-if="stage.tool"> · {{ stage.tool }}</span> · {{ stageDuration(stage.elapsed_ms) }}<span v-if="stage.failure_type"> · {{ stage.failure_type }}</span></p></details><details v-if="attempt.model_calls?.length"><summary>模型调用摘要</summary><p v-for="(call, index) in attempt.model_calls" :key="index" class="eval-meta">{{ call.kind }} · {{ call.stage ? stageLabel(call.stage) : '阶段未知' }} · {{ stageDuration(call.elapsed_ms) }} · {{ call.success ? '成功' : '失败' }} · Token {{ call.usage?.total_tokens ?? '未知' }}</p></details><details><summary>本次轨迹</summary><pre>{{ JSON.stringify(attempt.result, null, 2) }}</pre></details></div>
              <details><summary>完整轨迹（JSON）</summary><pre>{{ JSON.stringify(item.result, null, 2) }}</pre></details>
            </div>
          </article>
        </template>
        <p v-else class="eval-empty">运行一个任务，或从历史记录中打开报告。</p>
      </section>
    </div>
    <section class="eval-card eval-history"><h2>历史运行</h2><button v-for="run in runs" :key="run.id" @click="openRun(run.id)">{{ run.started_at.slice(0, 19).replace('T', ' ') }} · {{ run.mode }} · {{ run.fixture_id || 'base' }} · {{ run.model }}<span v-if="run.baseline_run_id"> · 候选 ← {{ run.baseline_run_id }}</span> · {{ run.status || 'completed' }} · {{ run.summary.passed }}/{{ run.summary.total }}</button><p v-if="!runs.length">暂无运行记录</p></section>
    <section class="eval-card eval-history">
      <h2>4 · 回归对比</h2>
      <p>选择同一任务版本、数据、模式、试验次数和判分器版本的两次已完成运行；仅比较共同题目。</p>
      <div class="eval-compare-controls">
        <label>基线<select v-model="baselineId"><option value="">选择运行</option><option v-for="run in runs.filter(item => !item.status || item.status === 'completed')" :key="run.id" :value="run.id">{{ run.id }} · {{ run.mode }} · {{ run.model }}</option></select></label>
        <label>候选<select v-model="candidateId"><option value="">选择运行</option><option v-for="run in runs.filter(item => !item.status || item.status === 'completed')" :key="run.id" :value="run.id">{{ run.id }} · {{ run.mode }} · {{ run.model }}</option></select></label>
        <button class="eval-secondary" @click="compareRuns">比较</button>
      </div>
      <p v-if="compareError" class="eval-error">{{ compareError }}</p>
      <template v-if="comparison">
        <p class="eval-meta">共同题 {{ comparison.counts.common }} · {{ comparison.mode }} · {{ comparison.trials }} 次试验</p>
        <p class="eval-meta">模型：{{ comparison.baseline.model }} → {{ comparison.candidate.model }}</p>
        <p v-if="comparison.grader_comparison?.status === 'same'" class="eval-meta">判分器一致：{{ comparison.grader_comparison.baseline_hash }}</p>
        <p v-else class="eval-notice">至少一份旧运行缺少判分器版本，无法确认判分规则一致。</p>
        <p v-if="comparison.mode === 'live'" class="eval-meta">比较模型时请保持选题、数据集、试验次数与请求上限一致；当前任务集仍需人工审核。</p>
        <p v-if="comparison.provenance?.status === 'unknown'" class="eval-notice">版本信息不足：至少一份旧运行缺少实现快照，无法判断提示词、工作流和配置是否一致。</p>
        <p v-else-if="comparison.provenance?.status === 'changed'" class="eval-notice">实现已变化：下方列出提示词、工作流或模型配置的差异。分数仍可对比。</p>
        <p v-else-if="comparison.provenance?.status === 'same'" class="eval-notice">实现快照一致；远程服务版本仍可能变化。</p>
        <p v-if="comparison.mode === 'live' && comparison.budget_comparison?.status === 'changed'" class="eval-notice">远程请求上限不同：{{ comparison.budget_comparison.baseline_limit }} → {{ comparison.budget_comparison.candidate_limit }}；差异可能影响完成率。</p>
        <p v-else-if="comparison.mode === 'live' && comparison.budget_comparison?.status === 'unknown'" class="eval-notice">至少一份旧运行缺少远程请求上限信息，无法确认预算一致。</p>
        <details v-if="comparison.provenance?.changes.length" class="eval-provenance"><summary>查看 {{ comparison.provenance.changes.length }} 项实现差异</summary><p v-for="change in comparison.provenance.changes" :key="change.field" class="eval-meta">{{ change.field }}：{{ change.before ?? '未知' }} → {{ change.after ?? '未知' }}</p></details>
        <div class="eval-metrics"><span class="fail">退化 {{ comparison.counts.regressed }}</span><span class="pass">改善 {{ comparison.counts.improved }}</span><span>保持通过 {{ comparison.counts.still_passed }}</span><span>保持失败 {{ comparison.counts.still_failed }}</span></div>
        <p v-if="comparison.only_baseline.length" class="eval-meta">仅基线：{{ comparison.only_baseline.join('、') }}</p>
        <p v-if="comparison.only_candidate.length" class="eval-meta">仅候选：{{ comparison.only_candidate.join('、') }}</p>
        <article v-for="item in comparison.cases" :key="item.task_id" class="eval-result"><strong :class="item.state === 'regressed' ? 'fail' : item.state === 'improved' ? 'pass' : ''">{{ comparisonLabel(item.state) }}</strong> · {{ item.query }} <small>({{ item.task_id }})</small><p v-if="item.check_changes.length" class="eval-meta">判分变化：<span v-for="change in item.check_changes" :key="change.name">{{ change.name }} {{ change.before === null ? '缺失' : change.before ? '✓' : '×' }} → {{ change.after === null ? '缺失' : change.after ? '✓' : '×' }}　</span></p><p v-if="item.cost_change" class="eval-meta">耗时变化：{{ item.cost_change.elapsed_ms_delta === null ? '未知' : formatDuration(item.cost_change.elapsed_ms_delta) }} · 调用数变化：{{ item.cost_change.model_calls_delta ?? '未知' }} · Token 变化：{{ item.cost_change.total_tokens_delta ?? '未知' }}</p><p v-if="item.attribution_change && (item.attribution_change.before || item.attribution_change.after)" class="eval-meta">主要归因：{{ failureLabel(item.attribution_change.before) }} → {{ failureLabel(item.attribution_change.after) }}</p><details v-if="item.stage_changes?.length" class="eval-provenance"><summary>阶段变化</summary><p v-for="stage in item.stage_changes" :key="stage.stage" class="eval-meta">{{ stageLabel(stage.stage) }}：耗时 {{ signedDuration(stage.elapsed_ms_delta) }} · 调用 {{ stage.calls_delta > 0 ? '+' : '' }}{{ stage.calls_delta }} · 失败 {{ stage.failures_delta > 0 ? '+' : '' }}{{ stage.failures_delta }}</p></details></article>
      </template>
    </section>
    <section class="eval-card eval-history">
      <h2>5 · 评测集区分力审计</h2>
      <p>运行固定的错误 SQL，检查原始和边界数据是否能把结果与标准答案区分开。只执行本地 SQL，不调用模型。</p>
      <button class="eval-secondary" :disabled="auditBusy" @click="runAudit">{{ auditBusy ? '正在审计…' : '检查评测题区分力' }}</button>
      <p v-if="auditError" class="eval-error">{{ auditError }}</p>
      <template v-if="audit">
        <p class="eval-meta">错误样例 v{{ audit.mutant_version }} · 清单指纹 {{ audit.mutant_hash }} · 任务集 v{{ audit.task_version }}</p>
        <div class="eval-metrics"><span v-for="fixture in auditFixtures" :key="fixture.id">{{ fixture.label }}识别 {{ audit.counts[fixture.id].killed }}/{{ audit.cases.length }}</span><span>仍漏检 {{ audit.counts.overall.survived }}</span><span>无效 {{ audit.counts.overall.invalid }}</span></div>
        <article v-for="item in auditCases" :key="item.id" class="eval-result eval-audit-row">
          <strong :class="item.status === 'killed' ? 'pass' : 'fail'">{{ auditLabel(item.status) }}</strong> · {{ item.fault }} <small>({{ item.task_id }})</small>
          <p class="eval-meta"><span v-for="fixture in auditFixtures" :key="fixture.id">{{ fixture.label }}：<b :class="item.fixtures[fixture.id].status === 'killed' ? 'pass' : 'fail'">{{ auditLabel(item.fixtures[fixture.id].status) }}</b>　</span></p>
          <p v-if="item.status === 'survived' && item.suggestion" class="eval-notice">补数建议：{{ item.suggestion }}</p>
          <details><summary>查看错误 SQL 与执行异常</summary><pre>{{ item.sql }}</pre><p v-for="fixture in auditFixtures.filter(option => item.fixtures[option.id].error)" :key="fixture.id" class="eval-error">{{ fixture.label }}：{{ item.fixtures[fixture.id].error }}</p></details>
        </article>
      </template>
    </section>
    <section class="eval-card eval-history">
      <h2>6 · 人工审核任务</h2>
      <p>查看任务意图、判分依据、标准 SQL 和各数据集预期结果后，再由管理员记录审核结论。系统不会自动批准题目。</p>
      <p v-if="reviews" class="eval-meta">任务集 v{{ reviews.task_version }} · 已通过 {{ reviews.counts.approved }} · 待审核 {{ reviews.counts.pending }} · 已退回 {{ reviews.counts.rejected }} · 已过期 {{ reviews.counts.stale }}</p>
      <div class="eval-review-layout">
        <div class="eval-review-list"><button v-for="item in reviews?.tasks || []" :key="item.task_id" :class="{ active: reviewPreview?.task_id === item.task_id }" @click="openReview(item.task_id)"><strong>{{ item.query }}</strong><small>{{ item.task_id }} · {{ reviewLabel(item.status) }}</small></button></div>
        <div v-if="reviewPreview" class="eval-review-detail">
          <h3>{{ reviewPreview.query }}</h3>
          <p class="eval-meta">{{ reviewPreview.category }} · {{ reviewLabel(reviewPreview.status) }} · 题目指纹 {{ reviewPreview.fingerprint }}</p>
          <p><b>业务意图：</b>{{ reviewPreview.spec.intent }}</p>
          <p><b>预期结果：</b>{{ reviewPreview.spec.result }}</p>
          <p><b>过程要求：</b>{{ reviewPreview.spec.process }}</p>
          <p><b>易错边界：</b>{{ reviewPreview.spec.pitfall }}</p>
          <details v-if="reviewPreview.oracle_sql"><summary>查看标准 SQL</summary><pre>{{ reviewPreview.oracle_sql }}</pre></details>
          <details v-for="fixture in auditFixtures.filter(option => reviewPreview?.expected_rows[option.id])" :key="fixture.id"><summary>查看{{ fixture.label }}预期结果（{{ reviewPreview.expected_rows[fixture.id].length }} 行）</summary><pre>{{ JSON.stringify(reviewPreview.expected_rows[fixture.id], null, 2) }}</pre></details>
          <p v-if="!reviewPreview.oracle_sql" class="eval-notice">此题以路由或权限行为判分；请按过程要求审核。</p>
          <label class="eval-review-note">审核意见<textarea v-model="reviewNote" rows="3" placeholder="写下核对依据或需修改的问题"></textarea></label>
          <div class="eval-review-actions"><button class="eval-secondary" :disabled="reviewBusy" @click="submitReview('approved')">确认通过</button><button class="eval-secondary eval-cancel" :disabled="reviewBusy" @click="submitReview('rejected')">退回修改</button></div>
          <p v-if="reviewPreview.history.length" class="eval-meta">历史记录：<span v-for="(entry, index) in reviewPreview.history" :key="index">{{ reviewLabel(entry.decision) }} · {{ entry.reviewer }} · {{ entry.reviewed_at.slice(0, 19).replace('T', ' ') }} · {{ entry.note }}； </span></p>
        </div>
        <p v-else class="eval-meta">选择左侧题目查看审核材料。</p>
      </div>
      <p v-if="reviewError" class="eval-error">{{ reviewError }}</p>
    </section>
  </div>
</template>

<style scoped>
.eval-page{height:100%;overflow:auto;padding:32px;max-width:1300px;margin:auto;color:#24344a}.eval-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}.eval-heading small{letter-spacing:.16em;color:#6780a3}.eval-heading h1{font-size:30px;margin:8px 0}.eval-heading p,.eval-card>p{color:#718099}.eval-heading>span{border:1px solid #d9e3ee;border-radius:20px;padding:7px 12px}.eval-grid{display:grid;grid-template-columns:minmax(300px,420px) 1fr;gap:20px}.eval-card{background:#fff;border:1px solid #e0e7ef;border-radius:16px;padding:22px;box-shadow:0 8px 24px #1734550a}.eval-card h2{font-size:17px;margin:0 0 14px}.eval-card h2:not(:first-child){margin-top:28px}.eval-task{display:flex;gap:10px;padding:12px 0;border-bottom:1px solid #edf1f5;cursor:pointer}.eval-task span{display:grid;gap:5px}.eval-task small,.eval-meta{color:#78879c;font-size:12px}.eval-option{display:block;margin:12px 0}.eval-notice{background:#f4f7fb;padding:10px;border-radius:8px;font-size:13px}.eval-primary,.eval-secondary{border:0;border-radius:8px;padding:11px 16px;cursor:pointer}.eval-primary{background:#345fe5;color:white;width:100%;margin-top:12px}.eval-secondary{background:#e9efff;color:#2446a1}.eval-primary:disabled{opacity:.55;cursor:wait}.eval-error,.fail{color:#bd3e4a}.pass{color:#21845a}.eval-score{display:flex;align-items:baseline;gap:10px;margin:20px 0}.eval-score strong{font-size:34px}.eval-metrics{display:flex;gap:8px;flex-wrap:wrap}.eval-metrics span{background:#f1f5fb;padding:8px;border-radius:7px}.eval-result{border-top:1px solid #e5eaf0;margin-top:14px}.eval-result>button{border:0;background:none;width:100%;padding:14px 0;display:flex;gap:12px;text-align:left;cursor:pointer}.eval-result>button span:nth-child(2){flex:1}.eval-trace{padding:0 0 12px;font-size:13px}.eval-trace code{white-space:pre-wrap}.eval-trace pre{overflow:auto;max-height:340px;background:#f5f7fa;padding:12px}.eval-empty{padding:40px;text-align:center}.eval-history{margin-top:20px}.eval-history button{display:block;width:100%;text-align:left;border:0;border-top:1px solid #edf1f5;background:none;padding:12px;cursor:pointer}@media(max-width:850px){.eval-grid{grid-template-columns:1fr}.eval-page{padding:18px}}
.eval-compare-controls{display:flex;align-items:end;gap:12px;flex-wrap:wrap;margin:16px 0}.eval-compare-controls label{display:grid;gap:6px;color:#63758e}.eval-compare-controls select{max-width:260px;padding:9px;border:1px solid #d8e1ec;border-radius:7px;background:white}
.eval-audit-row{padding:14px 0}.eval-audit-row p{margin:8px 0}.eval-audit-row pre{overflow:auto;white-space:pre-wrap;background:#f5f7fa;padding:12px;border-radius:8px}.eval-audit-row summary{cursor:pointer;color:#345fe5}
.eval-cancel{margin-left:10px;color:#a33f49;background:#fff0f1}.eval-cancel:disabled{opacity:.55;cursor:wait}
.eval-provenance{margin:12px 0;color:#566a86;font-size:13px}.eval-provenance summary{cursor:pointer}.eval-provenance pre{white-space:pre-wrap;overflow:auto;background:#f5f7fa;padding:12px;border-radius:8px}
.eval-review-layout{display:grid;grid-template-columns:minmax(240px,360px) 1fr;gap:20px;margin-top:16px}.eval-review-list{max-height:500px;overflow:auto;border:1px solid #e0e7ef;border-radius:8px}.eval-review-list button{width:100%;display:grid;gap:5px;text-align:left;padding:12px;border:0;border-bottom:1px solid #edf1f5;background:white;cursor:pointer}.eval-review-list button.active{background:#f1f5ff}.eval-review-list small{color:#78879c}.eval-review-detail{min-width:0}.eval-review-detail h3{margin:0 0 10px}.eval-review-detail p{line-height:1.5}.eval-review-detail details{margin:10px 0}.eval-review-detail summary{cursor:pointer;color:#345fe5}.eval-review-detail pre{white-space:pre-wrap;overflow:auto;max-height:300px;background:#f5f7fa;padding:12px;border-radius:8px}.eval-review-note{display:grid;gap:7px;margin:16px 0;color:#63758e}.eval-review-note textarea{width:100%;box-sizing:border-box;border:1px solid #d8e1ec;border-radius:8px;padding:10px;font:inherit}.eval-review-actions{display:flex;gap:10px}.eval-review-actions .eval-cancel{margin-left:0}@media(max-width:850px){.eval-review-layout{grid-template-columns:1fr}.eval-review-list{max-height:240px}}
</style>
