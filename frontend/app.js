import {
  createApp, ref, reactive, computed, onMounted, onBeforeUnmount,
} from '/static/vue.esm-browser.prod.js'

// ── API 与通用格式化 ──────────────────────────────────────────────
const api = {
  async request(method, url, body) {
    const options = { method, headers: {} }
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json'
      options.body = JSON.stringify(body)
    }
    const response = await fetch(url, options)
    const contentType = response.headers.get('content-type') || ''
    const data = contentType.includes('application/json')
      ? await response.json()
      : await response.text()
    if (!response.ok) {
      throw new Error((data && (data.detail || data.error)) || data || `请求失败（${response.status}）`)
    }
    return data
  },
  get(url) { return this.request('GET', url) },
  post(url, body = {}) { return this.request('POST', url, body) },
  put(url, body = {}) { return this.request('PUT', url, body) },
  delete(url, body) { return this.request('DELETE', url, body) },
}

const fmtTime = value => {
  if (!value) return '暂无记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 19)
  return date.toLocaleString('zh-CN', { hour12: false })
}
const arrayOf = value => Array.isArray(value) ? value : (value && Array.isArray(value.items) ? value.items : [])
const first = (...values) => values.find(value => value !== undefined && value !== null && value !== '')
const score = (job, kind) => {
  if (kind === 'job') return first(job.job_score, job.l1_score, '—')
  if (kind === 'match') return first(job.match_score, job.match_rough, '—')
  return first(job.composite, job.composite_rough, '—')
}
const splitLines = value => String(value || '').split(/[\n,，]+/).map(x => x.trim()).filter(Boolean)
const joinText = value => typeof value === 'string' ? value : JSON.stringify(value || {}, null, 2)
const parseProfileField = (value, fallbackKey) => {
  const text = String(value || '').trim()
  if (!text) return {}
  try { return JSON.parse(text) } catch (_) {
    return fallbackKey === 'skills' ? { skills: splitLines(text) } : { [fallbackKey]: text }
  }
}

// ── Hash 路由 ────────────────────────────────────────────────────
const route = ref(location.hash.slice(1) || '/dashboard')
const nav = [
  ['/dashboard', '总览看板'],
  ['/jobs', '岗位列表'],
  ['/jobcard', '收藏工作台'],
  ['/collect', '采集中心'],
  ['/analytics', '数据分析'],
  ['/greetings', '招呼语'],
  ['/profile', '简历档案'],
  ['/interview', '模拟面试'],
  ['/accounts', '账号管理'],
  ['/settings', '设置'],
]
const ACCOUNT_LABELS = { collect: '采集号', account_a: '沟通号' }
const accountLabel = value => String(value || '').split(',')
  .map(name => ACCOUNT_LABELS[name] || name).join('·')
window.addEventListener('hashchange', () => {
  route.value = location.hash.slice(1) || '/dashboard'
})

// ── 页面：总览看板 ───────────────────────────────────────────────
const DashboardView = {
  setup() {
    const dashboard = ref({}), loading = ref(true), error = ref('')
    async function load() {
      loading.value = true; error.value = ''
      try { dashboard.value = await api.get('/api/dashboard') }
      catch (e) { error.value = e.message }
      finally { loading.value = false }
    }
    onMounted(load)
    const metrics = computed(() => {
      const d = dashboard.value.metrics || dashboard.value.counts || {}
      const jobs = d.jobs || {}
      const favorites = d.favorites || {}
      const applications = d.applications || {}
      const greetings = d.greetings || {}
      return [
        { label: '岗位总量', value: first(jobs.total, d.total_jobs, d.total, 0), sub: `当前有效 ${first(jobs.active, d.active_jobs, d.active, 0)} 个`, href: '#/jobs' },
        { label: '当前收藏', value: first(favorites.total, d.favorite_jobs, 0), sub: `今日 +${first(favorites.today, 0)} · 近 7 天 +${first(favorites.last_7_days, 0)}`, href: '#/jobcard' },
        { label: '已确认投递', value: first(applications.total, d.applied_jobs, 0), sub: `平台 ${first(applications.platform_confirmed, d.platform_confirmed, 0)} · 人工 ${first(applications.manual_confirmed, d.manual_confirmed, 0)}`, href: '#/jobcard' },
        { label: '已确认招呼', value: first(greetings.total, d.greeted_jobs, 0), sub: `今日 ${first(greetings.today, 0)} · 近 7 天 ${first(greetings.last_7_days, 0)}`, href: '#/greetings' },
      ]
    })
    const system = computed(() => dashboard.value.system || {})
    const accountStatus = computed(() => system.value.accounts || {})
    const risk = computed(() => system.value.risk || {})
    const accounts = computed(() => {
      const source = Array.isArray(accountStatus.value.accounts)
        ? accountStatus.value.accounts
        : Object.entries(accountStatus.value).filter(([, item]) => item && typeof item === 'object').map(([account, item]) => ({ account, ...item }))
      return source.map(item => ({
        key: item.account,
        label: item.label || (item.account === 'collector' || item.account === 'collect' ? '采集号' : '沟通号'),
        loggedIn: item.logged_in,
        checkedAt: item.checked_at,
        hint: item.hint || '',
      }))
    })
    const collect = computed(() => system.value.collection || system.value.collect || {})
    const sender = computed(() => system.value.sender || {
      halted_today: risk.value.send_halted_today,
      halt_reason: (risk.value.reasons || []).join('；'),
      sending: false,
      sent_today: first(dashboard.value.metrics?.greetings?.today, 0),
    })
    const effectiveCollectTime = computed(() => first(
      collect.value.latest_data_at, collect.value.last_effective_at,
      collect.value.online_collected_at, collect.value.file_data_at,
    ))
    const accountTone = item => item.loggedIn === true ? 'ok' : (item.loggedIn === false ? 'warn' : 'neutral')
    const collectTone = computed(() => collect.value.risk || risk.value.active ? 'bad' : (collect.value.stale ? 'warn' : 'ok'))
    return { dashboard, loading, error, metrics, system, accountStatus, accounts, collect, sender,
      effectiveCollectTime, accountTone, collectTone, fmtTime, load }
  },
  template: `
  <div>
    <div class="page-head">
      <div><h1>总览看板</h1><p>岗位进度、账号状态和采集新鲜度</p></div>
      <button :disabled="loading" @click="load">刷新状态</button>
    </div>
    <div v-if="error" class="notice bad">看板加载失败：{{error}}</div>
    <div class="metric-grid" :class="{loading}">
      <a v-for="item in metrics" :key="item.label" class="metric" :href="item.href">
        <span>{{item.label}}</span><strong>{{item.value}}</strong><small>{{item.sub}}</small>
      </a>
    </div>
    <section class="section-block">
      <div class="section-title"><div><h2>系统状态</h2><p>这里只展示最近一次缓存结果，不会主动启动 Chrome。</p></div>
        <span class="pill">{{accountStatus.label || (accountStatus.mode === 'single' ? '单账号' : '双账号')}}</span>
      </div>
      <div class="status-grid">
        <div v-for="item in accounts" :key="item.key" class="status-item">
          <span class="status-dot" :class="accountTone(item)"></span>
          <div><b>{{item.label}} · {{item.loggedIn === true ? '已登录' : (item.loggedIn === false ? '未登录' : '状态未知')}}</b>
            <p>{{item.hint || ('检测于 ' + fmtTime(item.checkedAt))}}</p></div>
          <a href="#/accounts">管理</a>
        </div>
        <div class="status-item">
          <span class="status-dot" :class="collectTone"></span>
          <div><b>{{collect.running ? '采集执行中' : (!collect.collected ? '尚未采集数据' : (collect.stale ? '采集数据需要更新' : '采集数据正常'))}}</b>
            <p>最近有效数据：{{fmtTime(effectiveCollectTime)}}<template v-if="collect.collected && collect.stale"> · 已超过 3 天</template></p></div>
          <a href="#/collect">查看</a>
        </div>
        <div class="status-item">
          <span class="status-dot" :class="sender.halted_today ? 'bad' : (sender.sending ? 'warn' : 'ok')"></span>
          <div><b>{{sender.halted_today ? '发送已熔断' : (sender.sending ? '发送执行中' : '发送护栏正常')}}</b>
            <p>{{sender.halt_reason || ('今日已确认发送 ' + (sender.sent_today || 0) + ' 条')}}</p></div>
          <a href="#/greetings">查看</a>
        </div>
      </div>
    </section>
    <section class="section-block">
      <div class="section-title"><div><h2>快速开始</h2><p>继续当前求职流程</p></div></div>
      <div class="quick-grid">
        <a href="#/collect"><b>更新岗位数据</b><span>调整关键词与城市后开始采集</span></a>
        <a href="#/jobs"><b>筛选岗位</b><span>展开 JD、收藏或排除岗位</span></a>
        <a href="#/jobcard"><b>处理收藏</b><span>比较评分并准备招呼语</span></a>
        <a href="#/analytics"><b>查看市场分布</b><span>比较关键词的薪资与门槛</span></a>
      </div>
    </section>
  </div>`,
}

