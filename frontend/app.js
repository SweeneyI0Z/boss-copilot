import {
  createApp, ref, reactive, computed, watch, onMounted, onBeforeUnmount,
} from '/static/vue.esm-browser.prod.js'
import { marked } from '/static/vendor/marked.esm.js'
import DOMPurify from '/static/vendor/purify.es.mjs'

// -- 通用格式化与安全渲染 -----------------------------------------------
const clone = value => JSON.parse(JSON.stringify(value))
const fmtTime = value => {
  if (!value) return '暂无记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}
const shortTime = value => {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value || '')
  return `${date.getMonth() + 1}月${date.getDate()}日 ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}
const percentOf = run => Number(run && run.progressPercent || 0)

marked.setOptions({ gfm: true, breaks: true })
function renderMarkdown(value) {
  const html = marked.parse(String(value || ''))
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form'],
    FORBID_ATTR: ['style'],
  })
}

// -- API 与全局真实数据仓库 ---------------------------------------------
class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

async function apiRequest(path, { method = 'GET', body, headers = {} } = {}) {
  let response
  try {
    response = await fetch(path, {
      method,
      headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (error) {
    // fetch 在网络层失败（服务未启动/连接被重置）只会抛 TypeError，翻译成可操作的提示
    throw new ApiError('无法连接本地后端，请确认服务窗口已开启（关闭 start.bat 窗口即停止服务）', 0)
  }
  const contentType = response.headers.get('content-type') || ''
  let payload = null
  if (contentType.includes('application/json')) {
    try { payload = await response.json() } catch (_) { payload = null }
  } else {
    try { payload = await response.text() } catch (_) { payload = '' }
  }
  if (!response.ok) {
    const message = payload && typeof payload === 'object'
      ? payload.detail || payload.error || payload.message
      : payload
    throw new ApiError(String(message || `请求失败（HTTP ${response.status}）`), response.status, payload)
  }
  return payload
}

async function streamNdjson(path, body, onEvent, signal) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    signal,
  })
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    try {
      const payload = await response.json()
      message = payload.detail || payload.error || payload.message || message
    } catch (_) { /* 保留 HTTP 状态提示。 */ }
    throw new ApiError(String(message), response.status)
  }
  if (!response.body) throw new ApiError('浏览器不支持流式响应')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const lines = buffer.split('\n')
      buffer = done ? '' : lines.pop()
      for (const raw of lines) {
        const line = raw.trim()
        if (!line) continue
        let event
        try { event = JSON.parse(line) }
        catch (_) { throw new ApiError('流式响应格式无效') }
        onEvent(event)
      }
      if (done) break
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer.trim()))
  } catch (error) {
    try { await reader.cancel() } catch (_) { /* 连接可能已经关闭。 */ }
    throw error
  } finally {
    reader.releaseLock()
  }
}

function withQuery(path, params = {}) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) query.set(key, String(value))
  })
  const encoded = query.toString()
  return encoded ? `${path}?${encoded}` : path
}

const apiClient = {
  settings: {
    get: () => apiRequest('/api/settings'),
    save: body => apiRequest('/api/settings', { method: 'PUT', body }),
    testLlm: body => apiRequest('/api/llm/test', { method: 'POST', body }),
  },
  resumes: {
    list: () => apiRequest('/api/resumes'),
    create: body => apiRequest('/api/resumes', { method: 'POST', body }),
    update: (id, body) => apiRequest(`/api/resumes/${id}`, { method: 'PUT', body }),
  },
  dashboard: () => apiRequest('/api/dashboard'),
  runs: {
    list: () => apiRequest('/api/runs?limit=100'),
    setEnabled: (id, enabled) => apiRequest(`/api/runs/${id}/enabled`, { method: 'PUT', body: { enabled } }),
    deletePreview: id => apiRequest(`/api/runs/${id}/delete-preview`),
    delete: id => apiRequest(`/api/runs/${id}`, { method: 'DELETE', body: { confirm_run_id: id } }),
    exportUrl: id => `/api/runs/${id}/export`,
  },
  collect: {
    status: () => apiRequest('/api/collect/status'),
    config: resumeId => apiRequest(withQuery('/api/collect/config', { resume_id: resumeId })),
    options: () => apiRequest('/api/collect/options'),
    saveConfig: (body, resumeId) => apiRequest(withQuery('/api/collect/config', { resume_id: resumeId }), { method: 'PUT', body }),
    start: body => apiRequest('/api/collect/run', { method: 'POST', body }),
    pause: () => apiRequest('/api/collect/pause', { method: 'POST' }),
    resume: () => apiRequest('/api/collect/resume', { method: 'POST' }),
    cancel: () => apiRequest('/api/collect/cancel', { method: 'POST' }),
  },
  favorites: {
    start: () => apiRequest('/api/favorites/sync', { method: 'POST', body: {} }),
    status: () => apiRequest('/api/favorites/sync/status'),
    cancel: () => apiRequest('/api/favorites/sync/cancel', { method: 'POST' }),
  },
  analytics: params => apiRequest(withQuery('/api/analytics', params)),
  jobs: {
    list: params => apiRequest(withQuery('/api/jobs', params)),
    detail: (key, resumeId) => apiRequest(withQuery(`/api/jobs/${encodeURIComponent(key)}`, { resume_id: resumeId })),
    favorite: (key, favorite) => apiRequest(`/api/jobs/${encodeURIComponent(key)}/favorite`, { method: 'PUT', body: { favorite } }),
    exclude: key => apiRequest(`/api/jobs/${encodeURIComponent(key)}/exclude`, { method: 'POST' }),
    openBoss: key => apiRequest(`/api/jobs/${encodeURIComponent(key)}/open-boss`, { method: 'POST' }),
    scoreJob: body => apiRequest('/api/score/l1', { method: 'POST', body }),
    scoreMatch: body => apiRequest('/api/score/l2', { method: 'POST', body }),
    workflow: (key, body) => apiRequest(`/api/jobs/${encodeURIComponent(key)}/workflow`, { method: 'PUT', body }),
  },
  strategy: {
    generate: resumeId => apiRequest('/api/strategy/generate', { method: 'POST', body: { resume_id: resumeId } }),
    generateStream: (resumeId, onEvent, signal) => streamNdjson(
      '/api/strategy/generate/stream', { resume_id: resumeId }, onEvent, signal),
  },
  greetings: {
    list: () => apiRequest('/api/greetings'),
    generate: (jobKey, resumeId) => apiRequest('/api/greeting/generate', { method: 'POST', body: { job_key: jobKey, resume_id: resumeId } }),
    approve: (id, index, chosenText) => apiRequest(`/api/greetings/${id}`, { method: 'PUT', body: { index, chosen_text: chosenText } }),
    sendBatch: jobKeys => apiRequest('/api/greeting/send-batch', { method: 'POST', body: { job_keys: jobKeys } }),
    sendStatus: () => apiRequest('/api/greeting/send-status'),
  },
  ai: {
    status: page => apiRequest(`/api/ai/tasks/${page}`),
    enqueue: (page, body) => apiRequest(`/api/ai/tasks/${page}`, { method: 'POST', body }),
    cancel: (page, body = {}) => apiRequest(`/api/ai/tasks/${page}/cancel`, { method: 'POST', body }),
    streamUrl: page => `/api/ai/tasks/${page}/stream`,
  },
  interviews: {
    list: () => apiRequest('/api/interviews'),
    start: (jobKey, resumeId) => apiRequest('/api/interview/start', { method: 'POST', body: { job_key: jobKey, resume_id: resumeId } }),
    answer: (id, text) => apiRequest(`/api/interview/${id}/answer`, { method: 'POST', body: { text } }),
    finish: id => apiRequest(`/api/interview/${id}/finish`, { method: 'POST' }),
  },
  accounts: {
    list: () => apiRequest('/api/accounts'),
    launch: name => apiRequest(`/api/accounts/${name}/launch`, { method: 'POST' }),
    login: name => apiRequest(`/api/accounts/${name}/login-page`, { method: 'POST' }),
    check: name => apiRequest(`/api/accounts/${name}/login-state`),
    stop: name => apiRequest(`/api/accounts/${name}/stop`, { method: 'POST' }),
  },
}

const EMPTY_SETTINGS = {
  dualAccount: true, llmBaseUrl: '', llmApiKey: '', llmModel: '',
  sendDailyLimit: 40, sendDailyHardCap: 110, sendGapMin: 30, sendGapMax: 90,
  matchScoreTopN: 60, inactiveDays: 14, collectPace: 'balanced',
}
const EMPTY_ANALYTICS = {
  summary: {}, distributions: {},
  meta: {
    keywords: [], cities: [], keyword_options: [], city_options: [],
    options: { experience: [], degree: [], industry: [], scale: [] },
  },
  trends: [], cross: {}, funnel: { stages: [] }, top_companies: [], top_skills: [],
  job_rows: [],
}
const EMPTY_AI_PAGE = page => ({
  page, tasks: [], active: 0, effective_concurrency: 5, max_concurrency: 5,
  progress: { total: 0, completed: 0, remaining: 0, percent: 0, eta_seconds: null,
    queued: 0, running: 0, retrying: 0, succeeded: 0, failed: 0, cancelled: 0 },
})
const store = reactive({
  resumes: [], selectedResumeId: null, jobs: [], jobTotal: 0, runs: [], accounts: [],
  settings: clone(EMPTY_SETTINGS), dashboard: null, analytics: clone(EMPTY_ANALYTICS),
  greetings: [], interviews: [],
  collect: { status: {}, favoritesStatus: {}, config: null, options: { city_options: [], filter_values: {}, max_combinations: 20 } },
  ai: { jobs: EMPTY_AI_PAGE('jobs'), workbench: EMPTY_AI_PAGE('workbench') },
  aiTracking: { jobs: [], workbench: [] },
  ui: { toast: null, confirm: null, bootstrapLoading: true, bootstrapError: '', refreshing: false, lastUpdated: '' },
  batch: { running: false, type: '', label: '', done: 0, total: 0, failures: [] },
})

function settingsFromApi(raw = {}) {
  return {
    dualAccount: Boolean(raw.dual_account_enabled),
    llmBaseUrl: raw.llm_base_url || '', llmApiKey: raw.llm_api_key || '', llmModel: raw.llm_model || '',
    sendDailyLimit: Number(raw.send_daily_limit ?? 40), sendDailyHardCap: Number(raw.send_daily_hard_cap ?? 110),
    sendGapMin: Number(raw.send_gap_min_sec ?? 30), sendGapMax: Number(raw.send_gap_max_sec ?? 90),
    matchScoreTopN: Number(raw.l2_top_n ?? 60), inactiveDays: Number(raw.hr_inactive_days ?? 14),
    collectPace: ['standard', 'balanced', 'fast'].includes(raw.collect_pace) ? raw.collect_pace : 'balanced',
  }
}
function settingsToApi(settings) {
  return {
    dual_account_enabled: Boolean(settings.dualAccount),
    llm_base_url: String(settings.llmBaseUrl || '').trim(),
    llm_api_key: String(settings.llmApiKey || '').trim(),
    llm_model: String(settings.llmModel || '').trim(),
    send_daily_limit: Number(settings.sendDailyLimit), send_daily_hard_cap: Number(settings.sendDailyHardCap),
    send_gap_min_sec: Number(settings.sendGapMin), send_gap_max_sec: Number(settings.sendGapMax),
    l2_top_n: Number(settings.matchScoreTopN), hr_inactive_days: Number(settings.inactiveDays),
    collect_pace: settings.collectPace,
  }
}
function normalizeResume(row = {}) {
  return { ...row, body: row.resume_text || '', updatedAt: row.updated_at || row.created_at || '' }
}
function runName(row) {
  const names = { favorite_sync: 'BOSS 收藏同步', config: '配置采集', search: '关键词采集', company: '公司定向采集', plan: 'AI 采集计划', sync: '同步刷新', detail_retry: 'JD 补采', xlsx_import: 'Excel 导入', json_import: '采集文件导入' }
  return row.plan_name || row.name || (row.params && row.params.name) || `${names[row.kind] || '采集任务'} #${row.id}`
}
function normalizeRun(row = {}) {
  const runtime = row.runtime || {}
  const progress = runtime.progress || row.progress || {}
  const finalStatus = ['succeeded', 'completed'].includes(row.status) ? 'completed' : row.status
  const running = Boolean(runtime.running || (!row.finished_at && row.status === 'running'))
  const paused = Boolean(runtime.paused || row.paused)
  const status = paused ? 'paused' : running ? 'running' : finalStatus || 'interrupted'
  const phase = runtime.phase || row.phase || ''
  const listTotal = Number(progress.list_total || 0)
  const listCompleted = Number(progress.list_completed || 0)
  const detailTotal = Number(progress.jd_total || progress.detail_total || 0)
  const detailCompleted = Number(progress.jd_completed || progress.detail_completed || 0)
  const completed = phase === 'details' ? detailCompleted : listCompleted
  const total = phase === 'details' ? detailTotal : listTotal
  const succeeded = status === 'completed'
  const backendPercent = Number(progress.percent)
  const progressPercent = succeeded ? 100 : Number.isFinite(backendPercent)
    ? Math.max(0, Math.min(99, Math.round(backendPercent)))
    : total ? Math.min(99, Math.round(completed / total * 100)) : 0
  const itemCount = Number(row.item_count ?? (row.stats && (row.stats.total || row.stats.touched)) ?? 0)
  const jobsDiscovered = Number(progress.jobs_discovered ?? itemCount)
  const withJdCount = Math.max(Number(row.with_jd_count || 0), detailCompleted)
  const completenessKnown = row.completeness_known !== false
  return {
    ...row, name: runName(row), startedAt: row.started_at || '', finishedAt: row.finished_at || '',
    status, enabled: row.enabled !== false, collected: itemCount, target: itemCount,
    itemCount, ownedItemCount: Number(row.owned_item_count || 0),
    hasSourceOwnership: Boolean(row.has_source_ownership), canExport: Boolean(row.can_export),
    phase, listTotal, listCompleted, detailTotal, detailCompleted,
    jobsDiscovered, jobsTotal: Number(progress.jobs_total || itemCount),
    withJdCount, listOnlyCount: completenessKnown ? Math.max(0, itemCount - withJdCount) : null,
    completenessKnown, etaSeconds: progress.eta_seconds == null ? null : Number(progress.eta_seconds),
    progressCompleted: completed, progressTotal: total, progressPercent,
    fetchDetails: Boolean(row.params && row.params.fetch_details),
    source: row.kind === 'favorite_sync' ? 'BOSS 收藏同步' : '本地后端',
  }
}
function formatEta(seconds) {
  if (seconds === null || seconds === undefined || seconds === '') return '预计时间计算中'
  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return '预计时间计算中'
  if (value < 60) return `预计剩余 ${Math.max(1, Math.ceil(value))} 秒`
  if (value < 3600) return `预计剩余 ${Math.ceil(value / 60)} 分钟`
  return `预计剩余 ${Math.floor(value / 3600)} 小时 ${Math.ceil(value % 3600 / 60)} 分钟`
}
function etaValue(value) {
  if (value === null || value === undefined || value === '') return null
  const seconds = Number(value)
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null
}
function runProgressSummary(run) {
  if (run.phase === 'details') {
    return `完整 JD ${run.detailCompleted} / ${run.detailTotal || run.jobsTotal} · 共 ${run.jobsTotal || run.jobsDiscovered} 个岗位`
  }
  if (run.status === 'running' || run.status === 'paused') {
    return `已发现 ${run.jobsDiscovered} 个岗位 · 列表 ${run.listCompleted} / ${run.listTotal}`
  }
  return `共 ${run.itemCount} 个岗位`
}
function adviceFromDetail(detail = {}) {
  if (!detail || !Object.keys(detail).length) return ''
  const lines = []
  if (detail.summary) lines.push(`### 总体判断\n${detail.summary}`)
  if (detail.advice) lines.push(`### 投递建议\n${detail.advice}`)
  if (Array.isArray(detail.strengths) && detail.strengths.length) {
    lines.push(`### 匹配优势\n${detail.strengths.map(item => `- ${item}`).join('\n')}`)
  }
  if (Array.isArray(detail.gaps) && detail.gaps.length) {
    lines.push(`### 面试前准备\n${detail.gaps.map(item => {
      if (typeof item === 'string') return `- ${item}`
      return `- **${item.gap || '能力缺口'}**：${item.action || '建议结合真实经历准备说明'}`
    }).join('\n')}`)
  }
  if (detail.resume_advice) lines.push(`### 简历侧重点\n${detail.resume_advice}`)
  return lines.join('\n\n')
}
function normalizeJob(row = {}, previous = {}) {
  const detail = row.l2_detail && typeof row.l2_detail === 'object' ? row.l2_detail : previous.l2_detail || {}
  const workflow = row.workflow || {}
  return {
    ...previous, ...row,
    favorite: Boolean(row.is_favorite ?? row.favorite ?? row.favorite_at ?? previous.favorite),
    excluded: (row.status || previous.status) === 'excluded',
    contacted: Boolean(row.greeted ?? row.contacted ?? workflow.greeted ?? previous.contacted),
    applied: Boolean(row.applied ?? workflow.applied ?? previous.applied),
    interviewed: Boolean(row.interviewed ?? workflow.interviewed ?? previous.interviewed),
    offered: Boolean(row.offered ?? workflow.offered ?? previous.offered),
    active: row.hr_active || previous.active || '未标注', active_ts: row.last_seen_at || previous.active_ts || '',
    source_keyword: row.source_keyword || row.keyword || row.source || previous.source_keyword || '未标注',
    priority: row.priority || previous.priority || '—', jd: row.jd ?? previous.jd ?? '',
    l2_detail: detail, advice: adviceFromDetail(detail), analysisReady: Boolean(detail && Object.keys(detail).length),
    actionError: previous.actionError || '',
  }
}
function normalizeAccounts(raw = {}) {
  return Object.entries(raw || {}).map(([key, account]) => {
    const loginState = account.login_state || {}
    return {
      key, label: account.label || key, description: account.description || '', port: account.port || '',
      running: Boolean(account.running), loggedIn: loginState.logged_in == null ? null : loginState.logged_in === true,
      checkedAt: loginState.checked_at || '', hint: loginState.hint || '', roles: account.roles || [],
    }
  })
}
function isLlmConfigured(settings = store.settings) {
  return Boolean(String(settings.llmBaseUrl || '').trim()
    && String(settings.llmApiKey || '').trim()
    && String(settings.llmModel || '').trim())
}
function jobsFromEnabledRuns() {
  return store.jobs.filter(job => !job.excluded)
}

async function refreshSettings() {
  Object.assign(store.settings, settingsFromApi(await apiClient.settings.get()))
}
async function refreshResumes() {
  store.resumes = (await apiClient.resumes.list()).map(normalizeResume)
  if (!store.resumes.some(item => item.id === store.selectedResumeId)) {
    const preferred = store.resumes.find(item => item.is_default) || store.resumes[0]
    store.selectedResumeId = preferred ? preferred.id : null
  }
}
async function refreshJobs() {
  if (!store.selectedResumeId) { store.jobs = []; store.jobTotal = 0; return }
  const result = await apiClient.jobs.list({ status: 'active', resume_id: store.selectedResumeId, limit: 500, sort: 'composite' })
  const sameResume = store.jobs.length === 0
    || store.jobs.every(job => Number(job.score_resume_id) === Number(result.resume_id))
  const previous = new Map((sameResume ? store.jobs : []).map(job => [job.job_key, job]))
  store.jobs = (result.items || []).map(row => normalizeJob(
    { ...row, score_resume_id: result.resume_id }, previous.get(row.job_key)))
  store.jobTotal = Number(result.total || 0)
  applyGreetingsToJobs()
}
async function refreshJobDetail(jobKey) {
  const detail = await apiClient.jobs.detail(jobKey, store.selectedResumeId)
  const index = store.jobs.findIndex(job => job.job_key === jobKey)
  if (index >= 0) store.jobs[index] = normalizeJob(
    { ...detail, score_resume_id: detail.resume && detail.resume.id }, store.jobs[index])
  applyGreetingsToJobs()
  return index >= 0 ? store.jobs[index] : normalizeJob(detail)
}
async function refreshRuns() { store.runs = (await apiClient.runs.list()).map(normalizeRun) }
async function refreshDashboard() { store.dashboard = await apiClient.dashboard() }
async function refreshCollectStatus() { store.collect.status = await apiClient.collect.status() || {} }
async function refreshFavoriteStatus() { store.collect.favoritesStatus = await apiClient.favorites.status() || {} }
async function refreshCollectOptions() {
  store.collect.options = await apiClient.collect.options() || store.collect.options
  syncFilterOptions()
}
async function refreshCollectConfig() {
  if (!store.selectedResumeId) return
  const result = await apiClient.collect.config(store.selectedResumeId)
  store.collect.config = result.config || null
}
async function refreshAnalytics(params = {}) {
  store.analytics = await apiClient.analytics({ ...params, resume_id: store.selectedResumeId }) || clone(EMPTY_ANALYTICS)
}
async function refreshGreetings() {
  store.greetings = await apiClient.greetings.list() || []
  applyGreetingsToJobs()
}
async function refreshInterviews() { store.interviews = await apiClient.interviews.list() || [] }
async function refreshAccounts() { store.accounts = normalizeAccounts(await apiClient.accounts.list()) }

