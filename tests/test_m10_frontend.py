"""M10/M15 前端静态回归：信息架构、关键交互与无构建约束。"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "frontend" / "app.js").read_text()
        cls.style = (ROOT / "frontend" / "style.css").read_text()

    def test_dashboard_is_default_and_navigation_is_reordered(self):
        self.assertIn("|| '/dashboard'", self.source)
        labels = ("总览看板", "简历档案", "采集中心", "数据分析", "岗位列表",
                  "收藏工作台", "账号管理", "设置")
        offsets = [self.source.index(f"'{label}'", self.source.index("const nav = ["))
                   for label in labels]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn("const ROUTE_ALIASES", self.source)
        self.assertIn("'/greetings': '/jobcard'", self.source)
        self.assertIn("'/interview': '/jobcard'", self.source)

    def test_demo_frontend_never_calls_real_business_api(self):
        self.assertNotIn("fetch(", self.source)
        self.assertNotIn("/api/", self.source)
        self.assertNotIn("window.open", self.source)
        self.assertNotIn("alert(", self.source)
        self.assertIn("演示数据", self.source)
        self.assertIn("不连接真实账号", self.source)

    def test_chat_and_standalone_greeting_views_are_removed(self):
        for symbol in ("MessagesView", "GreetingsView", "InterviewView"):
            self.assertNotIn(symbol, self.source)
        self.assertNotIn("'/messages'", self.source)
        self.assertNotIn("消息中心", self.source)

    def test_job_table_has_click_sort_and_inline_detail(self):
        self.assertIn('class="sort-button"', self.source)
        self.assertIn(':aria-sort="ariaSort(column.key)"', self.source)
        self.assertIn("function setSort(key)", self.source)
        self.assertIn('@click="toggleExpanded(job)"', self.source)
        self.assertIn('class="detail-row"', self.source)
        self.assertIn("职位描述（JD）", self.source)

    def test_job_title_cells_clip_long_text_and_keep_chevron_aligned(self):
        self.assertIn('class="chevron"', self.source)
        self.assertIn('grid-template-columns: 16px minmax(0, 1fr)', self.style)
        self.assertIn('.job-title > div { min-width: 0; }', self.style)
        self.assertIn('.job-row.open .chevron::before', self.style)

    def test_workbench_contains_batch_and_single_job_workflows(self):
        for text in ("全选当前岗位", "生成分析", "生成招呼语", "自动打招呼",
                     "打开 BOSS", "模拟面试", "AI 应聘建议"):
            self.assertIn(text, self.source)
        self.assertIn('v-model="selectedKeys"', self.source)
        self.assertIn("async function executeBatch", self.source)
        self.assertIn("本次仅演示护栏确认", self.source)

    def test_resume_and_collect_workflows_are_present(self):
        for text in ("Markdown 预览", "档案名称", "简历正文", "新建采集计划",
                     "AI 自动生成", "手动选择", "采集完整JD（推荐）", "导出 Excel 兼容文件"):
            self.assertIn(text, self.source)
        self.assertIn("DOMPurify.sanitize", self.source)
        self.assertIn("marked.parse", self.source)

    def test_native_charts_and_responsive_constraints_are_present(self):
        self.assertIn("const SvgBars", self.source)
        self.assertIn('<svg viewBox="0 0 100 12"', self.source)
        self.assertNotIn("chart.js", self.source.lower())
        self.assertIn("table-layout: fixed", self.style)
        self.assertIn("overflow-x: auto", self.style)
        self.assertIn("@media (max-width: 820px)", self.style)
        self.assertIn("@media (max-width: 560px)", self.style)


if __name__ == "__main__":
    unittest.main()