// ── 页面：岗位列表 ───────────────────────────────────────────────
const JobsView = {
  setup() {
    const items = ref([]), total = ref(0), q = ref(''), keyword = ref('')
    const headhunter = ref('all'), favorite = ref('all'), sort = ref('composite')
    const page = ref(0), loading = ref(false), scoring = ref(false), resumes = ref([])
    const resumeId = ref(''), expanded = ref(''), details = reactive({}), detailLoading = ref(''), excludedMode = ref(false)
    const PAGE = 40
    async function loadResumes() {
      try {
        const data = await api.get('/api/resumes')
        resumes.value = arrayOf(data).filter(item => !item.archived_at)
        resumeId.value = String(first(data.default_id, resumes.value.find(item => item.is_default)?.id, resumes.value[0]?.id, ''))
      } catch (_) { resumes.value = [] }
    }
    async function load() {
      loading.value = true
      try {
        if (excludedMode.value) {
          const data = await api.get('/api/jobs-excluded?limit=' + PAGE + '&offset=' + page.value * PAGE)
          items.value = data.items || []; total.value = data.total || 0
          return
        }
        const p = new URLSearchParams({ status: 'active', q: q.value, keyword: keyword.value,
          headhunter: headhunter.value, favorite: favorite.value, sort: sort.value,
          resume_id: resumeId.value, limit: PAGE, offset: page.value * PAGE })
        const data = await api.get('/api/jobs?' + p)
        items.value = data.items || []; total.value = data.total || 0
      } catch (e) { alert('岗位加载失败：' + e.message) }
      finally { loading.value = false }
    }
    onMounted(async () => { await loadResumes(); await load() })
    async function openJob(j) {
      if (expanded.value === j.job_key) { expanded.value = ''; return }
      expanded.value = j.job_key
      detailLoading.value = j.job_key
      try {
        details[j.job_key] = await api.get('/api/jobs/' + encodeURIComponent(j.job_key) +
          (resumeId.value ? '?resume_id=' + encodeURIComponent(resumeId.value) : ''))
      } catch (e) { details[j.job_key] = { error: e.message } }
      finally { detailLoading.value = '' }
    }
    async function reloadDetail(j) {
      expanded.value = ''
      delete details[j.job_key]
      await openJob(j)
    }
    async function toggleFavorite(j) {
      try {
        const enabled = !(j.favorite_at || j.is_favorite || j.favorite)
        await api.post(`/api/jobs/${encodeURIComponent(j.job_key)}/favorite`, { favorite: enabled })
        j.is_favorite = enabled; j.favorite = enabled; j.favorite_at = enabled ? new Date().toISOString() : null
        if (details[j.job_key]) details[j.job_key].favorite_at = j.favorite_at
      } catch (e) { alert('收藏操作失败：' + e.message) }
    }
    async function openBoss(j) {
      try { await api.post(`/api/jobs/${encodeURIComponent(j.job_key)}/open-boss`) }
      catch (e) { alert('沟通号 Chrome 打开岗位失败：' + e.message) }
    }
    async function excludeJob(j) {
      if (!confirm(`确认不再显示「${j.title} · ${j.company}」？后续采集会跳过该岗位，可在排除管理中恢复。`)) return
      try {
        await api.post(`/api/jobs/${encodeURIComponent(j.job_key)}/exclude`)
        items.value = items.value.filter(item => item.job_key !== j.job_key); total.value--
      } catch (e) { alert('排除失败：' + e.message) }
    }
    async function setHeadhunter(j, value) {
      try {
        const data = await api.put(`/api/jobs/${encodeURIComponent(j.job_key)}/headhunter`, { value })
        j.is_headhunter = data.is_headhunter ?? value
        j.effective_headhunter = data.effective_headhunter ?? value
        j.headhunter_override = value
        if (details[j.job_key]) Object.assign(details[j.job_key], data)
      } catch (e) { alert('猎头标记保存失败：' + e.message) }
    }
    async function probeApplication(j) {
      try {
        const data = await api.post(`/api/applications/${encodeURIComponent(j.job_key)}/probe`, { resume_id: Number(resumeId.value) || null })
        if (details[j.job_key]) details[j.job_key].application = data
        alert(data.status === 'platform_confirmed' ? 'BOSS 页面已明确显示投递成功。' : '未发现可确认的投递证据，状态保持未知。')
      } catch (e) { alert('同步失败，投递状态保持未知：' + e.message) }
    }
    async function confirmApplication(j) {
      if (!confirm('请先在 BOSS 原平台核验。确认已使用当前简历完成投递？')) return
      try {
        const data = await api.post(`/api/applications/${encodeURIComponent(j.job_key)}/confirm`, { resume_id: Number(resumeId.value) || null })
        if (details[j.job_key]) details[j.job_key].application = data
      } catch (e) { alert('确认失败：' + e.message) }
    }
    async function restoreJob(j) {
      try {
        await api.post(`/api/jobs/${encodeURIComponent(j.job_key)}/restore`)
        items.value = items.value.filter(item => item.job_key !== j.job_key); total.value--
      } catch (e) { alert('恢复失败：' + e.message) }
    }
    async function runL1() {
      scoring.value = true
      try { const result = await api.post('/api/score/l1', { resume_id: Number(resumeId.value) || null }); alert(`L1 评分完成：${result.scored ?? result.count ?? 0} 条`); load() }
      catch (e) { alert('L1 评分失败：' + e.message) }
      finally { scoring.value = false }
    }
    async function runL2() {
      const count = prompt('精评当前简历下综合粗分最高且尚无有效 L2 的岗位数量：', '10')
      if (!count) return
      scoring.value = true
      try { await api.post('/api/score/l2', { limit: Number(count), resume_id: Number(resumeId.value) || null }); await load() }
      catch (e) { alert('L2 精评失败：' + e.message) }
      finally { scoring.value = false }
    }
    const pages = computed(() => Math.max(1, Math.ceil(total.value / PAGE)))
    const search = () => { page.value = 0; expanded.value = ''; load() }
    const toggleExcludedMode = () => { excludedMode.value = !excludedMode.value; page.value = 0; expanded.value = ''; load() }
    return { items, total, q, keyword, headhunter, favorite, sort, page, pages, loading, scoring,
      resumes, resumeId, expanded, details, detailLoading, excludedMode, fmtTime, score, first, search, load, openJob,
      reloadDetail, toggleFavorite, openBoss, excludeJob, setHeadhunter, probeApplication,
      confirmApplication, restoreJob, toggleExcludedMode, runL1, runL2,
      prev: () => { page.value--; load() }, next: () => { page.value++; load() } }
  },
  template: `
  <div>
    <div class="page-head"><div><h1>{{excludedMode ? '排除岗位管理' : '岗位列表'}}</h1><p>{{excludedMode ? '仅在此恢复已排除岗位。' : '当前有效岗位 ' + total + ' 个，点击任意岗位在列表内查看完整 JD。'}}</p></div>
      <div class="row"><button @click="toggleExcludedMode">{{excludedMode ? '返回有效岗位' : '排除管理'}}</button><template v-if="!excludedMode"><button :disabled="scoring" @click="runL2">L2 精评</button><button :disabled="scoring" @click="runL1">运行 L1</button></template></div>
    </div>
    <div v-if="!excludedMode" class="filterbar">
      <input v-model="q" aria-label="搜索岗位或公司" placeholder="搜索岗位或公司" @keyup.enter="search">
      <input v-model="keyword" aria-label="采集关键词" placeholder="采集关键词" @keyup.enter="search">
      <select v-model="headhunter" aria-label="猎头筛选" @change="search"><option value="all">全部来源</option><option value="only">仅猎头岗位</option><option value="exclude">排除猎头岗位</option></select>
      <select v-model="favorite" aria-label="收藏筛选" @change="search"><option value="all">全部岗位</option><option value="only">仅收藏</option></select>
      <select v-model="resumeId" aria-label="评分简历" @change="search"><option value="">默认评分</option><option v-for="r in resumes" :key="r.id" :value="String(r.id)">{{r.name}}</option></select>
      <select v-model="sort" aria-label="排序" @change="search"><option value="composite">综合评分</option><option value="job">岗位评分</option><option value="match">匹配度</option><option value="salary">薪资上限</option><option value="recent">最近活跃</option></select>
      <button class="primary" @click="search">筛选</button>
    </div>
    <div class="table-wrap">
      <table class="jobs-table">
        <thead><tr><th>岗位</th><th>公司</th><th>薪资</th><th>经验/学历</th><th>岗位评分</th><th>匹配度评分</th><th>P级</th><th>活跃时间</th></tr></thead>
        <tbody>
          <template v-for="j in items" :key="j.job_key">
            <tr class="job-row" :class="{open: expanded===j.job_key}" tabindex="0" @click="openJob(j)" @keyup.enter="openJob(j)" @keyup.space.prevent="openJob(j)">
              <td><div class="job-title"><span class="chevron" aria-hidden="true"></span><div><b>{{j.title}}</b><p>{{j.location || '地区未知'}}<span v-if="j.effective_headhunter ?? j.is_headhunter" class="tag warn-tag">猎头</span><span v-if="j.favorite_at || j.is_favorite || j.favorite" class="tag ok-tag">已收藏</span></p></div></div></td>
              <td><b>{{j.company}}</b><p>{{[j.industry,j.scale].filter(Boolean).join(' · ') || '公司信息待补充'}}</p></td>
              <td class="nowrap">{{j.salary || '面议'}}</td>
              <td><span>{{j.experience || '不限'}}</span><p>{{j.degree || '不限'}}</p></td>
              <td class="score">{{score(j,'job')}}</td><td class="score">{{score(j,'match')}}</td>
              <td><span v-if="j.priority" class="tag" :class="String(j.priority).toLowerCase()">{{j.priority}}</span><span v-else>—</span></td>
              <td><span>{{j.hr_active || '未知'}}</span><p>{{fmtTime(j.last_seen_at)}}</p></td>
            </tr>
            <tr v-if="expanded===j.job_key" class="detail-row"><td colspan="8">
              <div v-if="detailLoading===j.job_key" class="detail-loading">正在加载职位详情…</div>
              <div v-else-if="details[j.job_key]?.error" class="notice bad">加载失败：{{details[j.job_key].error}}</div>
              <div v-else-if="details[j.job_key]" class="job-detail">
                <div class="detail-toolbar">
                  <template v-if="excludedMode"><button class="primary" @click.stop="restoreJob(j)">恢复岗位</button></template>
                  <template v-else><button :class="{primary: details[j.job_key].favorite_at || j.is_favorite || j.favorite}" @click.stop="toggleFavorite(j)">{{details[j.job_key].favorite_at || j.is_favorite || j.favorite ? '取消收藏' : '收藏岗位'}}</button>
                  <button @click.stop="excludeJob(j)">不再显示</button></template>
                  <button v-if="details[j.job_key].job_link || j.job_link" @click.stop="openBoss(j)">打开 BOSS</button>
                  <template v-if="!excludedMode"><span class="separator"></span>
                  <label>猎头标记 <select :value="details[j.job_key].headhunter_override === null || details[j.job_key].headhunter_override === undefined ? 'auto' : String(Boolean(details[j.job_key].headhunter_override))" @click.stop @change.stop="setHeadhunter(j, $event.target.value==='auto' ? null : $event.target.value==='true')"><option value="auto">自动识别</option><option value="true">是猎头</option><option value="false">非猎头</option></select></label>
                  <span class="grow"></span>
                  <button @click.stop="probeApplication(j)">同步投递状态</button><button @click.stop="confirmApplication(j)">人工确认已投递</button></template>
                </div>
                <div class="detail-meta">
                  <div><span>当前岗位评分</span><b>{{score(details[j.job_key],'job')}}</b></div><div><span>当前匹配度</span><b>{{score(details[j.job_key],'match')}}</b></div><div><span>导入基线</span><b>{{first(details[j.job_key].imported_baseline?.composite, details[j.job_key].baseline?.composite, '—')}}</b></div><div><span>投递状态</span><b>{{details[j.job_key].application?.status_label || details[j.job_key].application?.status || '未知'}}</b></div>
                </div>
                <div v-if="details[j.job_key].headhunter_reason" class="hint">猎头识别依据：{{details[j.job_key].headhunter_reason}}</div>
                <h3>职位描述（JD）</h3><pre class="jd">{{details[j.job_key].jd || '暂无完整 JD，可在采集中心重试详情。'}}</pre>
              </div>
            </td></tr>
          </template>
        </tbody>
      </table>
      <div v-if="!items.length && !loading" class="empty">没有符合条件的有效岗位</div>
    </div>
    <div class="pagination"><button :disabled="page===0 || loading" @click="prev">上一页</button><span>第 {{page+1}} / {{pages}} 页</span><button :disabled="page>=pages-1 || loading" @click="next">下一页</button></div>
  </div>`,
}