async function refreshAiTasks(page, notify = false) {
  applyAiSnapshot(page, await apiClient.ai.status(page), notify)
}

function applyGreetingsToJobs() {
  const byJob = new Map()
  for (const greeting of store.greetings) {
    if (greeting.resume_id && store.selectedResumeId && Number(greeting.resume_id) !== Number(store.selectedResumeId)) continue
    if (greeting.stale) continue
    if (!byJob.has(greeting.job_key)) byJob.set(greeting.job_key, greeting)
  }
  for (const job of store.jobs) {
    const greeting = byJob.get(job.job_key)
    job.greetingId = greeting ? greeting.id : null
    job.greetingStatus = greeting ? greeting.status : ''
    job.greetingVariants = greeting ? (greeting.variants || []).slice(0, 3) : []
    job.greetingLabels = greeting ? (greeting.variant_labels || []).slice(0, 3) : []
    const chosenIndex = greeting && greeting.chosen ? job.greetingVariants.indexOf(greeting.chosen) : -1
    const previousIndex = Number(job.greetingSelectedIndex || 0)
    job.greetingSelectedIndex = chosenIndex >= 0 ? chosenIndex : Math.min(previousIndex, Math.max(0, job.greetingVariants.length - 1))
    job.greeting = greeting ? (greeting.chosen || job.greetingVariants[job.greetingSelectedIndex] || '') : ''
    if (greeting && (greeting.delivery_status === 'confirmed' || greeting.status === 'sent')) job.contacted = true
  }
}

const AI_ACTIVE = new Set(['queued', 'running', 'retrying'])
const AI_TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])
const seenAiFailures = new Set()
const aiRefreshTimers = { jobs: null, workbench: null }

function trackAiTasks(page, tasks = []) {
  const current = store.aiTracking[page] || []
  const hasActive = current.some(id => {
    const task = (store.ai[page].tasks || []).find(item => item.id === id)
    return task && AI_ACTIVE.has(task.status)
  })
  const base = hasActive ? current : []
  store.aiTracking[page] = [...new Set([...base, ...tasks.map(task => task.id)])]
}

function scheduleAiRefresh(page) {
  window.clearTimeout(aiRefreshTimers[page])
  aiRefreshTimers[page] = window.setTimeout(async () => {
    const loaders = page === 'workbench'
      ? [['岗位', refreshJobs], ['招呼语', refreshGreetings], ['总览', refreshDashboard]]
      : [['岗位', refreshJobs], ['总览', refreshDashboard], ['数据分析', refreshAnalytics]]
    await runLoaders(loaders)
  }, 180)
}

function applyAiSnapshot(page, snapshot, notify = true) {
  if (!snapshot || !Array.isArray(snapshot.tasks)) return
  const previous = new Map((store.ai[page].tasks || []).map(task => [task.id, task.status]))
  store.ai[page] = snapshot
  const activeForResume = snapshot.tasks.filter(task => AI_ACTIVE.has(task.status)
    && Number(task.resume_id) === Number(store.selectedResumeId))
  if (!store.aiTracking[page].length && activeForResume.length) trackAiTasks(page, activeForResume)
  let shouldRefresh = false
  for (const task of snapshot.tasks) {
    const changedToTerminal = AI_TERMINAL.has(task.status) && previous.get(task.id) !== task.status
    if (changedToTerminal && (task.status === 'succeeded' || task.status === 'cancelled')) {
      shouldRefresh = true
      const job = store.jobs.find(item => item.job_key === task.job_key)
      if (job && (!job.actionErrorType || job.actionErrorType === task.kind)) {
        job.actionError = ''
        job.actionErrorType = ''
      }
    }
    if (notify && task.status === 'failed' && !seenAiFailures.has(task.id)) {
      seenAiFailures.add(task.id)
      const job = store.jobs.find(item => item.job_key === task.job_key)
      if (job) {
        job.actionError = task.error || '生成失败'
        job.actionErrorType = task.kind
      }
      const label = task.kind === 'greeting' ? '招呼语' : task.kind === 'analysis' ? '分析' : '评分'
      showToast(`${label}生成失败：${task.error || '未知错误'}，请重新生成`, 'bad')
    }
  }
  if (shouldRefresh) scheduleAiRefresh(page)
}

function aiTaskFor(page, jobKey, kinds, activeOnly = true) {
  const wanted = Array.isArray(kinds) ? kinds : [kinds]
  const tasks = [...(store.ai[page].tasks || [])].reverse()
  return tasks.find(task => task.job_key === jobKey
    && Number(task.resume_id) === Number(store.selectedResumeId)
    && (!wanted[0] || wanted.includes(task.kind))
    && (!activeOnly || AI_ACTIVE.has(task.status))) || null
}

function scoreLabel(job, field, page) {
  const kinds = page === 'jobs' ? ['score'] : ['analysis']
  return aiTaskFor(page, job.job_key, kinds) ? '评分中' : (job[field] ?? '未评分')
}

function aiSummary(page) {
  const all = store.ai[page].tasks || []
  const tracked = new Set(store.aiTracking[page] || [])
  const tasks = all.filter(task => tracked.has(task.id))
  const active = tasks.filter(task => AI_ACTIVE.has(task.status))
  const completed = tasks.filter(task => AI_TERMINAL.has(task.status)).length
  const failures = tasks.filter(task => task.status === 'failed').length
  const cancelling = active.length > 0 && active.every(task => task.cancel_requested)
  const kinds = [...new Set(active.map(task => task.kind))]
  const labels = kinds.map(kind => kind === 'greeting' ? '正在生成招呼语' : kind === 'analysis' ? '正在生成分析' : '正在生成岗位与匹配度评分')
  const progressUnits = tasks.reduce((total, task) => {
    if (AI_TERMINAL.has(task.status)) return total + 1
    const value = Number(task.progress)
    return total + (Number.isFinite(value) ? Math.max(0, Math.min(0.99, value)) : 0)
  }, 0)
  const taskEtas = active.map(task => etaValue(task.eta_seconds))
    .filter(value => value !== null)
  const pageEta = etaValue(store.ai[page].progress && store.ai[page].progress.eta_seconds)
  return {
    visible: tasks.length > 0, running: active.length > 0, cancelling,
    total: tasks.length, completed,
    failures, percent: tasks.length ? Math.round(progressUnits / tasks.length * 100) : 0,
    label: labels.join(' / ') || (failures ? '生成完成，部分任务失败' : '最近生成任务已完成'),
    effective: Number(store.ai[page].effective_concurrency || 1),
    max: Number(store.ai[page].max_concurrency || 5),
    etaSeconds: taskEtas.length ? Math.max(...taskEtas) : pageEta,
  }
}

function taskProgressPercent(task) {
  if (!task) return 0
  if (AI_TERMINAL.has(task.status)) return 100
  const value = Number(task.progress)
  if (!Number.isFinite(value)) return task.status === 'running' || task.status === 'retrying' ? 5 : 0
  return Math.max(0, Math.min(99, Math.round(value * 100)))
}

function taskEtaSeconds(task, page) {
  const direct = etaValue(task && task.eta_seconds)
  if (direct !== null) return direct
  return etaValue(store.ai[page] && store.ai[page].progress && store.ai[page].progress.eta_seconds)
}

async function enqueueAi(page, kind, jobKeys, force = false) {
  const result = await apiClient.ai.enqueue(page, {
    kind, job_keys: jobKeys, resume_id: store.selectedResumeId, force,
  })
  trackAiTasks(page, result.tasks || [])
  applyAiSnapshot(page, result.snapshot || store.ai[page], false)
  return result
}

async function cancelAiTasks(page) {
  try {
    const result = await apiClient.ai.cancel(page, { resume_id: store.selectedResumeId })
    applyAiSnapshot(page, result.snapshot || store.ai[page], false)
    showToast(result.cancelled
      ? `已请求取消 ${result.cancelled} 个生成任务，已完成结果会保留`
      : '当前没有可取消的生成任务', 'info')
  } catch (error) {
    showToast(`取消生成任务失败：${error.message}`, 'bad')
  }
}

async function cancelAiTask(page, task) {
  if (!task) return
  try {
    const result = await apiClient.ai.cancel(page, { task_id: task.id })
    applyAiSnapshot(page, result.snapshot || store.ai[page], false)
    showToast(result.cancelled
      ? '已请求取消当前任务，已完成结果会保留'
      : '任务已结束，无需取消', 'info')
  } catch (error) {
    showToast(`取消任务失败：${error.message}`, 'bad')
  }
}

async function runLoaders(loaders) {
  const results = await Promise.allSettled(loaders.map(item => item[1]()))
  return results.flatMap((result, index) => result.status === 'rejected'
    ? [`${loaders[index][0]}：${result.reason && result.reason.message || '加载失败'}`] : [])
}
async function bootstrap() {
  store.ui.bootstrapLoading = true
  store.ui.bootstrapError = ''
  const errors = await runLoaders([
    ['设置', refreshSettings], ['简历', refreshResumes], ['采集选项', refreshCollectOptions],
  ])
  errors.push(...await runLoaders([
    ['总览', refreshDashboard], ['采集历史', refreshRuns], ['采集状态', refreshCollectStatus],
    ['收藏同步状态', refreshFavoriteStatus], ['账号', refreshAccounts], ['岗位', refreshJobs],
    ['招呼语', refreshGreetings], ['面试记录', refreshInterviews], ['数据分析', refreshAnalytics],
    ['采集配置', refreshCollectConfig],
    ['岗位生成任务', () => refreshAiTasks('jobs', false)],
    ['工作台生成任务', () => refreshAiTasks('workbench', false)],
  ]))
  store.ui.bootstrapError = errors.join('；')
  store.ui.bootstrapLoading = false
  store.ui.lastUpdated = new Date().toISOString()
  observedRuntimeActive = store.runs.some(run => run.status === 'running' || run.status === 'paused')
}
let observedRuntimeActive = false
async function refreshRuntime() {
  const errors = await runLoaders([
    ['采集状态', refreshCollectStatus], ['采集历史', refreshRuns],
    ['收藏同步状态', refreshFavoriteStatus], ['发送状态', async () => { store.sendStatus = await apiClient.greetings.sendStatus() }],
    ['岗位生成任务', () => refreshAiTasks('jobs')],
    ['工作台生成任务', () => refreshAiTasks('workbench')],
  ])
  if (errors.length) store.ui.bootstrapError = errors.join('；')
  const active = store.runs.some(run => run.status === 'running' || run.status === 'paused')
  if (observedRuntimeActive && !active) {
    await runLoaders([
      ['总览', refreshDashboard], ['岗位', refreshJobs], ['招呼语', refreshGreetings],
      ['数据分析', refreshAnalytics], ['账号', refreshAccounts],
    ])
  }
  observedRuntimeActive = active
}

const aiStreams = []
function connectAiStreams() {
  if (typeof EventSource === 'undefined' || aiStreams.length) return
  for (const page of ['jobs', 'workbench']) {
    const source = new EventSource(apiClient.ai.streamUrl(page))
    source.addEventListener('snapshot', event => {
      try { applyAiSnapshot(page, JSON.parse(event.data), true) }
      catch (_) { /* 轮询会继续兜底刷新状态。 */ }
    })
    aiStreams.push(source)
  }
}

function closeAiStreams() {
  aiStreams.splice(0).forEach(source => source.close())
}

let toastTimer = null
function showToast(message, tone = 'ok') {
  store.ui.toast = { message, tone }
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { store.ui.toast = null }, 3200)
}
function requestConfirm({ title, message, confirmText = '确认', tone = 'primary' }) {
  return new Promise(resolve => {
    store.ui.confirm = { title, message, confirmText, tone, resolve }
  })
}
function settleConfirm(result) {
  const dialog = store.ui.confirm
  store.ui.confirm = null
  if (dialog) dialog.resolve(result)
}

// -- 岗位标签、排序与文案生成 -------------------------------------------
function deriveJobTags(job) {
  const company = String(job.company || '').trim()
  const tags = []
  if (job.effective_headhunter || !company || company.includes('某')) tags.push({ key: 'headhunter', label: '猎头', tone: 'warn' })
  if (job.is_outsourcing) tags.push({ key: 'outsourcing', label: '外包', tone: 'bad' })
  if (job.has_alternating_weekend) tags.push({ key: 'alternating_weekend', label: '大小周', tone: 'warn' })
  else if (job.has_eight_hour_weekend) tags.push({ key: 'eight_hour_weekend', label: '八小时双休', tone: 'ok' })
  else if (job.has_weekend) tags.push({ key: 'weekend', label: '双休', tone: 'ok' })
  if (job.has_benefits) tags.push({ key: 'benefits', label: '福利', tone: 'blue' })
  return tags
}
function workflowLabel(job) {
  if (job.offered) return 'OFFER'
  if (job.interviewed) return '已面试'
  if (job.applied) return '已投递'
  if (job.contacted) return '已打招呼'
  return job.favorite ? '待开始' : ''
}

// -- Hash 路由 -----------------------------------------------------------
const nav = [
  ['/dashboard', '总览看板'],
  ['/profile', '简历档案'],
  ['/collect', '采集中心'],
  ['/analytics', '数据分析'],
  ['/jobs', '岗位列表'],
  ['/jobcard', '收藏工作台'],
  ['/accounts', '账号管理'],
  ['/settings', '设置'],
  ['/guide', '使用指南'],
]
const ROUTE_ALIASES = { '/greetings': '/jobcard', '/interview': '/jobcard' }
const VALID_ROUTES = new Set(nav.map(([path]) => path))
function parseHashRoute() {
  const raw = location.hash.slice(1) || '/dashboard'
  const [rawPath, rawQuery = ''] = raw.split('?', 2)
  const path = ROUTE_ALIASES[rawPath] || (VALID_ROUTES.has(rawPath) ? rawPath : '/dashboard')
  const params = new URLSearchParams(rawQuery)
  if (path !== rawPath) history.replaceState(
    null, '', `#${path}${rawQuery ? `?${rawQuery}` : ''}`)
  return { path, jobKey: params.get('job') || '' }
}
const initialRoute = parseHashRoute()
const route = ref(initialRoute.path)
const routeJobKey = ref(initialRoute.jobKey)
window.addEventListener('hashchange', () => {
  const next = parseHashRoute()
  route.value = next.path
  routeJobKey.value = next.jobKey
})
function jobCardHref(job) {
  return `#/jobcard?job=${encodeURIComponent(job && job.job_key || '')}`
}

// -- 新手引导共享状态 -----------------------------------------------------
// 使用指南页 / 首访弹窗跨视图请求打开求职作战向导：先跳转到总览看板，
// DashboardView 挂载时消费挂起意图，避免路由切换后 watch 错过自增信号
const wizardRequest = ref(0)
const wizardIntentPending = { value: false }
function requestWizardOpen() {
  wizardIntentPending.value = true
  wizardRequest.value++
  location.hash = '#/dashboard'
}
// 启动须知弹窗：每次启动默认显示；用户勾选「不再显示」并确认后才永久关闭
const ONBOARDING_KEY = 'bc-onboarding-v1'
function markOnboardingDone() {
  localStorage.setItem(ONBOARDING_KEY, '1')
}

// -- 通用图表 -----------------------------------------------------------
const SvgBars = {
  // clickable 时整行可点：用于分析页「点击条目加入/移除筛选」的下钻联动。
  props: { items: Array, color: String, clickable: Boolean },
  emits: ['pick'],
  setup(props, { emit }) {
    const rows = computed(() => (props.items || []).slice(0, 10))
    const max = computed(() => Math.max(1, ...rows.value.map(item => Number(item.value || 0))))
    function pick(item) {
      if (props.clickable) emit('pick', item)
    }
    return { rows, max, pick }
  },
  template: `
    <div class="bar-chart">
      <div v-for="item in rows" :key="item.label" class="bar-row" :class="{clickable}"
           :role="clickable ? 'button' : null" :tabindex="clickable ? 0 : null"
           @click="pick(item)" @keydown.enter.prevent="pick(item)" @keydown.space.prevent="pick(item)">
        <span :title="clickable ? item.label + '（点击筛选）' : item.label">{{item.label}}</span>
        <svg viewBox="0 0 100 12" preserveAspectRatio="none" role="img" :aria-label="item.label + ' ' + item.value">
          <rect class="bar-bg" width="100" height="12" rx="2"></rect>
          <rect :width="item.value > 0 ? Math.max(1,item.value/max*100) : 0" height="12" rx="2" :style="{fill:color || 'var(--accent)'}"></rect>
        </svg>
        <b>{{item.value}}</b>
      </div>
      <div v-if="!rows.length" class="empty compact">暂无可用数据</div>
    </div>`,
}

