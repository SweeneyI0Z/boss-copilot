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
  ['/greetings', '招呼语'], ['/messages', '消息'], ['/profile', '简历档案'], ['/accounts', '账号管理'], ['/settings', '设置'],
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
    async function runL2() {
      const n = prompt('对 L1 综合粗分 Top N 且无 L2 的岗位精评（每条约一次 LLM 调用），N =', '10')
      if (!n) return
      scoring.value = true
      try { const r = await api.post('/api/score/l2', { limit: parseInt(n) }); alert('L2 精评完成：' + JSON.stringify(r)); load() }
      catch (e) { alert('L2 失败：' + e.message) }
      scoring.value = false
    }
    function openJob(j) {
      window.location.hash = '#/jobcard?key=' + encodeURIComponent(j.job_key)
    }
    const pages = computed(() => Math.ceil(total.value / PAGE))
    return { items, total, counts, q, status, sort, page, loading, scoring, load, pages, runL1, runL2, openJob,
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
      <button :disabled="scoring" @click="runL2">{{scoring?'评分中…':'L2 精评 (LLM)'}}</button>
      <button :disabled="scoring" @click="runL1">运行 L1 评分</button>
    </div></div>
    <div class="card" style="padding:0 10px">
    <table>
      <thead><tr><th>岗位</th><th>薪资</th><th>经验/学历</th><th>公司</th><th>L2综合</th><th>匹配</th><th>P级</th><th>HR活跃</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="j in items" :key="j.job_key" class="job-row" tabindex="0" role="link"
            title="查看岗位详情与 JD" @click="openJob(j)" @keyup.enter="openJob(j)"
            @keyup.space.prevent="openJob(j)">
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

// ── 页面：模拟面试 ───────────────────────────────────────────────
const InterviewView = {
  setup() {
    const sessions = ref([]), cur = ref(null), input = ref(''), busy = ref(false), report = ref(null)
    async function load() { sessions.value = await api.get('/api/interviews') }
    onMounted(load)
    async function startNew() {
      const q = prompt('输入要模拟面试的岗位关键字（从岗位列表搜索）：', '')
      if (!q) return
      const jobs = await api.get('/api/jobs?q=' + encodeURIComponent(q) + '&limit=5')
      if (!jobs.items.length) { alert('没有匹配岗位'); return }
      const pick = jobs.items[0]
      if (!confirm(`对「${pick.title} · ${pick.company}」开始模拟面试？`)) return
      busy.value = true
      try {
        const s = await api.post('/api/interview/start', { job_key: pick.job_key })
        cur.value = { id: s.id, title: s.title, total: s.total_questions,
          transcript: [{ role: 'interviewer', content: s.first_question }] }
        report.value = null
      } catch (e) { alert('开始失败：' + e.message) }
      busy.value = false
    }
    async function send() {
      if (!input.value.trim() || !cur.value) return
      const text = input.value; input.value = ''
      cur.value.transcript.push({ role: 'candidate', content: text })
      busy.value = true
      try {
        const r = await api.post(`/api/interview/${cur.value.id}/answer`, { text })
        cur.value.transcript.push({ role: 'interviewer', content: r.content,
          feedback: r.feedback })
        if (String(r.content).includes('面试结束')) await finish()
      } catch (e) { alert('失败：' + e.message) }
      busy.value = false
    }
    async function finish() {
      busy.value = true
      try { report.value = await api.post(`/api/interview/${cur.value.id}/finish`) }
      catch (e) { alert('报告失败：' + e.message) }
      busy.value = false; load()
    }
    async function openSession(s) {
      const d = await api.get('/api/interview/' + s.id)
      cur.value = { id: d.id, title: s.title, total: 0, transcript: d.transcript.filter(t => t.role !== 'bank') }
      report.value = d.report || null
    }
    return { sessions, cur, input, busy, report, load, startNew, send, finish, openSession }
  },
  template: `
  <div class="grid2">
    <div>
      <h2>模拟面试 <button class="primary" :disabled="busy" @click="startNew">＋ 新面试</button></h2>
      <div class="card"><h3>历史会话</h3>
        <table><tbody><tr v-for="s in sessions" :key="s.id" style="cursor:pointer" @click="openSession(s)">
          <td>{{s.title}}<div class="muted">{{s.company}} · {{s.status}} · {{(s.created_at||'').slice(0,16)}}</div></td>
        </tr></tbody></table>
        <div v-if="!sessions.length" class="muted">暂无</div>
      </div>
      <div v-if="report" class="card"><h3>面试报告 <span class="score">{{report.score ?? '—'}}</span>/100</h3>
        <p>{{report.overall}}</p>
        <h3>优势</h3><ul style="padding-left:18px"><li v-for="x in report.strengths">{{x}}</li></ul>
        <h3>风险</h3><ul style="padding-left:18px"><li v-for="x in report.risks">{{x}}</li></ul>
        <h3>面试前必补</h3><ul style="padding-left:18px"><li v-for="x in report.prep">{{x}}</li></ul>
      </div>
    </div>
    <div v-if="cur">
      <h2>{{cur.title}}</h2>
      <div class="card" style="max-height:520px;overflow:auto">
        <div v-for="(t,i) in cur.transcript" :key="i" style="margin-bottom:12px">
          <div :style="{textAlign: t.role==='candidate'?'right':'left'}">
            <div class="tag" :style="{display:'inline-block'}">{{t.role==='candidate'?'我':'面试官'}}</div>
            <div style="white-space:pre-wrap">{{t.content}}</div>
            <div v-if="t.feedback" class="muted" style="margin-top:4px">💬 点评：{{t.feedback}}</div>
          </div>
        </div>
      </div>
      <div class="row" style="margin-top:10px" v-if="!report">
        <input class="grow" v-model="input" placeholder="输入你的回答，回车发送" @keyup.enter="send">
        <button class="primary" :disabled="busy" @click="send">回答</button>
        <button :disabled="busy" @click="finish">提前结束并出报告</button>
      </div>
    </div>
    <div v-else class="muted">左侧选择历史会话，或开始新面试</div>
  </div>`
}

// ── 页面：消息中心 ───────────────────────────────────────────────
const MessagesView = {
  setup() {
    const convs = ref([]), busy = ref(''), sel = ref(null)
    async function load() { convs.value = await api.get('/api/chat/conversations') }
    onMounted(load)
    async function doPoll() {
      busy.value = 'poll'
      const r = await api.post('/api/chat/poll', {})
      busy.value = ''
      if (!r.ok) alert('轮询失败：' + r.error)
      else alert('会话 ' + r.conversations + ' 个，新快照 ' + r.new_snapshots + ' 份')
      load()
    }
    async function doDraft(c) {
      busy.value = 'draft'
      try { const r = await api.post('/api/chat/draft', { conversation_id: c.id })
        alert('草稿：' + r.reply) } catch (e) { alert('草稿失败：' + e.message) }
      busy.value = ''; load()
    }
    async function doSend(c, reply) {
      if (!confirm('确认使用沟通号发送这条回复？\n\n' + reply)) return
      busy.value = 'send'
      const r = await api.post('/api/chat/send', { conversation_id: c.id, reply })
      busy.value = ''
      alert(r.ok ? '已发送' : '发送失败：' + (r.error || ''))
      load()
    }
    return { convs, busy, sel, doPoll, doDraft, doSend, load }
  },
  template: `
  <div>
    <h2>消息中心 <span class="muted">沟通号会话 · AI 起草 + 人工点发</span>
      <button class="primary" style="margin-left:10px" :disabled="busy" @click="doPoll">{{busy==='poll'?'轮询中…':'轮询会话'}}</button>
    </h2>
    <div v-if="!convs.length" class="card muted">暂无会话——先「轮询会话」（需沟通号已登录）。</div>
    <div v-for="c in convs" :key="c.id" class="card">
      <div class="row">
        <b class="grow">{{c.boss_name}}</b>
        <span class="muted">{{(c.last_message_at||'').slice(0,16)}}</span>
        <button :disabled="busy" @click="doDraft(c)">AI 起草回复</button>
      </div>
      <pre class="jd" style="max-height:160px;overflow:auto;margin-top:8px">{{(c.latest_snapshot||'').slice(0,600)}}</pre>
      <div v-for="d in c.drafts" :key="d.id" class="row" style="margin-top:6px">
        <span class="grow" :class="d.draft_status==='sent'?'muted':''">✎ {{d.draft_reply}} <span class="muted">({{d.draft_status}})</span></span>
        <button v-if="d.draft_status!=='sent'" :disabled="busy" @click="doSend(c, d.draft_reply)">发送</button>
      </div>
    </div>
  </div>`
}

// ── 页面：招呼语队列 ─────────────────────────────────────────────
const GreetingsView = {
  setup() {
    const items = ref([]), tab = ref('draft'), st = ref({})
    async function load() {
      items.value = await api.get('/api/greetings' + (tab.value ? '?status=' + tab.value : ''))
    }
    async function loadSt() { try { st.value = await api.get('/api/greeting/send-status') } catch (e) {} }
    onMounted(() => { load(); loadSt() })
    function refresh() { load(); loadSt() }
    async function approve(g, i) { await api.post('/api/greetings/' + g.id + '/approve', { index: i }); refresh() }
    async function skip(g) { await api.post('/api/greetings/' + g.id + '/skip'); refresh() }
    async function sendBatch() {
      if (!confirm('使用沟通号发送所有已批准招呼语（随机间隔30-90秒，每日上限受护栏控制）。确认？')) return
      const r = await api.post('/api/greeting/send-batch')
      if (!r.ok) alert(r.error || '启动失败')
      loadSt()
    }
    setInterval(refresh, 15000)
    return { items, tab, st, approve, skip, sendBatch, refresh,
      switchTab: t => { tab.value = t; load() } }
  },
  template: `
  <div>
    <h2>招呼语队列</h2>
    <div class="card"><div class="row">
      <span>今日已发 <b>{{st.sent_today ?? '—'}}</b> 条</span>
      <span v-if="st.halted_today" class="bad">⚠ 已熔断：{{st.halt_reason}}</span>
      <span v-if="st.sending" class="warn">● 发送执行中…</span>
      <span class="grow"></span>
      <button class="primary" :disabled="st.sending || st.halted_today" @click="sendBatch">使用沟通号发送已批准批次</button>
    </div>
    <div v-if="st.last_result" class="muted" style="margin-top:8px">上次结果：{{JSON.stringify(st.last_result).slice(0,300)}}</div>
    </div>
    <div class="card"><div class="row" style="margin-bottom:10px">
      <button :class="{primary: tab===t}" v-for="t in ['draft','approved','sent','failed','skipped']" :key="t" @click="switchTab(t)">{{t}}</button>
    </div>
    <div v-for="g in items" :key="g.id" class="card" style="background:var(--panel2)">
      <div class="row">
        <div class="grow"><b>{{g.title}}</b> <span class="muted">{{g.company}} · {{g.salary}} · {{g.priority}}</span></div>
        <span class="tag">{{g.status}}</span>
      </div>
      <div v-for="(v,i) in g.variants" :key="i" class="row" style="margin-top:8px">
        <input type="radio" :name="'g'+g.id" :checked="g.chosen===v" @change="approve(g,i)" :disabled="g.status!=='draft' && g.status!=='approved'">
        <span class="grow">{{v}}</span>
      </div>
      <div class="row" style="margin-top:8px">
        <span class="muted" v-if="g.status==='approved'">已选定，等待发送</span>
        <span class="ok" v-if="g.status==='sent'">已发送 {{(g.sent_at||'').slice(0,16)}}</span>
        <span class="bad" v-if="g.status==='failed'">失败：{{g.error}}</span>
        <span class="grow"></span>
        <button v-if="g.status==='draft'" @click="skip(g.id)">跳过</button>
      </div>
    </div>
    <div v-if="!items.length" class="muted">暂无{{tab}}条目——到「作战卡」页为岗位生成招呼语</div>
    </div>
  </div>`
}

// ── 页面：作战卡（岗位详情）─────────────────────────────────────
const JobCardView = {
  setup() {
    const job = ref(null), err = ref('')
    const key = () => new URLSearchParams(location.hash.split('?')[1] || '').get('key')
    async function genGreeting() {
      try { const r = await api.post('/api/greeting/generate', { job_key: key() })
        alert('已生成 ' + r.variants.length + ' 个变体（' + r.source + '），到「招呼语」页选择发送')
      } catch (e) { alert('生成失败：' + e.message) }
    }
    onMounted(async () => {
      try { job.value = await api.get('/api/jobs/' + encodeURIComponent(key())) }
      catch (e) { err.value = e.message }
    })
    return { job, err, genGreeting }
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
    <div class="grid2" v-if="job.l2_detail">
      <div class="card"><h3>L2 评分明细（{{job.l2_detail.type || '—'}} · {{job.l2_source==='imported'?'导入基线':'LLM 精评'}}）</h3>
        <div class="dims" v-if="job.l2_detail.dims">
          <div class="dim" v-for="(v,k) in job.l2_detail.dims"><span class="muted">{{k}}</span><b>{{v}}</b></div>
        </div>
        <p v-if="job.l2_detail.summary" style="margin-top:12px">{{job.l2_detail.summary}}</p>
        <p v-if="job.l2_detail.advice" class="ok" style="margin-top:8px">💡 {{job.l2_detail.advice}}</p>
        <div class="row" style="margin-top:14px">
          <button class="primary" @click="genGreeting">✍ 生成招呼语（3 个变体）</button>
          <a href="#/greetings" class="muted">去队列选择发送 →</a>
        </div>
        <div v-if="job.l2_detail.greeting_angle" class="muted" style="margin-top:8px">招呼语切入点：{{job.l2_detail.greeting_angle}}</div>
      </div>
      <div class="card">
        <h3>优势 / GAP / 简历建议</h3>
        <div v-if="job.l2_detail.strengths && job.l2_detail.strengths.length">
          <div class="ok" style="margin:4px 0">✓ 突出优势</div>
          <ul style="padding-left:18px"><li v-for="s in job.l2_detail.strengths">{{s}}</li></ul>
        </div>
        <div v-if="job.l2_detail.gaps && job.l2_detail.gaps.length" style="margin-top:10px">
          <div class="warn" style="margin:4px 0">△ 待补 GAP</div>
          <ul style="padding-left:18px">
            <li v-for="g in job.l2_detail.gaps">{{g.gap || g}}<div class="muted">→ {{g.action}}</div></li>
          </ul>
        </div>
        <div v-if="job.l2_detail.resume_advice" class="card" style="margin-top:10px;background:var(--panel2)">
          📄 简历定制：{{job.l2_detail.resume_advice}}
        </div>
        <div v-if="!job.l2_detail.strengths && !job.l2_detail.gaps" class="muted">（导入基线无明细，可用 L2 精评重算补充）</div>
      </div>
    </div>
    <div class="card"><h3>L1 电算明细</h3>
      <pre class="jd">{{JSON.stringify(job.l1_detail,null,1)}}</pre>
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
    const plan = ref(null), planText = ref(''), genBusy = ref(false), planSaved = ref(false)
    const st = ref({ running: false, log: [], current: '' })
    const kw = ref(''), city = ref('深圳'), pages = ref(3), coUrl = ref('')
    let timer = null
    async function pollStatus() {
      try { st.value = await api.get('/api/collect/status'); runs.value = st.value.recent || runs.value } catch (e) {}
      if (timer) clearTimeout(timer)
      timer = setTimeout(pollStatus, st.value.running ? 3000 : 8000)
    }
    onMounted(pollStatus)
    async function doImport(kind) {
      busy.value = kind; result.value = null
      try {
        result.value = await api.post(kind === 'xlsx' ? '/api/import/xlsx' : '/api/import/json',
          kind === 'xlsx' ? { path: xlsxPath.value } : { dir: jsonDir.value })
        runs.value = await api.get('/api/runs')
      } catch (e) { result.value = { error: e.message } }
      busy.value = ''
    }
    async function loadPlan() {
      plan.value = await api.get('/api/strategy')
      planText.value = JSON.stringify(plan.value, null, 1)
    }
    async function genPlan() {
      genBusy.value = true
      try { plan.value = await api.post('/api/strategy/generate'); planText.value = JSON.stringify(plan.value, null, 1) }
      catch (e) { alert('生成失败：' + e.message) }
      genBusy.value = false
    }
    async function savePlan() {
      try { await api.put('/api/strategy', JSON.parse(planText.value)); planSaved.value = true; setTimeout(()=>planSaved.value=false, 1500) }
      catch (e) { alert('保存失败（JSON 格式错误？）：' + e.message) }
    }
    async function runCollect(kind, extra) {
      try { const r = await api.post('/api/collect/run', { kind, ...extra }); if (!r.ok) alert(r.error); pollStatus() }
      catch (e) { alert('启动失败：' + e.message) }
    }
    async function doSync() {
      if (!confirm('按保存的计划重跑采集（走采集号 Chrome）：新岗位入库、同词消失的下架、HR 不活跃剔除。继续？')) return
      runCollect('plan', { sync: true })
    }
    async function doRescore() {
      try { const r = await api.post('/api/sync/rescore', {}); alert('L1 已全量重算：' + r.scored + ' 条；P 级变化 ' + r.report.changed.length + ' 条（详见作战卡）') }
      catch (e) { alert('重算失败：' + e.message) }
    }
    async function cancelCollect() { await api.post('/api/collect/cancel'); pollStatus() }
    onMounted(async () => { runs.value = await api.get('/api/runs'); loadPlan() })
    return { xlsxPath, jsonDir, busy, result, runs, doImport, plan, planText, genBusy, planSaved, genPlan, savePlan,
      st, kw, city, pages, coUrl, runCollect, doSync, doRescore, cancelCollect }
  },
  template: `
  <div>
    <h2>采集中心
      <span v-if="st.running" class="warn">● 采集中：{{st.current}}</span>
      <button v-if="st.running" style="margin-left:10px" @click="cancelCollect()">取消</button>
    </h2>
    <div class="card">
      <h3>AI 采集策略（根据简历智能生成搜索计划）</h3>
      <div class="row" style="margin-bottom:8px">
        <button class="primary" :disabled="genBusy||st.running" @click="genPlan">{{genBusy?'生成中（LLM）…':'根据简历生成策略'}}</button>
        <button :disabled="st.running" @click="doSync">同步刷新（按计划重跑+下架+HR剔除）</button>
        <button :disabled="st.running" @click="doRescore">简历变更重算 L1</button>
        <span class="muted">需先在「简历档案」粘贴简历、在「设置」配置 LLM</span>
      </div>
      <textarea v-model="planText" style="min-height:180px" spellcheck="false"></textarea>
      <div class="row" style="margin-top:8px">
        <button :disabled="!planText" @click="savePlan">保存计划</button>
        <span v-if="planSaved" class="ok">已保存 ✓</span>
        <span class="grow"></span>
        <button :disabled="st.running" @click="runCollect('plan', {})">▶ 按计划采集</button>
        <span class="muted" v-if="plan && plan.searches">搜索词 {{plan.searches.length}} 组 · 定向公司 {{(plan.companies||[]).length}} 家</span>
      </div>
    </div>
    <div class="card"><h3>单项采集（采集号 Chrome · 9222）</h3>
      <div class="row">
        <input v-model="kw" placeholder="关键词" style="width:160px">
        <input v-model="city" placeholder="城市" style="width:90px">
        <input v-model.number="pages" type="number" min="1" max="10" style="width:70px">
        <button :disabled="!kw||st.running" @click="runCollect('search', {keyword:kw, city, pages})">搜索采集</button>
        <span class="grow"></span>
        <input v-model="coUrl" placeholder="公司页 URL / brandId" style="width:260px">
        <button :disabled="!coUrl||st.running" @click="runCollect('company', {url:coUrl})">公司定向</button>
      </div>
    </div>
    <div class="card" v-if="st.log && st.log.length"><h3>执行日志</h3>
      <pre class="jd" style="max-height:220px;overflow:auto">{{st.log.join('\\n')}}</pre>
    </div>
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
        <tr v-for="r in runs"><td class="muted">{{r.finished_at || '进行中'}}</td><td>{{r.kind}}</td>
        <td class="muted">{{JSON.stringify(r.stats)}}</td></tr>
      </tbody></table>
    </div>
  </div>`,
  methods: {}
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

// ── 页面：账号管理 ───────────────────────────────────────────────
const AccountsView = {
  setup() {
    const accounts = ref({}), dualEnabled = ref(true), modeBusy = ref(false)
    const loginStates = reactive({}), checking = ref('')
    const visibleAccounts = computed(() => Object.fromEntries(
      Object.entries(accounts.value).filter(([, account]) => account.enabled)))
    async function load() {
      const [accountData, settings] = await Promise.all([
        api.get('/api/accounts'), api.get('/api/settings')
      ])
      accounts.value = accountData
      dualEnabled.value = settings.dual_account_enabled !== false
    }
    onMounted(load)
    const refresh = () => load()
    async function launch(name) { await api.post('/api/accounts/' + name + '/launch'); load() }
    async function loginPage(name) { const r = await api.post('/api/accounts/' + name + '/login-page'); alert(r.ok ? '已打开登录页，请在弹出的 Chrome 中登录' : r.error); load() }
    async function loginState(name) {
      checking.value = name
      try {
        const r = await api.get('/api/accounts/' + name + '/login-state')
        if (!r.running) loginStates[name] = { kind: 'bad', text: r.hint || 'Chrome 未启动' }
        else if (r.logged_in === true) loginStates[name] = { kind: 'ok', text: '已登录' }
        else if (r.logged_in === false) loginStates[name] = { kind: 'warn', text: r.hint || '未登录' }
        else loginStates[name] = { kind: 'warn', text: r.hint || '暂时无法判断登录态' }
      } catch (e) {
        loginStates[name] = { kind: 'bad', text: '检测失败：' + e.message }
      } finally { checking.value = '' }
    }
    async function stop(name) { await api.post('/api/accounts/' + name + '/stop'); load() }
    async function changeMode(event) {
      const desired = event.target.checked
      dualEnabled.value = desired
      modeBusy.value = true
      try {
        await api.put('/api/settings', { dual_account_enabled: desired })
        accounts.value = await api.get('/api/accounts')
      } catch (e) {
        dualEnabled.value = !desired
        alert('账号模式保存失败：' + e.message)
      } finally { modeBusy.value = false }
    }
    return { visibleAccounts, dualEnabled, modeBusy, loginStates, checking,
      refresh, launch, loginPage, loginState, stop, changeMode }
  },
  template: `
  <div>
    <h2>账号管理 <button @click="refresh">刷新</button></h2>
    <div class="card">
      <div class="row">
        <div class="grow"><b>双账号模式</b>
          <div class="muted">开启后采集号与沟通号隔离；关闭后统一使用沟通号采集和沟通。</div></div>
        <label class="switch" :class="{disabled: modeBusy}">
          <input type="checkbox" :checked="dualEnabled" :disabled="modeBusy" @change="changeMode">
          <span class="switch-track"><span class="switch-thumb"></span></span>
          <span>{{dualEnabled?'已开启':'已关闭'}}</span>
        </label>
      </div>
    </div>
    <div class="card" v-for="(a,name) in visibleAccounts" :key="name">
      <div class="row">
        <div class="grow"><b>{{a.label}}</b>
          <span class="tag" v-for="role in a.roles" :key="role">{{role}}</span>
          <div class="muted">{{a.description}} · {{a.profile}} · CDP :{{a.port}}</div></div>
        <span :class="a.running?'ok':'bad'">{{a.running?'运行中':'未启动'}}</span>
      </div>
      <div class="row" style="margin-top:10px">
        <button @click="launch(name)">启动</button>
        <button @click="loginPage(name)">打开登录页</button>
        <button :disabled="checking===name" @click="loginState(name)">{{checking===name?'检测中…':'检测登录态'}}</button>
        <button @click="stop(name)">停止</button>
        <span v-if="loginStates[name]" :class="loginStates[name].kind">{{loginStates[name].text}}</span>
      </div>
    </div>
  </div>`
}

// ── 页面：设置 ───────────────────────────────────────────────────
const SettingsView = {
  setup() {
    const s = reactive({}), loaded = ref(false), saved = ref(false)
    const testing = ref(false), testResult = ref(null)
    onMounted(async () => { Object.assign(s, await api.get('/api/settings')); loaded.value = true })
    async function save() {
      await api.put('/api/settings', JSON.parse(JSON.stringify(s)))
      saved.value = true; setTimeout(() => saved.value = false, 1500)
    }
    async function testLlm() {
      testing.value = true; testResult.value = null
      try {
        const r = await api.post('/api/llm/test', {
          base_url: s.llm_base_url, api_key: s.llm_api_key, model: s.llm_model
        })
        testResult.value = { ok: true, text: `连接成功 · ${r.model} · ${r.latency_ms} ms${r.reply ? ' · ' + r.reply : ''}` }
      } catch (e) {
        testResult.value = { ok: false, text: e.message }
      } finally { testing.value = false }
    }
    return { s, loaded, saved, testing, testResult, save, testLlm }
  },
  template: `
  <div v-if="loaded">
    <h2>设置</h2>
    <div class="card"><h3>LLM（BYOK · OpenAI 兼容）</h3>
      <div class="row" style="margin-bottom:8px"><span class="muted" style="width:90px">Base URL</span><input v-model="s.llm_base_url" placeholder="https://api.deepseek.com/v1"></div>
      <div class="row" style="margin-bottom:8px"><span class="muted" style="width:90px">API Key</span><input v-model="s.llm_api_key" type="password" placeholder="sk-…"></div>
      <div class="row"><span class="muted" style="width:90px">模型</span><input v-model="s.llm_model" placeholder="deepseek-chat / gpt-4o-mini / …"></div>
      <div class="row" style="margin-top:10px">
        <button :disabled="testing" @click="testLlm">{{testing?'测试中…':'测试连通性'}}</button>
        <span v-if="testResult" :class="testResult.ok?'ok':'bad'">{{testResult.text}}</span>
      </div>
    </div>
    <div class="card"><h3>发送护栏（沟通号）</h3>
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
      if (r.startsWith('/interview')) return InterviewView
      if (r.startsWith('/messages')) return MessagesView
      if (r.startsWith('/greetings')) return GreetingsView
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