// ── 页面：收藏工作台 ─────────────────────────────────────────────
const JobCardView = {
  setup() {
    const jobs = ref([]), resumes = ref([]), resumeId = ref(''), archived = ref(false), loading = ref(false)
    const selectedKeys = ref([]), greetedKeys = ref([]), generating = ref(false), progressText = ref('')
    const favSync = ref({}), favBusy = ref(''), favPrevRunning = ref(false)
    async function loadResumes() {
      const data = await api.get('/api/resumes')
      resumes.value = arrayOf(data).filter(item => !item.archived_at)
      resumeId.value = String(first(data.default_id, resumes.value.find(item => item.is_default)?.id, resumes.value[0]?.id, ''))
    }
    async function loadGreetings() {
      // 已生成 = 当前简历下存在未跳过的招呼语（skipped 视为可重新生成）
      try {
        const items = arrayOf(await api.get('/api/greetings'))
        greetedKeys.value = items
          .filter(g => g.status !== 'skipped' && g.resume_id === Number(resumeId.value))
          .map(g => g.job_key)
      } catch (_) { greetedKeys.value = [] }
    }
    async function load() {
      loading.value = true
      try {
        await loadGreetings()
        const p = new URLSearchParams({ favorite: 'only', status: archived.value ? 'archived' : 'active', resume_id: resumeId.value, limit: 200 })
        const data = await api.get('/api/jobs?' + p); jobs.value = data.items || []
      } catch (e) { alert('收藏工作台加载失败：' + e.message) }
      finally {
        // 刷新后勾选只保留当前列表里尚未生成的岗位
        const greeted = greetedKeys.value
        selectedKeys.value = jobs.value
          .filter(job => selectedKeys.value.includes(job.job_key) && !greeted.includes(job.job_key))
          .map(job => job.job_key)
        loading.value = false
      }
    }
    onMounted(async () => { try { await loadResumes() } catch (_) {} await load(); pollFavSync() })
    const favTimer = setInterval(pollFavSync, 4000)
    onBeforeUnmount(() => clearInterval(favTimer))
    async function pollFavSync() {
      try { favSync.value = await api.get('/api/favorites/sync/status') } catch (_) { return }
      // 同步从运行转为结束时自动刷新收藏列表
      if (favPrevRunning.value && !favSync.value.running) load()
      favPrevRunning.value = favSync.value.running
    }
    async function startFavSync() {
      try {
        const result = await api.post('/api/favorites/sync', {})
        if (result.ok === false) alert(result.error || '启动失败')
        pollFavSync()
      } catch (e) { alert('收藏同步启动失败：' + e.message) }
    }
    async function cancelFavSync() { try { await api.post('/api/favorites/sync/cancel') } catch (_) {} pollFavSync() }
    async function retryFavJd() {
      favBusy.value = 'jd'
      try {
        const result = await api.post('/api/favorites/sync/retry-details')
        alert(`已开始补齐 ${result.missing} 个缺失 JD（采集号只读执行，进度见下方状态）`)
      } catch (e) { alert('补齐启动失败：' + e.message) }
      finally { favBusy.value = ''; pollFavSync() }
    }
    const lastAccounts = computed(() => favSync.value.last_result?.accounts || [])
    const hasGreeting = job => greetedKeys.value.includes(job.job_key)
    const pendingSelection = computed(() => selectedKeys.value.filter(key => !greetedKeys.value.includes(key)))
    function toggleAllPending() {
      const pending = jobs.value.filter(job => !hasGreeting(job)).map(job => job.job_key)
      const allSelected = pending.length > 0 && pending.every(key => selectedKeys.value.includes(key))
      selectedKeys.value = allSelected ? [] : pending
    }
    async function openBoss(job) {
      try { await api.post(`/api/jobs/${encodeURIComponent(job.job_key)}/open-boss`) }
      catch (e) { alert('沟通号 Chrome 打开岗位失败：' + e.message) }
    }
    async function remove(job) {
      if (!confirm('从收藏工作台移除此岗位？')) return
      await api.post(`/api/jobs/${encodeURIComponent(job.job_key)}/favorite`, { favorite: false }); load()
    }
    async function generate(job) {
      if (hasGreeting(job) || generating.value) return
      try { await api.post('/api/greeting/generate', { job_key: job.job_key, resume_id: Number(resumeId.value) || null }); window.location.hash = '#/greetings' }
      catch (e) { alert('生成失败：' + e.message) }
    }
    async function generateSelected() {
      const targets = jobs.value.filter(job => pendingSelection.value.includes(job.job_key))
      if (!targets.length) { alert('请先勾选尚未生成招呼语的岗位'); return }
      generating.value = true
      const failures = []
      for (let index = 0; index < targets.length; index++) {
        const job = targets[index]
        progressText.value = `${index + 1}/${targets.length} · ${(job.title || '').slice(0, 12)}`
        try { await api.post('/api/greeting/generate', { job_key: job.job_key, resume_id: Number(resumeId.value) || null }) }
        catch (e) { failures.push(`${job.title || job.job_key}：${e.message}`) }
      }
      generating.value = false; progressText.value = ''
      await loadGreetings()
      selectedKeys.value = selectedKeys.value.filter(key => !greetedKeys.value.includes(key))
      alert(failures.length
        ? `批量生成完成：成功 ${targets.length - failures.length} 条，失败 ${failures.length} 条\n${failures.join('\n')}`
        : `已为 ${targets.length} 个岗位生成招呼语，可到「招呼语」页编辑选用。`)
    }
    return { jobs, resumes, resumeId, archived, loading, selectedKeys, generating, progressText,
      favSync, favBusy, startFavSync, cancelFavSync, retryFavJd, lastAccounts, accountLabel,
      hasGreeting, pendingSelection, score, fmtTime, load, toggleAllPending, openBoss, remove, generate, generateSelected }
  },
  template: `
  <div>
    <div class="page-head"><div><h1>收藏工作台</h1><p>收藏一次即可在此切换简历比较评分并准备联系；「同步BOSS收藏」会把两个账号的感兴趣岗位增量合并进来。</p></div>
      <div class="row"><select v-model="resumeId" @change="load"><option v-for="r in resumes" :key="r.id" :value="String(r.id)">{{r.name}}</option></select><label class="check"><input type="checkbox" v-model="archived" @change="load"> 查看下架归档</label>
        <button :disabled="favSync.running || generating" @click="startFavSync">{{favSync.running ? 'BOSS收藏同步中…' : '同步BOSS收藏'}}</button>
        <button :disabled="generating || !jobs.length" @click="toggleAllPending">全选未生成</button>
        <button class="primary" :disabled="generating || !pendingSelection.length" @click="generateSelected">{{generating ? '生成中 ' + progressText : '一键生成选中招呼语' + (pendingSelection.length ? '（' + pendingSelection.length + '）' : '')}}</button></div>
    </div>
    <div v-if="favSync.running || lastAccounts.length" class="status-strip">
      <span v-if="favSync.running" class="warn">正在同步{{favSync.current ? '：' + favSync.current : ''}}<template v-if="favSync.page"> · 第 {{favSync.page}} 页</template></span>
      <span v-for="a in lastAccounts" :key="a.account" :class="a.ok ? 'ok' : 'bad'">{{a.label}} {{a.ok ? a.total + ' 个 · 新收藏 ' + a.newly_favorited : '失败：' + a.error}}</span>
      <span v-if="favSync.last_result && !favSync.running" class="hint">上次同步 {{fmtTime(favSync.last_result.finished_at)}}<template v-if="favSync.missing_jd"> · {{favSync.missing_jd}} 个缺 JD</template></span>
      <button v-if="favSync.running" @click="cancelFavSync">取消同步</button>
      <button v-else-if="favSync.missing_jd" :disabled="favBusy==='jd' || favSync.running" @click="retryFavJd">补齐缺失 JD</button>
    </div>
    <div v-if="favSync.last_result?.risk_signal" class="notice bad">收藏同步遇风控信号停止：{{favSync.last_result.risk_signal}}</div>
    <div v-if="!jobs.length && !loading" class="empty large">暂无收藏岗位，先到<a href="#/jobs">岗位列表</a>展开 JD 并收藏，或点「同步BOSS收藏」导入账号的感兴趣岗位。</div>
    <article v-for="job in jobs" :key="job.job_key" class="work-item">
      <div class="work-main"><div><h2>{{job.title}}</h2><p>{{job.company}} · {{job.salary}} · {{job.experience || '经验不限'}} · {{job.degree || '学历不限'}}<span v-if="job.favorite_accounts" class="tag ok-tag">BOSS收藏·{{accountLabel(job.favorite_accounts)}}</span></p></div>
        <div class="score-pair"><span><b>{{score(job,'job')}}</b>岗位分</span><span><b>{{score(job,'match')}}</b>匹配度</span><span><b>{{job.priority || '—'}}</b>P级</span></div></div>
      <div class="work-actions"><label class="check"><input type="checkbox" :value="job.job_key" v-model="selectedKeys" :disabled="hasGreeting(job) || generating"> 多选</label><span class="hint">最近活跃 {{job.hr_active || fmtTime(job.last_seen_at)}}</span><span class="grow"></span>
        <button @click="openBoss(job)">打开 BOSS</button><button :disabled="hasGreeting(job) || generating" @click="generate(job)">{{hasGreeting(job) ? '招呼语已生成' : '生成招呼语'}}</button><button @click="remove(job)">取消收藏</button></div>
    </article>
  </div>`,
}

