import { createApp, ref, reactive, computed, onMounted } from '/static/vue.esm-browser.prod.js'

// ── 极简 API 层 ──────────────────────────────────────────────────
const api = {
  async get(url) { const r = await fetch(url); if (!r.ok) throw new Error((await r.json()).detail || r.status); return r.json() },
  async post(url, body) { const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }); if (!r.ok) throw new Error((await r.json()).detail || r.status); return r.json() },
  async put(url, body) { const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) throw new Error((await r.json()).detail || r.status); return r.json() },
}

// ── 路由 ────────────────────────────────────────────────────────
const route = ref(location.hash.slice(1) || '/jobs')
const nav = [
  ['/jobs', '岗位列表'], ['/jobcard', '作战卡'], ['/collect', '采集中心'],
  ['/profile', '简历档案'], ['/accounts', '双账号'], ['/settings', '设置'],
]
window.addEventListener('hashchange', () => { route.value = location.hash.slice(1) || '/jobs' })

// ── 页面：岗位列表 ───────────────────────────────────────────────
const JobsView = {
  setup() {
    const items = ref([]), total = ref(0), counts = ref({}), q = ref(''),
      status = ref(''), sort = ref('composite'), page = ref(0), loading = ref(false),
      scoring = ref(false)
    const PAGE = 50
    async function load() {
      loading.value = true
      try {
        const p = new URLSearchParams({ q: q.value, status: status.value, sort: sort.value, limit: PAGE, offset: page.value * PAGE })
        const d = await api.get('/api/jobs?' + p)
        items.value = d.items; total.value = d.total; counts.value = d.status_counts
      } finally { loading.value = false }
    }
    onMounted(load)
    async function runL1() {
      scoring.value = true
      try { alert('L1 评分完成：' + JSON.stringify(await api.post('/api/score/l1', {}))); load() }
      catch (e) { alert('L1 失败：' + e.message) }
      scoring.value = false
    }
    const pages = computed(() => Math.ceil(total.value / PAGE))
    return { items, total, counts, q, status, sort, page, loading, scoring, load, pages, runL1,
      search: () => { page.value = 0; load() },
      prev: () => { page.value--; load() }, next: () => { page.value++; load() } }
  },
  template: `
  <div>
    <h2>岗位列表 <span class="muted">{{total}} 条</span></h2>
    <div class="card"><div class="row">
      <input v-model="q" placeholder="搜索岗位/公司" style="width:220px" @keyup.enter="search">
      <select v-model="status" @change="search">
        <option value="">全部状态</option>
        <option v-for="(c,s) in counts" :value="s">{{s}} ({{c}})</option>
      </select>
      <select v-model="sort" @change="search">
        <option value="composite">综合分</option><option value="l1">L1粗分</option>
        <option value="match">匹配度</option><option value="salary">薪资</option>
        <option value="recent">最近采集</option>
      </select>
      <button class="primary" @click="search">筛选</button>
      <span class="grow"></span>
      <button :disabled="scoring" @click="runL1">{{scoring?'评分中…':'运行 L1 评分'}}</button>
    </div></div>
    <div class="card" style="padding:0 10px">
    <table>
      <thead><tr><th>岗位</th><th>薪资</th><th>经验/学历</th><th>公司</th><th>L2综合</th><th>匹配</th><th>P级</th><th>HR活跃</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="j in items" :key="j.job_key" style="cursor:pointer"
            @click="location.hash='#/jobcard?key='+j.job_key">
          <td style="max-width:260px"><b>{{j.title}}</b><div class="muted">{{j.location}}</div></td>
          <td>{{j.salary}}</td>
          <td class="muted">{{j.experience}}<br>{{j.degree}}</td>
          <td style="max-width:180px">{{j.company}}<div class="muted">{{j.industry}} · {{j.scale}}</div></td>
          <td class="score">{{j.composite ?? j.composite_rough ?? '—'}}</td>
          <td class="score">{{j.match_score ?? j.match_rough ?? '—'}}</td>
          <td><span v-if="j.priority" class="tag" :class="j.priority.toLowerCase()">{{j.priority}}</span></td>
          <td class="muted">{{j.hr_active}}</td>
          <td><span class="badge" :class="j.status">{{j.status}}</span></td>
        </tr>
      </tbody>
    </table>
    <div class="row" style="padding:10px">
      <button :disabled="page===0||loading" @click="prev">上一页</button>
      <span class="muted">第 {{page+1}}/{{Math.max(pages,1)}} 页</span>
      <button :disabled="page>=pages-1||loading" @click="next">下一页</button>
    </div>
    </div>
  </div>`
}