// -- 页面：总览看板 -----------------------------------------------------
const DashboardView = {
  setup() {
    const activeJobs = computed(() => jobsFromEnabledRuns())
    const favorites = computed(() => activeJobs.value.filter(job => job.favorite))
    const staleJobs = computed(() => store.dashboard && store.dashboard.system
      && store.dashboard.system.collection && store.dashboard.system.collection.stale ? activeJobs.value : [])
    const metrics = computed(() => {
      const data = store.dashboard && store.dashboard.metrics || {}
      const jobs = data.jobs || {}
      const favoriteMetrics = data.favorites || {}
      const greetings = data.greetings || {}
      const applications = data.applications || {}
      return [
        { label: '当前岗位', value: Number(jobs.active ?? activeJobs.value.length), sub: `${Number(jobs.today || 0)} 个今日新增`, href: '#/jobs', tone: 'blue' },
        { label: '收藏岗位', value: Number(favoriteMetrics.total ?? favorites.value.length), sub: `${Number(favoriteMetrics.last_7_days || 0)} 个近 7 天收藏`, href: '#/jobcard', tone: 'green' },
        { label: '已打招呼', value: Number(greetings.total || 0), sub: `${Number(greetings.today || 0)} 个今日确认`, href: '#/jobcard', tone: 'amber' },
        { label: '已投递', value: Number(applications.total || 0), sub: `${Number(applications.last_7_days || 0)} 个近 7 天确认`, href: '#/jobcard', tone: 'red' },
      ]
    })
    const latestRun = computed(() => store.runs[0])
    const priorityJobs = computed(() => [...favorites.value].sort((a, b) => b.match_score - a.match_score).slice(0, 5))
    const runningCount = computed(() => store.runs.filter(run => run.status === 'running' || run.status === 'paused').length)
    const guide = reactive({
      open: false, step: 1, busy: '', resumeMode: 'existing', resumeId: store.selectedResumeId,
      resumeName: '新的求职简历', resumeBody: '# 我的简历\n\n在这里填写 Markdown 简历正文。', synced: false,
      llmBaseUrl: store.settings.llmBaseUrl, llmApiKey: store.settings.llmApiKey,
      llmModel: store.settings.llmModel, llmTested: false, llmAvailable: isLlmConfigured(),
      dualAccount: store.settings.dualAccount,
    })
    let guidePlanController = null
    const guidePlan = reactive(createPlan())
    const guideCityQuery = ref('')
    const guideCitySuggestions = computed(() => matchingCities(guideCityQuery.value, guidePlan.cities))
    const guideLlmConfigured = computed(() => isLlmConfigured({
      llmBaseUrl: guide.llmBaseUrl, llmApiKey: guide.llmApiKey, llmModel: guide.llmModel,
    }))
    const llmReady = computed(() => isLlmConfigured())
    const communicationAccount = computed(() => store.accounts.find(account => account.key === 'account_a')
      || { key: 'account_a', label: '沟通号', running: false, loggedIn: false, hint: '账号状态尚未加载' })
    const guideAccounts = computed(() => store.accounts.filter(account =>
      account.key === 'account_a' || guide.dualAccount).sort((a, b) =>
      (a.key === 'collect' ? 0 : 1) - (b.key === 'collect' ? 0 : 1)))
    function openGuide() {
      if (guidePlanController) guidePlanController.abort()
      Object.assign(guide, {
        open: true, step: 1, busy: '', resumeMode: store.resumes.length ? 'existing' : 'new',
        resumeId: store.selectedResumeId, resumeName: '新的求职简历',
        resumeBody: '# 我的简历\n\n在这里填写 Markdown 简历正文。', synced: false,
        llmBaseUrl: store.settings.llmBaseUrl, llmApiKey: store.settings.llmApiKey,
        llmModel: store.settings.llmModel, llmTested: false, llmAvailable: isLlmConfigured(),
        dualAccount: store.settings.dualAccount,
      })
      Object.assign(guidePlan, createPlan())
      guideCityQuery.value = ''
    }
    function closeGuide() {
      if (guidePlanController) guidePlanController.abort()
      guidePlanController = null
      guide.busy = ''
      guide.open = false
    }
    function consumeWizardRequest() {
      if (!wizardIntentPending.value || guide.open) return
      wizardIntentPending.value = false
      openGuide()
    }
    watch(wizardRequest, consumeWizardRequest)
    // 使用指南等页面跨视图跳转过来时，组件刚挂载，需主动消费一次挂起的打开请求
    consumeWizardRequest()
    async function finishResumeStep() {
      if (guide.resumeMode === 'new') {
        if (!guide.resumeName.trim() || !guide.resumeBody.trim()) {
          showToast('请填写简历名称和正文', 'warn')
          return
        }
        guide.busy = 'resume'
        try {
          const created = await apiClient.resumes.create({
            name: guide.resumeName.trim(), resume_text: guide.resumeBody,
            make_default: store.resumes.length === 0,
          })
          await refreshResumes()
          guide.resumeId = created.id
        } catch (error) {
          showToast(`简历创建失败：${error.message}`, 'bad')
          return
        } finally { guide.busy = '' }
      }
      if (!guide.resumeId) { showToast('请选择一份简历', 'warn'); return }
      store.selectedResumeId = Number(guide.resumeId)
      guidePlan.resumeId = store.selectedResumeId
      guidePlan.name = planNamePreview(guidePlan.resumeId)
      guide.step = 2
    }
    async function testGuideLlm() {
      if (!guideLlmConfigured.value) {
        guide.llmTested = false
        guide.llmAvailable = false
        showToast('请完整填写 Base URL、API Key 和模型', 'warn')
        return
      }
      guide.busy = 'llm'
      try {
        const result = await apiClient.settings.testLlm({
          base_url: guide.llmBaseUrl.trim(), api_key: guide.llmApiKey.trim(), model: guide.llmModel.trim(),
        })
        guide.llmTested = Boolean(result && result.ok !== false)
        guide.llmAvailable = guide.llmTested
        showToast(`LLM 连接成功 · ${result.model || guide.llmModel} · ${result.latency_ms || 0} ms`)
      } catch (error) {
        guide.llmTested = false
        guide.llmAvailable = false
        showToast(`LLM 连接失败：${error.message}`, 'bad')
      } finally { guide.busy = '' }
    }
    async function saveGuideLlm() {
      Object.assign(store.settings, {
        llmBaseUrl: guide.llmBaseUrl.trim(), llmApiKey: guide.llmApiKey.trim(), llmModel: guide.llmModel.trim(),
      })
      guide.llmAvailable = isLlmConfigured()
      if (!guide.llmAvailable) guidePlan.mode = 'manual'
      if (!guide.llmAvailable) { guide.step = 3; return }
      guide.busy = 'save-llm'
      try {
        await apiClient.settings.save(settingsToApi(store.settings))
        await refreshSettings()
        guide.step = 3
      } catch (error) {
        showToast(`LLM 配置保存失败：${error.message}`, 'bad')
      } finally { guide.busy = '' }
    }
    async function setGuideDualAccount(event) {
      const enabled = event.target.checked
      guide.busy = 'account-mode'
      try {
        await apiClient.settings.save({ dual_account_enabled: enabled })
        guide.dualAccount = enabled
        store.settings.dualAccount = enabled
        await Promise.all([refreshSettings(), refreshAccounts()])
        showToast(enabled ? '已开启双账号隔离模式' : '已切换为单账号模式', 'info')
      } catch (error) {
        event.target.checked = guide.dualAccount
        showToast(`账号模式切换失败：${error.message}`, 'bad')
      } finally { guide.busy = '' }
    }
    async function guideAccountAction(account, action) {
      guide.busy = `${account.key}-${action}`
      try {
        let result
        if (action === 'launch') result = await apiClient.accounts.launch(account.key)
        else if (action === 'login') result = await apiClient.accounts.login(account.key)
        else if (action === 'check') result = await apiClient.accounts.check(account.key)
        else result = await apiClient.accounts.stop(account.key)
        await refreshAccounts()
        if (action === 'check') {
          showToast(result.logged_in === true ? `${account.label}登录状态已确认`
            : (result.hint || `${account.label}登录状态尚未确认`), result.logged_in === true ? 'ok' : 'warn')
        } else if (result && result.ok === false) {
          showToast(`${account.label}操作失败：${result.error || '未知原因'}`, 'bad')
        } else {
          const labels = { launch: '已启动', login: '已打开登录页', stop: '已停止' }
          showToast(`${account.label}${labels[action]}`, action === 'stop' ? 'info' : 'ok')
        }
      } catch (error) {
        showToast(`${account.label}操作失败：${error.message}`, 'bad')
      } finally { guide.busy = '' }
    }
    async function guideSyncFavorites() {
      if (!communicationAccount.value.loggedIn) {
        showToast('请先完成 BOSS 登录', 'warn')
        return
      }
      guide.busy = 'sync'
      try {
        const result = await apiClient.favorites.start()
        guide.synced = Boolean(result && result.ok !== false)
        await Promise.allSettled([refreshFavoriteStatus(), refreshRuns()])
        showToast('BOSS 收藏同步已开始，并记为一次采集')
      } catch (error) {
        showToast(`收藏同步启动失败：${error.message}`, 'bad')
      } finally { guide.busy = '' }
    }
    function addGuideCity(city = guideCityQuery.value) {
      if (appendCity(guidePlan.cities, city)) guideCityQuery.value = ''
    }
    function removeGuideCity(city) {
      guidePlan.cities.splice(guidePlan.cities.indexOf(city), 1)
    }
    async function guideGeneratePlan() {
      if (!llmReady.value) {
        showToast('LLM 未配置，AI 生成采集计划不可用', 'warn')
        return
      }
      if (guidePlanController) guidePlanController.abort()
      guidePlanController = new AbortController()
      const controller = guidePlanController
      guide.busy = 'plan'
      try {
        await generateStrategyStreaming(guidePlan, guidePlan.resumeId, controller.signal)
        showToast('AI 采集计划已生成，可继续修改')
      } catch (error) {
        if (error.name !== 'AbortError') showToast(`AI 采集计划生成失败：${error.message}`, 'bad')
      } finally {
        if (guidePlanController === controller) {
          guidePlanController = null
          guide.busy = ''
        }
      }
    }
    function chooseGuidePlanMode(mode) {
      if (mode === 'manual' && guidePlanController) guidePlanController.abort()
      guidePlan.mode = mode
      guidePlan.generated = mode === 'manual'
      if (mode === 'manual') guidePlan.streamText = ''
    }
    function reviewGuidePlan() {
      const keywords = guidePlan.keywords.split(/[\n,，]+/).map(value => value.trim()).filter(Boolean)
      if (!keywords.length || !guidePlan.cities.length) {
        showToast('请填写关键词并至少选择一个城市', 'warn')
        return
      }
      guide.step = 5
    }
    async function finishGuide() {
      guide.busy = 'collect'
      try {
        await saveAndStartPlan(guidePlan)
        guide.open = false
        location.hash = '#/dashboard'
        showToast('向导已完成，采集计划开始执行')
      } catch (error) {
        showToast(`采集启动失败：${error.message}`, 'bad')
      } finally { guide.busy = '' }
    }
    return {
      store, metrics, latestRun, priorityJobs, runningCount, staleJobs, guide, guidePlan,
      communicationAccount, guideAccounts, guideCityQuery, guideCitySuggestions, guideLlmConfigured, llmReady,
      percentOf, shortTime, formatEta, runProgressSummary, deriveJobTags, jobCardHref, runStatusLabel,
      openGuide, closeGuide, finishResumeStep, testGuideLlm, saveGuideLlm,
      setGuideDualAccount, guideAccountAction, guideSyncFavorites,
      addGuideCity, removeGuideCity, guideGeneratePlan, chooseGuidePlanMode, reviewGuidePlan, finishGuide,
    }
  },
  template: `
  <div>
    <header class="page-head">
      <div><div class="eyebrow">工作概览</div><h1>总览看板</h1><p>求职进度、采集状态与优先岗位</p></div>
      <div class="row"><a class="button" href="#/guide">使用指南</a><button @click="openGuide">向导</button><a class="button primary" href="#/collect">新建采集计划</a></div>
    </header>
    <div v-if="staleJobs.length" class="freshness-warning"><div><b>采集数据需要更新</b><span>{{staleJobs.length}} 个岗位已超过 3 天没有更新，建议重新采集以确认岗位状态。</span></div><a class="button" href="#/collect">更新采集数据</a></div>
    <div class="metric-grid">
      <a v-for="item in metrics" :key="item.label" class="metric" :class="'tone-' + item.tone" :href="item.href">
        <span>{{item.label}}</span><strong>{{item.value}}</strong><small>{{item.sub}}</small>
      </a>
    </div>
    <div class="dashboard-grid">
      <section class="surface-panel run-overview">
        <div class="section-title"><div><h2>最近采集</h2><p>{{runningCount ? runningCount + ' 个计划处理中' : '当前没有运行中的计划'}}</p></div><a href="#/collect">查看全部</a></div>
        <template v-if="latestRun">
          <div class="run-title"><div><b>{{latestRun.name}}</b><span>{{latestRun.source}} · {{shortTime(latestRun.startedAt)}}</span></div><span class="status-badge" :class="latestRun.status">{{runStatusLabel(latestRun.status)}}</span></div>
          <div class="progress-copy"><span>{{runProgressSummary(latestRun)}}</span><b>{{percentOf(latestRun)}}%</b></div>
          <div class="progress"><i :style="{width:percentOf(latestRun)+'%'}"></i></div>
          <small v-if="latestRun.status==='running'" class="run-eta">{{formatEta(latestRun.etaSeconds)}}</small>
        </template>
      </section>
      <section class="surface-panel account-overview">
        <div class="section-title"><div><h2>账号状态</h2><p>{{store.settings.dualAccount ? '双账号隔离模式' : '单账号模式'}}</p></div><a href="#/accounts">管理</a></div>
        <div v-for="account in store.accounts" :key="account.key" class="account-line">
          <span class="status-dot" :class="account.running?'ok':'neutral'"></span>
          <div><b>{{account.label}}</b><small>{{account.loggedIn?'已登录':'待登录'}} · {{account.running?'运行中':'未启动'}}</small></div>
          <span>{{account.port}}</span>
        </div>
      </section>
    </div>
    <section class="section-block">
      <div class="section-title"><div><h2>优先处理</h2><p>按当前简历匹配度排序的收藏岗位</p></div><a href="#/jobcard">进入收藏工作台</a></div>
      <div class="priority-list">
        <a v-for="job in priorityJobs" :key="job.job_key" :href="jobCardHref(job)" class="priority-row">
          <div><b>{{job.title}}</b><span>{{job.company || '公司信息保密'}} · {{job.salary}}</span></div>
          <div class="tag-line"><span v-for="tag in deriveJobTags(job)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span></div>
          <strong>{{job.match_score ?? '—'}}</strong><small>匹配度</small>
        </a>
      </div>
    </section>

    <div v-if="guide.open" class="guide-overlay" role="dialog" aria-modal="true" aria-label="首次使用向导">
      <header><div><span class="eyebrow">开始使用</span><h2>求职作战向导</h2></div><button aria-label="关闭向导" title="关闭" @click="closeGuide">×</button></header>
      <div class="guide-shell">
        <ol class="guide-stepper">
          <li v-for="item in [{n:1,t:'添加简历'},{n:2,t:'LLM 配置'},{n:3,t:'登录与收藏'},{n:4,t:'采集计划'},{n:5,t:'确认开始'}]" :key="item.n" :class="{active:guide.step===item.n,done:guide.step>item.n}"><span>{{item.n}}</span><b>{{item.t}}</b></li>
        </ol>

        <section v-if="guide.step===1" class="guide-content">
          <div><h3>添加本次求职使用的简历</h3><p>后续采集计划、评分和招呼语都以这份简历为准。</p></div>
          <div class="segmented"><button :class="{active:guide.resumeMode==='existing'}" @click="guide.resumeMode='existing'">使用已有简历</button><button :class="{active:guide.resumeMode==='new'}" @click="guide.resumeMode='new'">新建简历</button></div>
          <label v-if="guide.resumeMode==='existing'">简历档案<select v-model.number="guide.resumeId"><option v-for="resume in store.resumes" :key="resume.id" :value="resume.id">{{resume.name}}</option></select></label>
          <template v-else><label>档案名称<input v-model="guide.resumeName"></label><label>简历正文<textarea v-model="guide.resumeBody" class="guide-resume"></textarea></label></template>
          <div class="guide-actions"><span></span><button class="primary" :disabled="!!guide.busy" @click="finishResumeStep">{{guide.busy==='resume'?'保存中…':'下一步'}}</button></div>
        </section>

        <section v-if="guide.step===2" class="guide-content">
          <div><h3>配置并测试 LLM 服务</h3><p>用于 AI 采集计划、岗位评分、匹配度评分、AI 招呼语和模拟面试。</p></div>
          <div v-if="!guideLlmConfigured" class="llm-warning"><b>LLM 尚未配置</b><span>跳过后，AI 生成采集计划、岗位评分、匹配度评分、AI 招呼语和模拟面试将不可用，手动采集仍可使用。</span></div>
          <div v-else-if="guide.llmTested" class="llm-success"><b>连接测试通过</b><span>{{guide.llmModel}} 可用</span></div>
          <div class="form-grid three"><label>Base URL<input v-model="guide.llmBaseUrl" placeholder="https://api.example.com/v1" @input="guide.llmTested=false"></label><label>API Key<input v-model="guide.llmApiKey" type="password" placeholder="sk-…" @input="guide.llmTested=false"></label><label>模型<input v-model="guide.llmModel" placeholder="model-name" @input="guide.llmTested=false"></label></div>
          <div class="guide-actions"><button @click="guide.step=1">上一步</button><div class="row"><button v-if="!guideLlmConfigured" :disabled="!!guide.busy" @click="saveGuideLlm">暂不配置，继续</button><button :disabled="!!guide.busy || !guideLlmConfigured" @click="testGuideLlm">{{guide.busy==='llm'?'测试中…':'测试连接'}}</button><button v-if="guideLlmConfigured" class="primary" :disabled="!!guide.busy || !guide.llmTested" @click="saveGuideLlm">{{guide.busy==='save-llm'?'保存中…':'保存并下一步'}}</button></div></div>
        </section>

        <section v-if="guide.step===3" class="guide-content">
          <div><h3>配置账号模式并完成登录</h3><p>双账号模式下采集号负责只读采集，沟通号负责受护栏保护的沟通操作。</p></div>
          <div class="guide-account-mode"><div><b>双账号隔离模式</b><span>{{guide.dualAccount?'采集号与沟通号分别登录':'采集与沟通统一使用沟通号'}}</span></div><label class="switch"><input type="checkbox" :checked="guide.dualAccount" :disabled="!!guide.busy" @change="setGuideDualAccount"><span class="switch-track"><span class="switch-thumb"></span></span><span>{{guide.dualAccount?'已开启':'已关闭'}}</span></label></div>
          <div class="guide-account-grid"><article v-for="account in guideAccounts" :key="account.key" class="guide-account-card"><header><span class="account-icon">{{account.label.slice(0,1)}}</span><div><b>{{account.label}}</b><small>{{account.roles.join(' / ') || account.description}}</small></div><span class="status-badge" :class="account.loggedIn===true?'completed':account.running?'running':'paused'">{{account.loggedIn===true?'已登录':account.running?'待检测':'未启动'}}</span></header><footer><button :disabled="!!guide.busy" @click="guideAccountAction(account,'launch')">启动</button><button :disabled="!!guide.busy" @click="guideAccountAction(account,'login')">打开登录页</button><button :disabled="!!guide.busy" @click="guideAccountAction(account,'check')">{{guide.busy===account.key+'-check'?'检测中…':'检测登录态'}}</button><button :disabled="!!guide.busy || !account.running" @click="guideAccountAction(account,'stop')">停止</button></footer></article></div>
          <div class="guide-sync"><div><b>同步 BOSS 收藏岗位 <span class="optional-mark">可选</span></b><span>{{guide.synced?'本次同步已加入采集历史':'读取“感兴趣”岗位并加入本地岗位池，也可稍后处理'}}</span></div><button class="primary" :disabled="!!guide.busy || guide.synced" @click="guideSyncFavorites">{{guide.busy==='sync'?'同步中…':guide.synced?'同步完成':'开始同步'}}</button></div>
          <div class="guide-actions"><button @click="guide.step=2">上一步</button><button class="primary" @click="guide.step=4">{{guide.synced?'下一步':'跳过，下一步'}}</button></div>
        </section>

        <section v-if="guide.step===4" class="guide-content">
          <div><h3>设置新的采集计划</h3><p>配置主动搜索范围，稍后可在采集中心继续调整。</p></div>
          <div class="segmented"><button :class="{active:guidePlan.mode==='manual'}" @click="chooseGuidePlanMode('manual')">手动配置</button><button :class="{active:guidePlan.mode==='auto'}" @click="chooseGuidePlanMode('auto')">AI 生成</button></div>
          <div v-if="guidePlan.mode==='auto'" class="guide-ai-plan"><div><b>{{llmReady?'根据当前简历生成采集配置':'LLM 未配置，AI 生成不可用'}}</b><span>{{llmReady?'AI 只生成关键词与城市，不附加额外筛选条件':'返回上一步配置 LLM，或切换为手动配置'}}</span></div><button class="primary" :disabled="!!guide.busy || !llmReady" @click="guideGeneratePlan">{{guide.busy==='plan'?'生成中…':guidePlan.generated?'重新生成':'AI 生成采集计划'}}</button></div>
          <pre v-if="guidePlan.mode==='auto' && guidePlan.streamText" class="strategy-stream" :class="{done:guidePlan.generated}">{{guidePlan.streamText}}</pre>
          <div class="form-grid two"><div class="plan-name-preview"><span>计划名称</span><b>{{guidePlan.name}}</b><small>启动后自动替换为实际流水号</small></div><label>每组页数<input type="number" min="1" max="10" v-model.number="guidePlan.pages"></label></div>
          <label>搜索关键词<textarea class="short" v-model="guidePlan.keywords" placeholder="每行一个关键词"></textarea></label>
          <div class="field-group"><span class="field-label">城市范围</span><div class="city-picker"><div class="city-input-row"><input v-model="guideCityQuery" placeholder="输入城市关键词" @keyup.enter="addGuideCity()"><button @click="addGuideCity()">添加</button></div><div v-if="guideCitySuggestions.length" class="city-suggestions"><button v-for="city in guideCitySuggestions" :key="city" @click="addGuideCity(city)">{{city}}</button></div><div class="city-tags"><span v-for="city in guidePlan.cities" :key="city" class="city-tag">{{city}}<button :aria-label="'移除城市 ' + city" @click="removeGuideCity(city)">×</button></span></div></div></div>
          <div class="field-group"><span class="field-label">采集详细程度</span><div class="segmented"><button :class="{active:!guidePlan.fetchDetails}" @click="guidePlan.fetchDetails=false">仅岗位列表</button><button :class="{active:guidePlan.fetchDetails}" @click="guidePlan.fetchDetails=true">采集完整JD（推荐）</button></div></div>
          <div class="guide-actions"><button @click="guide.step=3">上一步</button><button class="primary" :disabled="guidePlan.mode==='auto' && !guidePlan.generated" @click="reviewGuidePlan">检查计划</button></div>
        </section>

        <section v-if="guide.step===5" class="guide-content guide-finish">
          <span class="finish-mark">✓</span><h3>准备开始采集</h3><p>基础配置已确认，采集开始后可在左侧查看总进度。</p>
          <dl><div><dt>当前简历</dt><dd>{{store.resumes.find(item=>item.id===store.selectedResumeId)?.name}}</dd></div><div><dt>LLM</dt><dd>{{llmReady?guide.llmModel+' · 可用':'未配置 · AI 功能不可用'}}</dd></div><div><dt>收藏同步</dt><dd>{{guide.synced?'已作为采集记录应用':'已跳过，可稍后同步'}}</dd></div><div><dt>采集计划</dt><dd>{{guidePlan.name}} · {{guidePlan.cities.join('、')}}</dd></div></dl>
          <div class="guide-actions"><button @click="guide.step=4">返回修改</button><button class="primary" :disabled="!!guide.busy" @click="finishGuide">{{guide.busy==='collect'?'启动中…':'开始采集并返回总览'}}</button></div>
        </section>
      </div>
    </div>
  </div>`,
}