// ── 页面：招呼语 ─────────────────────────────────────────────────
const GreetingsView = {
  setup() {
    const items = ref([]), tab = ref(''), sendStatus = ref({}), busy = ref(''), manualOpened = reactive({})
    const tabs = [['', '全部'], ['draft', '待编辑'], ['approved', '待发送'], ['sent', '已确认'], ['needs_review', '待核验'], ['failed', '失败']]
    const normalize = item => ({ ...item,
      selectedIndex: Math.max(0, (item.variants || []).findIndex(variant => (typeof variant === 'string' ? variant : variant.text || variant.content) === item.chosen)),
      variants: (item.variants || []).map((variant, index) => typeof variant === 'string'
      ? { key: ['professional', 'concise', 'technical'][index] || String(index), label: ['专业完整版', '精简版', '技术聚焦版'][index] || `候选 ${index + 1}`, text: variant }
      : { ...variant, label: variant.label || ['专业完整版', '精简版', '技术聚焦版'][index], text: variant.text || variant.content || '' }) })
    async function load() {
      try { items.value = arrayOf(await api.get('/api/greetings' + (tab.value ? '?status=' + tab.value : ''))).map(normalize) }
      catch (e) { alert('招呼语加载失败：' + e.message) }
    }
    async function loadStatus() { try { sendStatus.value = await api.get('/api/greeting/send-status') } catch (_) {} }
    onMounted(() => { load(); loadStatus() })
    const timer = setInterval(loadStatus, 15000)
    onBeforeUnmount(() => clearInterval(timer))
    async function save(g) {
      busy.value = `save-${g.id}`
      try {
        const index = g.selectedIndex || 0
        await api.put(`/api/greetings/${g.id}`, { index, chosen_text: g.variants[index]?.text || '' })
        g.chosen = g.variants[index]?.text || ''
        alert('当前选用文案已保存')
      }
      catch (e) { alert('保存失败：' + e.message) }
      finally { busy.value = '' }
    }
    async function approve(g, index) {
      busy.value = `approve-${g.id}`
      try {
        await api.put(`/api/greetings/${g.id}`, { index, chosen_text: g.variants[index].text })
        g.selectedIndex = index
        await load()
      } catch (e) { alert('选定失败：' + e.message) }
      finally { busy.value = '' }
    }
    const chosenText = g => g.variants[g.selectedIndex || 0]?.text || g.chosen || ''
    async function openBoss(g) {
      try { await api.post(`/api/jobs/${encodeURIComponent(g.job_key)}/open-boss`) }
      catch (e) { alert('沟通号 Chrome 打开岗位失败：' + e.message); return false }
      return true
    }
    async function copyAndOpen(g) {
      const text = chosenText(g)
      if (!text) { alert('请先选定一版招呼语'); return }
      try { await navigator.clipboard.writeText(text) }
      catch (_) { alert('浏览器未授权复制，请手动复制文案。') }
      if (await openBoss(g)) manualOpened[g.id] = true
    }
    async function confirmManual(g) {
      if (!confirm('仅在 BOSS 原平台明确发送成功后确认。是否已发送？')) return
      try { await api.post(`/api/greetings/${g.id}/confirm-manual`, { chosen_text: chosenText(g) }); await load(); await loadStatus() }
      catch (e) { alert('确认失败：' + e.message) }
    }
    async function skip(g) { await api.post(`/api/greetings/${g.id}/skip`); load() }
    async function sendBatch() {
      if (!confirm('自动发送为次级方式，将使用沟通号并严格执行全部护栏。确认启动已批准批次？')) return
      try { const result = await api.post('/api/greeting/send-batch'); if (!result.ok) alert(result.error || '启动失败'); loadStatus() }
      catch (e) { alert('启动失败：' + e.message) }
    }
    return { items, tab, tabs, sendStatus, busy, manualOpened, load, save, approve, copyAndOpen, confirmManual, skip, sendBatch,
      switchTab: value => { tab.value = value; load() } }
  },
  template: `
  <div>
    <div class="page-head"><div><h1>招呼语</h1><p>优先复制到 BOSS 原平台人工发送；自动发送继续受全部护栏约束。</p></div>
      <button :disabled="sendStatus.sending || sendStatus.halted_today" @click="sendBatch">自动发送已批准批次</button>
    </div>
    <div class="status-strip"><span>今日已确认 {{sendStatus.sent_today ?? 0}} 条</span><span v-if="sendStatus.sending" class="warn">自动发送执行中</span><span v-if="sendStatus.halted_today" class="bad">已熔断：{{sendStatus.halt_reason}}</span><span v-else class="ok">护栏正常</span></div>
    <div class="tabs"><button v-for="[value,label] in tabs" :key="value" :class="{active:tab===value}" @click="switchTab(value)">{{label}}</button></div>
    <article v-for="g in items" :key="g.id" class="greeting-item">
      <div class="item-head"><div><h2>{{g.title}}</h2><p>{{g.company}} · {{g.salary}} · {{g.resume_name || '默认简历'}}</p></div><span class="pill">{{g.status_label || g.status}}</span></div>
      <div class="variant-grid">
        <label v-for="(variant,index) in g.variants" :key="variant.key" class="variant" :class="{selected:g.selectedIndex===index}">
          <span><input type="radio" :name="'g-'+g.id" :checked="g.selectedIndex===index" @change="approve(g,index)"> {{variant.label}}</span>
          <textarea v-model="variant.text" :aria-label="variant.label"></textarea><small>{{variant.text.length}} 字</small>
        </label>
      </div>
      <div class="item-actions"><button @click="save(g)">保存修改</button><button class="primary" @click="copyAndOpen(g)">复制并打开 BOSS</button><button v-if="manualOpened[g.id]" @click="confirmManual(g)">确认已发送</button><span class="grow"></span><button v-if="g.status==='draft'" @click="skip(g)">跳过</button></div>
      <div v-if="g.result_status==='needs_review' || g.status==='needs_review'" class="notice warn">自动发送结果不明确，请到 BOSS 原平台人工核验；系统不会自动重试。</div>
    </article>
    <div v-if="!items.length" class="empty large">当前分类暂无招呼语。请先在收藏工作台为岗位生成。</div>
  </div>`,
}

