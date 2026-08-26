import {
  createApp, ref, reactive, computed,
} from '/static/vue.esm-browser.prod.js'
import { marked } from '/static/vendor/marked.esm.js'
import DOMPurify from '/static/vendor/purify.es.mjs'

// -- 通用格式化与安全渲染 -----------------------------------------------
const clone = value => JSON.parse(JSON.stringify(value))
const wait = (ms = 500) => new Promise(resolve => setTimeout(resolve, ms))
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
const percentOf = run => run.target ? Math.min(100, Math.round(run.collected / run.target * 100)) : 0

marked.setOptions({ gfm: true, breaks: true })
function renderMarkdown(value) {
  const html = marked.parse(String(value || ''))
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form'],
    FORBID_ATTR: ['style'],
  })
}

// -- 演示数据：所有业务状态仅存在于当前页面会话 ----------------------------
const INITIAL_RESUMES = [
  {
    id: 1,
    name: '嵌入式软件主简历',
    updatedAt: '2026-08-26T09:20:00+08:00',
    body: `# 林知远

**嵌入式软件工程师 · 4 年经验 · 深圳**

## 核心能力

- 熟练使用 C、C++、Python，具备 STM32、GD32 与 FreeRTOS 量产经验
- 掌握 SPI、I2C、UART、CAN、BLE、4G 等通信链路
- 负责过医疗设备与智能硬件从原型、注册到量产的完整交付
- 使用 AI Agent 完成驱动开发、自动化测试和日志分析

## 工作经历

| 时间 | 公司 | 职位 |
| --- | --- | --- |
| 2023 - 至今 | 澄川医疗 | 嵌入式软件工程师 |
| 2021 - 2023 | 云栖电子 | 硬件工程师 |

## 代表项目

### 多通道生理信号采集设备

基于 STM32H7 与 FreeRTOS 完成 16 通道信号采集、USB 高速传输和设备状态管理，建立自动化压测工具并支持产品注册验证。

### 低功耗物联网终端

完成 BLE 与 4G 双链路通信、A/B 分区 OTA、异常恢复和远程日志系统。`,
  },
  {
    id: 2,
    name: 'AI 应用方向简历',
    updatedAt: '2026-08-25T18:10:00+08:00',
    body: `# 林知远

**AI 应用工程师 · 嵌入式背景**

## 技术栈

- Python、FastAPI、TypeScript、Vue、Electron
- LLM Agent、工具调用、RAG、自动化评测
- 嵌入式研发流程与软硬件联合调试

## 项目经验

1. 参与桌面 AI IDE 开发，负责工具调用、历史回放与质量验收。
2. 建立自动化测试体系，为复杂工程新增 200 余个场景。
3. 将大型工程目录扫描从 16 秒优化至 0.3 秒。`,
  },
]

const INITIAL_JOBS = [
  {
    job_key: 'demo-anker-fw', title: '嵌入式软件工程师', company: '安澜智能',
    salary: '30-50K·16薪', salary_min: 30, salary_max: 50, experience: '3-5年', degree: '本科',
    location: '深圳·宝安区', industry: '智能硬件', scale: '1000-9999人', source_keyword: '嵌入式软件工程师',
    job_score: 86, match_score: 94, priority: 'P0', active: '刚刚活跃', active_ts: '2026-08-26T14:18:00+08:00',
    favorite: true, excluded: false, contacted: false, analysisReady: true, actionError: '',
    jd: '负责智能硬件嵌入式软件架构与驱动开发，要求熟悉 STM32、FreeRTOS、BLE 与低功耗设计。团队实行周末双休，提供五险一金、年终奖、带薪年假和定期体检。',
    greeting: '您好，我有 4 年嵌入式产品开发经验，长期使用 STM32、FreeRTOS 和 BLE，完整参与过产品量产交付。贵司岗位与我的技术背景高度匹配，期待进一步沟通。',
    advice: '匹配度高。沟通时优先强调量产经验、低功耗优化和复杂问题定位能力，并准备一个 FreeRTOS 任务调度或 BLE 功耗优化案例。',
  },
  {
    job_key: 'demo-agent', title: 'AI Agent 应用工程师', company: '灵犀科技',
    salary: '25-40K·15薪', salary_min: 25, salary_max: 40, experience: '3-5年', degree: '本科',
    location: '深圳·南山区', industry: '人工智能', scale: '100-499人', source_keyword: 'AI Agent',
    job_score: 82, match_score: 91, priority: 'P0', active: '今日活跃', active_ts: '2026-08-26T10:26:00+08:00',
    favorite: true, excluded: false, contacted: false, analysisReady: true, actionError: '',
    jd: '负责企业级 Agent 产品研发，使用 Python、FastAPI 与大模型工具调用。要求具备工程质量意识和自动化测试经验。提供弹性工作、股票期权、餐补与节日福利。',
    greeting: '',
    advice: '可重点展示 AI IDE 项目中的工具调用和测试体系建设，避免只谈模型效果，突出工程可靠性、可观测性与交付速度。',
  },
  {
    job_key: 'demo-hunter', title: '机器人嵌入式软件工程师', company: '某大型机器人公司',
    salary: '28-45K·14薪', salary_min: 28, salary_max: 45, experience: '3-5年', degree: '本科',
    location: '深圳', industry: '机器人', scale: '1000-9999人', source_keyword: '机器人嵌入式',
    job_score: 79, match_score: 89, priority: 'P0', active: '3日内活跃', active_ts: '2026-08-24T16:30:00+08:00',
    favorite: true, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '猎头代招机器人嵌入式软件岗位，负责电机控制、CAN 通信和 RTOS 平台维护。要求有量产项目经验。',
    greeting: '', advice: '',
  },
  {
    job_key: 'demo-medical', title: '高级嵌入式工程师（医疗器械）', company: '澄海医疗',
    salary: '24-38K·13薪', salary_min: 24, salary_max: 38, experience: '5-10年', degree: '本科',
    location: '深圳·光明区', industry: '医疗健康', scale: '500-999人', source_keyword: '医疗嵌入式',
    job_score: 91, match_score: 96, priority: 'P0', active: '今日活跃', active_ts: '2026-08-26T09:12:00+08:00',
    favorite: true, excluded: false, contacted: true, applied: true, interviewed: false, analysisReady: true, actionError: '',
    jd: '负责二类医疗设备固件架构、风险控制与注册验证，要求熟悉 GB 9706、EMC 整改和量产流程。双休，提供补充医疗、住房补贴、年终奖。',
    greeting: '您好，我有医疗器械嵌入式开发与注册验证经验，熟悉 GB 9706、EMC 整改和量产问题闭环，希望有机会进一步了解岗位。',
    advice: '这是最匹配的岗位之一。面试应准备注册检验、风险控制与 EMC 整改的完整闭环案例，并量化自己主导的型号数量。',
  },
  {
    job_key: 'demo-secret', title: '资深 MCU 开发工程师', company: '',
    salary: '22-35K', salary_min: 22, salary_max: 35, experience: '3-5年', degree: '大专',
    location: '东莞·松山湖', industry: '电子制造', scale: '', source_keyword: 'MCU开发',
    job_score: 67, match_score: 84, priority: 'P1', active: '本周活跃', active_ts: '2026-08-22T11:00:00+08:00',
    favorite: false, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '负责消费电子 MCU 固件开发，熟悉 GD32、USB、UART 和生产测试工具。',
    greeting: '', advice: '',
  },
  {
    job_key: 'demo-rtos', title: 'RTOS 平台软件工程师', company: '远景智造',
    salary: '26-42K·14薪', salary_min: 26, salary_max: 42, experience: '3-5年', degree: '本科',
    location: '广州·黄埔区', industry: '工业自动化', scale: '500-999人', source_keyword: 'RTOS',
    job_score: 80, match_score: 90, priority: 'P0', active: '刚刚活跃', active_ts: '2026-08-26T14:05:00+08:00',
    favorite: true, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '负责 RTOS 内核适配、BSP、驱动框架和性能分析。周末双休，五险一金，提供交通补贴和员工旅游。',
    greeting: '', advice: '',
  },
  {
    job_key: 'demo-iot', title: '物联网终端软件工程师', company: '星途物联',
    salary: '20-32K·13薪', salary_min: 20, salary_max: 32, experience: '1-3年', degree: '本科',
    location: '杭州·滨江区', industry: '物联网', scale: '100-499人', source_keyword: '物联网终端',
    job_score: 75, match_score: 88, priority: 'P1', active: '今日活跃', active_ts: '2026-08-26T08:50:00+08:00',
    favorite: false, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '负责 4G、BLE 终端软件开发与 OTA 平台建设，要求熟悉 MQTT、TLS 和异常恢复。提供五险一金、餐补、带薪年假。',
    greeting: '', advice: '',
  },
  {
    job_key: 'demo-edge-ai', title: '边缘 AI 嵌入式工程师', company: '见微智能',
    salary: '30-55K·15薪', salary_min: 30, salary_max: 55, experience: '5-10年', degree: '硕士',
    location: '上海·浦东新区', industry: '人工智能', scale: '100-499人', source_keyword: '边缘AI',
    job_score: 73, match_score: 78, priority: 'P1', active: '3日内活跃', active_ts: '2026-08-24T13:20:00+08:00',
    favorite: false, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '负责端侧模型部署、算子优化和嵌入式 Linux 系统开发，要求熟悉 C++、ARM NEON 与模型量化。',
    greeting: '', advice: '',
  },
  {
    job_key: 'demo-bsp', title: '嵌入式 Linux BSP 工程师', company: '凌光科技',
    salary: '25-45K', salary_min: 25, salary_max: 45, experience: '3-5年', degree: '本科',
    location: '深圳·龙岗区', industry: '计算机硬件', scale: '500-999人', source_keyword: 'BSP',
    job_score: 61, match_score: 69, priority: 'P2', active: '本周活跃', active_ts: '2026-08-21T15:00:00+08:00',
    favorite: false, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '负责 Linux 内核、设备树、驱动与启动性能优化，要求有 Yocto 和 PCIe 经验。',
    greeting: '', advice: '',
  },
  {
    job_key: 'demo-test', title: '嵌入式自动化测试工程师', company: '知行电子',
    salary: '18-28K·14薪', salary_min: 18, salary_max: 28, experience: '1-3年', degree: '本科',
    location: '苏州·工业园区', industry: '智能硬件', scale: '100-499人', source_keyword: '嵌入式测试',
    job_score: 76, match_score: 92, priority: 'P1', active: '今日活跃', active_ts: '2026-08-26T07:45:00+08:00',
    favorite: true, excluded: false, contacted: false, analysisReady: true, actionError: '',
    jd: '负责固件自动化测试平台、串口日志分析和硬件在环测试，要求熟悉 Python。双休，提供年终奖、节日福利和定期体检。',
    greeting: '',
    advice: '技术匹配度很高，但岗位更偏测试。需要确认职业路径是否接受，并突出自动化平台对研发效率和质量的提升。',
  },
  {
    job_key: 'demo-control', title: '运动控制软件工程师', company: '极点机器人',
    salary: '24-40K', salary_min: 24, salary_max: 40, experience: '3-5年', degree: '本科',
    location: '深圳·南山区', industry: '机器人', scale: '100-499人', source_keyword: '运动控制',
    job_score: 68, match_score: 74, priority: 'P2', active: '3日内活跃', active_ts: '2026-08-24T10:00:00+08:00',
    favorite: false, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '负责电机控制、轨迹规划和实时通信，要求掌握 C++、EtherCAT 和控制理论。',
    greeting: '', advice: '',
  },
  {
    job_key: 'demo-firmware', title: '固件开发工程师', company: '青禾能源',
    salary: '20-30K·13薪', salary_min: 20, salary_max: 30, experience: '3-5年', degree: '本科',
    location: '深圳·坪山区', industry: '新能源', scale: '1000-9999人', source_keyword: '固件工程师',
    job_score: 72, match_score: 86, priority: 'P1', active: '本周活跃', active_ts: '2026-08-20T09:30:00+08:00',
    favorite: false, excluded: false, contacted: false, analysisReady: false, actionError: '',
    jd: '负责储能设备固件、Bootloader、CAN 通信和现场问题定位。提供五险一金、年终奖和交通补贴。',
    greeting: '', advice: '',
  },
]