// ── 页面：作战卡（岗位详情）─────────────────────────────────────
const JobCardView = {
  setup() {
    const job = ref(null), err = ref('')
    const key = () => new URLSearchParams(location.hash.split('?')[1] || '').get('key')
    onMounted(async () => {
      try { job.value = await api.get('/api/jobs/' + encodeURIComponent(key())) }
      catch (e) { err.value = e.message }
    })
    return { job, err }
  },
  template: `
  <div v-if="job">
    <a class="back" href="#/jobs">← 返回列表</a>
    <div class="card">
      <div class="row">
        <div class="grow"><h2>{{job.title}}</h2>
          <div class="muted">{{job.company}} · {{job.industry}} · {{job.scale}} · {{job.stage}}</div></div>
        <div style="text-align:center"><div class="score" style="font-size:26px">{{job.composite ?? job.composite_rough ?? '—'}}</div><div class="muted">综合分</div></div>
        <div style="text-align:center"><div class="score" style="font-size:26px">{{job.match_score ?? job.match_rough ?? '—'}}</div><div class="muted">匹配度</div></div>
        <div style="text-align:center"><div class="score" style="font-size:26px">{{job.job_score ?? job.l1_score ?? '—'}}</div><div class="muted">岗位分</div></div>
        <span v-if="job.priority" class="tag" :class="job.priority.toLowerCase()" style="font-size:14px;padding:4px 12px">{{job.priority}}</span>
      </div>
      <div class="row" style="margin-top:8px">
        <span class="tag">{{job.salary}}</span><span class="tag">{{job.experience}}</span>
        <span class="tag">{{job.degree}}</span><span class="tag">{{job.location}}</span>
        <span class="tag">HR: {{job.hr_active||'未知'}}</span>
        <a v-if="job.job_link" :href="job.job_link" target="_blank">原职位页 ↗</a>
      </div>
    </div>
    <div class="grid2" v-if="job.l2_detail && job.l2_detail.dims">
      <div class="card"><h3>L2 评分明细（{{job.l2_detail.type}} · {{job.l2_source==='imported'?'导入基线':'LLM'}}）</h3>
        <div class="dims">
          <div class="dim" v-for="(v,k) in job.l2_detail.dims"><span class="muted">{{k}}</span><b>{{v}}</b></div>
        </div>
        <p v-if="job.l2_detail.summary" style="margin-top:12px">{{job.l2_detail.summary}}</p>
        <p v-if="job.l2_detail.advice" class="ok" style="margin-top:8px">💡 {{job.l2_detail.advice}}</p>
      </div>
      <div class="card"><h3>L1 电算明细</h3>
        <pre class="jd">{{JSON.stringify(job.l1_detail,null,1)}}</pre>
      </div>
    </div>
    <div class="card"><h3>职位描述（JD）</h3>
      <pre class="jd">{{job.jd || '（暂无，采集详情后展示）'}}</pre></div>
  </div>
  <div v-else class="muted">{{err || '加载中…'}}</div>`
}

