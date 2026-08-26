"""M15 全模拟前端验收：安全渲染、导出、标签、排序与工作台。"""
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

    def test_mock_data_is_session_only_except_theme(self):
        self.assertIn("const store = reactive", self.app)
        self.assertIn("INITIAL_RESUMES", self.app)
        self.assertIn("INITIAL_JOBS", self.app)
        self.assertIn("INITIAL_RUNS", self.app)
        self.assertEqual(self.app.count("localStorage."), 1)
        self.assertIn("localStorage.setItem('theme'", self.app)

    def test_collection_history_wizard_pause_and_resume(self):
        for text in ("时间 / 计划", "采集条数", "状态与进度", "采集方式",
                     "计划配置", "确认开始", "生成采集计划", "暂停", "继续", "取消"):
            self.assertIn(text, self.app)
        self.assertIn("run.status = 'paused'", self.app)
        self.assertIn("run.status = 'running'", self.app)
        self.assertIn("run.status = 'cancelled'", self.app)
        self.assertIn("setInterval(() =>", self.app)

    def test_collection_sources_can_be_enabled_and_include_favorites_sync(self):
        self.assertIn("function jobsFromEnabledRuns", self.app)
        self.assertIn("store.runs.filter(run => run.enabled)", self.app)
        self.assertIn("function toggleRunData", self.app)
        self.assertIn("应用数据", self.app)
        self.assertIn("function addFavoriteSyncRun", self.app)
        self.assertIn("同步 BOSS 收藏", self.app)
        self.assertIn("kind: 'favorite_sync'", self.app)
        self.assertIn("收藏同步采集", self.app)

    def test_excel_export_is_spreadsheetml_and_formula_safe(self):
        self.assertIn("function exportRunAsXls", self.app)
        self.assertIn("application/vnd.ms-excel", self.app)
        self.assertIn("schemas-microsoft-com:office:spreadsheet", self.app)
        self.assertIn(".xls`", self.app)
        self.assertIn("safeSpreadsheetValue", self.app)
        self.assertIn("xmlEscape", self.app)
        self.assertIn('class="icon-button"', self.app)

    def test_dashboard_freshness_guide_and_sidebar_progress_exist(self):
        self.assertIn("3 * 24 * 60 * 60 * 1000", self.app)
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
        for default in ("salary: '不限'", "experience: '不限'", "scale: '不限'",
                        "stage: '不限'", "industry: '不限'", "companies: ''"):
            self.assertIn(default, self.app)
        self.assertIn("v-if=\"plan.mode==='manual'\" class=\"block-label\">定向公司", self.app)

    def test_job_detail_uses_direct_favorite_action(self):
        self.assertIn("job.favorite?'取消收藏':'添加收藏'", self.app)
        self.assertNotIn('href="#/jobcard" @click.stop>进入收藏工作台', self.app)

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
        self.assertIn("/周末双休|双休/", self.app)
        for keyword in ("五险一金", "年终奖", "带薪年假", "补充医疗", "股票期权",
                        "定期体检", "节日福利", "弹性工作"):
            self.assertIn(keyword, self.app)
        self.assertGreaterEqual(self.app.count("deriveJobTags("), 8)

    def test_workbench_and_full_screen_interview_are_responsive(self):
        self.assertIn('class="workbench-layout"', self.app)
        self.assertIn('class="workbench-detail"', self.app)
        self.assertIn('class="interview-overlay"', self.app)
        self.assertIn("提交并生成报告", self.app)
        self.assertIn("完成并返回工作台", self.app)
        self.assertIn("grid-template-columns: 355px minmax(0, 1fr)", self.css)
        self.assertIn(".workbench-layout { height: auto", self.css)

    def test_manual_workflow_status_is_shared_with_job_list(self):
        self.assertIn("function setWorkflowStage", self.app)
        self.assertIn("function workflowLabel", self.app)
        for text in ("已收藏", "已打招呼", "已投递", "已面试"):
            self.assertIn(text, self.app)
        self.assertIn('class="workflow-status"', self.app)
        self.assertIn('v-if="job.applied"', self.app)
        self.assertIn('v-if="job.interviewed"', self.app)

    def test_custom_feedback_replaces_native_dialogs(self):
        self.assertIn("function showToast", self.app)
        self.assertIn("function requestConfirm", self.app)
        self.assertIn('class="confirm-dialog"', self.app)
        for native in ("alert(", "confirm(", "prompt("):
            self.assertNotIn(native, self.app)


if __name__ == "__main__":
    unittest.main()
