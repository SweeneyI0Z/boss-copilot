"""M16 真实 API 前端验收：安全渲染、采集、标签、排序与工作台。"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text()
        cls.css = (ROOT / "frontend" / "style.css").read_text()

    def test_markdown_dependencies_are_local_and_licensed(self):
        self.assertIn("/static/vendor/marked.esm.js", self.app)
        self.assertIn("/static/vendor/purify.es.mjs", self.app)
        self.assertIn('v-html="rendered"', self.app)
        self.assertIn("FORBID_TAGS", self.app)
        for name in ("marked.esm.js", "purify.es.mjs", "LICENSE.marked",
                     "LICENSE.dompurify"):
            self.assertTrue((ROOT / "frontend" / "vendor" / name).is_file())

    def test_business_data_has_no_frontend_fallback_fixtures(self):
        self.assertIn("const store = reactive", self.app)
        self.assertIn("resumes: []", self.app)
        self.assertIn("jobs: []", self.app)
        self.assertIn("runs: []", self.app)
        for symbol in ("INITIAL_RESUMES", "INITIAL_JOBS", "INITIAL_RUNS",
                       "INITIAL_RUN_JOB_KEYS", "demo-anker-fw"):
            self.assertNotIn(symbol, self.app)
        self.assertEqual(self.app.count("localStorage."), 1)
        self.assertIn("localStorage.setItem('theme'", self.app)

    def test_api_client_and_global_bootstrap_cover_read_models(self):
        self.assertIn("async function apiRequest", self.app)
        self.assertIn("await fetch(path", self.app)
        self.assertIn("class ApiError", self.app)
        self.assertIn("async function bootstrap()", self.app)
        self.assertIn("async function refreshRuntime()", self.app)
        self.assertIn("window.setInterval(refreshRuntime", self.app)
        for endpoint in ("/api/settings", "/api/resumes", "/api/dashboard",
                         "/api/runs?limit=100", "/api/collect/status",
                         "/api/collect/config", "/api/collect/options",
                         "/api/favorites/sync/status", "/api/analytics",
                         "/api/jobs", "/api/greetings", "/api/interviews",
                         "/api/accounts"):
            self.assertIn(endpoint, self.app)
        self.assertIn("部分数据加载失败", self.app)
        self.assertIn("重新加载", self.app)

    def test_collection_history_wizard_pause_and_resume(self):
        for text in ("时间 / 计划", "采集条数", "状态与进度", "采集方式",
                     "计划配置", "确认开始", "生成采集计划", "暂停", "继续", "取消"):
            self.assertIn(text, self.app)
        self.assertIn("/api/collect/pause", self.app)
        self.assertIn("/api/collect/resume", self.app)
        self.assertIn("/api/collect/cancel", self.app)
        self.assertNotIn("run.collected = Math.min", self.app)
        self.assertNotIn("await wait(", self.app)
        self.assertIn("const succeeded = status === 'completed'", self.app)
        self.assertIn("succeeded ? 100 : total ? Math.min(99", self.app)

    def test_collection_sources_can_be_enabled_and_include_favorites_sync(self):
        self.assertIn("async function toggleRunData", self.app)
        self.assertIn("/api/runs/${id}/enabled", self.app)
        self.assertIn("refreshDashboard(), refreshAnalytics(), refreshJobs()", self.app)
        self.assertIn("应用数据", self.app)
        self.assertIn("apiClient.favorites.start()", self.app)
        self.assertIn("同步 BOSS 收藏", self.app)
        self.assertIn("收藏同步采集", self.app)

    def test_excel_export_is_a_backend_download_link(self):
        self.assertIn("`/api/runs/${id}/export`", self.app)
        self.assertIn(':href="apiClient.runs.exportUrl(run.id)"', self.app)
        self.assertIn("download", self.app)
        self.assertIn('class="icon-button"', self.app)
        self.assertNotIn("SpreadsheetML", self.app)
        self.assertNotIn("URL.createObjectURL", self.app)

    def test_dashboard_freshness_guide_and_sidebar_progress_exist(self):
        self.assertIn("store.dashboard.system.collection.stale", self.app)
        self.assertIn("采集数据需要更新", self.app)
        self.assertIn("求职作战向导", self.app)
        for text in ("添加简历", "登录与收藏", "同步 BOSS 收藏岗位",
                     "设置新的采集计划", "开始采集并返回总览"):
            self.assertIn(text, self.app)
        self.assertIn("const collectionSummary = computed", self.app)
        self.assertIn('class="side-progress"', self.app)

    def test_guide_configures_llm_and_keeps_favorite_sync_optional(self):
        self.assertIn("配置并测试 LLM 服务", self.app)
        self.assertIn("function testGuideLlm", self.app)
        self.assertIn("apiClient.settings.testLlm", self.app)
        self.assertIn("apiClient.settings.save", self.app)
        self.assertIn("LLM 尚未配置", self.app)
        self.assertIn("AI 生成采集计划、岗位评分、匹配度评分、AI 招呼语和模拟面试将不可用", self.app)
        self.assertIn("可选", self.app)
        self.assertIn("跳过，下一步", self.app)
        self.assertNotIn(':disabled="!guide.synced"', self.app)

    def test_collection_city_picker_and_ai_defaults_are_conservative(self):
        self.assertIn("function matchingCities", self.app)
        self.assertIn("function appendCity", self.app)
        self.assertIn('class="city-picker"', self.app)
        self.assertIn("输入城市关键词", self.app)
        self.assertIn('class="city-tag"', self.app)
        self.assertIn("function guideGeneratePlan", self.app)
        self.assertIn("apiClient.strategy.generate", self.app)
        for default in ("salary: '不限'", "experience: '不限'", "scale: '不限'",
                        "stage: '不限'", "industry: '不限'", "companies: ''"):
            self.assertIn(default, self.app)
        self.assertIn("v-if=\"plan.mode==='manual'\" class=\"block-label\">定向公司", self.app)

    def test_job_detail_uses_direct_favorite_action(self):
        self.assertIn("job.favorite?'取消收藏':'添加收藏'", self.app)
        self.assertNotIn('href="#/jobcard" @click.stop>进入收藏工作台', self.app)
        self.assertIn("apiClient.jobs.favorite", self.app)
        self.assertIn("apiClient.jobs.detail", self.app)
        self.assertIn("apiClient.jobs.exclude", self.app)

    def test_scoring_copy_never_exposes_internal_l1_l2_names(self):
        self.assertNotIn("L1", self.app)
        self.assertNotIn("L2", self.app)
        self.assertIn("岗位评分", self.app)
        self.assertIn("匹配度评分", self.app)

    def test_all_job_columns_use_stable_bidirectional_sorting(self):
        for key in ("title", "company", "salary_max", "experience", "job_score",
                    "match_score", "priority", "active_ts"):
            self.assertIn(f"key: '{key}'", self.app)
        self.assertIn("sortState.direction === 'asc' ? 'desc' : 'asc'", self.app)
        self.assertIn("localeCompare(String(b), 'zh-CN'", self.app)
        self.assertIn("return left.index - right.index", self.app)

    def test_job_tags_follow_shared_keyword_rules(self):
        self.assertIn("function deriveJobTags(job)", self.app)
        self.assertIn("!company || company.includes('某')", self.app)
        self.assertIn("job.has_weekend ||", self.app)
        self.assertIn("job.has_benefits ||", self.app)
        self.assertIn("/周末双休|双休/", self.app)
        for keyword in ("五险一金", "年终奖", "带薪年假", "补充医疗", "股票期权",
                        "定期体检", "节日福利", "弹性工作"):
            self.assertIn(keyword, self.app)
        self.assertGreaterEqual(self.app.count("deriveJobTags("), 6)

    def test_workbench_and_full_screen_interview_are_responsive(self):
        self.assertIn('class="workbench-layout"', self.app)
        self.assertIn('class="workbench-detail"', self.app)
        self.assertIn('class="interview-overlay"', self.app)
        self.assertIn("提交并生成报告", self.app)
        self.assertIn("完成并返回工作台", self.app)
        self.assertIn("apiClient.interviews.start", self.app)
        self.assertIn("apiClient.interviews.answer", self.app)
        self.assertIn("apiClient.interviews.finish", self.app)
        self.assertIn("grid-template-columns: 355px minmax(0, 1fr)", self.css)
        self.assertIn(".workbench-layout { height: auto", self.css)

    def test_manual_workflow_status_is_shared_with_job_list(self):
        self.assertIn("function setWorkflowStage", self.app)
        self.assertIn("apiClient.jobs.workflow", self.app)
        self.assertIn("function workflowLabel", self.app)
        for text in ("已收藏", "已打招呼", "已投递", "已面试"):
            self.assertIn(text, self.app)
        self.assertIn('class="workflow-status"', self.app)
        self.assertIn('v-if="job.applied"', self.app)
        self.assertIn('v-if="job.interviewed"', self.app)

    def test_scoring_greetings_accounts_and_boss_actions_use_backend(self):
        for call in ("apiClient.jobs.scoreJob", "apiClient.jobs.scoreMatch",
                     "apiClient.greetings.generate", "apiClient.greetings.approve",
                     "apiClient.greetings.sendBatch", "apiClient.jobs.openBoss",
                     "apiClient.accounts.launch", "apiClient.accounts.login",
                     "apiClient.accounts.check", "apiClient.accounts.stop"):
            self.assertIn(call, self.app)
        self.assertNotIn("window.open", self.app)
        self.assertIn("后端批准队列", self.app)

    def test_custom_feedback_replaces_native_dialogs(self):
        self.assertIn("function showToast", self.app)
        self.assertIn("function requestConfirm", self.app)
        self.assertIn('class="confirm-dialog"', self.app)
        for native in ("alert(", "confirm(", "prompt("):
            self.assertNotIn(native, self.app)


if __name__ == "__main__":
    unittest.main()