const INITIAL_RUNS = [
  { id: 105, name: '嵌入式核心岗位补采', startedAt: '2026-08-26T13:42:00+08:00', finishedAt: '', status: 'running', enabled: true, collected: 6, target: 12, fetchDetails: true, source: 'AI 自动生成', kind: 'plan', jobKeys: [] },
  { id: 104, name: 'BOSS 收藏同步', startedAt: '2026-08-26T12:10:00+08:00', finishedAt: '2026-08-26T12:12:00+08:00', status: 'completed', enabled: true, collected: 6, target: 6, fetchDetails: true, source: 'BOSS 收藏同步', kind: 'favorite_sync', jobKeys: [] },
  { id: 103, name: '深圳 AI Agent 岗位', startedAt: '2026-08-25T19:10:00+08:00', finishedAt: '2026-08-25T19:36:00+08:00', status: 'completed', enabled: true, collected: 8, target: 8, fetchDetails: true, source: '手动配置', kind: 'plan', jobKeys: [] },
  { id: 102, name: '医疗器械定向采集', startedAt: '2026-08-24T10:05:00+08:00', finishedAt: '2026-08-24T10:24:00+08:00', status: 'completed', enabled: true, collected: 7, target: 7, fetchDetails: false, source: 'AI 自动生成', kind: 'plan', jobKeys: [] },
  { id: 101, name: '边缘 AI 机会探索', startedAt: '2026-08-23T16:20:00+08:00', finishedAt: '2026-08-23T16:31:00+08:00', status: 'failed', enabled: false, collected: 3, target: 9, fetchDetails: true, source: '手动配置', kind: 'plan', jobKeys: [] },
]

const INITIAL_RUN_JOB_KEYS = {
  105: ['demo-anker-fw', 'demo-agent', 'demo-hunter', 'demo-medical', 'demo-secret', 'demo-rtos'],
  104: ['demo-anker-fw', 'demo-agent', 'demo-hunter', 'demo-medical', 'demo-rtos', 'demo-test'],
  103: ['demo-agent', 'demo-edge-ai', 'demo-bsp', 'demo-test', 'demo-control', 'demo-firmware', 'demo-iot', 'demo-rtos'],
  102: ['demo-medical', 'demo-anker-fw', 'demo-secret', 'demo-iot', 'demo-test', 'demo-firmware', 'demo-rtos'],
  101: ['demo-edge-ai', 'demo-bsp', 'demo-control'],
}

const store = reactive({
  resumes: clone(INITIAL_RESUMES),
  selectedResumeId: 1,
  jobs: clone(INITIAL_JOBS),
  runs: clone(INITIAL_RUNS),
  accounts: [
    { key: 'collect', label: '采集号', description: '只读采集与详情补齐', port: 9222, running: true, loggedIn: true, checkedAt: '2026-08-26T11:38:56+08:00', roles: ['岗位采集', '详情读取'] },
    { key: 'communication', label: '沟通号', description: '受护栏保护的沟通操作', port: 9223, running: false, loggedIn: true, checkedAt: '2026-08-26T11:39:45+08:00', roles: ['打开 BOSS', '自动招呼'] },
  ],
  settings: {
    dualAccount: true,
    llmBaseUrl: 'https://api.example.com/v1',
    llmApiKey: 'demo-key',
    llmModel: 'demo-chat',
    sendDailyLimit: 40,
    sendDailyHardCap: 110,
    sendGapMin: 30,
    sendGapMax: 90,
    matchScoreTopN: 20,
    inactiveDays: 14,
  },
  ui: { toast: null, confirm: null },
  batch: { running: false, type: '', label: '', done: 0, total: 0, failures: [] },
})

function isLlmConfigured(settings = store.settings) {
  return Boolean(String(settings.llmBaseUrl || '').trim()
    && String(settings.llmApiKey || '').trim()
    && String(settings.llmModel || '').trim())
}

store.runs.forEach(run => { run.jobKeys = clone(INITIAL_RUN_JOB_KEYS[run.id] || []) })
store.jobs.forEach(job => {
  job.applied = Boolean(job.applied)
  job.interviewed = Boolean(job.interviewed)
})

function nextRunId() {
  return Math.max(0, ...store.runs.map(run => Number(run.id))) + 1
}

function jobsFromEnabledRuns() {
  const enabledKeys = new Set(store.runs.filter(run => run.enabled).flatMap(run => run.jobKeys))
  return store.jobs.filter(job => enabledKeys.has(job.job_key) && !job.excluded)
}