// -- 页面：简历档案 -----------------------------------------------------
const ProfileView = {
  setup() {
    const selectedId = ref(store.selectedResumeId)
    const editing = ref(false)
    const draft = reactive({ name: '', body: '' })
    const selected = computed(() => store.resumes.find(item => item.id === selectedId.value) || null)
    const rendered = computed(() => renderMarkdown(selected.value ? selected.value.body : ''))
    function loadDraft() {
      if (!selected.value) return
      draft.name = selected.value.name
      draft.body = selected.value.body
    }
    function selectResume(id) {
      selectedId.value = id
      store.selectedResumeId = id
      editing.value = false
      loadDraft()
    }
    function edit() { loadDraft(); editing.value = true }
    function cancelEdit() { loadDraft(); editing.value = false }
    async function save() {
      if (!draft.name.trim() || !draft.body.trim()) {
        showToast('请填写档案名称和简历正文', 'warn')
        return
      }
      const item = selected.value
      try {
        const updated = await apiClient.resumes.update(item.id, { name: draft.name.trim(), resume_text: draft.body })
        const index = store.resumes.findIndex(resume => resume.id === item.id)
        if (index >= 0) store.resumes[index] = normalizeResume(updated)
        editing.value = false
        showToast('简历档案已保存，相关评分已按后端规则更新')
      } catch (error) {
        showToast(`简历保存失败：${error.message}`, 'bad')
      }
    }
    async function createResume() {
      try {
        const created = await apiClient.resumes.create({ name: '未命名简历', resume_text: '', make_default: store.resumes.length === 0 })
        await refreshResumes()
        selectedId.value = created.id
        store.selectedResumeId = created.id
        loadDraft()
        editing.value = true
      } catch (error) {
        showToast(`新建简历失败：${error.message}`, 'bad')
      }
    }
    watch(() => store.selectedResumeId, id => {
      if (!id || selectedId.value === id) return
      selectedId.value = id
      editing.value = false
      loadDraft()
    })
    loadDraft()
    return { store, selectedId, selected, editing, draft, rendered, fmtTime, selectResume, edit, cancelEdit, save, createResume }
  },
  template: `
  <div>
    <header class="page-head">
      <div><div class="eyebrow">候选人资料</div><h1>简历档案</h1><p>维护不同求职方向的 Markdown 简历</p></div>
      <button class="primary" @click="createResume">新建简历</button>
    </header>
    <div class="profile-layout">
      <aside class="resume-list" aria-label="简历列表">
        <button v-for="item in store.resumes" :key="item.id" :class="{active:selectedId===item.id}" @click="selectResume(item.id)">
          <b>{{item.name}}</b><span>更新于 {{fmtTime(item.updatedAt)}}</span>
        </button>
      </aside>
      <section v-if="selected" class="resume-workspace">
        <div class="workspace-toolbar">
          <div><h2>{{editing ? '编辑简历' : selected.name}}</h2><p>{{editing ? '仅保留档案名称与 Markdown 正文' : 'Markdown 预览'}}</p></div>
          <div class="row" v-if="editing"><button @click="cancelEdit">取消</button><button class="primary" @click="save">保存简历</button></div>
          <button v-else class="primary" @click="edit">编辑</button>
        </div>
        <div v-if="editing" class="resume-editor">
          <label>档案名称<input v-model="draft.name" maxlength="40" placeholder="例如：嵌入式软件主简历"></label>
          <label>简历正文<textarea v-model="draft.body" class="resume-text" spellcheck="false" placeholder="使用 Markdown 编写简历正文"></textarea></label>
        </div>
        <article v-else class="markdown-body" v-html="rendered"></article>
      </section>
    </div>
  </div>`,
}