// ── 页面：多简历档案 ─────────────────────────────────────────────
const ProfileView = {
  setup() {
    const resumes = ref([]), selectedId = ref(null), showArchived = ref(false), saving = ref(false)
    const form = reactive({ id: null, name: '', boss_resume_label: '', expectations: '', skill_profile: '', resume_text: '', revision: 1, archived_at: null })
    async function load(keepId) {
      try {
        const data = await api.get('/api/resumes?include_archived=' + String(showArchived.value))
        resumes.value = arrayOf(data)
        const id = keepId || selectedId.value || first(data.default_id, resumes.value[0]?.id)
        const current = resumes.value.find(item => item.id === Number(id)) || resumes.value[0]
        if (current) select(current); else newResume()
      } catch (e) { alert('简历档案加载失败：' + e.message) }
    }
    function select(item) {
      selectedId.value = item.id
      Object.assign(form, item, { expectations: joinText(item.expectations), skill_profile: joinText(item.skill_profile || item.skills_profile) })
    }
    function newResume() {
      selectedId.value = null
      Object.assign(form, { id: null, name: '', boss_resume_label: '', expectations: '', skill_profile: '', resume_text: '', revision: 1, archived_at: null })
    }
    onMounted(() => load())
    async function save() {
      if (!form.name.trim() || !form.resume_text.trim()) { alert('请填写简历名称和正文'); return }
      saving.value = true
      const body = { name: form.name.trim(), boss_resume_label: form.boss_resume_label.trim(),
        expectations: parseProfileField(form.expectations, 'notes'),
        skill_profile: parseProfileField(form.skill_profile, 'skills'), resume_text: form.resume_text }
      try {
        const data = form.id ? await api.put(`/api/resumes/${form.id}`, body) : await api.post('/api/resumes', body)
        const wasUpdate = Boolean(form.id)
        await load(data.id || form.id)
        alert(wasUpdate ? '已保存并生成新修订；L1 已更新，旧 L2 将按规则失效。' : '简历已创建。')
      } catch (e) { alert('保存失败：' + e.message) }
      finally { saving.value = false }
    }
    async function setDefault() { try { await api.post(`/api/resumes/${form.id}/default`); await load(form.id) } catch (e) { alert('设置失败：' + e.message) } }
    async function archive() {
      if (!confirm('归档后不会再用于新评分，但历史记录会保留。确认？')) return
      try { await api.delete(`/api/resumes/${form.id}`); await load() } catch (e) { alert('归档失败：' + e.message) }
    }
    async function restore() { try { await api.post(`/api/resumes/${form.id}/restore`); await load(form.id) } catch (e) { alert('恢复失败：' + e.message) } }
    return { resumes, selectedId, showArchived, form, saving, fmtTime, load, select, newResume, save, setDefault, archive, restore }
  },
  template: `
  <div>
    <div class="page-head"><div><h1>简历档案</h1><p>不同简历拥有独立评分与招呼语记录，更新会生成新修订。</p></div><button class="primary" @click="newResume">新建简历</button></div>
    <div class="profile-layout">
      <aside class="resume-list"><label class="check"><input type="checkbox" v-model="showArchived" @change="load()"> 显示已归档</label>
        <button v-for="item in resumes" :key="item.id" :class="{active:selectedId===item.id}" @click="select(item)"><b>{{item.name}}</b><span>{{item.is_default ? '默认 · ' : ''}}修订 {{item.revision || 1}}<template v-if="item.archived_at"> · 已归档</template></span></button>
      </aside>
      <section class="editor" v-if="form">
        <div class="form-grid two"><label>档案名称<input v-model="form.name" placeholder="如：嵌入式与 AI Agent"></label><label>BOSS 简历标签<input v-model="form.boss_resume_label" placeholder="便于在原平台核对"></label></div>
        <label>求职期望<textarea class="short" v-model="form.expectations" placeholder="可填写说明，或 JSON：城市、薪资、方向"></textarea></label>
        <label>个人技能画像<textarea class="short" v-model="form.skill_profile" placeholder="技能用逗号或换行分隔，也可填写分类 JSON"></textarea></label>
        <label>简历正文<textarea class="resume-text" v-model="form.resume_text" placeholder="粘贴 Markdown 或纯文本简历"></textarea></label>
        <div class="editor-foot"><span v-if="form.id" class="hint">修订 {{form.revision || 1}} · 更新于 {{fmtTime(form.updated_at)}}</span><span class="grow"></span><button v-if="form.id && !form.is_default && !form.archived_at" @click="setDefault">设为默认</button><button v-if="form.id && !form.archived_at" @click="archive">归档</button><button v-if="form.id && form.archived_at" @click="restore">恢复</button><button class="primary" :disabled="saving || form.archived_at" @click="save">{{saving ? '保存中…' : '保存简历'}}</button></div>
      </section>
    </div>
  </div>`,
}