function addFavoriteSyncRun() {
  const jobKeys = store.jobs.filter(job => job.favorite && !job.excluded).map(job => job.job_key)
  const now = new Date().toISOString()
  const run = {
    id: nextRunId(), name: 'BOSS 收藏同步', startedAt: now, finishedAt: now,
    status: 'completed', enabled: true, collected: jobKeys.length, target: jobKeys.length,
    fetchDetails: true, source: 'BOSS 收藏同步', kind: 'favorite_sync', jobKeys,
  }
  store.runs.unshift(run)
  return run
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

// 模拟采集任务在页面会话内持续推进，切换路由不会中断。
setInterval(() => {
  for (const run of store.runs) {
    if (run.status !== 'running') continue
    run.collected = Math.min(run.target, run.collected + 1)
    const nextJob = store.jobs[run.collected - 1]
    if (nextJob && !run.jobKeys.includes(nextJob.job_key)) run.jobKeys.push(nextJob.job_key)
    if (run.collected >= run.target) {
      run.status = 'completed'
      run.finishedAt = new Date().toISOString()
      showToast(`采集计划「${run.name}」已完成`)
    }
  }
}, 2600)

// -- 岗位标签、排序与文案生成 -------------------------------------------
const WELFARE_KEYWORDS = [
  '五险一金', '年终奖', '带薪年假', '补充医疗', '餐补', '房补', '住房补贴',
  '交通补贴', '股票期权', '定期体检', '节日福利', '员工旅游', '弹性工作',
]
function deriveJobTags(job) {
  const company = String(job.company || '').trim()
  const text = `${job.title || ''}\n${job.jd || ''}`
  const tags = []
  if (!company || company.includes('某')) tags.push({ key: 'headhunter', label: '猎头', tone: 'warn' })
  if (/周末双休|双休/.test(text)) tags.push({ key: 'weekend', label: '双休', tone: 'ok' })
  if (WELFARE_KEYWORDS.some(keyword => text.includes(keyword))) tags.push({ key: 'benefits', label: '福利', tone: 'blue' })
  return tags
}
function workflowLabel(job) {
  if (job.interviewed) return '已面试'
  if (job.applied) return '已投递'
  if (job.contacted) return '已打招呼'
  if (job.greeting) return '已生成'
  return '待准备'
}
function greetingFor(job) {
  const resume = store.resumes.find(item => item.id === store.selectedResumeId) || store.resumes[0]
  return `您好，我正在使用「${resume.name}」关注贵司的${job.title}岗位。我有嵌入式产品交付、自动化测试与复杂问题定位经验，岗位要求与我的经历较为匹配，期待进一步沟通。`
}
function adviceFor(job) {
  const tags = deriveJobTags(job).map(tag => tag.label)
  const focus = job.industry === '医疗健康'
    ? '重点准备法规验证、风险控制和量产闭环案例'
    : job.industry === '人工智能'
      ? '重点说明工程可靠性、工具调用与自动化评测方法'
      : '重点准备架构取舍、驱动调试与量产问题定位案例'
  return `综合匹配度 ${job.match_score} 分，建议优先沟通。${focus}。${tags.includes('猎头') ? '当前岗位可能由猎头代招，沟通前先确认真实公司、汇报关系与岗位编制。' : '首轮沟通可直接确认团队规模、核心职责与招聘优先级。'}`
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
]
const ROUTE_ALIASES = { '/greetings': '/jobcard', '/interview': '/jobcard' }
const VALID_ROUTES = new Set(nav.map(([path]) => path))
function normalizedRoute() {
  const raw = location.hash.slice(1) || '/dashboard'
  const path = ROUTE_ALIASES[raw] || (VALID_ROUTES.has(raw) ? raw : '/dashboard')
  if (path !== raw) history.replaceState(null, '', `#${path}`)
  return path
}
const route = ref(normalizedRoute())
window.addEventListener('hashchange', () => { route.value = normalizedRoute() })

// -- 通用图表 -----------------------------------------------------------
const SvgBars = {
  props: ['items', 'color'],
  setup(props) {
    const rows = computed(() => (props.items || []).slice(0, 10))
    const max = computed(() => Math.max(1, ...rows.value.map(item => Number(item.value || 0))))
    return { rows, max }
  },
  template: `
    <div class="bar-chart">
      <div v-for="item in rows" :key="item.label" class="bar-row">
        <span :title="item.label">{{item.label}}</span>
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
    const contacted = computed(() => activeJobs.value.filter(job => job.contacted))
    const generated = computed(() => activeJobs.value.filter(job => job.greeting))
    const staleJobs = computed(() => {
      const cutoff = Date.now() - 3 * 24 * 60 * 60 * 1000
      return activeJobs.value.filter(job => new Date(job.active_ts).getTime() < cutoff)
    })
    const metrics = computed(() => [
      { label: '当前岗位', value: activeJobs.value.length, sub: `${activeJobs.value.filter(job => job.priority === 'P0').length} 个 P0 优先岗位`, href: '#/jobs', tone: 'blue' },
      { label: '收藏岗位', value: favorites.value.length, sub: `${favorites.value.filter(job => job.match_score >= 90).length} 个匹配度 90+`, href: '#/jobcard', tone: 'green' },
      { label: '已生成招呼语', value: generated.value.length, sub: `${favorites.value.length - generated.value.length} 个待生成`, href: '#/jobcard', tone: 'amber' },
      { label: '已模拟招呼', value: contacted.value.length, sub: '全部为演示状态', href: '#/jobcard', tone: 'red' },
    ])
    const latestRun = computed(() => store.runs[0])
    const priorityJobs = computed(() => [...favorites.value].sort((a, b) => b.match_score - a.match_score).slice(0, 5))
    const runningCount = computed(() => store.runs.filter(run => run.status === 'running' || run.status === 'paused').length)
    const guide = reactive({
      open: false, step: 1, busy: '', resumeMode: 'existing', resumeId: store.selectedResumeId,
      resumeName: '新的求职简历', resumeBody: '# 我的简历\n\n在这里填写 Markdown 简历正文。', synced: false,
      llmBaseUrl: store.settings.llmBaseUrl, llmApiKey: store.settings.llmApiKey,
      llmModel: store.settings.llmModel, llmTested: false, llmAvailable: isLlmConfigured(),
    })
    const guidePlan = reactive(createPlan())
    const guideCityQuery = ref('')
    const guideCitySuggestions = computed(() => matchingCities(guideCityQuery.value, guidePlan.cities))
    const guideLlmConfigured = computed(() => isLlmConfigured({
      llmBaseUrl: guide.llmBaseUrl, llmApiKey: guide.llmApiKey, llmModel: guide.llmModel,
    }))
    const llmReady = computed(() => isLlmConfigured())
    const communicationAccount = computed(() => store.accounts.find(account => account.key === 'communication'))
    function openGuide() {
      Object.assign(guide, {
        open: true, step: 1, busy: '', resumeMode: store.resumes.length ? 'existing' : 'new',
        resumeId: store.selectedResumeId, resumeName: '新的求职简历',
        resumeBody: '# 我的简历\n\n在这里填写 Markdown 简历正文。', synced: false,
        llmBaseUrl: store.settings.llmBaseUrl, llmApiKey: store.settings.llmApiKey,
        llmModel: store.settings.llmModel, llmTested: false, llmAvailable: isLlmConfigured(),
      })
      Object.assign(guidePlan, createPlan(), { name: '向导创建的采集计划' })
      guideCityQuery.value = ''
    }
    function closeGuide() { guide.open = false }
    function finishResumeStep() {
      if (guide.resumeMode === 'new') {
        if (!guide.resumeName.trim() || !guide.resumeBody.trim()) {
          showToast('请填写简历名称和正文', 'warn')
          return
        }
        const id = Math.max(0, ...store.resumes.map(item => item.id)) + 1
        store.resumes.push({ id, name: guide.resumeName.trim(), body: guide.resumeBody, updatedAt: new Date().toISOString() })
        guide.resumeId = id
      }
      store.selectedResumeId = Number(guide.resumeId)
      guidePlan.resumeId = store.selectedResumeId
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
      await wait(750)
      guide.busy = ''
      guide.llmTested = true
      guide.llmAvailable = true
      showToast(`LLM 连接成功 · ${guide.llmModel}`)
    }
    function saveGuideLlm() {
      Object.assign(store.settings, {
        llmBaseUrl: guide.llmBaseUrl.trim(), llmApiKey: guide.llmApiKey.trim(), llmModel: guide.llmModel.trim(),
      })
      guide.llmAvailable = isLlmConfigured()
      if (!guide.llmAvailable) guidePlan.mode = 'manual'
      guide.step = 3
    }
    async function guideLogin() {
      guide.busy = 'login'
      await wait(650)
      communicationAccount.value.running = true
      communicationAccount.value.loggedIn = true
      communicationAccount.value.checkedAt = new Date().toISOString()
      guide.busy = ''
      showToast('BOSS 登录状态已确认')
    }
    async function guideSyncFavorites() {
      if (!communicationAccount.value.loggedIn) {
        showToast('请先完成 BOSS 登录', 'warn')
        return
      }
      guide.busy = 'sync'
      await wait(850)
      const run = addFavoriteSyncRun()
      guide.synced = true
      guide.busy = ''
      showToast(`已同步 ${run.collected} 个收藏岗位，并记为一次采集`)
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
      guide.busy = 'plan'
      await wait(900)
      const resume = store.resumes.find(item => item.id === store.selectedResumeId)
      const aiDirection = resume && resume.name.includes('AI')
      Object.assign(guidePlan, {
        mode: 'auto', generated: true,
        name: aiDirection ? 'AI 应用岗位采集' : '嵌入式岗位采集',
        keywords: aiDirection ? 'AI Agent 应用工程师\nLLM 应用工程师\n边缘 AI 工程师' : '嵌入式软件工程师\n固件开发工程师\nRTOS 平台工程师',
        cities: ['深圳'], salary: '不限', experience: '不限', degree: '不限',
        scale: '不限', stage: '不限', industry: '不限', companies: '',
      })
      guide.busy = ''
      showToast('AI 采集计划已生成，可继续修改')
    }
    function reviewGuidePlan() {
      const keywords = guidePlan.keywords.split(/[\n,，]+/).map(value => value.trim()).filter(Boolean)
      if (!guidePlan.name.trim() || !keywords.length || !guidePlan.cities.length) {
        showToast('请填写计划名称、关键词并至少选择一个城市', 'warn')
        return
      }
      guide.step = 5
    }
    function finishGuide() {
      createCollectionRun(guidePlan, '向导配置')
      guide.open = false
      location.hash = '#/dashboard'
      showToast('向导已完成，采集计划开始执行')
    }
    return {
      store, metrics, latestRun, priorityJobs, runningCount, staleJobs, guide, guidePlan,
      communicationAccount, guideCityQuery, guideCitySuggestions, guideLlmConfigured, llmReady,
      percentOf, shortTime, deriveJobTags, runStatusLabel,
      openGuide, closeGuide, finishResumeStep, testGuideLlm, saveGuideLlm, guideLogin, guideSyncFavorites,
      addGuideCity, removeGuideCity, guideGeneratePlan, reviewGuidePlan, finishGuide,
    }
  },
  template: `
  <div>
    <header class="page-head">
      <div><div class="eyebrow">工作概览</div><h1>总览看板</h1><p>求职进度、采集状态与优先岗位</p></div>
      <div class="row"><button @click="openGuide">向导</button><a class="button primary" href="#/collect">新建采集计划</a></div>
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
          <div class="progress-copy"><span>{{latestRun.collected}} / {{latestRun.target}} 条</span><b>{{percentOf(latestRun)}}%</b></div>
          <div class="progress"><i :style="{width:percentOf(latestRun)+'%'}"></i></div>
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
        <a v-for="job in priorityJobs" :key="job.job_key" href="#/jobcard" class="priority-row">
          <div><b>{{job.title}}</b><span>{{job.company || '公司信息保密'}} · {{job.salary}}</span></div>
          <div class="tag-line"><span v-for="tag in deriveJobTags(job)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span></div>
          <strong>{{job.match_score}}</strong><small>匹配度</small>
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
          <div class="guide-actions"><span></span><button class="primary" @click="finishResumeStep">下一步</button></div>
        </section>

        <section v-if="guide.step===2" class="guide-content">
          <div><h3>配置并测试 LLM 服务</h3><p>用于 AI 采集计划、岗位评分、匹配度评分、AI 招呼语和模拟面试。</p></div>
          <div v-if="!guideLlmConfigured" class="llm-warning"><b>LLM 尚未配置</b><span>跳过后，AI 生成采集计划、岗位评分、匹配度评分、AI 招呼语和模拟面试将不可用，手动采集仍可使用。</span></div>
          <div v-else-if="guide.llmTested" class="llm-success"><b>连接测试通过</b><span>{{guide.llmModel}} 可用</span></div>
          <div class="form-grid three"><label>Base URL<input v-model="guide.llmBaseUrl" placeholder="https://api.example.com/v1" @input="guide.llmTested=false"></label><label>API Key<input v-model="guide.llmApiKey" type="password" placeholder="sk-…" @input="guide.llmTested=false"></label><label>模型<input v-model="guide.llmModel" placeholder="model-name" @input="guide.llmTested=false"></label></div>
          <div class="guide-actions"><button @click="guide.step=1">上一步</button><div class="row"><button v-if="!guideLlmConfigured" @click="saveGuideLlm">暂不配置，继续</button><button :disabled="guide.busy || !guideLlmConfigured" @click="testGuideLlm">{{guide.busy==='llm'?'测试中…':'测试连接'}}</button><button v-if="guideLlmConfigured" class="primary" :disabled="!guide.llmTested" @click="saveGuideLlm">保存并下一步</button></div></div>
        </section>

        <section v-if="guide.step===3" class="guide-content">
          <div><h3>登录 BOSS 并同步收藏岗位</h3><p>同步结果会作为一条独立采集记录，可在采集中心启用或禁用。</p></div>
          <div class="guide-account"><span class="account-icon">沟</span><div><b>{{communicationAccount.loggedIn?'BOSS 已登录':'等待登录 BOSS'}}</b><small>{{communicationAccount.running?'沟通号运行中':'沟通号尚未启动'}}</small></div><button :disabled="guide.busy" @click="guideLogin">{{guide.busy==='login'?'检测中…':communicationAccount.loggedIn?'重新检测':'登录 BOSS'}}</button></div>
          <div class="guide-sync"><div><b>同步 BOSS 收藏岗位 <span class="optional-mark">可选</span></b><span>{{guide.synced?'本次同步已加入采集历史':'读取“感兴趣”岗位并加入本地岗位池，也可稍后处理'}}</span></div><button class="primary" :disabled="guide.busy || guide.synced" @click="guideSyncFavorites">{{guide.busy==='sync'?'同步中…':guide.synced?'同步完成':'开始同步'}}</button></div>
          <div class="guide-actions"><button @click="guide.step=2">上一步</button><button class="primary" @click="guide.step=4">{{guide.synced?'下一步':'跳过，下一步'}}</button></div>
        </section>

        <section v-if="guide.step===4" class="guide-content">
          <div><h3>设置新的采集计划</h3><p>配置主动搜索范围，稍后可在采集中心继续调整。</p></div>
          <div class="segmented"><button :class="{active:guidePlan.mode==='manual'}" @click="guidePlan.mode='manual';guidePlan.generated=true">手动配置</button><button :class="{active:guidePlan.mode==='auto'}" @click="guidePlan.mode='auto';guidePlan.generated=false">AI 生成</button></div>
          <div v-if="guidePlan.mode==='auto'" class="guide-ai-plan"><div><b>{{llmReady?'根据当前简历生成采集配置':'LLM 未配置，AI 生成不可用'}}</b><span>{{llmReady?'AI 只生成关键词与城市，不附加额外筛选条件':'返回上一步配置 LLM，或切换为手动配置'}}</span></div><button class="primary" :disabled="guide.busy || !llmReady" @click="guideGeneratePlan">{{guide.busy==='plan'?'生成中…':guidePlan.generated?'重新生成':'AI 生成采集计划'}}</button></div>
          <div class="form-grid two"><label>计划名称<input v-model="guidePlan.name"></label><label>每组页数<input type="number" min="1" max="10" v-model.number="guidePlan.pages"></label></div>
          <label>搜索关键词<textarea class="short" v-model="guidePlan.keywords" placeholder="每行一个关键词"></textarea></label>
          <div class="field-group"><span class="field-label">城市范围</span><div class="city-picker"><div class="city-input-row"><input v-model="guideCityQuery" placeholder="输入城市关键词" @keyup.enter="addGuideCity()"><button @click="addGuideCity()">添加</button></div><div v-if="guideCitySuggestions.length" class="city-suggestions"><button v-for="city in guideCitySuggestions" :key="city" @click="addGuideCity(city)">{{city}}</button></div><div class="city-tags"><span v-for="city in guidePlan.cities" :key="city" class="city-tag">{{city}}<button :aria-label="'移除城市 ' + city" @click="removeGuideCity(city)">×</button></span></div></div></div>
          <div class="field-group"><span class="field-label">采集详细程度</span><div class="segmented"><button :class="{active:!guidePlan.fetchDetails}" @click="guidePlan.fetchDetails=false">仅岗位列表</button><button :class="{active:guidePlan.fetchDetails}" @click="guidePlan.fetchDetails=true">采集完整JD（推荐）</button></div></div>
          <div class="guide-actions"><button @click="guide.step=3">上一步</button><button class="primary" :disabled="guidePlan.mode==='auto' && !guidePlan.generated" @click="reviewGuidePlan">检查计划</button></div>
        </section>

        <section v-if="guide.step===5" class="guide-content guide-finish">
          <span class="finish-mark">✓</span><h3>准备开始采集</h3><p>基础配置已确认，采集开始后可在左侧查看总进度。</p>
          <dl><div><dt>当前简历</dt><dd>{{store.resumes.find(item=>item.id===store.selectedResumeId)?.name}}</dd></div><div><dt>LLM</dt><dd>{{llmReady?guide.llmModel+' · 可用':'未配置 · AI 功能不可用'}}</dd></div><div><dt>收藏同步</dt><dd>{{guide.synced?'已作为采集记录应用':'已跳过，可稍后同步'}}</dd></div><div><dt>采集计划</dt><dd>{{guidePlan.name}} · {{guidePlan.cities.join('、')}}</dd></div></dl>
          <div class="guide-actions"><button @click="guide.step=4">返回修改</button><button class="primary" @click="finishGuide">开始采集并返回总览</button></div>
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
    function save() {
      if (!draft.name.trim() || !draft.body.trim()) {
        showToast('请填写档案名称和简历正文', 'warn')
        return
      }
      const item = selected.value
      item.name = draft.name.trim()
      item.body = draft.body
      item.updatedAt = new Date().toISOString()
      editing.value = false
      showToast('简历档案已保存到当前演示会话')
    }
    function createResume() {
      const id = Math.max(0, ...store.resumes.map(item => item.id)) + 1
      store.resumes.push({ id, name: '未命名简历', body: '# 新简历\n\n在这里填写简历正文。', updatedAt: new Date().toISOString() })
      selectedId.value = id
      store.selectedResumeId = id
      loadDraft()
      editing.value = true
    }
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
const CITY_OPTIONS = [
  '北京', '上海', '天津', '重庆', '深圳', '广州', '东莞', '佛山', '珠海', '惠州',
  '杭州', '宁波', '温州', '嘉兴', '绍兴', '金华', '南京', '苏州', '无锡', '常州',
  '南通', '扬州', '武汉', '长沙', '郑州', '成都', '绵阳', '西安', '厦门', '福州',
  '泉州', '合肥', '南昌', '济南', '青岛', '烟台', '沈阳', '大连', '长春', '哈尔滨',
  '石家庄', '太原', '呼和浩特', '兰州', '西宁', '银川', '乌鲁木齐', '昆明', '贵阳', '南宁',
  '海口', '三亚', '拉萨', '香港', '澳门',
]
function matchingCities(query, selected) {
  const keyword = String(query || '').trim().replace(/市$/, '')
  if (!keyword) return []
  return CITY_OPTIONS.filter(city => city.includes(keyword) && !selected.includes(city)).slice(0, 8)
}
function appendCity(list, value) {
  const city = String(value || '').trim().replace(/市$/, '')
  if (city && !list.includes(city)) list.push(city)
  return city
}
const FILTER_OPTIONS = {
  experience: ['不限', '1-3年', '3-5年', '5-10年'],
  degree: ['不限', '大专', '本科', '硕士'],
  scale: ['不限', '20-99人', '100-499人', '500-999人', '1000人以上'],
  stage: ['不限', '未融资', 'A轮', 'B轮', '已上市'],
  industry: ['不限', '智能硬件', '人工智能', '医疗健康', '机器人', '物联网'],
}
const createPlan = () => ({
  name: '新的采集计划', mode: 'auto', fetchDetails: true, resumeId: store.selectedResumeId,
  keywords: '', cities: ['深圳'], pages: 3, salary: '20-50K', experience: '3-5年',
  degree: '本科', scale: '不限', stage: '不限', industry: '不限', companies: '', generated: false,
})
function createCollectionRun(plan, sourceOverride = '') {
  const keywords = String(plan.keywords || '').split(/[\n,，]+/).map(value => value.trim()).filter(Boolean)
  const target = Math.min(store.jobs.length, Math.max(6, keywords.length * plan.cities.length))
  const run = {
    id: nextRunId(), name: plan.name.trim(), startedAt: new Date().toISOString(), finishedAt: '',
    status: 'running', enabled: true, collected: 0, target, fetchDetails: plan.fetchDetails,
    source: sourceOverride || (plan.mode === 'auto' ? 'AI 自动生成' : '手动配置'), kind: 'plan', jobKeys: [],
  }
  store.runs.unshift(run)
  return run
}
function runStatusLabel(status) {
  return { running: '采集中', paused: '已暂停', completed: '已完成', failed: '失败', cancelled: '已取消' }[status] || status
}
function xmlEscape(value) {
  return String(value ?? '').replace(/[<>&"']/g, char => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' }[char]))
}
function safeSpreadsheetValue(value) {
  const text = String(value ?? '')
  return /^[=+\-@]/.test(text) ? `'${text}` : text
}
function exportRunAsXls(run) {
  const keys = run.jobKeys.length ? run.jobKeys : store.jobs.slice(0, run.collected).map(job => job.job_key)
  const rows = keys.map(key => store.jobs.find(job => job.job_key === key)).filter(Boolean)
  if (!rows.length) { showToast('当前记录还没有可导出的岗位', 'warn'); return }
  const headers = ['岗位', '公司', '薪资', '地点', '经验', '学历', '标签', 'JD', '采集时间']
  const values = rows.map(job => [
    job.title, job.company || '公司信息保密', job.salary, job.location, job.experience, job.degree,
    deriveJobTags(job).map(tag => tag.label).join('、'), run.fetchDetails ? job.jd : '', fmtTime(run.startedAt),
  ])
  const cell = value => `<Cell><Data ss:Type="String">${xmlEscape(safeSpreadsheetValue(value))}</Data></Cell>`
  const table = [headers, ...values].map(row => `<Row>${row.map(cell).join('')}</Row>`).join('')
  const xml = `<?xml version="1.0" encoding="UTF-8"?><?mso-application progid="Excel.Sheet"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="采集结果"><Table>${table}</Table></Worksheet></Workbook>`
  const blob = new Blob(['\ufeff', xml], { type: 'application/vnd.ms-excel;charset=utf-8' })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = `采集结果_${run.id}.xls`
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(link.href), 0)
  showToast(`已导出 ${rows.length} 条岗位数据`)
}

const CollectView = {
  setup() {
    const screen = ref('history')
    const step = ref(1)
    const generating = ref(false)
    const syncingFavorites = ref(false)
    const cityQuery = ref('')
    const plan = reactive(createPlan())
    const keywords = computed(() => plan.keywords.split(/[\n,，]+/).map(value => value.trim()).filter(Boolean))
    const companyList = computed(() => plan.companies.split(/\n/).map(value => value.trim()).filter(Boolean))
    const citySuggestions = computed(() => matchingCities(cityQuery.value, plan.cities))
    const llmReady = computed(() => isLlmConfigured())
    const resume = computed(() => store.resumes.find(item => item.id === Number(plan.resumeId)) || store.resumes[0])
    function startWizard() {
      Object.assign(plan, createPlan())
      step.value = 1
      screen.value = 'wizard'
      cityQuery.value = ''
    }
    function closeWizard() { screen.value = 'history' }
    async function syncFavoriteJobs() {
      syncingFavorites.value = true
      await wait(800)
      const run = addFavoriteSyncRun()
      syncingFavorites.value = false
      showToast(`已同步 ${run.collected} 个 BOSS 收藏岗位，并新增采集记录`)
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
      generating.value = true
      await wait(1100)
      const aiDirection = resume.value.name.includes('AI')
      Object.assign(plan, {
        name: aiDirection ? 'AI 应用与 Agent 机会' : '嵌入式核心岗位补采',
        keywords: aiDirection ? 'AI Agent 应用工程师\nLLM 应用工程师\n边缘 AI 工程师' : '嵌入式软件工程师\n固件开发工程师\nRTOS 平台工程师\n医疗器械嵌入式',
        cities: ['深圳'], pages: 3, salary: '不限', experience: '不限',
        degree: '不限', scale: '不限', stage: '不限', industry: '不限', companies: '', generated: true,
      })
      generating.value = false
      showToast('AI 采集计划已生成，可继续修改')
    }
    function nextToConfig() {
      step.value = 2
      if (plan.mode === 'manual') plan.generated = true
    }
    function toReview() {
      if (!plan.name.trim() || !keywords.value.length || !plan.cities.length) {
        showToast('请填写计划名称、关键词并至少选择一个城市', 'warn')
        return
      }
      step.value = 3
    }
    function launchPlan() {
      createCollectionRun(plan)
      screen.value = 'history'
      showToast(`采集计划「${plan.name.trim()}」已开始`)
    }
    function toggleRun(run) {
      if (run.status === 'running') { run.status = 'paused'; showToast('采集计划已暂停', 'info') }
      else if (run.status === 'paused') { run.status = 'running'; showToast('采集计划已继续') }
    }
    function toggleRunData(run, event) {
      run.enabled = event.target.checked
      showToast(run.enabled ? `已应用「${run.name}」的岗位数据` : `已禁用「${run.name}」的岗位数据`, 'info')
    }
    async function cancelRun(run) {
      const accepted = await requestConfirm({
        title: '取消采集计划',
        message: `确认取消「${run.name}」？已采集的 ${run.collected} 条数据会保留，可继续通过“应用数据”开关控制。`,
        confirmText: '确认取消', tone: 'danger',
      })
      if (!accepted) return
      run.status = 'cancelled'
      run.finishedAt = new Date().toISOString()
      showToast('采集计划已取消', 'info')
    }
    return { store, screen, step, plan, generating, syncingFavorites, cityQuery, keywords, companyList, citySuggestions, llmReady, resume, FILTER_OPTIONS,
      percentOf, fmtTime, runStatusLabel, startWizard, closeWizard, addCity, removeCity, generatePlan,
      nextToConfig, toReview, launchPlan, syncFavoriteJobs, toggleRun, toggleRunData, cancelRun, exportRunAsXls }
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
              <td><strong class="table-number">{{run.collected}}</strong><span class="muted"> / {{run.target}} 条</span></td>
              <td><div class="run-progress-cell"><div class="progress-copy"><span class="status-badge" :class="run.status">{{runStatusLabel(run.status)}}</span><b>{{run.status==='completed'?100:percentOf(run)}}%</b><small>{{run.collected}} / {{run.target}}</small></div><div class="progress"><i :style="{width:(run.status==='completed'?100:percentOf(run))+'%'}"></i></div></div></td>
              <td><label class="switch compact"><input type="checkbox" :checked="run.enabled" @change="toggleRunData(run,$event)"><span class="switch-track"><span class="switch-thumb"></span></span><span>{{run.enabled?'已应用':'已禁用'}}</span></label></td>
              <td><div class="row end"><button v-if="run.status==='running' || run.status==='paused'" @click="toggleRun(run)">{{run.status==='running'?'暂停':'继续'}}</button><button v-if="run.status==='running' || run.status==='paused'" class="danger-quiet" @click="cancelRun(run)">取消</button><button class="icon-button" :disabled="!run.collected" title="导出 Excel 兼容文件" :aria-label="'导出 ' + run.name" @click="exportRunAsXls(run)">↓</button></div></td>
            </tr>
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
        <div class="field-group"><span class="field-label">计划生成方式</span><div class="segmented wide"><button :class="{active:plan.mode==='auto'}" :disabled="!llmReady" @click="plan.mode='auto'"><b>AI 自动生成</b><small>根据选定简历生成关键词与范围</small></button><button :class="{active:plan.mode==='manual'}" @click="plan.mode='manual'"><b>手动选择</b><small>逐项配置关键词、城市和筛选</small></button></div></div>
        <div class="field-group"><span class="field-label">采集详细程度</span><div class="segmented"><button :class="{active:!plan.fetchDetails}" @click="plan.fetchDetails=false">采集岗位列表</button><button :class="{active:plan.fetchDetails}" @click="plan.fetchDetails=true">采集完整JD（推荐）</button></div></div>
        <div class="wizard-actions"><span></span><button class="primary" @click="nextToConfig">下一步</button></div>
      </section>

      <section v-if="step===2" class="wizard-panel">
        <div v-if="plan.mode==='auto' && !plan.generated" class="ai-generate">
          <div><span class="ai-mark">AI</span><h2>根据简历生成采集计划</h2><p>{{llmReady?'选择本次采集使用的简历档案':'LLM 未配置，AI 生成功能不可用'}}</p></div>
          <label>简历档案<select v-model.number="plan.resumeId"><option v-for="item in store.resumes" :key="item.id" :value="item.id">{{item.name}}</option></select></label>
          <button class="primary" :disabled="generating || !llmReady" @click="generatePlan"><span v-if="generating" class="spinner"></span>{{generating?'正在分析简历…':'生成采集计划'}}</button>
        </div>
        <template v-else>
          <div class="form-grid two"><label>计划名称<input v-model="plan.name" maxlength="40"></label><label>每个关键词页数<input type="number" v-model.number="plan.pages" min="1" max="10"></label></div>
          <label class="block-label">搜索关键词<textarea class="short" v-model="plan.keywords" placeholder="每行一个关键词"></textarea></label>
          <div class="field-group"><span class="field-label">城市范围</span><div class="city-picker"><div class="city-input-row"><input v-model="cityQuery" placeholder="输入城市关键词" @keyup.enter="addCity()"><button @click="addCity()">添加</button></div><div v-if="citySuggestions.length" class="city-suggestions"><button v-for="city in citySuggestions" :key="city" @click="addCity(city)">{{city}}</button></div><div class="city-tags"><span v-for="city in plan.cities" :key="city" class="city-tag">{{city}}<button :aria-label="'移除城市 ' + city" @click="removeCity(city)">×</button></span></div></div></div>
          <div class="form-grid three"><label>薪资范围<select v-model="plan.salary"><option>不限</option><option>15-30K</option><option>20-50K</option><option>30K以上</option></select></label><label>经验<select v-model="plan.experience"><option v-for="value in FILTER_OPTIONS.experience" :key="value">{{value}}</option></select></label><label>学历<select v-model="plan.degree"><option v-for="value in FILTER_OPTIONS.degree" :key="value">{{value}}</option></select></label><label>公司规模<select v-model="plan.scale"><option v-for="value in FILTER_OPTIONS.scale" :key="value">{{value}}</option></select></label><label>融资阶段<select v-model="plan.stage"><option v-for="value in FILTER_OPTIONS.stage" :key="value">{{value}}</option></select></label><label>行业<select v-model="plan.industry"><option v-for="value in FILTER_OPTIONS.industry" :key="value">{{value}}</option></select></label></div>
          <label v-if="plan.mode==='manual'" class="block-label">定向公司<textarea class="short" v-model="plan.companies" placeholder="每行一个公司名称，可留空"></textarea></label>
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
function countRows(items, getter) {
  const counts = new Map()
  items.forEach(item => {
    const label = getter(item) || '未知'
    counts.set(label, (counts.get(label) || 0) + 1)
  })
  return [...counts].map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value)
}
const AnalyticsView = {
  components: { SvgBars },
  setup() {
    const filters = reactive({ keyword: '', city: '', tag: '', dateFrom: '', dateTo: '' })
    const baseJobs = computed(() => jobsFromEnabledRuns())
    const jobs = computed(() => baseJobs.value.filter(job => {
      if (filters.keyword && job.source_keyword !== filters.keyword) return false
      if (filters.city && !job.location.startsWith(filters.city)) return false
      if (filters.tag && !deriveJobTags(job).some(tag => tag.key === filters.tag)) return false
      if (filters.dateFrom && job.active_ts.slice(0, 10) < filters.dateFrom) return false
      if (filters.dateTo && job.active_ts.slice(0, 10) > filters.dateTo) return false
      return true
    }))
    const meta = computed(() => ({
      keywords: [...new Set(baseJobs.value.map(job => job.source_keyword))],
      cities: [...new Set(baseJobs.value.map(job => job.location.split('·')[0]))],
    }))
    const summary = computed(() => {
      const values = jobs.value
      const avgMin = values.length ? Math.round(values.reduce((sum, job) => sum + job.salary_min, 0) / values.length) : 0
      const avgMax = values.length ? Math.round(values.reduce((sum, job) => sum + job.salary_max, 0) / values.length) : 0
      return {
        total: values.length, avg: `${avgMin}-${avgMax}K`,
        highMatch: values.filter(job => job.match_score >= 90).length,
        favoriteRate: values.length ? Math.round(values.filter(job => job.favorite).length / values.length * 100) : 0,
      }
    })
    const salaryRows = computed(() => countRows(jobs.value, job => job.salary_max >= 45 ? '45K以上' : job.salary_max >= 35 ? '35-45K' : job.salary_max >= 25 ? '25-35K' : '25K以下'))
    const tagRows = computed(() => {
      const tags = jobs.value.flatMap(job => deriveJobTags(job).map(tag => ({ label: tag.label })))
      return countRows(tags, item => item.label)
    })
    const charts = computed(() => [
      { title: '薪资上限分布', rows: salaryRows.value, color: 'var(--chart-1)' },
      { title: '经验要求', rows: countRows(jobs.value, job => job.experience), color: 'var(--chart-2)' },
      { title: '学历要求', rows: countRows(jobs.value, job => job.degree), color: 'var(--chart-3)' },
      { title: '行业分布', rows: countRows(jobs.value, job => job.industry), color: 'var(--chart-4)' },
      { title: '岗位标签', rows: tagRows.value, color: 'var(--chart-5)' },
      { title: 'P 级分布', rows: countRows(jobs.value, job => job.priority), color: 'var(--chart-6)' },
      { title: '城市分布', rows: countRows(jobs.value, job => job.location.split('·')[0]), color: 'var(--chart-7)' },
      { title: '采集趋势', rows: store.runs.slice().reverse().map(run => ({ label: shortTime(run.startedAt).split(' ')[0], value: run.collected })), color: 'var(--chart-8)' },
    ])
    function resetFilters() { Object.assign(filters, { keyword: '', city: '', tag: '', dateFrom: '', dateTo: '' }) }
    return { filters, jobs, meta, summary, charts, resetFilters }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">市场洞察</div><h1>数据分析</h1><p>从演示岗位中观察机会分布与匹配质量</p></div><button @click="resetFilters">重置筛选</button></header>
    <div class="filterbar analytics-filter"><select v-model="filters.keyword"><option value="">全部关键词</option><option v-for="value in meta.keywords" :key="value">{{value}}</option></select><select v-model="filters.city"><option value="">全部城市</option><option v-for="value in meta.cities" :key="value">{{value}}</option></select><select v-model="filters.tag"><option value="">全部标签</option><option value="headhunter">猎头</option><option value="weekend">双休</option><option value="benefits">福利</option></select><label>起始日期<input type="date" v-model="filters.dateFrom"></label><label>结束日期<input type="date" v-model="filters.dateTo"></label></div>
    <div class="metric-grid compact"><div class="metric tone-blue"><span>分析样本</span><strong>{{summary.total}}</strong><small>当前筛选内岗位</small></div><div class="metric tone-green"><span>平均月薪</span><strong>{{summary.avg}}</strong><small>按薪资上下限估算</small></div><div class="metric tone-amber"><span>高匹配岗位</span><strong>{{summary.highMatch}}</strong><small>匹配度 90 分及以上</small></div><div class="metric tone-red"><span>收藏率</span><strong>{{summary.favoriteRate}}%</strong><small>当前样本收藏占比</small></div></div>
    <div class="chart-grid"><section v-for="chart in charts" :key="chart.title" class="chart-panel"><h2>{{chart.title}}</h2><SvgBars :items="chart.rows" :color="chart.color" /></section></div>
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
    const sortState = reactive({ key: 'match_score', direction: 'desc' })
    const columns = [
      { key: 'title', label: '岗位' }, { key: 'company', label: '公司' }, { key: 'salary_max', label: '薪资' },
      { key: 'experience', label: '经验 / 学历' }, { key: 'job_score', label: '岗位评分' },
      { key: 'match_score', label: '匹配度评分' }, { key: 'priority', label: 'P级' }, { key: 'active_ts', label: '活跃时间' },
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
    function toggleExpanded(job) { expanded.value = expanded.value === job.job_key ? '' : job.job_key }
    function toggleFavorite(job) {
      job.favorite = !job.favorite
      showToast(job.favorite ? '已加入收藏工作台' : '已取消收藏', job.favorite ? 'ok' : 'info')
    }
    async function excludeJob(job) {
      const accepted = await requestConfirm({ title: '排除岗位', message: `确认将「${job.title} · ${job.company || '公司信息保密'}」移出当前列表？`, confirmText: '确认排除', tone: 'danger' })
      if (!accepted) return
      job.excluded = true
      expanded.value = ''
      showToast('岗位已移入排除项', 'info')
    }
    async function simulateScore(kind) {
      showToast(`${kind} 评分正在演示执行`, 'info')
      await wait(700)
      showToast(`${kind} 评分已完成`)
    }
    return { q, favoriteFilter, tagFilter, expanded, sortState, columns, sortedJobs, deriveJobTags,
      setSort, ariaSort, toggleExpanded, toggleFavorite, excludeJob, simulateScore }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">岗位池</div><h1>岗位列表</h1><p>共 {{sortedJobs.length}} 个符合条件的演示岗位</p></div><div class="row"><button @click="simulateScore('匹配度评分')">匹配度评分</button><button class="primary" @click="simulateScore('岗位评分')">岗位评分</button></div></header>
    <div class="filterbar"><input v-model="q" aria-label="搜索岗位或公司" placeholder="搜索岗位、公司或地点"><select v-model="tagFilter" aria-label="岗位标签"><option value="all">全部标签</option><option value="headhunter">猎头</option><option value="weekend">双休</option><option value="benefits">福利</option></select><select v-model="favoriteFilter" aria-label="收藏状态"><option value="all">全部岗位</option><option value="only">仅收藏</option><option value="exclude">未收藏</option></select><span class="filter-result">{{sortedJobs.length}} 条结果</span></div>
    <div class="table-wrap">
      <table class="jobs-table">
        <thead><tr><th v-for="column in columns" :key="column.key" :aria-sort="ariaSort(column.key)"><button class="sort-button" @click="setSort(column.key)"><span>{{column.label}}</span><i :class="{active:sortState.key===column.key,desc:sortState.key===column.key&&sortState.direction==='desc'}"></i></button></th></tr></thead>
        <tbody>
          <template v-for="job in sortedJobs" :key="job.job_key">
            <tr class="job-row" :class="{open:expanded===job.job_key}" tabindex="0" @click="toggleExpanded(job)" @keyup.enter="toggleExpanded(job)">
              <td data-label="岗位"><div class="job-title"><span class="chevron"></span><div><b>{{job.title}}</b><p>{{job.location}}</p><div class="tag-line"><span v-for="tag in deriveJobTags(job)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span><span v-if="job.favorite" class="tag neutral">已收藏</span><span v-if="job.contacted" class="tag blue">已打招呼</span><span v-if="job.applied" class="tag ok">已投递</span><span v-if="job.interviewed" class="tag warn">已面试</span></div></div></div></td>
              <td data-label="公司"><b>{{job.company || '公司信息保密'}}</b><p>{{job.industry}} · {{job.scale || '规模未知'}}</p></td>
              <td data-label="薪资" class="nowrap"><b>{{job.salary}}</b></td>
              <td data-label="经验 / 学历"><b>{{job.experience}}</b><p>{{job.degree}}</p></td>
              <td data-label="岗位评分" class="score">{{job.job_score ?? '—'}}</td>
              <td data-label="匹配度评分" class="score accent-score">{{job.match_score ?? '—'}}</td>
              <td data-label="P级"><span class="priority" :class="job.priority.toLowerCase()">{{job.priority}}</span></td>
              <td data-label="活跃时间"><b>{{job.active}}</b><p>{{job.active_ts.slice(0,10)}}</p></td>
            </tr>
            <tr v-if="expanded===job.job_key" class="detail-row"><td colspan="8"><div class="job-detail"><div class="detail-toolbar"><button :class="{primary:job.favorite}" @click.stop="toggleFavorite(job)">{{job.favorite?'取消收藏':'添加收藏'}}</button><button @click.stop="excludeJob(job)">不再显示</button></div><div class="detail-meta"><div><span>岗位评分</span><b>{{job.job_score}}</b></div><div><span>匹配度</span><b>{{job.match_score}}</b></div><div><span>优先级</span><b>{{job.priority}}</b></div><div><span>来源关键词</span><b>{{job.source_keyword}}</b></div></div><h3>职位描述（JD）</h3><p class="jd-copy">{{job.jd}}</p></div></td></tr>
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
    const activeKey = ref(initial ? initial.job_key : '')
    const interview = reactive({ open: false, jobKey: '', questionIndex: 0, answers: [], input: '', report: null })
    const questions = computed(() => {
      const job = store.jobs.find(item => item.job_key === interview.jobKey)
      return job ? [
        `请用两分钟介绍你与「${job.title}」最相关的一段经历。`,
        `这个岗位重视${job.source_keyword}。请讲一个你独立定位复杂问题并完成闭环的案例。`,
        '如果需求、进度和质量发生冲突，你会如何做技术取舍？',
      ] : []
    })
    const jobs = computed(() => store.jobs.filter(job => {
      if (!job.favorite || job.excluded) return false
      if (query.value && !`${job.title}${job.company}`.toLowerCase().includes(query.value.toLowerCase())) return false
      if (statusFilter.value === 'pending' && (job.analysisReady || job.greeting)) return false
      if (statusFilter.value === 'generated' && !job.greeting) return false
      if (statusFilter.value === 'contacted' && !job.contacted) return false
      if (statusFilter.value === 'applied' && !job.applied) return false
      if (statusFilter.value === 'interviewed' && !job.interviewed) return false
      if (statusFilter.value === 'failed' && !job.actionError) return false
      return true
    }))
    const activeJob = computed(() => store.jobs.find(job => job.job_key === activeKey.value) || jobs.value[0] || null)
    const allSelected = computed(() => jobs.value.length > 0 && jobs.value.every(job => selectedKeys.value.includes(job.job_key)))
    function selectJob(job) { activeKey.value = job.job_key }
    function toggleAll() { selectedKeys.value = allSelected.value ? [] : jobs.value.map(job => job.job_key) }
    function openBoss(job) { showToast(`演示模式：已模拟打开「${job.title}」`, 'info') }
    function setWorkflowStage(job, stage) {
      if (stage === 'contacted') {
        const enabled = !job.contacted
        job.contacted = enabled
        if (!enabled) { job.applied = false; job.interviewed = false }
      } else if (stage === 'applied') {
        const enabled = !job.applied
        job.contacted = enabled ? true : job.contacted
        job.applied = enabled
        if (!enabled) job.interviewed = false
      } else {
        const enabled = !job.interviewed
        if (enabled) { job.contacted = true; job.applied = true }
        job.interviewed = enabled
      }
      showToast(`岗位状态已更新为「${workflowLabel(job)}」`, 'info')
    }
    async function generateAnalysis(job) {
      showToast('正在生成岗位分析…', 'info'); await wait(650)
      job.analysisReady = true; job.advice = adviceFor(job); job.actionError = ''
      showToast('岗位分析与应聘建议已生成')
    }
    async function generateGreeting(job) {
      showToast('正在生成招呼语…', 'info'); await wait(600)
      job.greeting = greetingFor(job); job.actionError = ''
      showToast('招呼语已生成')
    }
    async function executeBatch(type, explicitKeys = null) {
      if (store.batch.running) return
      const keys = explicitKeys || selectedKeys.value
      const targets = keys.map(key => store.jobs.find(job => job.job_key === key)).filter(Boolean)
      if (!targets.length) { showToast('请先选择岗位', 'warn'); return }
      const labels = { analysis: '生成分析', greeting: '生成招呼语', auto: '自动打招呼' }
      const accepted = await requestConfirm({
        title: `${labels[type]} · ${targets.length} 个岗位`,
        message: type === 'auto' ? '本次仅演示护栏确认和逐条执行，不会打开 BOSS 或发送任何消息。' : '将按当前简历逐条处理所选岗位。',
        confirmText: `开始${labels[type]}`,
        tone: type === 'auto' ? 'danger' : 'primary',
      })
      if (!accepted) return
      Object.assign(store.batch, { running: true, type, label: labels[type], done: 0, total: targets.length, failures: [] })
      for (let index = 0; index < targets.length; index++) {
        const job = targets[index]
        await wait(420)
        const shouldFail = type === 'auto' && targets.length > 3 && index === targets.length - 1
        if (shouldFail) {
          job.actionError = '演示：页面状态确认超时'
          store.batch.failures.push(job.job_key)
        } else if (type === 'analysis') {
          job.analysisReady = true; job.advice = adviceFor(job); job.actionError = ''
        } else if (type === 'greeting') {
          job.greeting = greetingFor(job); job.actionError = ''
        } else {
          if (!job.greeting) job.greeting = greetingFor(job)
          job.contacted = true; job.actionError = ''
        }
        store.batch.done = index + 1
      }
      store.batch.running = false
      const failed = store.batch.failures.length
      showToast(failed ? `处理完成，成功 ${targets.length - failed} 个，失败 ${failed} 个` : `${labels[type]}已完成`, failed ? 'warn' : 'ok')
      if (!explicitKeys) selectedKeys.value = []
    }
    function removeFavorite(job) {
      job.favorite = false
      selectedKeys.value = selectedKeys.value.filter(key => key !== job.job_key)
      const next = jobs.value.find(item => item.job_key !== job.job_key)
      activeKey.value = next ? next.job_key : ''
      showToast('已从收藏工作台移除', 'info')
    }
    function startInterview(job) {
      Object.assign(interview, { open: true, jobKey: job.job_key, questionIndex: 0, answers: [], input: '', report: null })
    }
    function submitAnswer() {
      if (!interview.input.trim()) { showToast('请先输入回答', 'warn'); return }
      interview.answers.push({ question: questions.value[interview.questionIndex], answer: interview.input.trim() })
      interview.input = ''
      if (interview.questionIndex >= questions.value.length - 1) {
        interview.report = { score: 87, summary: '回答结构清晰，能够结合真实项目说明技术判断。建议进一步量化结果，并减少背景铺垫。', strengths: ['项目经历与岗位高度相关', '问题定位过程完整', '能说明技术取舍'], improvements: ['补充关键指标和结果数据', '控制单题回答在 2-3 分钟'] }
      } else interview.questionIndex += 1
    }
    function closeInterview() { interview.open = false }
    return { store, query, statusFilter, selectedKeys, activeKey, interview, questions, jobs, activeJob, allSelected,
      deriveJobTags, workflowLabel, selectJob, toggleAll, openBoss, setWorkflowStage, generateAnalysis, generateGreeting, executeBatch,
      removeFavorite, startInterview, submitAnswer, closeInterview }
  },
  template: `
  <div>
    <header class="page-head workbench-head"><div><div class="eyebrow">候选岗位</div><h1>收藏工作台</h1><p>在同一视图完成分析、准备与沟通演练</p></div><select v-model.number="store.selectedResumeId" aria-label="当前简历"><option v-for="resume in store.resumes" :key="resume.id" :value="resume.id">{{resume.name}}</option></select></header>
    <div class="batch-toolbar">
      <label class="check"><input type="checkbox" :checked="allSelected" @change="toggleAll"> 全选当前岗位</label>
      <span class="selection-count">已选 {{selectedKeys.length}} 个</span><span class="grow"></span>
      <button :disabled="!selectedKeys.length || store.batch.running" @click="executeBatch('analysis')">生成分析</button>
      <button :disabled="!selectedKeys.length || store.batch.running" @click="executeBatch('greeting')">生成招呼语</button>
      <button class="primary" :disabled="!selectedKeys.length || store.batch.running" @click="executeBatch('auto')">自动打招呼</button>
    </div>
    <div v-if="store.batch.running || store.batch.total" class="batch-progress" :class="{done:!store.batch.running}"><span>{{store.batch.running ? store.batch.label + '处理中' : '最近批量操作已结束'}}</span><div class="progress"><i :style="{width:(store.batch.total?store.batch.done/store.batch.total*100:0)+'%'}"></i></div><b>{{store.batch.done}} / {{store.batch.total}}</b><small v-if="store.batch.failures.length">{{store.batch.failures.length}} 个待重试</small></div>
    <div class="workbench-layout">
      <aside class="workbench-list">
        <div class="workbench-filters"><input v-model="query" placeholder="搜索收藏岗位" aria-label="搜索收藏岗位"><select v-model="statusFilter" aria-label="处理状态"><option value="all">全部状态</option><option value="pending">待准备</option><option value="generated">已生成招呼语</option><option value="contacted">已打招呼</option><option value="applied">已投递</option><option value="interviewed">已面试</option><option value="failed">处理失败</option></select></div>
        <div class="workbench-scroll">
          <button v-for="job in jobs" :key="job.job_key" class="workbench-job" :class="{active:activeJob&&activeJob.job_key===job.job_key}" @click="selectJob(job)">
            <span class="workbench-check" @click.stop><input type="checkbox" :value="job.job_key" v-model="selectedKeys" :aria-label="'选择 ' + job.title"></span>
            <span class="workbench-copy"><b>{{job.title}}</b><small>{{job.company || '公司信息保密'}} · {{job.salary}}</small><span class="tag-line"><span v-for="tag in deriveJobTags(job)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span><span class="tag neutral">{{workflowLabel(job)}}</span></span></span>
            <strong>{{job.match_score}}</strong>
          </button>
          <div v-if="!jobs.length" class="empty"><b>当前筛选无岗位</b></div>
        </div>
      </aside>
      <section v-if="activeJob" class="workbench-detail">
        <div class="detail-hero"><div><div class="tag-line"><span v-for="tag in deriveJobTags(activeJob)" :key="tag.key" class="tag" :class="tag.tone">{{tag.label}}</span></div><h2>{{activeJob.title}}</h2><p>{{activeJob.company || '公司信息保密'}} · {{activeJob.location}} · {{activeJob.salary}} · {{activeJob.experience}}</p></div><div class="row"><button @click="openBoss(activeJob)">打开 BOSS</button><button @click="startInterview(activeJob)">模拟面试</button><button @click="removeFavorite(activeJob)">取消收藏</button></div></div>
        <section class="workflow-status"><div><h3>求职状态</h3><p>点击即可手动更新，后续阶段会自动补齐前置状态</p></div><div class="workflow-actions"><button :class="{active:activeJob.contacted}" @click="setWorkflowStage(activeJob,'contacted')"><span>{{activeJob.contacted?'✓':'1'}}</span>已打招呼</button><button :class="{active:activeJob.applied}" @click="setWorkflowStage(activeJob,'applied')"><span>{{activeJob.applied?'✓':'2'}}</span>已投递</button><button :class="{active:activeJob.interviewed}" @click="setWorkflowStage(activeJob,'interviewed')"><span>{{activeJob.interviewed?'✓':'3'}}</span>已面试</button></div></section>
        <div class="score-overview"><div><span>岗位评分</span><strong>{{activeJob.job_score}}</strong><small>岗位本身质量</small></div><div><span>匹配度评分</span><strong>{{activeJob.match_score}}</strong><small>当前简历匹配</small></div><div><span>优先级</span><strong>{{activeJob.priority}}</strong><small>{{activeJob.active}}</small></div></div>
        <section class="detail-section"><div class="section-title"><div><h3>职位描述（JD）</h3><p>{{activeJob.industry}} · {{activeJob.scale || '规模未知'}}</p></div></div><p class="jd-copy">{{activeJob.jd}}</p></section>
        <section class="detail-section"><div class="section-title"><div><h3>招呼语</h3><p>按当前简历生成，可继续编辑</p></div><button v-if="!activeJob.greeting" @click="generateGreeting(activeJob)">生成招呼语</button></div><textarea v-if="activeJob.greeting" v-model="activeJob.greeting" class="greeting-editor"></textarea><div v-else class="inline-empty">尚未生成招呼语</div><div v-if="activeJob.greeting" class="section-actions"><span>{{activeJob.greeting.length}} 字</span><button class="primary" @click="executeBatch('auto',[activeJob.job_key])">自动打招呼</button></div></section>
        <section class="detail-section"><div class="section-title"><div><h3>AI 应聘建议</h3><p>结合岗位要求与当前简历</p></div><button v-if="!activeJob.analysisReady" @click="generateAnalysis(activeJob)">生成分析</button></div><p v-if="activeJob.advice" class="advice-copy">{{activeJob.advice}}</p><div v-else class="inline-empty">等待生成岗位分析与应聘建议</div></section>
        <div v-if="activeJob.actionError" class="notice bad"><b>上次处理失败</b><span>{{activeJob.actionError}}</span><button @click="executeBatch('auto',[activeJob.job_key])">重试</button></div>
      </section>
      <section v-else class="workbench-detail empty large"><b>选择一个收藏岗位查看详情</b></section>
    </div>

    <div v-if="interview.open" class="interview-overlay" role="dialog" aria-modal="true" aria-label="模拟面试">
      <header><div><span class="eyebrow">模拟面试</span><h2>{{store.jobs.find(j=>j.job_key===interview.jobKey)?.title}}</h2></div><button aria-label="关闭模拟面试" title="关闭" @click="closeInterview">×</button></header>
      <main v-if="!interview.report" class="interview-main">
        <div class="interview-progress"><span>第 {{interview.questionIndex+1}} / {{questions.length}} 题</span><div class="progress"><i :style="{width:(interview.questionIndex/questions.length*100)+'%'}"></i></div></div>
        <div class="interview-history"><div v-for="(turn,index) in interview.answers" :key="index" class="interview-turn"><b>面试官</b><p>{{turn.question}}</p><b>我的回答</b><p>{{turn.answer}}</p></div></div>
        <section class="current-question"><span>面试官</span><h3>{{questions[interview.questionIndex]}}</h3></section>
        <label class="answer-box">我的回答<textarea v-model="interview.input" placeholder="结合真实项目，按背景、行动、结果组织回答"></textarea></label>
        <div class="interview-actions"><button @click="closeInterview">退出面试</button><button class="primary" @click="submitAnswer">{{interview.questionIndex===questions.length-1?'提交并生成报告':'提交回答'}}</button></div>
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
      await wait(520)
      if (action === 'launch' || action === 'login') account.running = true
      if (action === 'stop') account.running = false
      if (action === 'check') { account.loggedIn = true; account.checkedAt = new Date().toISOString() }
      busy.value = ''
      const labels = { launch: '已模拟启动', login: '已模拟打开登录页', check: '登录态检测完成', stop: '已模拟停止' }
      showToast(`${account.label}${labels[action]}`, action === 'stop' ? 'info' : 'ok')
    }
    function toggleMode(event) {
      store.settings.dualAccount = event.target.checked
      showToast(store.settings.dualAccount ? '已切换为双账号隔离模式' : '已切换为单账号模式', 'info')
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
        <dl><div><dt>调试端口</dt><dd>{{account.port}}</dd></div><div><dt>登录状态</dt><dd :class="account.loggedIn?'ok':'warn'">{{account.loggedIn?'已登录':'待登录'}}</dd></div><div><dt>上次检测</dt><dd>{{fmtTime(account.checkedAt)}}</dd></div></dl>
        <div class="tag-line"><span v-for="role in account.roles" :key="role" class="tag neutral">{{role}}</span></div>
        <footer><button :disabled="busy" @click="accountAction(account,'launch')">启动</button><button :disabled="busy" @click="accountAction(account,'login')">打开登录页</button><button :disabled="busy" @click="accountAction(account,'check')">{{busy===account.key+'-check'?'检测中…':'检测登录态'}}</button><button :disabled="busy || !account.running" @click="accountAction(account,'stop')">停止</button></footer>
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
    function save() {
      Object.assign(store.settings, clone(draft))
      showToast('设置已保存到当前演示会话')
    }
    async function testConnection() {
      testResult.value = null
      if (!draft.llmBaseUrl.trim() || !draft.llmModel.trim()) { showToast('请填写服务地址和模型', 'warn'); return }
      testing.value = true
      await wait(850)
      testing.value = false
      testResult.value = { ok: true, text: `连接成功 · ${draft.llmModel} · 286 ms（演示）` }
    }
    return { draft, testing, testResult, save, testConnection }
  },
  template: `
  <div>
    <header class="page-head"><div><div class="eyebrow">系统偏好</div><h1>设置</h1><p>模型服务、发送护栏与评分参数</p></div><button class="primary" @click="save">保存设置</button></header>
    <section class="settings-section"><div class="section-title"><div><h2>LLM 服务</h2><p>用于采集计划、精评、招呼语和模拟面试</p></div><span class="status-badge completed">演示连接</span></div><div class="form-grid three"><label>Base URL<input v-model="draft.llmBaseUrl" placeholder="https://api.example.com/v1"></label><label>API Key<input v-model="draft.llmApiKey" type="password" placeholder="仅保留在当前会话"></label><label>模型<input v-model="draft.llmModel" placeholder="model-name"></label></div><div class="setting-actions"><button :disabled="testing" @click="testConnection">{{testing?'测试中…':'测试连通性'}}</button><span v-if="testResult" :class="testResult.ok?'ok':'bad'">{{testResult.text}}</span></div></section>
    <section class="settings-section"><div class="section-title"><div><h2>发送护栏</h2><p>自动打招呼始终遵守以下边界</p></div><span class="status-badge paused">不可关闭</span></div><div class="form-grid four"><label>每日上限<input type="number" v-model.number="draft.sendDailyLimit" min="1" max="110"></label><label>硬顶<input type="number" v-model.number="draft.sendDailyHardCap" min="1" max="110"></label><label>最小间隔（秒）<input type="number" v-model.number="draft.sendGapMin" min="30" max="90"></label><label>最大间隔（秒）<input type="number" v-model.number="draft.sendGapMax" min="30" max="90"></label></div><div class="guardrail-list"><span>每日上限 {{draft.sendDailyLimit}}</span><span>硬顶 {{draft.sendDailyHardCap}}</span><span>{{draft.sendGapMin}}-{{draft.sendGapMax}} 秒随机间隔</span><span>同公司 30 天去重</span><span>风控信号当日熔断</span></div></section>
    <section class="settings-section"><div class="section-title"><div><h2>评分与活跃度</h2><p>控制匹配度评分数量和岗位有效性判断</p></div></div><div class="form-grid two"><label>匹配度评分默认岗位数<input type="number" v-model.number="draft.matchScoreTopN" min="1" max="100"></label><label>HR 不活跃阈值（天）<input type="number" v-model.number="draft.inactiveDays" min="1" max="90"></label></div></section>
  </div>`,
}

// -- 应用组装 -----------------------------------------------------------
const App = {
  setup() {
    const view = computed(() => ({
      '/dashboard': DashboardView, '/profile': ProfileView, '/collect': CollectView,
      '/analytics': AnalyticsView, '/jobs': JobsView, '/jobcard': JobCardView,
      '/accounts': AccountsView, '/settings': SettingsView,
    })[route.value] || DashboardView)
    const collectionSummary = computed(() => {
      const runs = store.runs.filter(run => run.status === 'running' || run.status === 'paused')
      const collected = runs.reduce((sum, run) => sum + run.collected, 0)
      const target = runs.reduce((sum, run) => sum + run.target, 0)
      return {
        visible: runs.length > 0,
        label: runs.some(run => run.status === 'running') ? '采集中' : '采集已暂停',
        collected, target, percent: target ? Math.round(collected / target * 100) : 0,
      }
    })
    const theme = ref(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
    function toggleTheme() {
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
      document.documentElement.dataset.theme = theme.value
      localStorage.setItem('theme', theme.value)
    }
    return { store, route, nav, view, theme, collectionSummary, toggleTheme, settleConfirm }
  },
  template: `
  <div class="layout">
    <aside class="side">
      <div class="brand-row"><div class="logo">boss<span>-copilot</span></div><span class="demo-badge">演示数据</span></div>
      <nav aria-label="主导航"><a v-for="[href,label] in nav" :key="href" :class="{on:route===href}" :href="'#'+href">{{label}}</a></nav>
      <div class="side-bottom">
        <a v-if="collectionSummary.visible" class="side-progress" href="#/collect"><div><span>{{collectionSummary.label}}</span><b>{{collectionSummary.percent}}%</b></div><div class="progress"><i :style="{width:collectionSummary.percent+'%'}"></i></div><small>{{collectionSummary.collected}} / {{collectionSummary.target}} 条</small></a>
        <button class="theme-toggle" type="button" :aria-label="theme==='dark'?'切换浅色主题':'切换深色主题'" @click="toggleTheme"><span>{{theme==='dark'?'☀':'☾'}}</span>{{theme==='dark'?'切换浅色':'切换深色'}}</button>
        <div class="side-foot"><span class="status-dot ok"></span>本地演示 · 不连接真实账号</div>
      </div>
    </aside>
    <main class="main"><component :is="view" /></main>

    <transition name="toast"><div v-if="store.ui.toast" class="toast" :class="store.ui.toast.tone" role="status">{{store.ui.toast.message}}</div></transition>
    <div v-if="store.ui.confirm" class="modal-backdrop" role="presentation" @click.self="settleConfirm(false)">
      <section class="confirm-dialog" role="dialog" aria-modal="true" :aria-label="store.ui.confirm.title"><h2>{{store.ui.confirm.title}}</h2><p>{{store.ui.confirm.message}}</p><div class="row end"><button @click="settleConfirm(false)">取消</button><button :class="store.ui.confirm.tone==='danger'?'danger':'primary'" @click="settleConfirm(true)">{{store.ui.confirm.confirmText}}</button></div></section>
    </div>
  </div>`,
}

createApp(App).mount('#app')