// -- 页面：采集中心 -----------------------------------------------------
function cityOptionNames() {
  return (store.collect.options.city_options || [])
    .flatMap(group => (group.cities || []).map(city => city.name))
}
function matchingCities(query, selected) {
  const keyword = String(query || '').trim().replace(/市$/, '')
  if (!keyword) return []
  return cityOptionNames().filter(city => city.includes(keyword) && !selected.includes(city)).slice(0, 8)
}
function appendCity(list, value) {
  const city = String(value || '').trim().replace(/市$/, '')
  if (city && !list.includes(city)) list.push(city)
  return city
}
const FILTER_OPTIONS = reactive({ salary: ['不限'], experience: ['不限'], degree: ['不限'], scale: ['不限'], stage: ['不限'], industry: ['不限'] })
function syncFilterOptions() {
  const values = store.collect.options.filter_values || {}
  for (const key of Object.keys(FILTER_OPTIONS)) {
    FILTER_OPTIONS[key] = ['不限', ...Object.keys(values[key] || {}).filter(value => value !== '不限')]
  }
}
function planNamePreview(resumeId, serial = '####') {
  const now = new Date()
  const date = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`
  const resume = store.resumes.find(item => Number(item.id) === Number(resumeId))
  const name = String(resume && resume.name || '简历')
    .replace(/[\\/:*?"<>|\s]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 20) || '简历'
  return `${date}-${name}-${serial}`
}
const createPlan = () => ({
  name: planNamePreview(store.selectedResumeId), mode: 'auto', fetchDetails: true, resumeId: store.selectedResumeId,
  keywords: '', cities: [], pages: 3, salary: '不限', experience: '不限',
  degree: '不限', scale: '不限', stage: '不限', industry: '不限', companies: '', generated: false,
  streamText: '',
})
function filterLabel(key, raw) {
  const code = String(raw || '').split(',')[0]
  const entries = Object.entries((store.collect.options.filter_values || {})[key] || {})
  const found = entries.find(([, value]) => String(value) === code)
  return found ? found[0] : '不限'
}
function applySavedConfig(plan, config) {
  if (!config) return
  const filters = config.filters || {}
  Object.assign(plan, {
    resumeId: config.resume_id || store.selectedResumeId,
    keywords: (config.keywords || []).join('\n'),
    cities: (config.cities || []).map(city => city.city || city.name).filter(Boolean),
    pages: Number(config.pages || 3), fetchDetails: config.fetch_details !== false,
    salary: filterLabel('salary', filters.salary), experience: filterLabel('experience', filters.experience),
    degree: filterLabel('degree', filters.degree), scale: filterLabel('scale', filters.scale),
    stage: filterLabel('stage', filters.stage), industry: filterLabel('industry', filters.industry),
    companies: (config.companies || []).map(company => [company.name, company.url || company.brand_id].filter(Boolean).join(' | ')).join('\n'),
    name: planNamePreview(config.resume_id || store.selectedResumeId),
    mode: 'manual', generated: true, streamText: '',
  })
}
function applyGeneratedStrategy(plan, generated = {}) {
  const searches = Array.isArray(generated.searches) ? generated.searches : []
  Object.assign(plan, {
    mode: 'auto', generated: true,
    name: planNamePreview(plan.resumeId),
    keywords: [...new Set(searches.map(item => String(item.keyword || '').trim()).filter(Boolean))].join('\n'),
    cities: [...new Set(searches.map(item => String(item.city || '').trim()).filter(Boolean))],
    pages: Number(searches[0] && searches[0].pages || 3),
    salary: '不限', experience: '不限', degree: '不限', scale: '不限', stage: '不限', industry: '不限', companies: '',
  })
}
async function generateStrategyStreaming(plan, resumeId, signal) {
  let generated = null
  plan.streamText = ''
  await apiClient.strategy.generateStream(resumeId, event => {
    if (event.type === 'delta') {
      plan.streamText = (plan.streamText + String(event.delta || '')).slice(-12000)
    } else if (event.type === 'done') {
      generated = event.plan
    } else if (event.type === 'failed') {
      throw new ApiError(event.message || 'AI 采集计划生成失败')
    } else if (event.type === 'cancelled') {
      throw new DOMException('生成已取消', 'AbortError')
    }
  }, signal)
  if (!generated) throw new ApiError('AI 未返回完整采集计划')
  applyGeneratedStrategy(plan, generated)
  return generated
}
function planToCollectConfig(plan) {
  const filters = {}
  for (const key of ['salary', 'experience', 'degree', 'scale', 'stage', 'industry']) {
    if (plan[key] && plan[key] !== '不限') filters[key] = plan[key]
  }
  const companies = String(plan.companies || '').split(/\n/).map(line => line.trim()).filter(Boolean).map(line => {
    const parts = line.split('|').map(value => value.trim())
    const identity = parts.find(value => /^https?:\/\//.test(value)) || ''
    return identity ? { name: parts.find(value => value !== identity) || '', url: identity, pages: Number(plan.pages || 3) } : null
  }).filter(Boolean)
  return {
    name: plan.name.trim(), resume_id: Number(plan.resumeId || store.selectedResumeId),
    keywords: String(plan.keywords || '').split(/[\n,，]+/).map(value => value.trim()).filter(Boolean),
    cities: [...plan.cities], pages: Number(plan.pages), filters, companies,
    fetch_details: Boolean(plan.fetchDetails),
  }
}
async function saveAndStartPlan(plan) {
  const config = planToCollectConfig(plan)
  await apiClient.collect.saveConfig(config, config.resume_id)
  store.collect.config = config
  const result = await apiClient.collect.start({ kind: 'config', resume_id: config.resume_id, config })
  if (result && result.name) plan.name = result.name
  await Promise.allSettled([refreshCollectStatus(), refreshRuns(), refreshDashboard()])
  return result
}
function runStatusLabel(status) {
  return { running: '采集中', paused: '已暂停', completed: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' }[status] || status
}

const CollectView = {
  setup() {
    const screen = ref('history')
    const step = ref(1)
    const generating = ref(false)
    const syncingFavorites = ref(false)
    const cityQuery = ref('')
    const plan = reactive(createPlan())
    let planController = null
    const keywords = computed(() => plan.keywords.split(/[\n,，]+/).map(value => value.trim()).filter(Boolean))
    const companyList = computed(() => plan.companies.split(/\n/).map(value => value.trim()).filter(Boolean))
    const citySuggestions = computed(() => matchingCities(cityQuery.value, plan.cities))
    const llmReady = computed(() => isLlmConfigured())
    const resume = computed(() => store.resumes.find(item => item.id === Number(plan.resumeId)) || store.resumes[0])
    function startWizard() {
      if (planController) planController.abort()
      Object.assign(plan, createPlan())
      applySavedConfig(plan, store.collect.config)
      step.value = 1
      screen.value = 'wizard'
      cityQuery.value = ''
    }
    function closeWizard() {
      if (planController) planController.abort()
      planController = null
      generating.value = false
      screen.value = 'history'
    }
    async function syncFavoriteJobs() {
      syncingFavorites.value = true
      try {
        await apiClient.favorites.start()
        await Promise.allSettled([refreshFavoriteStatus(), refreshRuns()])
        showToast('BOSS 收藏同步已启动，并新增采集记录')
      } catch (error) {
        showToast(`BOSS 收藏同步失败：${error.message}`, 'bad')
      } finally { syncingFavorites.value = false }
    }
    function addCity(city = cityQuery.value) {
      if (appendCity(plan.cities, city)) cityQuery.value = ''
    }
    function removeCity(city) {
      plan.cities.splice(plan.cities.indexOf(city), 1)
    }
    async function generatePlan() {
      if (!llmReady.value) {
        showToast('LLM 未配置，AI 生成采集计划不可用', 'warn')
        return
      }
      if (planController) planController.abort()
      planController = new AbortController()
      const controller = planController
      generating.value = true
      try {
        await generateStrategyStreaming(plan, plan.resumeId, controller.signal)
        showToast('AI 采集计划已生成，可继续修改')
      } catch (error) {
        if (error.name !== 'AbortError') showToast(`AI 采集计划生成失败：${error.message}`, 'bad')
      } finally {
        if (planController === controller) {
          planController = null
          generating.value = false
        }
      }
    }
    function setPlanMode(mode) {
      if (mode === 'manual' && planController) planController.abort()
      plan.mode = mode
      plan.generated = mode === 'manual'
      if (mode === 'manual') plan.streamText = ''
    }
    function nextToConfig() {
      step.value = 2
      if (plan.mode === 'manual') plan.generated = true
    }
    function toReview() {
      if (!keywords.value.length || !plan.cities.length) {
        showToast('请填写关键词并至少选择一个城市', 'warn')
        return
      }
      step.value = 3
    }
    async function launchPlan() {
      try {
        const result = await saveAndStartPlan(plan)
        screen.value = 'history'
        showToast(`采集计划「${result.name || plan.name}」已开始`)
      } catch (error) {
        showToast(`采集计划启动失败：${error.message}`, 'bad')
      }
    }
    async function toggleRun(run) {
      try {
        if (run.status === 'running') {
          await apiClient.collect.pause()
          showToast('采集计划已请求暂停', 'info')
        } else if (run.status === 'paused') {
          await apiClient.collect.resume()
          showToast('采集计划已继续')
        }
        await Promise.allSettled([refreshCollectStatus(), refreshRuns()])
      } catch (error) {
        showToast(`采集状态更新失败：${error.message}`, 'bad')
      }
    }
    async function toggleRunData(run, event) {
      const enabled = event.target.checked
      try {
        await apiClient.runs.setEnabled(run.id, enabled)
        run.enabled = enabled
        await Promise.allSettled([refreshRuns(), refreshDashboard(), refreshAnalytics(), refreshJobs()])
        showToast(enabled ? `已应用「${run.name}」的岗位数据` : `已禁用「${run.name}」的岗位数据`, 'info')
      } catch (error) {
        event.target.checked = run.enabled
        showToast(`采集数据状态更新失败：${error.message}`, 'bad')
      }
    }
    async function cancelRun(run) {
      const accepted = await requestConfirm({
        title: '取消采集计划',
        message: `确认取消「${run.name}」？已采集的 ${run.collected} 条数据会保留，可继续通过“应用数据”开关控制。`,
        confirmText: '确认取消', tone: 'danger',
      })
      if (!accepted) return
      try {
        if (run.kind === 'favorite_sync') await apiClient.favorites.cancel()
        else await apiClient.collect.cancel()
        await Promise.allSettled([refreshCollectStatus(), refreshFavoriteStatus(), refreshRuns()])
        showToast('已请求取消，当前读取步骤结束后停止', 'info')
      } catch (error) {
        showToast(`取消失败：${error.message}`, 'bad')
      }
    }
    async function deleteRunData(run) {
      let preview
      try {
        preview = await apiClient.runs.deletePreview(run.id)
      } catch (error) {
        showToast(`删除范围读取失败：${error.message}`, 'bad')
        return
      }
      const exclusive = Number(preview.exclusive_jobs ?? preview.exclusive_job_count ?? 0)
      const shared = Number(preview.shared_jobs ?? preview.shared_job_count ?? 0)
      const unattributed = Number(preview.legacy_unattributed ?? preview.unattributed_jobs ?? 0)
      const accepted = await requestConfirm({
        title: `删除采集数据 · ${run.name}`,
        message: `此操作会永久删除该采集记录和 ${exclusive} 个仅属于它的岗位；${shared} 个同时属于其他采集记录的岗位会保留。${unattributed ? `另有 ${unattributed} 条旧数据无法可靠追溯，不会猜测删除。` : ''} 删除后无法恢复。`,
        confirmText: '永久删除', tone: 'danger',
      })
      if (!accepted) return
      try {
        const result = await apiClient.runs.delete(run.id)
        await Promise.allSettled([refreshRuns(), refreshDashboard(), refreshAnalytics(), refreshJobs()])
        const deleted = Number(result.deleted_jobs ?? result.deleted_job_count ?? exclusive)
        const preserved = Number(result.preserved_shared_jobs ?? result.shared_jobs ?? shared)
        showToast(`采集数据已删除：删除 ${deleted} 个独占岗位，保留 ${preserved} 个共享岗位`, 'info')
      } catch (error) {
        showToast(`采集数据删除失败：${error.message}`, 'bad')
      }
    }
    watch(() => plan.resumeId, resumeId => { plan.name = planNamePreview(resumeId) })
    onBeforeUnmount(() => { if (planController) planController.abort() })
    return { store, screen, step, plan, generating, syncingFavorites, cityQuery, keywords, companyList, citySuggestions, llmReady, resume, FILTER_OPTIONS,
      percentOf, fmtTime, formatEta, runProgressSummary, runStatusLabel,
      startWizard, closeWizard, addCity, removeCity, generatePlan,
      setPlanMode, nextToConfig, toReview, launchPlan, syncFavoriteJobs, toggleRun, toggleRunData, cancelRun, deleteRunData, apiClient }
  },
  template: `
  <div>
    <header class="page-head">
      <div><div class="eyebrow">岗位发现</div><h1>采集中心</h1><p>{{screen==='history'?'采集计划与历史记录':'创建可检查、可修改的采集计划'}}</p></div>
      <div v-if="screen==='history'" class="row"><button :disabled="syncingFavorites" @click="syncFavoriteJobs">{{syncingFavorites?'同步中…':'同步 BOSS 收藏'}}</button><button class="primary" @click="startWizard">新建采集计划</button></div>
      <button v-else @click="closeWizard">返回历史记录</button>
    </header>

    <template v-if="screen==='history'">
      <div class="table-wrap collect-history">
        <table>
          <thead><tr><th>时间 / 计划</th><th>采集条数</th><th>状态与进度</th><th>应用数据</th><th class="actions-col">操作</th></tr></thead>
          <tbody>
            <tr v-for="run in store.runs" :key="run.id">
              <td><b>{{run.name}}</b><p>{{fmtTime(run.startedAt)}} · {{run.source}} · {{run.fetchDetails?'含完整 JD':'仅岗位列表'}}</p><span v-if="run.kind==='favorite_sync'" class="tag blue">收藏同步采集</span></td>
              <td><strong class="table-number">{{run.itemCount}}</strong><span class="muted"> 个岗位</span><div v-if="run.completenessKnown" class="collect-counts"><span>仅岗位描述 {{run.listOnlyCount}}</span><span>完整 JD {{run.withJdCount}}</span></div><small v-else class="muted">完整度无法追溯</small></td>
              <td><div class="run-progress-cell"><div class="progress-copy"><span class="status-badge" :class="run.status">{{runStatusLabel(run.status)}}</span><b>{{percentOf(run)}}%</b><small>{{runProgressSummary(run)}}</small></div><div class="progress"><i :style="{width:percentOf(run)+'%'}"></i></div><small v-if="run.status==='running'" class="run-eta">{{formatEta(run.etaSeconds)}}</small><small v-else-if="run.status==='paused'" class="run-eta">已暂停，预计时间停止计算</small></div></td>
              <td><label class="switch compact" :title="run.hasSourceOwnership?'控制该次采集数据是否参与岗位展示':'旧记录缺少精确岗位归属'"><input type="checkbox" :checked="run.enabled" :disabled="!run.hasSourceOwnership" @change="toggleRunData(run,$event)"><span class="switch-track"><span class="switch-thumb"></span></span><span>{{run.hasSourceOwnership?(run.enabled?'已应用':'已禁用'):'仅统计'}}</span></label></td>
              <td><div class="row end"><button v-if="(run.status==='running' || run.status==='paused') && run.kind!=='favorite_sync'" @click="toggleRun(run)">{{run.status==='running'?'暂停':'继续'}}</button><button v-if="run.status==='running' || run.status==='paused'" class="danger-quiet" @click="cancelRun(run)">取消</button><button v-else class="danger-quiet" title="永久删除本次采集及独占岗位" @click="deleteRunData(run)">删除数据</button><a class="icon-button" :href="apiClient.runs.exportUrl(run.id)" download :aria-disabled="!run.canExport" :title="run.canExport?'导出 Excel':'旧记录缺少可导出的岗位归属'" :aria-label="'导出 ' + run.name" @click="!run.canExport && $event.preventDefault()">↓</a></div></td>
            </tr>
            <tr v-if="!store.runs.length"><td colspan="5"><div class="empty compact">暂无采集记录</div></td></tr>
          </tbody>
        </table>
      </div>
    </template>

    <template v-else>
      <ol class="stepper" aria-label="采集计划步骤">
        <li :class="{active:step===1,done:step>1}"><span>1</span><div><b>采集方式</b><small>选择生成方式</small></div></li>
        <li :class="{active:step===2,done:step>2}"><span>2</span><div><b>计划配置</b><small>生成并修改</small></div></li>
        <li :class="{active:step===3}"><span>3</span><div><b>确认开始</b><small>检查执行范围</small></div></li>
      </ol>

      <section v-if="step===1" class="wizard-panel">
        <div v-if="!llmReady" class="llm-warning"><b>LLM 未配置</b><span>AI 自动生成不可用，请改用手动选择或先到设置页配置 LLM。</span></div>
        <div class="field-group"><span class="field-label">计划生成方式</span><div class="segmented wide"><button :class="{active:plan.mode==='auto'}" :disabled="!llmReady" @click="setPlanMode('auto')"><b>AI 自动生成</b><small>根据选定简历生成关键词与范围</small></button><button :class="{active:plan.mode==='manual'}" @click="setPlanMode('manual')"><b>手动选择</b><small>逐项配置关键词、城市和筛选</small></button></div></div>
        <div class="field-group"><span class="field-label">采集详细程度</span><div class="segmented"><button :class="{active:!plan.fetchDetails}" @click="plan.fetchDetails=false">采集岗位列表</button><button :class="{active:plan.fetchDetails}" @click="plan.fetchDetails=true">采集完整JD（推荐）</button></div></div>
        <div class="wizard-actions"><span></span><button class="primary" @click="nextToConfig">下一步</button></div>
      </section>

      <section v-if="step===2" class="wizard-panel">
        <pre v-if="plan.mode==='auto' && plan.streamText" class="strategy-stream" :class="{done:plan.generated}">{{plan.streamText}}</pre>
        <div v-if="plan.mode==='auto' && !plan.generated" class="ai-generate">
          <div><span class="ai-mark">AI</span><h2>根据简历生成采集计划</h2><p>{{llmReady?'选择本次采集使用的简历档案':'LLM 未配置，AI 生成功能不可用'}}</p></div>
          <label>简历档案<select v-model.number="plan.resumeId"><option v-for="item in store.resumes" :key="item.id" :value="item.id">{{item.name}}</option></select></label>
          <button class="primary" :disabled="generating || !llmReady" @click="generatePlan"><span v-if="generating" class="spinner"></span>{{generating?'正在流式生成…':'生成采集计划'}}</button>
        </div>
        <template v-else>
          <div class="form-grid two"><div class="plan-name-preview"><span>计划名称</span><b>{{plan.name}}</b><small>启动后自动替换为实际流水号</small></div><label>每个关键词页数<input type="number" v-model.number="plan.pages" min="1" max="10"></label></div>
          <label class="block-label">搜索关键词<textarea class="short" v-model="plan.keywords" placeholder="每行一个关键词"></textarea></label>
          <div class="field-group"><span class="field-label">城市范围</span><div class="city-picker"><div class="city-input-row"><input v-model="cityQuery" placeholder="输入城市关键词" @keyup.enter="addCity()"><button @click="addCity()">添加</button></div><div v-if="citySuggestions.length" class="city-suggestions"><button v-for="city in citySuggestions" :key="city" @click="addCity(city)">{{city}}</button></div><div class="city-tags"><span v-for="city in plan.cities" :key="city" class="city-tag">{{city}}<button :aria-label="'移除城市 ' + city" @click="removeCity(city)">×</button></span></div></div></div>
          <div class="form-grid three"><label>薪资范围<select v-model="plan.salary"><option v-for="value in FILTER_OPTIONS.salary" :key="value">{{value}}</option></select></label><label>经验<select v-model="plan.experience"><option v-for="value in FILTER_OPTIONS.experience" :key="value">{{value}}</option></select></label><label>学历<select v-model="plan.degree"><option v-for="value in FILTER_OPTIONS.degree" :key="value">{{value}}</option></select></label><label>公司规模<select v-model="plan.scale"><option v-for="value in FILTER_OPTIONS.scale" :key="value">{{value}}</option></select></label><label>融资阶段<select v-model="plan.stage"><option v-for="value in FILTER_OPTIONS.stage" :key="value">{{value}}</option></select></label><label>行业<select v-model="plan.industry"><option v-for="value in FILTER_OPTIONS.industry" :key="value">{{value}}</option></select></label></div>
          <label v-if="plan.mode==='manual'" class="block-label">定向公司<textarea class="short" v-model="plan.companies" placeholder="每行填写 公司名 | BOSS 公司主页链接，可留空"></textarea></label>
        </template>
        <div class="wizard-actions"><button @click="step=1">上一步</button><button v-if="plan.mode==='manual' || plan.generated" class="primary" @click="toReview">检查计划</button></div>
      </section>

      <section v-if="step===3" class="wizard-panel review-plan">
        <div class="review-head"><div><span class="status-badge completed">准备就绪</span><h2>{{plan.name}}</h2><p>{{plan.mode==='auto'?'AI 自动生成':'手动配置'}} · {{plan.fetchDetails?'采集完整JD':'仅岗位列表'}}</p></div><strong>{{keywords.length * plan.cities.length}}</strong><small>关键词 × 城市组合</small></div>
        <dl class="review-grid"><div><dt>关键词</dt><dd><span v-for="value in keywords" :key="value" class="tag blue">{{value}}</span></dd></div><div><dt>城市</dt><dd>{{plan.cities.join('、')}}</dd></div><div><dt>筛选条件</dt><dd>{{[plan.salary,plan.experience,plan.degree,plan.scale,plan.stage,plan.industry].filter(v=>v&&v!=='不限').join(' · ') || '不限'}}</dd></div><div v-if="plan.mode==='manual'"><dt>定向公司</dt><dd>{{companyList.join('、') || '无'}}</dd></div></dl>
        <div class="wizard-actions"><button @click="step=2">返回修改</button><button class="primary" @click="launchPlan">开始采集</button></div>
      </section>
    </template>
  </div>`,
}

// -- 页面：数据分析 -----------------------------------------------------
function apiChartRows(items) {
  return (items || []).map(item => ({ label: item.label || item.day || '未标注', value: Number(item.count || 0) }))
}
const ANALYTICS_DIMENSIONS = [
  { key: 'experience', label: '经验要求' },
  { key: 'degree', label: '学历要求' },
  { key: 'industry', label: '行业' },
  { key: 'scale', label: '公司规模' },
]
// 与后端月薪分桶一致的快捷档；min/max 为空表示不设该边界。
const ANALYTICS_SALARY_PRESETS = [
  { label: '10K以下', min: '', max: '10' },
  { label: '10-20K', min: '10', max: '20' },
  { label: '20-30K', min: '20', max: '30' },
  { label: '30-50K', min: '30', max: '50' },
  { label: '50K以上', min: '50', max: '' },
]
// 图表配色固定走 CSS 变量，随明暗主题切换（主题回归静态断言依赖这些字面量）。
const CHART_PALETTE = [
  'var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)',
  'var(--chart-5)', 'var(--chart-6)', 'var(--chart-7)', 'var(--chart-8)',
]
const AnalyticsView = {
  components: { SvgBars },
  setup() {
    // 多标签交叉筛选：维度内多值 OR、维度间 AND；后端候选刻面按互斥语义返回计数。
    const filters = reactive({
      keywords: [], cities: [], dateFrom: '', dateTo: '',
      experience: [], degree: [], industry: [], scale: [],
      salaryMin: '', salaryMax: '', headhunter: '',
    })
    const loading = ref(false)
    const meta = computed(() => store.analytics.meta
      || { keywords: [], cities: [], options: {}, keyword_options: [], city_options: [] })
    const keywordOptions = computed(() => {
      // 兼容回退：旧后端返回体只有 meta.keywords（此时无法区分版本与数据缺失），
      // 用候选列表兜底展示但不显示计数；重启后端后自动回到带计数的互斥刻面。
      if ((meta.value.keyword_options || []).length) return meta.value.keyword_options
      const legacyKeywords = meta.value.keywords || []
      return legacyKeywords.map(label => ({
        label, code: label, count: null,
        selected: filters.keywords.includes(label),
      }))
    })
    const cityOptions = computed(() => {
      if ((meta.value.city_options || []).length) return meta.value.city_options
      return (meta.value.cities || []).map(city => ({
        label: city.name, code: city.code, count: null,
        selected: filters.cities.includes(city.code),
      }))
    })
    const summary = computed(() => {
      const value = store.analytics.summary || {}
      const min = value.avg_salary_min
      const max = value.avg_salary_max
      const band = value.p25_monthly_salary_k != null && value.p75_monthly_salary_k != null
        ? `${value.p25_monthly_salary_k}–${value.p75_monthly_salary_k}K` : '待数据'
      return {
        total: Number(value.jobs || 0), avg: min != null && max != null ? `${min}-${max}K` : '—',
        median: value.median_monthly_salary_k != null ? `${value.median_monthly_salary_k}K` : '—',
        quartileBand: band,
        headhunterRate: Number(value.headhunter_rate || 0), sourceCoverage: Number(value.source_coverage_rate || 0),
      }
    })
    const funnelStages = computed(() => (store.analytics.funnel && store.analytics.funnel.stages) || [])
    const jobRows = computed(() => store.analytics.job_rows || [])
    const dimText = value => (value && String(value).trim() ? String(value) : '未标注')
    const salaryText = row => {
      if (row.salary && String(row.salary).trim()) return row.salary
      if (row.salary_min != null && row.salary_max != null) return `${row.salary_min}-${row.salary_max}K`
      return '未标注'
    }
    const activePreset = computed(() => {
      const found = ANALYTICS_SALARY_PRESETS.find(item =>
        item.min === filters.salaryMin && item.max === filters.salaryMax)
      return found ? found.label : ''
    })
    const activeChips = computed(() => {
      const chips = []
      for (const value of filters.keywords) chips.push({ kind: 'keywords', value, label: `关键词：${value}` })
      for (const code of filters.cities) {
        const city = cityOptions.value.find(item => item.code === code)
        chips.push({ kind: 'cities', value: code, label: `城市：${city ? city.label : code}` })
      }
      if (filters.dateFrom) chips.push({ kind: 'dateFrom', label: `起始 ${filters.dateFrom}` })
      if (filters.dateTo) chips.push({ kind: 'dateTo', label: `截止 ${filters.dateTo}` })
      for (const dim of ANALYTICS_DIMENSIONS)
        for (const value of filters[dim.key]) chips.push({ kind: 'dim', field: dim.key, value, label: `${dim.label}：${value}` })
      if (filters.salaryMin !== '' || filters.salaryMax !== '') {
        chips.push({ kind: 'salary', label: `月薪 ${filters.salaryMin === '' ? '不限' : filters.salaryMin + 'K'} – ${filters.salaryMax === '' ? '不限' : filters.salaryMax + 'K'}` })
      }
      if (filters.headhunter === '1') chips.push({ kind: 'headhunter', label: '仅猎头岗位' })
      if (filters.headhunter === '0') chips.push({ kind: 'headhunter', label: '排除猎头岗位' })
      return chips
    })
    const charts = computed(() => {
      const distributions = store.analytics.distributions || {}
      const cross = store.analytics.cross || {}
      const crossRows = items => (items || []).map(item => ({ label: item.label, value: Number(item.value || 0) }))
      const result = [
        { title: '月薪分布', rows: apiChartRows(distributions.salary), id: 'salary', clickable: true, hint: '点击档位按月薪区间筛选' },
        { title: '经验要求', rows: apiChartRows(distributions.experience), id: 'experience', clickable: true, hint: '点击类别加入经验筛选' },
        { title: '学历要求', rows: apiChartRows(distributions.degree), id: 'degree', clickable: true, hint: '点击类别加入学历筛选' },
        { title: '行业分布', rows: apiChartRows(distributions.industry), id: 'industry', clickable: true, hint: '点击行业加入行业筛选' },
        { title: '公司规模', rows: apiChartRows(distributions.scale), id: 'scale', clickable: true, hint: '点击规模加入规模筛选' },
        { title: '岗位评分', rows: apiChartRows(distributions.job_score) },
        { title: '城市分布', rows: apiChartRows(store.analytics.by_city), id: 'city', clickable: true, hint: '点击条目加入/移除城市筛选' },
        { title: '关键词命中', rows: apiChartRows(store.analytics.by_keyword), id: 'keyword', clickable: true, hint: '点击条目加入/移除关键词筛选' },
        { title: '采集趋势', rows: apiChartRows(store.analytics.trends || store.analytics.trend) },
        { title: '经验 × 平均月薪', rows: crossRows(cross.experience_avg_salary) },
        { title: '学历 × 平均月薪', rows: crossRows(cross.degree_avg_salary) },
        { title: '行业 × 平均月薪', rows: crossRows(cross.industry_avg_salary) },
        { title: '公司岗位数 Top10', rows: crossRows(store.analytics.top_companies) },
        { title: '技能需求 Top10', rows: crossRows(store.analytics.top_skills) },
      ]
      result.forEach((chart, index) => { chart.color = chart.color || CHART_PALETTE[index % CHART_PALETTE.length] })
      return result
    })
    async function loadAnalytics() {
      loading.value = true
      try {
        await refreshAnalytics({
          keyword: filters.keywords.join(','), city_code: filters.cities.join(','),
          date_from: filters.dateFrom, date_to: filters.dateTo,
          experience: filters.experience.join(','), degree: filters.degree.join(','),
          industry: filters.industry.join(','), scale: filters.scale.join(','),
          headhunter: filters.headhunter,
          salary_min: filters.salaryMin, salary_max: filters.salaryMax,
        })
      } catch (error) {
        showToast(`数据分析加载失败：${error.message}`, 'bad')
      } finally { loading.value = false }
    }
    function toggleDimValue(field, value) {
      const list = filters[field]
      const index = list.indexOf(value)
      if (index >= 0) list.splice(index, 1)
      else list.push(value)
      loadAnalytics()
    }
    function applySalaryBounds(min, max) {
      filters.salaryMin = min
      filters.salaryMax = max
      loadAnalytics()
    }
    function applySalaryPreset(preset) {
      // 再次点击当前档位视为取消；「自定义」输入框改动即时生效。
      if (preset.min === filters.salaryMin && preset.max === filters.salaryMax) applySalaryBounds('', '')
      else applySalaryBounds(preset.min, preset.max)
    }
    function clearSalary() { applySalaryBounds('', '') }
    function pickChart(chart, item) {
      if (!chart.clickable) return
      if (['experience', 'degree', 'industry', 'scale'].includes(chart.id)) {
        toggleDimValue(chart.id, item.label)
        return
      }
      if (chart.id === 'salary') {
        const preset = ANALYTICS_SALARY_PRESETS.find(row => row.label === item.label)
        if (preset) applySalaryPreset(preset)
        return
      }
      if (chart.id === 'city') {
        const option = cityOptions.value.find(row => row.label === item.label)
        if (!option) return
        toggleDimValue('cities', option.code)
        return
      }
      if (chart.id === 'keyword') {
        toggleDimValue('keywords', item.label)
      }
    }
    function removeChip(chip) {
      if (chip.kind === 'dim' || chip.kind === 'keywords' || chip.kind === 'cities') {
        const list = filters[chip.kind === 'dim' ? chip.field : chip.kind]
        const index = list.indexOf(chip.value)
        if (index >= 0) list.splice(index, 1)
      } else if (chip.kind === 'salary') {
        filters.salaryMin = ''
        filters.salaryMax = ''
      } else {
        filters[chip.kind] = ''
      }
      loadAnalytics()
    }
    async function resetFilters() {
      Object.assign(filters, {
        keywords: [], cities: [], dateFrom: '', dateTo: '',
        experience: [], degree: [], industry: [], scale: [],
        salaryMin: '', salaryMax: '', headhunter: '',
      })
      await loadAnalytics()
    }
    return {
      filters, dimensions: ANALYTICS_DIMENSIONS, salaryPresets: ANALYTICS_SALARY_PRESETS,
      meta, summary, charts, funnelStages, activeChips, activePreset, loading,
      keywordOptions, cityOptions, jobRows, dimText, salaryText,
      loadAnalytics, resetFilters, clearSalary, applySalaryPreset, toggleDimValue,
      pickChart, removeChip,
    }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">市场洞察</div><h1>数据分析</h1><p>多标签交叉下钻，点击图表条目即可加入或移除筛选</p></div><button :disabled="loading" @click="resetFilters">{{loading?'加载中…':'重置筛选'}}</button></header>
    <div class="filterbar analytics-filter">
      <label>起始日期<input type="date" v-model="filters.dateFrom" @change="loadAnalytics"></label>
      <label>结束日期<input type="date" v-model="filters.dateTo" @change="loadAnalytics"></label>
      <select v-model="filters.headhunter" @change="loadAnalytics"><option value="">含/不含猎头</option><option value="1">仅猎头岗位</option><option value="0">排除猎头岗位</option></select>
    </div>
    <section class="surface-panel dimension-filters">
      <div class="dim-row">
        <span class="dim-label">岗位关键词</span>
        <div class="chip-group">
          <button v-for="option in keywordOptions" :key="'kw-' + option.label"
                  class="chip" :class="{on: option.selected}" type="button"
                  :title="option.label + '：' + option.count + ' 个岗位'"
                  @click="toggleDimValue('keywords', option.label)">
            {{option.label}}<i v-if="option.count != null">{{option.count}}</i>
          </button>
          <span v-if="!keywordOptions.length" class="dim-empty">暂无候选：候选仅统计启用采集来源的命中，可到采集中心跑一轮关键词采集</span>
        </div>
      </div>
      <div class="dim-row">
        <span class="dim-label">城市</span>
        <div class="chip-group">
          <button v-for="option in cityOptions" :key="'city-' + option.code"
                  class="chip" :class="{on: option.selected}" type="button"
                  :title="option.label + '：' + option.count + ' 个岗位'"
                  @click="toggleDimValue('cities', option.code)">
            {{option.label}}<i v-if="option.count != null">{{option.count}}</i>
          </button>
          <span v-if="!cityOptions.length" class="dim-empty">暂无候选：候选仅统计启用采集来源的命中，可到采集中心跑一轮关键词采集</span>
        </div>
      </div>
      <div class="dim-row" v-for="dim in dimensions" :key="dim.key">
        <span class="dim-label">{{dim.label}}</span>
        <div class="chip-group">
          <button v-for="option in (meta.options[dim.key] || [])" :key="option.label"
                  class="chip" :class="{on: option.selected}" type="button"
                  :title="option.label + '：' + option.count + ' 个岗位'"
                  @click="toggleDimValue(dim.key, option.label)">
            {{option.label}}<i v-if="option.count != null">{{option.count}}</i>
          </button>
          <span v-if="!(meta.options[dim.key] || []).length" class="dim-empty">暂无可选项</span>
        </div>
      </div>
      <div class="dim-row">
        <span class="dim-label">月薪区间</span>
        <div class="chip-group">
          <button class="chip" :class="{on: !filters.salaryMin && !filters.salaryMax}" type="button" @click="clearSalary">不限</button>
          <button v-for="preset in salaryPresets" :key="preset.label" class="chip"
                  :class="{on: activePreset === preset.label}" type="button"
                  @click="applySalaryPreset(preset)">{{preset.label}}</button>
          <label class="salary-custom">自定义<input type="number" min="0" step="1" v-model="filters.salaryMin" @change="loadAnalytics" placeholder="最低K"><span>–</span><input type="number" min="0" step="1" v-model="filters.salaryMax" @change="loadAnalytics" placeholder="最高K"></label>
        </div>
      </div>
      <div class="active-chips" v-if="activeChips.length">
        <span class="dim-label">已选条件</span>
        <button v-for="chip in activeChips" :key="chip.label" class="chip on removable" type="button"
                :title="'移除：' + chip.label" @click="removeChip(chip)">{{chip.label}}<i>×</i></button>
      </div>
    </section>
    <div class="metric-grid compact">
      <div class="metric tone-blue"><span>分析样本</span><strong>{{summary.total}}</strong><small>当前筛选内岗位</small></div>
      <div class="metric tone-green"><span>平均月薪</span><strong>{{summary.avg}}</strong><small>按薪资上下限估算</small></div>
      <div class="metric tone-cyan"><span>中位数月薪</span><strong>{{summary.median}}</strong><small>P25–P75：{{summary.quartileBand}}</small></div>
      <div class="metric tone-amber"><span>猎头岗位占比</span><strong>{{summary.headhunterRate}}%</strong><small>按后端识别结果统计</small></div>
      <div class="metric tone-red"><span>来源覆盖率</span><strong>{{summary.sourceCoverage}}%</strong><small>具备可靠采集归因</small></div>
    </div>
    <section class="surface-panel funnel-panel" v-if="funnelStages.length">
      <h2>投递转化漏斗<small>当前筛选范围 · 按简历工作流标记与 HR 回复事实统计</small></h2>
      <div class="funnel">
        <div v-for="(stage,index) in funnelStages" :key="stage.key" class="funnel-stage">
          <div class="funnel-head"><span>{{index + 1}}. {{stage.label}}</span><b>{{stage.count}}</b></div>
          <div class="funnel-track"><i :style="{width: stage.pool_rate + '%'}"></i></div>
          <small v-if="stage.step_rate != null">较上一级转化 {{stage.step_rate}}%</small>
          <small v-else>样本池基准</small>
        </div>
      </div>
    </section>
    <div class="chart-grid">
      <section v-for="chart in charts" :key="chart.title" class="chart-panel">
        <h2>{{chart.title}}<small v-if="chart.hint">{{chart.hint}}</small></h2>
        <SvgBars :items="chart.rows" :color="chart.color" :clickable="!!chart.clickable" @pick="item => pickChart(chart, item)" />
      </section>
    </div>
    <section class="surface-panel analytics-jobs">
      <h2>筛选结果岗位<small>共 {{summary.total}} 个 · 按综合评分与薪资排序，最多展示 {{jobRows.length}} 条</small></h2>
      <div class="table-wrap" v-if="jobRows.length"><table>
        <thead><tr><th>岗位</th><th>公司</th><th>薪资</th><th>经验</th><th>学历</th><th>行业</th></tr></thead>
        <tbody>
          <tr v-for="row in jobRows" :key="row.job_key">
            <td>{{row.title}}</td>
            <td>{{dimText(row.company)}}</td>
            <td>{{salaryText(row)}}</td>
            <td>{{dimText(row.experience)}}</td>
            <td>{{dimText(row.degree)}}</td>
            <td>{{dimText(row.industry)}}</td>
          </tr>
        </tbody>
      </table></div>
      <div v-else class="empty compact">当前筛选条件下暂无岗位</div>
    </section>
  </div>`,
}