// ── 页面：采集中心 ───────────────────────────────────────────────
const CollectView = {
  setup() {
    const xlsxPath = ref(''), jsonDir = ref(''), busy = ref(''), result = ref(null), runs = ref([])
    async function doImport(kind) {
      busy.value = kind
      result.value = null
      try {
        result.value = await api.post(kind === 'xlsx' ? '/api/import/xlsx' : '/api/import/json',
          kind === 'xlsx' ? { path: xlsxPath.value } : { dir: jsonDir.value })
        runs.value = await api.get('/api/runs')
      } catch (e) { result.value = { error: e.message } }
      busy.value = ''
    }
    onMounted(async () => { runs.value = await api.get('/api/runs') })
    return { xlsxPath, jsonDir, busy, result, runs, doImport }
  },
  template: `
  <div>
    <h2>采集中心</h2>
    <div class="card">
      <h3>导入岗位表格（xlsx）</h3>
      <div class="row">
        <input class="grow" v-model="xlsxPath" placeholder="表格路径，如 ~/Desktop/深圳_嵌入式_AI应用_Agent_岗位JD详情_20260823.xlsx">
        <button class="primary" :disabled="!xlsxPath||busy" @click="doImport('xlsx')">{{busy==='xlsx'?'导入中…':'导入'}}</button>
      </div>
      <div class="muted" style="margin-top:6px">导入总览 + JD 全文 + 历史 L1/L2 评分（作为基线，不会被覆盖）</div>
    </div>
    <div class="card">
      <h3>导入 scraper 采集结果（JSON）</h3>
      <div class="row">
        <input class="grow" v-model="jsonDir" placeholder="默认 ~/.boss-zhipin-scraper/job-result/">
        <button :disabled="busy" @click="doImport('json')">{{busy==='json'?'导入中…':'导入'}}</button>
      </div>
    </div>
    <div class="card" v-if="result">
      <h3>导入结果</h3><pre class="jd">{{JSON.stringify(result,null,1)}}</pre>
    </div>
    <div class="card"><h3>最近任务</h3>
      <table><thead><tr><th>时间</th><th>类型</th><th>统计</th></tr></thead><tbody>
        <tr v-for="r in runs"><td class="muted">{{r.finished_at}}</td><td>{{r.kind}}</td>
        <td class="muted">{{JSON.stringify(r.stats)}}</td></tr>
      </tbody></table>
    </div>
    <div class="card muted">在线采集（关键词搜索 / 公司定向 / 同步刷新）在后续里程碑接入。</div>
  </div>`
}

// ── 页面：简历档案 ───────────────────────────────────────────────
const ProfileView = {
  setup() {
    const resume = ref(''), updatedAt = ref(''), saving = ref(false), saved = ref(false)
    onMounted(async () => {
      const d = await api.get('/api/profile')
      resume.value = d.resume_text; updatedAt.value = d.updated_at
    })
    async function save() {
      saving.value = true; saved.value = false
      const d = await api.put('/api/profile', { resume_text: resume.value })
      saving.value = false; saved.value = d.ok
      updatedAt.value = (await api.get('/api/profile')).updated_at
    }
    return { resume, updatedAt, saving, saved, save }
  },
  template: `
  <div>
    <h2>简历档案 <span class="muted">更新于 {{updatedAt}}</span></h2>
    <div class="card">
      <p class="muted" style="margin-bottom:10px">粘贴简历全文（Markdown 或纯文本）。后续评分、招呼语、模拟面试都以它为基线。</p>
      <textarea v-model="resume" placeholder="在此粘贴简历…"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="primary" :disabled="saving" @click="save">{{saving?'保存中…':'保存'}}</button>
        <span v-if="saved" class="ok">已保存 ✓</span>
      </div>
    </div>
  </div>`
}