// ── 页面：模块化采集中心 ─────────────────────────────────────────
const DEFAULT_CITY_GROUPS = [
  { province: '直辖市', cities: [{ code: '101010100', name: '北京' }, { code: '101020100', name: '上海' }, { code: '101030100', name: '天津' }, { code: '101040100', name: '重庆' }] },
  { province: '广东', cities: [{ code: '101280600', name: '深圳' }, { code: '101280100', name: '广州' }, { code: '101280800', name: '佛山' }, { code: '101281600', name: '东莞' }, { code: '101280300', name: '惠州' }] },
  { province: '浙江', cities: [{ code: '101210100', name: '杭州' }, { code: '101210200', name: '湖州' }, { code: '101210300', name: '嘉兴' }, { code: '101210900', name: '金华' }] },
  { province: '江苏', cities: [{ code: '101190400', name: '苏州' }, { code: '101190100', name: '南京' }, { code: '101190200', name: '无锡' }, { code: '101191100', name: '常州' }] },
  { province: '湖北', cities: [{ code: '101200100', name: '武汉' }] },
  { province: '四川', cities: [{ code: '101270100', name: '成都' }] },
  { province: '陕西', cities: [{ code: '101110100', name: '西安' }] },
  { province: '福建', cities: [{ code: '101230200', name: '厦门' }, { code: '101230100', name: '福州' }] },
  { province: '湖南', cities: [{ code: '101250100', name: '长沙' }] },
  { province: '河南', cities: [{ code: '101180100', name: '郑州' }] },
]
const FILTER_LABEL_BY_CODE = {
  salary: { 402: '3K以下', 403: '3-5K', 404: '5-10K', 405: '10-20K', 406: '20-50K', 407: '50K以上' },
  experience: { 108: '在校生', 102: '应届生', 101: '经验不限', 103: '1年以内', 104: '1-3年', 105: '3-5年', 106: '5-10年', 107: '10年以上' },
  degree: { 209: '初中及以下', 208: '中专/中技', 206: '高中', 202: '大专', 203: '本科', 204: '硕士', 205: '博士' },
  scale: { 301: '0-20人', 302: '20-99人', 303: '100-499人', 304: '500-999人', 305: '1000-9999人', 306: '10000人以上' },
  stage: { 801: '未融资', 802: '天使轮', 803: 'A轮', 804: 'B轮', 805: 'C轮', 806: 'D轮及以上', 807: '已上市', 808: '不需要融资' },
  industry: { 1001: '互联网', 1002: '电子商务', 1003: '金融', 1004: '游戏', 1005: '企业服务', 1006: '教育培训', 1007: '社交网络', 1008: '医疗健康', 1009: '生活服务', 1010: '广告营销' },
}
const CollectView = {
  setup() {
    const config = reactive({ keywords: '嵌入式开发\nAI Agent', city_codes: ['101280600'], pages: 3,
      salary: '', experience: [], degree: [], scale: [], stage: [], industry: [], companies: '' })
    const cityGroups = ref(DEFAULT_CITY_GROUPS), citySearch = ref(''), status = ref({ running: false, log: [] })
    const runs = ref([]), busy = ref(''), xlsxPath = ref(''), jsonDir = ref(''), importResult = ref(null)
    const optionGroups = {
      experience: ['经验不限', '在校生', '应届生', '1年以内', '1-3年', '3-5年', '5-10年', '10年以上'],
      degree: ['不限', '大专', '本科', '硕士', '博士'],
      scale: ['0-20人', '20-99人', '100-499人', '500-999人', '1000-9999人', '10000人以上'],
      stage: ['未融资', '天使轮', 'A轮', 'B轮', 'C轮', 'D轮及以上', '已上市', '不需要融资'],
      industry: ['互联网', '电子商务', '金融', '游戏', '企业服务', '教育培训', '社交网络', '医疗健康', '生活服务', '广告营销'],
    }
    const optionLabels = { experience: '经验', degree: '学历', scale: '公司规模', stage: '融资阶段', industry: '行业' }
    const keywords = computed(() => splitLines(config.keywords))
    const companyTargets = computed(() => splitLines(config.companies))
    const hasTasks = computed(() => keywords.value.length > 0 || companyTargets.value.length > 0)
    const combinations = computed(() => keywords.value.length * config.city_codes.length)
    const selectedCities = computed(() => cityGroups.value.flatMap(group => group.cities.map(city => ({
      province: group.province, city: city.name, city_code: city.code,
    }))).filter(city => config.city_codes.includes(city.city_code)))
    const filteredCityGroups = computed(() => !citySearch.value.trim() ? cityGroups.value : cityGroups.value.map(group => ({ ...group, cities: group.cities.filter(city => city.name.includes(citySearch.value.trim())) })).filter(group => group.cities.length))
    const listProgress = computed(() => status.value.list_progress || {
      done: status.value.progress?.list_completed || 0,
      total: status.value.progress?.list_total || combinations.value,
    })
    const detailProgress = computed(() => status.value.detail_progress || {
      done: status.value.progress?.detail_completed || 0,
      total: status.value.progress?.detail_total || 0,
    })
    const percent = value => value.total ? Math.min(100, Math.round(value.done / value.total * 100)) : 0
    function toggleOption(field, value) {
      const list = config[field]; const index = list.indexOf(value)
      if (index >= 0) list.splice(index, 1); else list.push(value)
    }
    function payload() {
      const companies = companyTargets.value.map(value => /^https?:\/\//.test(value) ? { url: value } : { brand_id: value })
      return { keywords: keywords.value, cities: selectedCities.value, city_codes: [...config.city_codes], pages: Number(config.pages),
        filters: { salary: config.salary, experience: [...config.experience], degree: [...config.degree], scale: [...config.scale], stage: [...config.stage], industry: [...config.industry] },
        companies, company_urls: splitLines(config.companies) }
    }
    async function loadConfig() {
      try {
        const data = await api.get('/api/collect/config')
        const saved = data.config || data
        if (Array.isArray(data.city_options) && data.city_options.length) cityGroups.value = data.city_options
        const asChoices = (field, value) => {
          const values = Array.isArray(value) ? value : splitLines(value)
          return values.map(item => FILTER_LABEL_BY_CODE[field]?.[item] || item)
        }
        const savedCompanies = saved.company_urls || saved.companies || []
        Object.assign(config, {
          keywords: Array.isArray(saved.keywords) ? saved.keywords.join('\n') : (saved.keywords || config.keywords),
          city_codes: saved.city_codes || (saved.cities || []).map(city => city.city_code || city.code),
          pages: saved.pages || config.pages,
          salary: asChoices('salary', first(saved.filters?.salary, saved.salary, []))[0] || '',
          experience: asChoices('experience', first(saved.filters?.experience, saved.experience, [])),
          degree: asChoices('degree', first(saved.filters?.degree, saved.degree, [])),
          scale: asChoices('scale', first(saved.filters?.scale, saved.scale, [])),
          stage: asChoices('stage', first(saved.filters?.stage, saved.stage, [])),
          industry: asChoices('industry', first(saved.filters?.industry, saved.industry, [])),
          companies: savedCompanies.map ? savedCompanies.map(item => typeof item === 'string' ? item : item.url || item.brand_id).filter(Boolean).join('\n') : '',
        })
      } catch (_) { /* 首次启动使用内置常用城市。 */ }
    }
    async function poll() {
      try { status.value = await api.get('/api/collect/status'); runs.value = status.value.recent || runs.value }
      catch (_) {}
    }
    onMounted(async () => { await loadConfig(); try { runs.value = await api.get('/api/runs') } catch (_) {} await poll() })
    const timer = setInterval(poll, 3000); onBeforeUnmount(() => clearInterval(timer))
    async function save() {
      if (!hasTasks.value) { alert('至少填写一个关键词或一个公司 URL / brandId'); return false }
      if (keywords.value.length && !config.city_codes.length) { alert('关键词采集至少选择一个城市'); return false }
      if (combinations.value > 20) { alert(`当前共有 ${combinations.value} 个关键词×城市组合，最多允许 20 个`); return false }
      try { await api.put('/api/collect/config', payload()); return true }
      catch (e) { alert('采集配置保存失败：' + e.message); return false }
    }
    async function start() {
      if (!await save()) return
      try { const result = await api.post('/api/collect/run', { kind: 'config', config: payload() }); if (result.ok === false) alert(result.error); await poll() }
      catch (e) { alert('采集启动失败：' + e.message) }
    }
    async function cancel() { await api.post('/api/collect/cancel'); poll() }
    async function retryDetails() { try { await api.post('/api/collect/retry-details'); poll() } catch (e) { alert('重试失败：' + e.message) } }
    async function doImport(kind) {
      busy.value = kind
      try { importResult.value = await api.post(kind === 'xlsx' ? '/api/import/xlsx' : '/api/import/json', kind === 'xlsx' ? { path: xlsxPath.value } : { dir: jsonDir.value }) }
      catch (e) { alert('导入失败：' + e.message) }
      finally { busy.value = '' }
    }
    return { config, citySearch, filteredCityGroups, status, runs, busy, xlsxPath, jsonDir, importResult,
      optionGroups, optionLabels, keywords, companyTargets, hasTasks, combinations,
      listProgress, detailProgress, percent, toggleOption, save, start, cancel,
      retryDetails, doImport, fmtTime }
  },
  template: `
  <div>
    <div class="page-head"><div><h1>采集中心</h1><p>全局配置一次城市与筛选条件，任务按关键词×城市串行执行。</p></div><div class="row"><button @click="save">保存配置</button><button class="primary" :disabled="status.running || !hasTasks || combinations>20" @click="start">开始采集</button></div></div>
    <div v-if="status.risk || status.halted || status.risk_signal" class="notice bad">采集已因风控信号停止：{{status.risk_reason || status.risk_signal || status.error || '请检查采集账号'}}</div>
    <div class="collect-grid">
      <section class="config-module"><h2>搜索关键词</h2><p>每行一个关键词。</p><textarea class="short" v-model="config.keywords"></textarea><div class="module-foot"><span>{{keywords.length}} 个关键词</span></div></section>
      <section class="config-module"><h2>采集范围</h2><p>页数作用于每个关键词和城市组合。</p><label>每组页数<input type="number" v-model.number="config.pages" min="1" max="10"></label><label>薪资区间<select v-model="config.salary"><option value="">不限</option><option>3K以下</option><option>3-5K</option><option>5-10K</option><option>10-20K</option><option>20-50K</option><option>50K以上</option></select></label><div class="module-foot" :class="combinations>20?'bad':''">{{combinations}} / 20 个组合</div></section>
    </div>
    <section class="config-module wide"><div class="section-title"><div><h2>省份 / 城市</h2><p>至少选择一项，城市以 BOSS code 保存。</p></div><input v-model="citySearch" placeholder="搜索城市"></div>
      <div class="province-groups"><fieldset v-for="group in filteredCityGroups" :key="group.province"><legend>{{group.province}}</legend><label v-for="city in group.cities" :key="city.code" class="check"><input type="checkbox" :value="city.code" v-model="config.city_codes"> {{city.name}}</label></fieldset></div>
    </section>
    <section class="config-module wide"><h2>岗位筛选</h2><p>可多选；不选择表示不限。</p>
      <div class="choice-row" v-for="(options,field) in optionGroups" :key="field"><b>{{optionLabels[field]}}</b><button v-for="value in options" :key="value" :class="{selected:config[field].includes(value)}" @click="toggleOption(field,value)">{{value}}</button></div>
      <label>公司定向（每行 URL 或 brandId）<textarea class="short" v-model="config.companies"></textarea></label>
    </section>
    <section v-if="status.running || status.current || status.run_id || status.last_result" class="run-panel"><div class="section-title"><div><h2>执行状态</h2><p>{{status.current || (status.phase==='finished' ? '最近任务已结束' : '等待任务')}}</p></div><div class="row"><button v-if="!status.running && (status.retry_details_available || detailProgress.total>detailProgress.done)" @click="retryDetails">仅重试缺失 JD</button><button v-if="status.running" @click="cancel">取消采集</button></div></div>
      <div class="progress-row"><span>列表采集 {{listProgress.done}} / {{listProgress.total}}</span><div class="progress"><i :style="{width:percent(listProgress)+'%'}"></i></div></div>
      <div class="progress-row"><span>JD 详情 {{detailProgress.done}} / {{detailProgress.total}}</span><div class="progress"><i :style="{width:percent(detailProgress)+'%'}"></i></div></div>
      <pre v-if="status.log?.length" class="log">{{status.log.join('\\n')}}</pre>
    </section>
    <details class="import-panel"><summary>导入已有文件</summary><div class="form-grid two"><label>xlsx 文件路径<div class="input-action"><input v-model="xlsxPath" placeholder="~/Desktop/岗位表.xlsx"><button :disabled="!xlsxPath || busy" @click="doImport('xlsx')">导入</button></div></label><label>scraper JSON 目录<div class="input-action"><input v-model="jsonDir" placeholder="留空使用默认目录"><button :disabled="busy" @click="doImport('json')">导入</button></div></label></div><pre v-if="importResult" class="log">{{JSON.stringify(importResult,null,2)}}</pre></details>
    <section class="section-block"><div class="section-title"><div><h2>最近任务</h2><p>只有成功任务才参与来源缺失判断。</p></div></div><div class="table-wrap"><table><thead><tr><th>开始时间</th><th>类型</th><th>状态</th><th>列表 / JD</th></tr></thead><tbody><tr v-for="run in runs" :key="run.id"><td>{{fmtTime(run.started_at)}}</td><td>{{run.kind}}</td><td>{{run.status || (run.finished_at ? '已完成' : '执行中')}}</td><td>{{run.list_count ?? run.stats?.list_count ?? '—'}} / {{run.detail_count ?? run.stats?.detail_count ?? '—'}}</td></tr></tbody></table></div></section>
  </div>`,
}

// ── 原生 SVG 数据图表 ────────────────────────────────────────────
const SvgBars = {
  props: ['items', 'color'],
  setup(props) {
    const rows = computed(() => arrayOf(props.items).slice(0, 10))
    const max = computed(() => Math.max(1, ...rows.value.map(item => Number(item.count || item.value || 0))))
    return { rows, max }
  },
  // 柱状图颜色走 CSS 变量（随主题切换）；fill 属性不支持 var()，须用内联 style
  template: `<div class="bar-chart"><div v-for="item in rows" :key="item.label" class="bar-row"><span :title="item.label">{{item.label}}</span><svg viewBox="0 0 100 12" preserveAspectRatio="none" role="img" :aria-label="item.label + ' ' + (item.count ?? item.value)"><rect class="bar-bg" width="100" height="12" rx="2"></rect><rect :width="(item.count ?? item.value) > 0 ? Math.max(1,(item.count ?? item.value)/max*100) : 0" height="12" rx="2" :style="{fill: color || 'var(--accent)'}"></rect></svg><b>{{item.count ?? item.value}}</b></div><div v-if="!rows.length" class="empty">暂无数据</div></div>`,
}
const AnalyticsView = {
  components: { SvgBars },
  setup() {
    const data = ref({}), loading = ref(false), filters = reactive({ keyword: '', city_code: '', date_from: '', date_to: '' })
    async function load() {
      loading.value = true
      try { data.value = await api.get('/api/analytics?' + new URLSearchParams(filters)) }
      catch (e) { alert('分析数据加载失败：' + e.message) }
      finally { loading.value = false }
    }
    onMounted(load)
    const summary = computed(() => data.value.summary || {})
    const dist = computed(() => data.value.distributions || {})
    const meta = computed(() => data.value.meta || {})
    const charts = computed(() => [
      ['salary', '月薪分布', 'var(--chart-1)'], ['annual_salary', '年包估算', 'var(--chart-2)'],
      ['experience', '经验要求', 'var(--chart-3)'], ['degree', '学历要求', 'var(--chart-4)'],
      ['industry', '行业分布', 'var(--chart-5)'], ['scale', '公司规模', 'var(--chart-6)'],
      ['priority', '岗位 P 级', 'var(--chart-7)'], ['trend', '采集趋势', 'var(--chart-8)'],
    ])
    return { data, loading, filters, summary, dist, meta, charts, load }
  },
  template: `
  <div><div class="page-head"><div><h1>数据分析</h1><p>仅统计新采集任务中具有可靠来源关系的数据。</p></div><button class="primary" :disabled="loading" @click="load">应用筛选</button></div>
    <div class="filterbar"><select v-model="filters.keyword"><option value="">全部关键词</option><option v-for="value in meta.keywords" :value="value">{{value}}</option></select><select v-model="filters.city_code"><option value="">全部城市</option><option v-for="city in meta.cities" :value="city.code">{{city.name}}</option></select><label>起始日期<input type="date" v-model="filters.date_from"></label><label>结束日期<input type="date" v-model="filters.date_to"></label></div>
    <div class="metric-grid compact"><div class="metric"><span>可靠样本</span><strong>{{summary.jobs || 0}}</strong><small>去重岗位</small></div><div class="metric"><span>平均月薪</span><strong>{{summary.avg_salary_min ?? '—'}} - {{summary.avg_salary_max ?? '—'}}K</strong><small>按可解析薪资统计</small></div><div class="metric"><span>猎头占比</span><strong>{{summary.headhunter_ratio ?? 0}}%</strong><small>{{summary.headhunter_jobs || 0}} 个岗位</small></div><div class="metric"><span>覆盖关键词</span><strong>{{summary.keywords || 0}}</strong><small>{{summary.cities || 0}} 个城市</small></div></div>
    <div class="chart-grid"><section v-for="[key,title,color] in charts" :key="key" class="chart-panel"><h2>{{title}}</h2><SvgBars :items="key==='trend' ? data.trends : dist[key]" :color="color" /></section></div>
  </div>`,
}

// ── 页面：模拟面试（保留原能力）──────────────────────────────────
const InterviewView = {
  setup() {
    const sessions = ref([]), current = ref(null), input = ref(''), busy = ref(false), report = ref(null)
    async function load() { try { sessions.value = await api.get('/api/interviews') } catch (_) {} }
    onMounted(load)
    async function start() {
      const query = prompt('输入要模拟面试的岗位关键词：', '')
      if (!query) return
      const data = await api.get('/api/jobs?status=active&q=' + encodeURIComponent(query) + '&limit=5')
      const job = data.items?.[0]
      if (!job || !confirm(`对「${job.title} · ${job.company}」开始模拟面试？`)) return
      busy.value = true
      try { const result = await api.post('/api/interview/start', { job_key: job.job_key }); current.value = { id: result.id, title: result.title, transcript: [{ role: 'interviewer', content: result.first_question }] }; report.value = null }
      catch (e) { alert('开始失败：' + e.message) } finally { busy.value = false }
    }
    async function answer() {
      if (!input.value.trim() || !current.value) return
      const text = input.value; input.value = ''; current.value.transcript.push({ role: 'candidate', content: text }); busy.value = true
      try { const result = await api.post(`/api/interview/${current.value.id}/answer`, { text }); current.value.transcript.push({ role: 'interviewer', content: result.content, feedback: result.feedback }) }
      catch (e) { alert('提交失败：' + e.message) } finally { busy.value = false }
    }
    async function finish() { busy.value = true; try { report.value = await api.post(`/api/interview/${current.value.id}/finish`); load() } catch (e) { alert('报告生成失败：' + e.message) } finally { busy.value = false } }
    async function open(item) { const data = await api.get('/api/interview/' + item.id); current.value = { id: data.id, title: item.title, transcript: data.transcript.filter(turn => turn.role !== 'bank') }; report.value = data.report || null }
    return { sessions, current, input, busy, report, start, answer, finish, open }
  },
  template: `<div><div class="page-head"><div><h1>模拟面试</h1><p>基于当前岗位和简历进行结构化练习。</p></div><button class="primary" :disabled="busy" @click="start">新建面试</button></div><div class="interview-layout"><aside class="session-list"><button v-for="item in sessions" :key="item.id" @click="open(item)"><b>{{item.title}}</b><span>{{item.company}} · {{item.status}}</span></button><div v-if="!sessions.length" class="empty">暂无历史面试</div></aside><section><div v-if="current" class="transcript"><h2>{{current.title}}</h2><div v-for="(turn,index) in current.transcript" :key="index" class="turn" :class="turn.role"><b>{{turn.role==='candidate'?'我':'面试官'}}</b><p>{{turn.content}}</p><small v-if="turn.feedback">点评：{{turn.feedback}}</small></div><div v-if="!report" class="input-action"><input v-model="input" placeholder="输入回答" @keyup.enter="answer"><button :disabled="busy" @click="answer">回答</button><button :disabled="busy" @click="finish">结束并生成报告</button></div><div v-else class="report"><h2>面试报告 · {{report.score ?? '—'}} 分</h2><p>{{report.overall}}</p></div></div><div v-else class="empty large">选择历史记录或开始一次新面试</div></section></div></div>`,
}

// ── 页面：账号管理 ───────────────────────────────────────────────
const AccountsView = {
  setup() {
    const accounts = ref({}), dualEnabled = ref(true), modeBusy = ref(false), checking = ref('')
    const loginStates = reactive({})
    const visibleAccounts = computed(() => Object.fromEntries(Object.entries(accounts.value).filter(([, item]) => item.enabled)))
    function formatLoginState(state, cached = false) {
      const prefix = cached ? '上次检测：' : ''
      const suffix = state.checked_at ? ' · ' + fmtTime(state.checked_at) : ''
      if (state.logged_in === true) return { kind: 'ok', text: prefix + '已登录' + suffix }
      if (state.logged_in === false) return { kind: 'warn', text: prefix + '未登录' + suffix }
      return { kind: 'neutral', text: prefix + (state.hint || '无法确认登录态') + suffix }
    }
    async function load() {
      try {
        const [accountData, settings] = await Promise.all([api.get('/api/accounts'), api.get('/api/settings')])
        accounts.value = accountData; dualEnabled.value = settings.dual_account_enabled !== false
        for (const [name, item] of Object.entries(accountData)) if (item.login_state) loginStates[name] = formatLoginState(item.login_state, true)
      } catch (e) { alert('账号状态加载失败：' + e.message) }
    }
    onMounted(load)
    async function launch(name) { await api.post('/api/accounts/' + name + '/launch'); load() }
    async function loginPage(name) { const result = await api.post('/api/accounts/' + name + '/login-page'); if (!result.ok) alert(result.error); load() }
    async function loginState(name) { checking.value = name; try { loginStates[name] = formatLoginState(await api.get('/api/accounts/' + name + '/login-state')) } catch (e) { loginStates[name] = { kind: 'bad', text: '检测失败：' + e.message } } finally { checking.value = '' } }
    async function stop(name) { await api.post('/api/accounts/' + name + '/stop'); load() }
    async function changeMode(event) {
      const desired = event.target.checked
      dualEnabled.value = desired; modeBusy.value = true
      try { await api.put('/api/settings', { dual_account_enabled: desired }); await load() }
      catch (e) { dualEnabled.value = !desired; alert('账号模式保存失败：' + e.message) }
      finally { modeBusy.value = false }
    }
    return { visibleAccounts, dualEnabled, modeBusy, checking, loginStates, load, launch, loginPage, loginState, stop, changeMode }
  },
  template: `<div><div class="page-head"><div><h1>账号管理</h1><p>采集号承担读操作风险，沟通号仅用于受护栏保护的发送。</p></div><button @click="load">刷新</button></div><section class="section-block"><div class="setting-row"><div><b>双账号模式</b><p>开启后采集与沟通隔离；关闭后统一使用沟通号。</p></div><label class="switch" :class="{disabled:modeBusy}"><input type="checkbox" :checked="dualEnabled" :disabled="modeBusy" @change="changeMode"><span class="switch-track"><span class="switch-thumb"></span></span><span>{{dualEnabled?'已开启':'已关闭'}}</span></label></div></section><article v-for="(account,name) in visibleAccounts" :key="name" class="account-item"><div><h2>{{account.label}}</h2><p>{{account.description}} · CDP :{{account.port}}</p><span v-for="role in account.roles" :key="role" class="tag">{{role}}</span></div><div class="account-state"><span class="status-dot" :class="account.running?'ok':'neutral'"></span>{{account.running?'运行中':'未启动'}}</div><div class="item-actions"><button @click="launch(name)">启动</button><button @click="loginPage(name)">打开登录页</button><button :disabled="checking===name" @click="loginState(name)">{{checking===name?'检测中…':'检测登录态'}}</button><button @click="stop(name)">停止</button><span v-if="loginStates[name]" :class="loginStates[name].kind">{{loginStates[name].text}}</span></div></article></div>`,
}

// ── 页面：设置 ───────────────────────────────────────────────────
const SettingsView = {
  setup() {
    const settings = reactive({}), loaded = ref(false), saving = ref(false), testing = ref(false), result = ref(null)
    onMounted(async () => { Object.assign(settings, await api.get('/api/settings')); loaded.value = true })
    async function save() { saving.value = true; try { await api.put('/api/settings', JSON.parse(JSON.stringify(settings))); alert('设置已保存') } catch (e) { alert('保存失败：' + e.message) } finally { saving.value = false } }
    async function testLlm() { testing.value = true; result.value = null; try { const data = await api.post('/api/llm/test', { base_url: settings.llm_base_url, api_key: settings.llm_api_key, model: settings.llm_model }); result.value = { ok: true, text: `连接成功 · ${data.model} · ${data.latency_ms} ms` } } catch (e) { result.value = { ok: false, text: e.message } } finally { testing.value = false } }
    return { settings, loaded, saving, testing, result, save, testLlm }
  },
  template: `<div v-if="loaded"><div class="page-head"><div><h1>设置</h1><p>LLM 未配置时，导入、L1、采集和人工流程仍可使用。</p></div><button class="primary" :disabled="saving" @click="save">保存设置</button></div><section class="settings-section"><h2>LLM 服务</h2><div class="form-grid"><label>Base URL<input v-model="settings.llm_base_url" placeholder="https://api.deepseek.com/v1"></label><label>API Key<input v-model="settings.llm_api_key" type="password" placeholder="sk-…"></label><label>模型<input v-model="settings.llm_model" placeholder="deepseek-chat"></label></div><div class="item-actions"><button :disabled="testing" @click="testLlm">{{testing?'测试中…':'测试连通性'}}</button><span v-if="result" :class="result.ok?'ok':'bad'">{{result.text}}</span></div></section><section class="settings-section"><h2>发送护栏</h2><div class="form-grid four"><label>每日上限<input type="number" v-model.number="settings.send_daily_limit"></label><label>硬顶<input type="number" v-model.number="settings.send_daily_hard_cap"></label><label>最小间隔（秒）<input type="number" v-model.number="settings.send_gap_min_sec"></label><label>最大间隔（秒）<input type="number" v-model.number="settings.send_gap_max_sec"></label></div><p class="hint">护栏不可关闭；自动发送结果不明确时停止批次并等待人工核验。</p></section><section class="settings-section"><h2>评分与同步</h2><div class="form-grid two"><label>HR 不活跃阈值（天）<input type="number" v-model.number="settings.hr_inactive_days"></label><label>L2 默认 Top N<input type="number" v-model.number="settings.l2_top_n"></label></div></section></div>`,
}

// ── 应用组装 ─────────────────────────────────────────────────────
const App = {
  setup() {
    const view = computed(() => {
      const path = route.value
      if (path.startsWith('/dashboard')) return DashboardView
      if (path.startsWith('/jobcard')) return JobCardView
      if (path.startsWith('/analytics')) return AnalyticsView
      if (path.startsWith('/greetings')) return GreetingsView
      if (path.startsWith('/collect')) return CollectView
      if (path.startsWith('/profile')) return ProfileView
      if (path.startsWith('/interview')) return InterviewView
      if (path.startsWith('/accounts')) return AccountsView
      if (path.startsWith('/settings')) return SettingsView
      return JobsView
    })
    // 主题：默认深色（初版风格），index.html 已在首帧前设置 data-theme，这里接管切换并持久化
    const theme = ref(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
    function toggleTheme() {
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
      document.documentElement.dataset.theme = theme.value
      localStorage.setItem('theme', theme.value)
    }
    return { route, nav, view, theme, toggleTheme }
  },
  template: `<div class="layout"><aside class="side"><a class="logo" href="#/dashboard"><span class="logo-mark">BC</span><span>求职作战室<small>boss-copilot</small></span></a><nav><a v-for="[href,label] in nav" :key="href" :class="{on:route.startsWith(href)}" :href="'#'+href">{{label}}</a></nav><button class="theme-toggle" type="button" @click="toggleTheme">{{theme === 'dark' ? '☀️ 切换浅色' : '🌙 切换深色'}}</button><div class="side-foot">本地运行 · 数据不出设备</div></aside><main class="main"><component :is="view" /></main></div>`,
}

createApp(App).mount('#app')