// -- 页面：岗位列表 -----------------------------------------------------
const EXPERIENCE_RANK = { '不限': 0, '经验不限': 0, '1-3年': 1, '3-5年': 2, '5-10年': 3, '10年以上': 4 }
const DEGREE_RANK = { '不限': 0, '大专': 1, '本科': 2, '硕士': 3, '博士': 4 }
const PRIORITY_RANK = { P0: 0, P1: 1, P2: 2, P3: 3 }
const JobsView = {
  setup() {
    const q = ref('')
    const favoriteFilter = ref('all')
    const tagFilter = ref('all')
    const expanded = ref('')
    const taskSummary = computed(() => aiSummary('jobs'))
    const sortState = reactive({ key: 'match_score', direction: 'desc' })
    const columns = [
      { key: 'title', label: '岗位' }, { key: 'company', label: '公司' }, { key: 'salary_max', label: '薪资' },
      { key: 'experience', label: '经验 / 学历' }, { key: 'job_score', label: '岗位评分' },
      { key: 'match_score', label: '匹配度评分' }, { key: 'composite', label: '综合评分' },
      { key: 'priority', label: 'P级' }, { key: 'active_ts', label: '活跃时间' },
    ]
    const filtered = computed(() => jobsFromEnabledRuns().filter(job => {
      const query = q.value.trim().toLowerCase()
      if (query && !`${job.title}${job.company}${job.location}`.toLowerCase().includes(query)) return false
      if (favoriteFilter.value === 'only' && !job.favorite) return false
      if (favoriteFilter.value === 'exclude' && job.favorite) return false
      if (tagFilter.value !== 'all' && !deriveJobTags(job).some(tag => tag.key === tagFilter.value)) return false
      return true
    }))
    function sortValue(job, key) {
      if (key === 'experience') return EXPERIENCE_RANK[job.experience] ?? 99
      if (key === 'degree') return DEGREE_RANK[job.degree] ?? 99
      if (key === 'priority') return PRIORITY_RANK[job.priority] ?? 99
      if (key === 'active_ts') return new Date(job.active_ts).getTime()
      return job[key]
    }
    const sortedJobs = computed(() => filtered.value.map((job, index) => ({ job, index })).sort((left, right) => {
      const a = sortValue(left.job, sortState.key)
      const b = sortValue(right.job, sortState.key)
      const emptyA = a === null || a === undefined || a === ''
      const emptyB = b === null || b === undefined || b === ''
      if (emptyA !== emptyB) return emptyA ? 1 : -1
      let result = 0
      if (typeof a === 'number' && typeof b === 'number') result = a - b
      else result = String(a).localeCompare(String(b), 'zh-CN', { numeric: true })
      if (result === 0) return left.index - right.index
      return sortState.direction === 'asc' ? result : -result
    }).map(item => item.job))
    function setSort(key) {
      if (sortState.key === key) sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc'
      else { sortState.key = key; sortState.direction = 'asc' }
    }
    const ariaSort = key => sortState.key === key ? (sortState.direction === 'asc' ? 'ascending' : 'descending') : 'none'
    async function toggleExpanded(job) {
      if (expanded.value === job.job_key) { expanded.value = ''; return }
      expanded.value = job.job_key
      try { await refreshJobDetail(job.job_key) }
      catch (error) { showToast(`岗位详情加载失败：${error.message}`, 'bad') }
    }
    async function toggleFavorite(job) {
      const favorite = !job.favorite
      try {
        await apiClient.jobs.favorite(job.job_key, favorite)
        job.favorite = favorite
        await Promise.allSettled([refreshDashboard(), refreshGreetings()])
        showToast(favorite ? '已加入收藏工作台' : '已取消收藏', favorite ? 'ok' : 'info')
      } catch (error) {
        showToast(`收藏状态更新失败：${error.message}`, 'bad')
      }
    }
    async function excludeJob(job) {
      const accepted = await requestConfirm({ title: '排除岗位', message: `确认将「${job.title} · ${job.company || '公司信息保密'}」移出当前列表？`, confirmText: '确认排除', tone: 'danger' })
      if (!accepted) return
      try {
        await apiClient.jobs.exclude(job.job_key)
        job.excluded = true
        expanded.value = ''
        await Promise.allSettled([refreshJobs(), refreshDashboard(), refreshAnalytics()])
        showToast('岗位已移入排除项', 'info')
      } catch (error) {
        showToast(`岗位排除失败：${error.message}`, 'bad')
      }
    }
    async function runScore(kind) {
      const jobKeys = sortedJobs.value.map(job => job.job_key)
      if (!jobKeys.length) { showToast('当前没有可评分岗位', 'warn'); return }
      try {
        const result = await enqueueAi('jobs', kind === '岗位评分' ? 'job_score' : 'match_score', jobKeys)
        const skipped = Number(result.skipped_existing_count || 0)
        const noJd = Number(result.skipped_no_jd_count || 0)
        let note
        if (result.added) {
          // 无 JD 的岗位由后端直接剔除，这里把三类结果向用户讲清楚
          const skips = [skipped ? `跳过 ${skipped} 个已有评分` : '',
            noJd ? `${noJd} 个缺 JD` : ''].filter(Boolean).join('、')
          note = `已将 ${result.added} 个岗位加入评分队列${skips ? `，${skips}` : ''}`
        } else if (noJd && skipped) {
          note = `所选岗位均不可评分：${skipped} 个已有评分、${noJd} 个缺 JD`
        } else if (noJd) {
          note = `所选 ${noJd} 个岗位均缺少职位描述（JD），补齐后才能参与评分`
        } else if (skipped) {
          note = `所选 ${skipped} 个岗位均已有评分，无需重复生成`
        } else {
          note = '所选岗位已在评分队列中'
        }
        showToast(note, result.added || !noJd ? 'info' : 'warn')
      } catch (error) {
        showToast(`${kind}任务创建失败：${error.message}`, 'bad')
      }
    }
    return { store, q, favoriteFilter, tagFilter, expanded, sortState, columns, sortedJobs, taskSummary,
      deriveJobTags, workflowLabel, scoreLabel, jobCardHref, formatEta, cancelAiTasks,
      setSort, ariaSort, toggleExpanded, toggleFavorite, excludeJob, runScore }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">岗位池</div><h1>岗位列表</h1><p>共 {{sortedJobs.length}} 个符合条件的岗位</p></div><div class="row scoring-controls"><label class="inline-select"><span>当前简历</span><select v-model.number="store.selectedResumeId"><option v-for="resume in store.resumes" :key="resume.id" :value="resume.id">{{resume.name}}</option></select></label><button @click="runScore('匹配度评分')">匹配度评分</button><button class="primary" @click="runScore('岗位评分')">岗位评分</button></div></header>
    <div v-if="taskSummary.visible" class="ai-task-progress" :class="{done:!taskSummary.running}"><div><b>{{taskSummary.label}}</b><span>动态并发 {{taskSummary.effective}} / {{taskSummary.max}}<template v-if="taskSummary.running"> · {{formatEta(taskSummary.etaSeconds)}}</template></span></div><div class="progress"><i :style="{width:taskSummary.percent+'%'}"></i></div><strong>{{taskSummary.completed}} / {{taskSummary.total}}</strong><small v-if="taskSummary.failures">{{taskSummary.failures}} 个失败，可重新评分</small><button v-if="taskSummary.running" class="danger-quiet" :disabled="taskSummary.cancelling" @click="cancelAiTasks('jobs')">{{taskSummary.cancelling?'取消中…':'取消评分'}}</button></div>
    <div class="filterbar"><input v-model="q" aria-label="搜索岗位或公司" placeholder="搜索岗位、公司或地点"><select v-model="tagFilter" aria-label="岗位标签"><option value="all">全部标签</option><option value="headhunter">猎头</option><option value="benefits">福利</option><option value="weekend">双休</option><option value="eight_hour_weekend">八小时双休</option><option value="alternating_weekend">大小周</option><option value="outsourcing">外包</option></select><select v-model="favoriteFilter" aria-label="收藏状态"><option value="all">全部岗位</option><option value="only">仅收藏</option><option value="exclude">未收藏</option></select><span class="filter-result">{{sortedJobs.length}} 条结果</span></div>
    <div class="table-wrap">
      <table class="jobs-table">
        <thead><tr><th v-for="column in columns" :key="column.key" :aria-sort="ariaSort(column.key)"><button class="sort-button" @click="setSort(column.key)"><span>{{column.label}}</span><i :class="{active:sortState.key===column.key,desc:sortState.key===column.key&&sortState.direction==='desc'}"></i></button></th></tr></thead>
        <tbody>
          <template v-for="job in sortedJobs" :key="job.job_key">
            <tr class="job-row" :class="{open:expanded===job.job_key}" tabindex="0" @click="toggleExpanded(job)" @keyup.enter="toggleExpanded(job)">
              <td data-label="岗位"><div class="job-title"><span class="chevron"></span><div><b>{{job.title}}</b><p>{{job.location}}</p><div class="tag-line"><span v-for="tag in deriveJobTags(job)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span><span v-if="job.favorite" class="tag neutral">已收藏</span><span v-if="workflowLabel(job)" class="tag blue">{{workflowLabel(job)}}</span></div></div></div></td>
              <td data-label="公司"><b>{{job.company || '公司信息保密'}}</b><p>{{job.industry}} · {{job.scale || '规模未知'}}</p></td>
              <td data-label="薪资" class="nowrap"><b>{{job.salary}}</b></td>
              <td data-label="经验 / 学历"><b>{{job.experience}}</b><p>{{job.degree}}</p></td>
              <td data-label="岗位评分" class="score" :class="{'score-pending':scoreLabel(job,'job_score','jobs')==='评分中'}">{{scoreLabel(job,'job_score','jobs')}}</td>
              <td data-label="匹配度评分" class="score accent-score" :class="{'score-pending':scoreLabel(job,'match_score','jobs')==='评分中'}">{{scoreLabel(job,'match_score','jobs')}}</td>
              <td data-label="综合评分" class="score" :class="{'score-pending':scoreLabel(job,'composite','jobs')==='评分中'}">{{scoreLabel(job,'composite','jobs')}}</td>
              <td data-label="P级"><span class="priority" :class="String(job.priority||'').toLowerCase()">{{job.priority}}</span></td>
              <td data-label="活跃时间"><b>{{job.active}}</b><p>{{(job.active_ts||'').slice(0,10) || '—'}}</p></td>
            </tr>
            <tr v-if="expanded===job.job_key" class="detail-row"><td colspan="9"><div class="job-detail"><div class="detail-toolbar"><button :class="{primary:job.favorite}" @click.stop="toggleFavorite(job)">{{job.favorite?'取消收藏':'添加收藏'}}</button><a v-if="job.favorite" class="button" :href="jobCardHref(job)" @click.stop>岗位分析</a><button @click.stop="excludeJob(job)">不再显示</button></div><div class="detail-meta"><div><span>岗位评分</span><b>{{scoreLabel(job,'job_score','jobs')}}</b></div><div><span>匹配度</span><b>{{scoreLabel(job,'match_score','jobs')}}</b></div><div><span>综合分</span><b>{{scoreLabel(job,'composite','jobs')}}</b></div><div><span>优先级</span><b>{{job.priority || '—'}}</b></div><div><span>来源关键词</span><b>{{job.source_keyword}}</b></div></div><h3>职位描述（JD）</h3><p class="jd-copy">{{job.jd}}</p></div></td></tr>
          </template>
        </tbody>
      </table>
      <div v-if="!sortedJobs.length" class="empty large"><b>没有匹配的岗位</b><span>调整关键词或筛选条件后重试</span></div>
    </div>
  </div>`,
}

// -- 页面：收藏工作台 ---------------------------------------------------
const JobCardView = {
  setup() {
    const query = ref('')
    const statusFilter = ref('all')
    const selectedKeys = ref([])
    const initial = store.jobs.find(job => job.favorite && !job.excluded)
    const activeKey = ref(routeJobKey.value || (initial ? initial.job_key : ''))
    const taskSummary = computed(() => aiSummary('workbench'))
    const interview = reactive({
      open: false, busy: false, sessionId: null, jobKey: '', totalQuestions: 0,
      questionIndex: 0, currentQuestion: '', answers: [], input: '', report: null, canFinish: false,
    })
    const favoriteJobs = computed(() => store.jobs.filter(job => job.favorite && !job.excluded))
    const jobs = computed(() => favoriteJobs.value.filter(job => {
      if (query.value && !`${job.title}${job.company}`.toLowerCase().includes(query.value.toLowerCase())) return false
      if (statusFilter.value === 'generated' && !job.greeting) return false
      const latestStage = job.offered ? 'offered' : job.interviewed ? 'interviewed'
        : job.applied ? 'applied' : job.contacted ? 'contacted' : 'pending'
      if (['pending', 'contacted', 'applied', 'interviewed', 'offered'].includes(statusFilter.value)
          && latestStage !== statusFilter.value) return false
      if (statusFilter.value === 'failed' && !job.actionError) return false
      return true
    }))
    const activeJob = computed(() => favoriteJobs.value.find(job => job.job_key === activeKey.value)
      || jobs.value[0] || favoriteJobs.value[0] || null)
    const allSelected = computed(() => jobs.value.length > 0 && jobs.value.every(job => selectedKeys.value.includes(job.job_key)))
    watch(() => activeJob.value && activeJob.value.job_key, async jobKey => {
      if (!jobKey) return
      const job = store.jobs.find(item => item.job_key === jobKey)
      if (job && !job.jd) {
        try { await refreshJobDetail(jobKey) }
        catch (error) { showToast(`岗位详情加载失败：${error.message}`, 'bad') }
      }
    }, { immediate: true })
    async function selectJob(job) {
      activeKey.value = job.job_key
      routeJobKey.value = job.job_key
      history.replaceState(null, '', jobCardHref(job))
      try { await refreshJobDetail(job.job_key) }
      catch (error) { showToast(`岗位详情加载失败：${error.message}`, 'bad') }
    }
    function toggleAll() { selectedKeys.value = allSelected.value ? [] : jobs.value.map(job => job.job_key) }
    // 旧版“演示模式：已模拟打开”行为已移除；BOSS 页面只能由后端选择沟通号打开。
    async function openBoss(job) {
      try {
        await apiClient.jobs.openBoss(job.job_key)
        showToast(`已在沟通号 Chrome 打开「${job.title}」`, 'info')
      } catch (error) { showToast(`打开 BOSS 失败：${error.message}`, 'bad') }
    }
    async function setWorkflowStage(job, stage) {
      const pending = stage === 'pending'
      const property = stage === 'contacted' ? 'contacted' : stage
      const backendStage = pending ? 'greeted' : (stage === 'contacted' ? 'greeted' : stage)
      try {
        const result = await apiClient.jobs.workflow(job.job_key, {
          stage: backendStage, enabled: pending ? false : !job[property], resume_id: store.selectedResumeId,
        })
        job.contacted = Boolean(result.greeted)
        job.applied = Boolean(result.applied)
        job.interviewed = Boolean(result.interviewed)
        job.offered = Boolean(result.offered)
        showToast(`岗位状态已更新为「${workflowLabel(job)}」`, 'info')
      } catch (error) { showToast(`岗位状态更新失败：${error.message}`, 'bad') }
    }
    async function generateAnalysis(job, silent = false) {
      try {
        const result = await enqueueAi('workbench', 'analysis', [job.job_key])
        job.actionError = ''
        job.actionErrorType = ''
        const noJd = Number(result.skipped_no_jd_count || 0)
        if (!silent) showToast(noJd ? '该岗位缺少职位描述（JD），补齐后才能生成分析'
          : result.added ? '岗位分析已加入生成队列' : '该岗位正在生成分析', noJd ? 'warn' : 'info')
        return result
      } catch (error) {
        job.actionError = error.message
        job.actionErrorType = 'analysis'
        if (!silent) showToast(`岗位分析失败：${error.message}`, 'bad')
        throw error
      }
    }
    async function generateGreeting(job, silent = false) {
      try {
        const result = await enqueueAi('workbench', 'greeting', [job.job_key])
        job.actionError = ''
        job.actionErrorType = ''
        const skipped = Number(result.skipped_existing_count || 0)
        const noJd = Number(result.skipped_no_jd_count || 0)
        if (skipped) await refreshGreetings()
        if (!silent) showToast(noJd ? '该岗位缺少职位描述（JD），补齐后才能生成招呼语'
          : result.added ? '招呼语已加入生成队列'
            : skipped ? '该岗位已有招呼语，已保留原结果'
              : '该岗位正在生成招呼语', noJd ? 'warn' : 'info')
        return result
      } catch (error) {
        job.actionError = error.message
        job.actionErrorType = 'greeting'
        if (!silent) showToast(`招呼语生成失败：${error.message}`, 'bad')
        throw error
      }
    }
    async function saveGreeting(job, silent = false) {
      if (!job.greetingId || !job.greeting.trim()) throw new ApiError('请先生成并填写招呼语')
      await apiClient.greetings.approve(job.greetingId, Number(job.greetingSelectedIndex || 0), job.greeting.trim())
      job.greetingStatus = 'approved'
      if (!silent) showToast('招呼语已保存并批准')
    }
    function selectGreetingVariant(job, index) {
      job.greetingSelectedIndex = Number(index)
      job.greeting = (job.greetingVariants || [])[index] || ''
    }
    async function copyAndOpenBoss(job) {
      if (!job.greeting || !job.greeting.trim()) { showToast('请先生成招呼语', 'warn'); return }
      try {
        await navigator.clipboard.writeText(job.greeting.trim())
        await apiClient.jobs.openBoss(job.job_key)
        showToast('招呼语已复制，并已在沟通号 Chrome 打开 BOSS', 'info')
      } catch (error) { showToast(`复制并打开 BOSS 失败：${error.message}`, 'bad') }
    }
    async function waitForTaskIds(ids, timeoutMs = 600000) {
      const wanted = new Set(ids)
      const deadline = Date.now() + timeoutMs
      while (Date.now() < deadline) {
        const snapshot = await apiClient.ai.status('workbench')
        applyAiSnapshot('workbench', snapshot, true)
        const tasks = snapshot.tasks.filter(task => wanted.has(task.id))
        if (tasks.length === wanted.size && tasks.every(task => AI_TERMINAL.has(task.status))) return tasks
        await new Promise(resolve => window.setTimeout(resolve, 700))
      }
      throw new ApiError('等待招呼语生成超时，请稍后重试')
    }
    async function executeBatch(type, explicitKeys = null) {
      const keys = explicitKeys || selectedKeys.value
      const targets = keys.map(key => store.jobs.find(job => job.job_key === key)).filter(Boolean)
      if (!targets.length) { showToast('请先选择岗位', 'warn'); return }
      const labels = { analysis: '生成分析', greeting: '生成招呼语', auto: '自动打招呼' }
      const accepted = await requestConfirm({
        title: `${labels[type]} · ${targets.length} 个岗位`,
        message: type === 'auto'
          ? '所选招呼语会加入后端批准队列并由沟通号发送。后端只消费本次选择，并始终遵守每日上限、随机间隔、同公司去重和风控熔断。'
          : '将按当前简历逐条调用后端处理所选岗位。',
        confirmText: `开始${labels[type]}`,
        tone: type === 'auto' ? 'danger' : 'primary',
      })
      if (!accepted) return
      if (type === 'analysis' || type === 'greeting') {
        try {
          const result = await enqueueAi('workbench', type, targets.map(job => job.job_key))
          const skipped = Number(result.skipped_existing_count || 0)
          const noJd = Number(result.skipped_no_jd_count || 0)
          // 无 JD 的岗位由后端剔除，批量时把三类结果一次讲清楚
          const skips = [skipped ? `${skipped} 个已有结果` : '',
            noJd ? `${noJd} 个缺 JD` : ''].filter(Boolean).join('、')
          if (type === 'greeting' && skipped) await refreshGreetings()
          showToast(result.added
            ? `${result.added} 个岗位已加入${labels[type]}队列${skips ? `，${skips}` : ''}`
            : noJd && !skipped ? `所选 ${noJd} 个岗位缺少职位描述（JD），补齐后才能${labels[type]}`
              : skips ? `所选岗位均无需生成：${skips}` : '所选岗位已在生成队列中',
            result.added ? 'info' : 'warn')
          if (!explicitKeys) selectedKeys.value = []
        } catch (error) { showToast(`${labels[type]}任务创建失败：${error.message}`, 'bad') }
        return
      }
      if (store.batch.running) return
      Object.assign(store.batch, { running: true, type, label: labels[type], done: 0, total: targets.length, failures: [] })
      try {
        const missing = targets.filter(job => !job.greetingId || !job.greeting)
        if (missing.length) {
          const queued = await enqueueAi('workbench', 'greeting', missing.map(job => job.job_key))
          const taskIds = (queued.tasks || []).map(task => task.id)
          const completed = await waitForTaskIds(taskIds)
          completed.filter(task => task.status !== 'succeeded').forEach(task => store.batch.failures.push(task.job_key))
          await Promise.allSettled([refreshGreetings(), refreshJobs()])
        }
        for (const [index, original] of targets.entries()) {
          const job = store.jobs.find(item => item.job_key === original.job_key) || original
          if (store.batch.failures.includes(job.job_key)) continue
          try { await saveGreeting(job, true) }
          catch (error) {
            job.actionError = error.message
            job.actionErrorType = 'greeting'
            store.batch.failures.push(job.job_key)
          }
          store.batch.done = index + 1
        }
        const sendKeys = keys.filter(key => !store.batch.failures.includes(key))
        if (sendKeys.length) {
          const result = await apiClient.greetings.sendBatch(sendKeys)
          if (result && result.ok === false) throw new ApiError(result.error || '发送队列启动失败')
          store.sendStatus = await apiClient.greetings.sendStatus()
        }
      } catch (error) {
        showToast(`自动打招呼启动失败：${error.message}`, 'bad')
        store.batch.failures.push('send-batch')
      }
      store.batch.running = false
      const failed = store.batch.failures.length
      const success = Math.max(0, targets.length - failed)
      showToast(type === 'auto' && !failed ? '招呼语已交给后端护栏队列处理' : failed ? `处理完成，成功 ${success} 个，失败 ${failed} 个` : `${labels[type]}已完成`, failed ? 'warn' : 'ok')
      await Promise.allSettled([refreshGreetings(), refreshJobs(), refreshDashboard()])
      if (!explicitKeys) selectedKeys.value = []
    }
    function retryFailed(job) {
      return job.actionErrorType === 'greeting' ? generateGreeting(job) : generateAnalysis(job)
    }
    async function removeFavorite(job) {
      try {
        await apiClient.jobs.favorite(job.job_key, false)
        job.favorite = false
        selectedKeys.value = selectedKeys.value.filter(key => key !== job.job_key)
        const next = jobs.value.find(item => item.job_key !== job.job_key)
        activeKey.value = next ? next.job_key : ''
        await refreshDashboard()
        showToast('已从收藏工作台移除', 'info')
      } catch (error) { showToast(`取消收藏失败：${error.message}`, 'bad') }
    }
    async function startInterview(job) {
      Object.assign(interview, { open: true, busy: true, sessionId: null, jobKey: job.job_key, totalQuestions: 0, questionIndex: 0, currentQuestion: '', answers: [], input: '', report: null, canFinish: false })
      try {
        const result = await apiClient.interviews.start(job.job_key, store.selectedResumeId)
        interview.sessionId = result.id
        interview.totalQuestions = Number(result.total_questions || 1)
        interview.currentQuestion = result.first_question || ''
      } catch (error) {
        interview.open = false
        showToast(`模拟面试启动失败：${error.message}`, 'bad')
      } finally { interview.busy = false }
    }
    async function finishInterview() {
      interview.busy = true
      try {
        const report = await apiClient.interviews.finish(interview.sessionId)
        interview.report = {
          score: report.score, summary: report.overall || report.summary || '',
          strengths: report.strengths || [], improvements: [...(report.risks || []), ...(report.prep || []), ...(report.improvements || [])],
        }
        const job = store.jobs.find(item => item.job_key === interview.jobKey)
        if (job) { job.contacted = true; job.applied = true; job.interviewed = true }
        await Promise.allSettled([refreshInterviews(), refreshJobs(), refreshDashboard()])
      } catch (error) { showToast(`面试报告生成失败：${error.message}`, 'bad') }
      finally { interview.busy = false }
    }
    async function submitAnswer() {
      if (interview.canFinish) { await finishInterview(); return }
      if (!interview.input.trim()) { showToast('请先输入回答', 'warn'); return }
      const question = interview.currentQuestion
      const answer = interview.input.trim()
      interview.busy = true
      try {
        const result = await apiClient.interviews.answer(interview.sessionId, answer)
        interview.answers.push({ question, answer, feedback: result.feedback || '' })
        interview.input = ''
        if (result.action === 'next') interview.questionIndex = Math.min(interview.totalQuestions - 1, interview.questionIndex + 1)
        interview.currentQuestion = result.content || ''
        interview.canFinish = /面试结束|生成报告|查看报告/.test(interview.currentQuestion)
      } catch (error) { showToast(`提交回答失败：${error.message}`, 'bad') }
      finally { interview.busy = false }
    }
    function closeInterview() { interview.open = false }
    watch([() => routeJobKey.value, () => favoriteJobs.value.map(job => job.job_key).join('|')], ([target]) => {
      const matched = favoriteJobs.value.find(job => job.job_key === target)
      if (matched) activeKey.value = matched.job_key
      else if (!favoriteJobs.value.some(job => job.job_key === activeKey.value)) {
        const fallback = jobs.value[0] || favoriteJobs.value[0]
        activeKey.value = fallback ? fallback.job_key : ''
      }
    }, { immediate: true })
    const activeAnalysisTask = job => job ? aiTaskFor('workbench', job.job_key, ['analysis']) : null
    const activeGreetingTask = job => job ? aiTaskFor('workbench', job.job_key, ['greeting']) : null
    return { store, query, statusFilter, selectedKeys, activeKey, interview, jobs, favoriteJobs, activeJob, allSelected, taskSummary,
      deriveJobTags, workflowLabel, scoreLabel, renderMarkdown, formatEta, taskProgressPercent, taskEtaSeconds,
      activeAnalysisTask, activeGreetingTask,
      cancelAiTasks, cancelAiTask,
      selectJob, toggleAll, openBoss, setWorkflowStage, generateAnalysis, generateGreeting, executeBatch,
      saveGreeting, selectGreetingVariant, copyAndOpenBoss, retryFailed, removeFavorite, startInterview, submitAnswer, closeInterview }
  },
  template: `
  <div>
    <header class="page-head workbench-head"><div><div class="eyebrow">候选岗位</div><h1>收藏工作台</h1><p>在同一视图完成分析、准备与沟通演练</p></div><label class="inline-select"><span>当前简历</span><select v-model.number="store.selectedResumeId" aria-label="当前简历"><option v-for="resume in store.resumes" :key="resume.id" :value="resume.id">{{resume.name}}</option></select></label></header>
    <div class="batch-toolbar">
      <label class="check"><input type="checkbox" :checked="allSelected" @change="toggleAll"> 全选当前岗位</label>
      <span class="selection-count">已选 {{selectedKeys.length}} 个</span><span class="grow"></span>
      <button :disabled="!selectedKeys.length" @click="executeBatch('analysis')">生成分析</button>
      <button :disabled="!selectedKeys.length" @click="executeBatch('greeting')">生成招呼语</button>
      <button class="primary" :disabled="!selectedKeys.length || store.batch.running" @click="executeBatch('auto')">自动打招呼</button>
    </div>
    <div v-if="taskSummary.visible" class="ai-task-progress" :class="{done:!taskSummary.running}"><div><b>{{taskSummary.label}}</b><span>动态并发 {{taskSummary.effective}} / {{taskSummary.max}}<template v-if="taskSummary.running"> · {{formatEta(taskSummary.etaSeconds)}}</template></span></div><div class="progress"><i :style="{width:taskSummary.percent+'%'}"></i></div><strong>{{taskSummary.completed}} / {{taskSummary.total}}</strong><small v-if="taskSummary.failures">{{taskSummary.failures}} 个失败，可在岗位内重新生成</small><button v-if="taskSummary.running" class="danger-quiet" :disabled="taskSummary.cancelling" @click="cancelAiTasks('workbench')">{{taskSummary.cancelling?'取消中…':'取消生成'}}</button></div>
    <div v-if="store.batch.running" class="batch-progress"><span>{{store.batch.label}}处理中</span><div class="progress"><i :style="{width:(store.batch.total?store.batch.done/store.batch.total*100:0)+'%'}"></i></div><b>{{store.batch.done}} / {{store.batch.total}}</b><small v-if="store.batch.failures.length">{{store.batch.failures.length}} 个待重试</small></div>
    <div class="workbench-layout">
      <aside class="workbench-list">
        <div class="workbench-filters"><input v-model="query" placeholder="搜索收藏岗位" aria-label="搜索收藏岗位"><select v-model="statusFilter" aria-label="求职状态"><option value="all">全部状态</option><option value="pending">待开始</option><option value="generated">已生成招呼语</option><option value="contacted">已打招呼</option><option value="applied">已投递</option><option value="interviewed">已面试</option><option value="offered">OFFER</option><option value="failed">处理失败</option></select></div>
        <div class="workbench-scroll">
          <button v-for="job in jobs" :key="job.job_key" class="workbench-job" :class="{active:activeJob&&activeJob.job_key===job.job_key}" @click="selectJob(job)">
            <span class="workbench-check" @click.stop><input type="checkbox" :value="job.job_key" v-model="selectedKeys" :aria-label="'选择 ' + job.title"></span>
            <span class="workbench-copy"><b>{{job.title}}</b><small>{{job.company || '公司信息保密'}} · {{job.salary}}</small><span class="tag-line"><span v-for="tag in deriveJobTags(job)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span><span class="tag neutral">{{workflowLabel(job)}}</span></span></span>
            <strong :class="{'score-pending':scoreLabel(job,'match_score','workbench')==='评分中'}">{{scoreLabel(job,'match_score','workbench')}}</strong>
          </button>
          <div v-if="!jobs.length" class="empty"><b>当前筛选无岗位</b></div>
        </div>
      </aside>
      <section v-if="activeJob" class="workbench-detail">
        <div class="detail-hero"><div><div class="tag-line"><span v-for="tag in deriveJobTags(activeJob)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span></div><h2>{{activeJob.title}}</h2><p>{{activeJob.company || '公司信息保密'}} · {{activeJob.location}} · {{activeJob.salary}} · {{activeJob.experience}}</p></div><div class="row"><button @click="openBoss(activeJob)">打开 BOSS</button><button @click="startInterview(activeJob)">模拟面试</button><button @click="removeFavorite(activeJob)">取消收藏</button></div></div>
        <section class="workflow-status"><div><h3>求职状态</h3><p>选择当前阶段，推进时自动补齐前置状态</p></div><div class="workflow-actions five"><button :class="{active:!activeJob.contacted&&!activeJob.applied&&!activeJob.interviewed&&!activeJob.offered}" @click="setWorkflowStage(activeJob,'pending')"><span>0</span>待开始</button><button :class="{active:activeJob.contacted&&!activeJob.applied}" @click="setWorkflowStage(activeJob,'contacted')"><span>1</span>已打招呼</button><button :class="{active:activeJob.applied&&!activeJob.interviewed}" @click="setWorkflowStage(activeJob,'applied')"><span>2</span>已投递</button><button :class="{active:activeJob.interviewed&&!activeJob.offered}" @click="setWorkflowStage(activeJob,'interviewed')"><span>3</span>已面试</button><button :class="{active:activeJob.offered}" @click="setWorkflowStage(activeJob,'offered')"><span>4</span>OFFER</button></div></section>
        <div class="score-overview"><div><span>岗位评分</span><strong :class="{'score-pending':scoreLabel(activeJob,'job_score','workbench')==='评分中'}">{{scoreLabel(activeJob,'job_score','workbench')}}</strong><small>岗位本身质量</small></div><div><span>匹配度评分</span><strong :class="{'score-pending':scoreLabel(activeJob,'match_score','workbench')==='评分中'}">{{scoreLabel(activeJob,'match_score','workbench')}}</strong><small>当前简历匹配</small></div><div><span>优先级</span><strong>{{activeJob.priority || '未评分'}}</strong><small>{{activeJob.active}}</small></div></div>
        <section class="detail-section"><div class="section-title"><div><h3>职位描述（JD）</h3><p>{{activeJob.industry}} · {{activeJob.scale || '规模未知'}}</p></div></div><p class="jd-copy">{{activeJob.jd}}</p></section>
        <section class="detail-section"><div class="section-title"><div><h3>招呼语</h3><p>按当前简历生成，可在三版之间切换并继续编辑</p></div><button v-if="!activeJob.greeting && !activeGreetingTask(activeJob)" @click="generateGreeting(activeJob)">生成招呼语</button></div><div v-if="activeGreetingTask(activeJob)" class="generation-stream"><span class="spinner"></span><div><b>正在生成招呼语</b><p>{{activeGreetingTask(activeJob).message || '任务已进入队列，生成内容将实时返回'}}</p><div class="generation-progress"><div class="progress"><i :style="{width:taskProgressPercent(activeGreetingTask(activeJob))+'%'}"></i></div><small>{{taskProgressPercent(activeGreetingTask(activeJob))}}% · {{formatEta(taskEtaSeconds(activeGreetingTask(activeJob),'workbench'))}}</small></div></div></div><template v-else-if="activeJob.greeting"><div class="variant-tabs" role="tablist"><button v-for="(variant,index) in activeJob.greetingVariants" :key="index" :class="{active:Number(activeJob.greetingSelectedIndex)===index}" role="tab" @click="selectGreetingVariant(activeJob,index)">{{activeJob.greetingLabels[index] || ('版本 ' + (index+1))}}</button></div><textarea v-model="activeJob.greeting" class="greeting-editor"></textarea></template><div v-else class="inline-empty">尚未生成招呼语</div><div v-if="activeJob.greeting && !activeGreetingTask(activeJob)" class="section-actions"><span>{{activeJob.greeting.length}} 字 · {{activeJob.greetingStatus==='approved'?'已批准':'草稿'}}</span><div class="row"><button @click="saveGreeting(activeJob)">保存并批准</button><button @click="copyAndOpenBoss(activeJob)">复制并打开 BOSS</button><button class="primary" @click="executeBatch('auto',[activeJob.job_key])">自动打招呼</button></div></div></section>
        <section class="detail-section"><div class="section-title"><div><h3>应聘建议</h3><p>结合岗位要求与当前简历，按重点分段呈现</p></div><button v-if="!activeJob.analysisReady && !activeAnalysisTask(activeJob)" @click="generateAnalysis(activeJob)">生成分析</button></div><div v-if="activeAnalysisTask(activeJob)" class="generation-stream"><span class="spinner"></span><div><b>正在生成分析</b><p>{{activeAnalysisTask(activeJob).message || '正在读取 JD 与当前简历'}}</p><div class="generation-progress"><div class="progress"><i :style="{width:taskProgressPercent(activeAnalysisTask(activeJob))+'%'}"></i></div><small>{{taskProgressPercent(activeAnalysisTask(activeJob))}}% · {{formatEta(taskEtaSeconds(activeAnalysisTask(activeJob),'workbench'))}}</small></div></div><button class="danger-quiet" :disabled="activeAnalysisTask(activeJob).cancel_requested" @click="cancelAiTask('workbench',activeAnalysisTask(activeJob))">{{activeAnalysisTask(activeJob).cancel_requested?'取消中…':'取消分析'}}</button></div><div v-else-if="activeJob.advice" class="advice-copy markdown-body" v-html="renderMarkdown(activeJob.advice)"></div><div v-else class="inline-empty">等待生成岗位分析与应聘建议</div></section>
        <div v-if="activeJob.actionError" class="notice bad"><b>上次处理失败</b><span>{{activeJob.actionError}}</span><button @click="retryFailed(activeJob)">重新生成</button></div>
      </section>
      <section v-else class="workbench-detail empty large"><b>选择一个收藏岗位查看详情</b></section>
    </div>

    <div v-if="interview.open" class="interview-overlay" role="dialog" aria-modal="true" aria-label="模拟面试">
      <header><div><span class="eyebrow">模拟面试</span><h2>{{store.jobs.find(j=>j.job_key===interview.jobKey)?.title}}</h2></div><button aria-label="关闭模拟面试" title="关闭" @click="closeInterview">×</button></header>
      <main v-if="!interview.report" class="interview-main">
        <div class="interview-progress"><span>第 {{Math.min(interview.questionIndex+1,interview.totalQuestions||1)}} / {{interview.totalQuestions||1}} 题</span><div class="progress"><i :style="{width:((interview.questionIndex+1)/(interview.totalQuestions||1)*100)+'%'}"></i></div></div>
        <div class="interview-history"><div v-for="(turn,index) in interview.answers" :key="index" class="interview-turn"><b>面试官</b><p>{{turn.question}}</p><b>我的回答</b><p>{{turn.answer}}</p><template v-if="turn.feedback"><b>面试官点评</b><p>{{turn.feedback}}</p></template></div></div>
        <section class="current-question"><span>面试官</span><h3>{{interview.busy && !interview.currentQuestion?'正在生成题目…':interview.currentQuestion}}</h3></section>
        <label v-if="!interview.canFinish" class="answer-box">我的回答<textarea v-model="interview.input" :disabled="interview.busy" placeholder="结合真实项目，按背景、行动、结果组织回答"></textarea></label>
        <div class="interview-actions"><button @click="closeInterview">退出面试</button><button class="primary" :disabled="interview.busy || (!interview.canFinish && !interview.input.trim())" @click="submitAnswer">{{interview.busy?'处理中…':interview.canFinish?'提交并生成报告':'提交回答'}}</button></div>
      </main>
      <main v-else class="interview-report"><div class="report-score"><strong>{{interview.report.score}}</strong><span>综合得分</span></div><div><h3>面试总结</h3><p>{{interview.report.summary}}</p><div class="report-columns"><section><h3>表现亮点</h3><ul><li v-for="item in interview.report.strengths" :key="item">{{item}}</li></ul></section><section><h3>改进建议</h3><ul><li v-for="item in interview.report.improvements" :key="item">{{item}}</li></ul></section></div><button class="primary" @click="closeInterview">完成并返回工作台</button></div></main>
    </div>
  </div>`,
}

// -- 页面：账号管理 -----------------------------------------------------
const AccountsView = {
  setup() {
    const busy = ref('')
    async function accountAction(account, action) {
      busy.value = `${account.key}-${action}`
      try {
        let result
        if (action === 'launch') result = await apiClient.accounts.launch(account.key)
        else if (action === 'login') result = await apiClient.accounts.login(account.key)
        else if (action === 'check') result = await apiClient.accounts.check(account.key)
        else result = await apiClient.accounts.stop(account.key)
        await refreshAccounts()
        const labels = { launch: '已启动', login: '已打开登录页', check: '登录态检测完成', stop: '已停止' }
        // 后端返回 ok:false 表示操作未真正完成（如 CDP 未就绪），如实提示而不是谎报成功
        if (action !== 'check' && result && result.ok === false) {
          showToast(`${account.label}操作失败：${result.error || '未知原因'}`, 'bad')
        } else {
          showToast(`${account.label}${labels[action]}`, action === 'stop' ? 'info' : 'ok')
        }
      } catch (error) {
        showToast(`${account.label}操作失败：${error.message}`, 'bad')
      } finally { busy.value = '' }
    }
    async function toggleMode(event) {
      const enabled = event.target.checked
      try {
        await apiClient.settings.save({ dual_account_enabled: enabled })
        store.settings.dualAccount = enabled
        await refreshAccounts()
        showToast(enabled ? '已切换为双账号隔离模式' : '已切换为单账号模式', 'info')
      } catch (error) {
        event.target.checked = store.settings.dualAccount
        showToast(`账号模式切换失败：${error.message}`, 'bad')
      }
    }
    return { store, busy, fmtTime, accountAction, toggleMode }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">运行环境</div><h1>账号管理</h1><p>采集与沟通账号状态</p></div></header>
    <section class="setting-row section-block"><div><h2>双账号隔离模式</h2><p>采集号负责只读操作，沟通号承接受护栏保护的沟通动作</p></div><label class="switch"><input type="checkbox" :checked="store.settings.dualAccount" @change="toggleMode"><span class="switch-track"><span class="switch-thumb"></span></span><span>{{store.settings.dualAccount?'已开启':'已关闭'}}</span></label></section>
    <div class="account-grid">
      <article v-for="account in store.accounts" :key="account.key" class="account-card">
        <header><div><span class="account-icon">{{account.label.slice(0,1)}}</span><div><h2>{{account.label}}</h2><p>{{account.description}}</p></div></div><span class="status-badge" :class="account.running?'running':'paused'">{{account.running?'运行中':'未启动'}}</span></header>
        <dl><div><dt>调试端口</dt><dd>{{account.port}}</dd></div><div><dt>登录状态</dt><dd :class="account.loggedIn===true?'ok':'warn'">{{account.loggedIn===true?'已登录':account.loggedIn===false?'未登录':'待检测'}}</dd></div><div><dt>上次检测</dt><dd>{{fmtTime(account.checkedAt)}}</dd></div></dl>
        <div class="tag-line"><span v-for="role in account.roles" :key="role" class="tag neutral">{{role}}</span></div>
        <footer><button :disabled="!!busy" @click="accountAction(account,'launch')">启动</button><button :disabled="!!busy" @click="accountAction(account,'login')">打开登录页</button><button :disabled="!!busy" @click="accountAction(account,'check')">{{busy===account.key+'-check'?'检测中…':'检测登录态'}}</button><button :disabled="!!busy || !account.running" @click="accountAction(account,'stop')">停止</button></footer>
      </article>
    </div>
  </div>`,
}

// -- 页面：设置 ---------------------------------------------------------
const SettingsView = {
  setup() {
    const draft = reactive(clone(store.settings))
    const testing = ref(false)
    const testResult = ref(null)
    async function save() {
      try {
        await apiClient.settings.save(settingsToApi(draft))
        await refreshSettings()
        Object.assign(draft, clone(store.settings))
        showToast('设置已保存到本地后端')
      } catch (error) { showToast(`设置保存失败：${error.message}`, 'bad') }
    }
    async function testConnection() {
      testResult.value = null
      if (!draft.llmBaseUrl.trim() || !draft.llmApiKey.trim() || !draft.llmModel.trim()) { showToast('请填写服务地址、API Key 和模型', 'warn'); return }
      testing.value = true
      try {
        const result = await apiClient.settings.testLlm({ base_url: draft.llmBaseUrl.trim(), api_key: draft.llmApiKey.trim(), model: draft.llmModel.trim() })
        testResult.value = { ok: result.ok !== false, text: `连接成功 · ${result.model || draft.llmModel} · ${result.latency_ms || 0} ms` }
      } catch (error) {
        testResult.value = { ok: false, text: `连接失败 · ${error.message}` }
      } finally { testing.value = false }
    }
    watch(() => store.settings, value => Object.assign(draft, clone(value)), { deep: true })
    const paceOptions = [
      { value: 'standard', label: '稳妥 · scraper 内置节奏，任务间隔 120s，最保守' },
      { value: 'balanced', label: '均衡 · 整体时长约减半，任务间隔 60s（推荐）' },
      { value: 'fast', label: '快速 · 约再砍半，任务间隔 20s，风控概率明显上升' },
    ]
    const paceHint = computed(() => (paceOptions.find(option => option.value === draft.collectPace) || paceOptions[1]).label)
    return { draft, testing, testResult, save, testConnection, isLlmConfigured, paceOptions, paceHint }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">系统偏好</div><h1>设置</h1><p>模型服务、发送护栏与评分参数</p></div><button class="primary" @click="save">保存设置</button></header>
    <section class="settings-section"><div class="section-title"><div><h2>LLM 服务</h2><p>用于采集计划、匹配度评分、招呼语和模拟面试</p></div><span class="status-badge" :class="isLlmConfigured(draft)?'completed':'paused'">{{isLlmConfigured(draft)?'已配置':'未配置'}}</span></div><div class="llm-token-warning"><b>请注意 Token 消耗</b><span>批量岗位评分、应聘分析和招呼语会按岗位分别调用模型；建议先筛选目标岗位，再按需加入生成队列。</span></div><div class="form-grid three"><label>Base URL<input v-model="draft.llmBaseUrl" placeholder="https://api.example.com/v1"></label><label>API Key<input v-model="draft.llmApiKey" type="password" placeholder="由本地后端保存"></label><label>模型<input v-model="draft.llmModel" placeholder="model-name"></label></div><div class="setting-actions"><button :disabled="testing" @click="testConnection">{{testing?'测试中…':'测试连通性'}}</button><span v-if="testResult" :class="testResult.ok?'ok':'bad'">{{testResult.text}}</span></div></section>
    <section class="settings-section"><div class="section-title"><div><h2>发送护栏</h2><p>自动打招呼始终遵守以下边界</p></div><span class="status-badge paused">不可关闭</span></div><div class="form-grid four"><label>每日上限<input type="number" v-model.number="draft.sendDailyLimit" min="1" max="110"></label><label>硬顶<input type="number" v-model.number="draft.sendDailyHardCap" min="1" max="110"></label><label>最小间隔（秒）<input type="number" v-model.number="draft.sendGapMin" min="30" max="90"></label><label>最大间隔（秒）<input type="number" v-model.number="draft.sendGapMax" min="30" max="90"></label></div><div class="guardrail-list"><span>每日上限 {{draft.sendDailyLimit}}</span><span>硬顶 {{draft.sendDailyHardCap}}</span><span>{{draft.sendGapMin}}-{{draft.sendGapMax}} 秒随机间隔</span><span>同公司 30 天去重</span><span>风控信号当日熔断</span></div></section>
    <section class="settings-section"><div class="section-title"><div><h2>采集节奏</h2><p>采集号只读抓取的随机等待与列表任务间隔，切换后从下一次采集生效</p></div></div><div class="form-grid two"><label>节奏档位<select v-model="draft.collectPace"><option v-for="option in paceOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label><p class="muted">{{ paceHint }}。越快越接近机器流量特征，命中风控信号当日熔断；重跑重复内容会自动提前停止翻页。</p></div></section>
    <section class="settings-section"><div class="section-title"><div><h2>评分与活跃度</h2><p>控制匹配度评分数量和岗位有效性判断</p></div></div><div class="form-grid two"><label>匹配度评分默认岗位数<input type="number" v-model.number="draft.matchScoreTopN" min="1" max="100"></label><label>HR 不活跃阈值（天）<input type="number" v-model.number="draft.inactiveDays" min="1" max="90"></label></div></section>
  </div>`,
}

// -- 页面：使用指南（新人引导） ------------------------------------------
// 注意事项内容与 README「注意事项」章节保持一致
const GUIDE_NOTES = [
  { tone: 'risk', title: '非官方工具，请先评估账号风控风险', detail:
    '本项目是本地运行的辅助程序，与 BOSS 直聘官方无关。自动化访问无法完全避免被平台识别，因此强烈建议用非常用的备用账号担任采集号；万一因此触发风控、功能受限甚至封号，后果需要由您自行承担，本项目概不负责。开始使用即视为您已知晓并接受这一前提。' },
  { tone: 'warn', title: '仅支持 Chrome 浏览器', detail:
    '采集与发送全程通过独立的 Chrome 配置文件模拟真人点击完成，依赖本机安装的 Google Chrome。配置文件相互隔离，不会影响您日常浏览器中的登录状态和数据。' },
  { tone: 'warn', title: '采集号与投递号建议分开两个账号', detail:
    '更安全的分工方式：一个专用账号只负责浏览和采集岗位数据（风控风险集中于此），日常打招呼、投递沟通交给另一个常用账号。应用默认开启的双账号隔离模式正是为这套分工设计的。' },
  { tone: 'warn', title: '首次采集耗时较久，建议一次采齐 JD', detail:
    '首次采集要逐条打开岗位页面抓取完整职位描述（JD），关键词和城市越多越慢，通常需要数十分钟到数小时，请耐心等待。创建计划时选择「采集完整JD」可把列表和详情一次采齐；过程中随时可以暂停或恢复。' },
  { tone: 'warn', title: '提前准备 API Key，并留意 token 消耗', detail:
    '所有 AI 能力（岗位评分、竞争力分析、招呼语等）都调用您自己的模型服务：需在设置页填好 Base URL、API Key 与模型名。批量任务按岗位逐条消耗 token，建议先人工筛掉明显不合适的岗位再批量生成；暂未配置时，规则评分与手动流程不受影响。' },
  { tone: 'ok', title: '简历直接粘贴即可，Markdown 或纯文本都能识别', detail:
    '在「简历档案」新建简历时，既可以粘贴现成的 Markdown 文档，也可以把 Word / PDF 简历里的文字直接复制进正文框——AI 会自动识别整理结构，再用于评分与招呼语生成。' },
  { tone: 'ok', title: '推荐与 BOSS 直聘原版分工搭配', detail:
    '让本项目承担海量岗位的筛选排序、竞争力分析与招呼语初稿；确定目标岗位后，再回到 BOSS 直聘原版完成发送，后续沟通交流也使用其自带的消息机制，既省力也更贴近真人习惯。' },
]
// 推荐使用顺序：向导覆盖前四步，后续步骤在各页面完成
const GUIDE_FLOW_STEPS = [
  { title: '填写简历', detail: '按向导第 1 步或在「简历档案」建立本次求职使用的简历' },
  { title: '配置 LLM', detail: '设置页填写 Base URL / API Key / 模型并测试连通性（可跳过，见注意事项）' },
  { title: '登录账号', detail: '「账号管理」启动并登录采集号与沟通号，双账号模式保持默认开启即可' },
  { title: '生成采集计划', detail: '向导第 4 步或「采集中心」新建计划：配置关键词、城市与每组页数' },
  { title: '等待采集完成', detail: '首次较久，可暂停恢复；完成后在采集中心确认岗位与 JD 是否齐全，缺失可一键补齐' },
  { title: '生成评分', detail: '「岗位列表」右上角确认当前简历无误后，点「匹配度评分」「岗位评分」，按这份简历批量评分' },
  { title: '挑选岗位', detail: '在岗位列表按匹配度排序、展开 JD 阅读比较，把最匹配或最有优势的岗位加入收藏' },
  { title: '工作台作战', detail: '「收藏工作台」里生成竞争力分析和招呼语，再「复制并打开 BOSS」人工发送，或交给受全部护栏保护的「自动打招呼」批量执行' },
]

const GuideView = {
  setup() {
    return { GUIDE_NOTES, GUIDE_FLOW_STEPS, requestWizardOpen }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">新人引导</div><h1>使用指南</h1><p>注意事项与推荐使用顺序，随时可以回来查看</p></div><button class="primary" @click="requestWizardOpen">打开求职作战向导</button></header>
    <section class="section-block guide-doc">
      <div class="section-title"><div><h2>注意事项</h2><p>开始之前请先阅读以下内容</p></div></div>
      <div class="guide-note-list">
        <article v-for="note in GUIDE_NOTES" :key="note.title" class="guide-note" :class="'tone-' + note.tone">
          <b>{{note.title}}</b><span>{{note.detail}}</span>
        </article>
      </div>
      <p class="guide-doc-foot">发送频率与熔断等安全约束由系统护栏强制执行且不可关闭，详见 README 的「发送护栏（不可关闭）」章节。</p>
    </section>
    <section class="section-block guide-doc">
      <div class="section-title"><div><h2>推荐使用顺序</h2><p>从简历到投递的完整流程，点击右上角按钮可跟随向导完成前四步</p></div></div>
      <ol class="guide-flow">
        <li v-for="step in GUIDE_FLOW_STEPS" :key="step.title"><b>{{step.title}}</b><span>{{step.detail}}</span></li>
      </ol>
    </section>
  </div>`,
}

// -- 应用组装 -----------------------------------------------------------
const App = {
  setup() {
    const view = computed(() => ({
      '/dashboard': DashboardView, '/profile': ProfileView, '/collect': CollectView,
      '/analytics': AnalyticsView, '/jobs': JobsView, '/jobcard': JobCardView,
      '/accounts': AccountsView, '/settings': SettingsView, '/guide': GuideView,
    })[route.value] || DashboardView)
    const collectionSummary = computed(() => {
      const runs = store.runs.filter(run => run.status === 'running' || run.status === 'paused')
      const primary = runs.find(run => run.status === 'running') || runs[0]
      const percent = runs.length
        ? Math.round(runs.reduce((sum, run) => sum + percentOf(run), 0) / runs.length) : 0
      return {
        visible: runs.length > 0,
        label: runs.some(run => run.status === 'running') ? '采集中' : '采集已暂停',
        percent, summary: primary ? runProgressSummary(primary) : '',
      }
    })
    const theme = ref(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
    function toggleTheme() {
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
      document.documentElement.dataset.theme = theme.value
      localStorage.setItem('theme', theme.value)
    }
    // 启动须知弹窗：默认每次启动都显示；仅当用户勾选「不再显示」后确认关闭才持久化
    const onboarding = reactive({ suppress: false })
    const onboardingOpen = ref(false)
    function tryStartOnboarding() {
      if (localStorage.getItem(ONBOARDING_KEY)) return
      onboarding.suppress = false
      onboardingOpen.value = true
    }
    function dismissOnboarding(target) {
      if (onboarding.suppress) markOnboardingDone()
      onboardingOpen.value = false
      if (target === 'wizard') requestWizardOpen()
      else if (target === 'guide') location.hash = '#/guide'
    }
    let runtimeTimer = null
    onMounted(async () => {
      await bootstrap()
      connectAiStreams()
      runtimeTimer = window.setInterval(refreshRuntime, 5000)
      // 后端连接异常时不弹引导，优先展示错误信息
      if (!store.ui.bootstrapError) tryStartOnboarding()
    })
    onBeforeUnmount(() => {
      if (runtimeTimer) window.clearInterval(runtimeTimer)
      closeAiStreams()
    })
    watch(() => store.selectedResumeId, async (id, previous) => {
      if (!id || !previous || id === previous || store.ui.bootstrapLoading) return
      store.ui.refreshing = true
      const cancellations = await Promise.allSettled([
        apiClient.ai.cancel('jobs', { resume_id: previous }),
        apiClient.ai.cancel('workbench', { resume_id: previous }),
      ])
      const cancelled = cancellations.reduce((total, result) => total + (
        result.status === 'fulfilled' ? Number(result.value.cancelled || 0) : 0), 0)
      store.aiTracking.jobs = []
      store.aiTracking.workbench = []
      if (cancelled) showToast(`已停止旧简历的 ${cancelled} 个生成任务`, 'info')
      const errors = await runLoaders([
        ['岗位', refreshJobs], ['招呼语', refreshGreetings], ['数据分析', refreshAnalytics],
        ['采集配置', refreshCollectConfig],
        ['岗位生成任务', () => refreshAiTasks('jobs', false)],
        ['工作台生成任务', () => refreshAiTasks('workbench', false)],
      ])
      store.ui.bootstrapError = errors.join('；')
      store.ui.refreshing = false
    })
    return { store, route, nav, view, theme, collectionSummary, toggleTheme,
      onboarding, onboardingOpen, dismissOnboarding, settleConfirm, bootstrap }
  },
  template: `
  <div class="layout">
    <aside class="side">
      <div class="brand-row"><div class="logo">boss<span>-copilot</span></div><span class="demo-badge">本地数据</span></div>
      <nav aria-label="主导航"><a v-for="[href,label] in nav" :key="href" :class="{on:route===href}" :href="'#'+href">{{label}}</a></nav>
      <div class="side-bottom">
        <a v-if="collectionSummary.visible" class="side-progress" href="#/collect"><div><span>{{collectionSummary.label}}</span><b>{{collectionSummary.percent}}%</b></div><div class="progress"><i :style="{width:collectionSummary.percent+'%'}"></i></div><small>{{collectionSummary.summary}}</small></a>
        <button class="theme-toggle" type="button" :aria-label="theme==='dark'?'切换浅色主题':'切换深色主题'" @click="toggleTheme"><span>{{theme==='dark'?'☀':'☾'}}</span>{{theme==='dark'?'切换浅色':'切换深色'}}</button>
        <div class="side-foot"><span class="status-dot" :class="store.ui.bootstrapError?'warn':'ok'"></span>{{store.ui.bootstrapError?'后端连接异常':'本地后端已连接'}}</div>
      </div>
    </aside>
    <main class="main">
      <div v-if="store.ui.bootstrapError" class="notice bad"><b>部分数据加载失败</b><span>{{store.ui.bootstrapError}}</span><button :disabled="store.ui.bootstrapLoading" @click="bootstrap">重新加载</button></div>
      <div v-if="store.ui.bootstrapLoading" class="empty large"><span class="spinner"></span><b>正在连接本地后端…</b></div>
      <component v-else :is="view" />
    </main>

    <transition name="toast"><div v-if="store.ui.toast" class="toast" :class="store.ui.toast.tone" role="status">{{store.ui.toast.message}}</div></transition>
    <div v-if="store.ui.confirm" class="modal-backdrop" role="presentation" @click.self="settleConfirm(false)">
      <section class="confirm-dialog" role="dialog" aria-modal="true" :aria-label="store.ui.confirm.title"><h2>{{store.ui.confirm.title}}</h2><p>{{store.ui.confirm.message}}</p><div class="row end"><button @click="settleConfirm(false)">取消</button><button :class="store.ui.confirm.tone==='danger'?'danger':'primary'" @click="settleConfirm(true)">{{store.ui.confirm.confirmText}}</button></div></section>
    </div>

    <div v-if="onboardingOpen" class="modal-backdrop" role="presentation">
      <section class="onboarding-dialog" role="dialog" aria-modal="true" aria-label="启动须知">
        <h3>欢迎使用 boss-copilot · 求职作战室</h3>
        <p class="onboarding-lead">每次启动都会先展示这份须知。完整教程与推荐使用顺序见左侧导航「使用指南」，也可从下方按钮直达。</p>
        <div class="guide-note tone-risk onboarding-risk"><b>非官方工具 · 账号风控风险提示</b><span>本项目并非 BOSS 直聘官方程序，自动化访问无法完全避免被平台识别。建议用非常用的备用账号担任采集号；如因使用本项目触发风控、限制或封禁，后果由您自行承担，本项目概不负责。继续使用即代表您已知晓并接受上述内容。</span></div>
        <ul class="onboarding-list">
          <li>仅支持 <b>Chrome 浏览器</b>：采集与发送均通过独立的 Chrome 配置文件完成，不影响日常浏览器数据。</li>
          <li>建议<b>采集号与投递号分开两个账号</b>：保持双账号隔离模式默认开启即可。</li>
          <li><b>首次采集耗时较久</b>（可能数十分钟以上），创建计划时勾选「采集完整JD」一次采齐。</li>
          <li>AI 功能需自备 <b>API Key</b>（BYOK），批量任务会逐岗位消耗 token，请留意开销。</li>
          <li>简历粘贴 <b>Markdown 或纯文本</b>均可，AI 会自动识别整理。</li>
          <li>分工建议：筛选分析交给本项目，<b>沟通交流回到 BOSS 直聘原版</b>的消息机制。</li>
        </ul>
        <label class="onboarding-check"><input type="checkbox" v-model="onboarding.suppress">下次启动不再显示</label>
        <div class="row end onboarding-actions"><button @click="dismissOnboarding('guide')">查看完整指南</button><button @click="dismissOnboarding('wizard')">打开求职作战向导</button><button class="primary" @click="dismissOnboarding('close')">我已知晓</button></div>
      </section>
    </div>
  </div>`,
}

createApp(App).mount('#app')