// ── 页面：双账号 ─────────────────────────────────────────────────
const AccountsView = {
  setup() {
    const accounts = ref({})
    async function load() { accounts.value = await api.get('/api/accounts') }
    onMounted(load)
    const refresh = () => load()
    async function launch(name) { await api.post('/api/accounts/' + name + '/launch'); load() }
    async function loginPage(name) { const r = await api.post('/api/accounts/' + name + '/login-page'); alert(r.ok ? '已打开登录页，请在弹出的 Chrome 中登录' : r.error); load() }
    async function loginState(name) { const r = await api.get('/api/accounts/' + name + '/login-state'); alert(JSON.stringify(r)) }
    async function stop(name) { await api.post('/api/accounts/' + name + '/stop'); load() }
    return { accounts, refresh, launch, loginPage, loginState, stop }
  },
  template: `
  <div>
    <h2>双账号管理 <button @click="refresh">刷新</button></h2>
    <div class="card" v-for="(a,name) in accounts">
      <div class="row">
        <div class="grow"><b>{{a.label}}</b>
          <div class="muted">{{a.profile}} · CDP :{{a.port}}</div></div>
        <span :class="a.running?'ok':'bad'">{{a.running?'运行中':'未启动'}}</span>
      </div>
      <div class="row" style="margin-top:10px">
        <button @click="launch(name)">启动</button>
        <button @click="loginPage(name)">打开登录页</button>
        <button @click="loginState(name)">检测登录态</button>
        <button @click="stop(name)">停止</button>
      </div>
      <div class="muted" style="margin-top:6px">采集号承担所有采集动作的风控风险；账号A只在发送招呼语/收发消息时启动。</div>
    </div>
  </div>`
}

// ── 页面：设置 ───────────────────────────────────────────────────
const SettingsView = {
  setup() {
    const s = reactive({}), loaded = ref(false), saved = ref(false)
    onMounted(async () => { Object.assign(s, await api.get('/api/settings')); loaded.value = true })
    async function save() {
      await api.put('/api/settings', JSON.parse(JSON.stringify(s)))
      saved.value = true; setTimeout(() => saved.value = false, 1500)
    }
    return { s, loaded, saved, save }
  },
  template: `
  <div v-if="loaded">
    <h2>设置</h2>
    <div class="card"><h3>LLM（BYOK · OpenAI 兼容）</h3>
      <div class="row" style="margin-bottom:8px"><span class="muted" style="width:90px">Base URL</span><input v-model="s.llm_base_url" placeholder="https://api.deepseek.com/v1"></div>
      <div class="row" style="margin-bottom:8px"><span class="muted" style="width:90px">API Key</span><input v-model="s.llm_api_key" type="password" placeholder="sk-…"></div>
      <div class="row"><span class="muted" style="width:90px">模型</span><input v-model="s.llm_model" placeholder="deepseek-chat / gpt-4o-mini / …"></div>
    </div>
    <div class="card"><h3>发送护栏（账号A）</h3>
      <div class="dims">
        <div class="dim"><span class="muted">每日上限</span><input type="number" v-model.number="s.send_daily_limit"></div>
        <div class="dim"><span class="muted">硬顶</span><input type="number" v-model.number="s.send_daily_hard_cap"></div>
        <div class="dim"><span class="muted">间隔下限(秒)</span><input type="number" v-model.number="s.send_gap_min_sec"></div>
        <div class="dim"><span class="muted">间隔上限(秒)</span><input type="number" v-model.number="s.send_gap_max_sec"></div>
      </div>
    </div>
    <div class="card"><h3>同步与评分</h3>
      <div class="dims">
        <div class="dim"><span class="muted">HR不活跃阈值(天)</span><input type="number" v-model.number="s.hr_inactive_days"></div>
        <div class="dim"><span class="muted">L2 取 Top N</span><input type="number" v-model.number="s.l2_top_n"></div>
      </div>
    </div>
    <button class="primary" @click="save">保存设置</button>
    <span v-if="saved" class="ok">已保存 ✓</span>
  </div>`
}

// ── 组装 ────────────────────────────────────────────────────────
const App = {
  setup() {
    const view = computed(() => {
      const r = route.value
      if (r.startsWith('/jobcard')) return JobCardView
      if (r.startsWith('/collect')) return CollectView
      if (r.startsWith('/profile')) return ProfileView
      if (r.startsWith('/accounts')) return AccountsView
      if (r.startsWith('/settings')) return SettingsView
      return JobsView
    })
    return { route, nav, view }
  },
  template: `
  <div class="layout">
    <div class="side">
      <div class="logo">boss<span>-copilot</span></div>
      <div class="nav">
        <a v-for="[href,label] in nav" :class="{on: route.startsWith(href)}" :href="'#'+href">{{label}}</a>
      </div>
    </div>
    <div class="main"><component :is="view"/></div>
  </div>`
}
createApp(App).mount('#app')
