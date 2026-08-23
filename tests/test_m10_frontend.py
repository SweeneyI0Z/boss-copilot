"""M10-M12 前端静态回归：路由、关键交互与无构建约束。"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "frontend" / "app.js").read_text()
        cls.style = (ROOT / "frontend" / "style.css").read_text()

    def test_dashboard_is_default_and_has_quick_links(self):
        self.assertIn("|| '/dashboard'", self.source)
        self.assertIn("'/api/dashboard'", self.source)
        for text in ("岗位总量", "当前收藏", "已确认投递", "已确认招呼", "快速开始"):
            self.assertIn(text, self.source)

    def test_chat_sync_is_removed_from_frontend(self):
        self.assertNotIn("/api/chat/", self.source)
        self.assertNotIn("MessagesView", self.source)
        self.assertNotIn("'/messages'", self.source)
        self.assertNotIn("消息中心", self.source)

    def test_job_table_order_and_inline_detail(self):
        expected = ("<th>岗位</th><th>公司</th><th>薪资</th><th>经验/学历</th>"
                    "<th>岗位评分</th><th>匹配度评分</th><th>P级</th><th>活跃时间</th>")
        self.assertIn(expected, self.source)
        self.assertIn('@click="openJob(j)"', self.source)
        self.assertIn("encodeURIComponent(j.job_key)", self.source)
        self.assertIn('class="detail-row"', self.source)
        self.assertIn("职位描述（JD）", self.source)
        self.assertIn("排除岗位管理", self.source)
        self.assertIn("恢复岗位", self.source)

    def test_resume_and_manual_greeting_workflows_are_present(self):
        for endpoint in ("/api/resumes", "/default", "/confirm-manual"):
            self.assertIn(endpoint, self.source)
        for text in ("新建简历", "个人技能画像", "专业完整版", "精简版",
                     "技术聚焦版", "复制并打开 BOSS", "确认已发送"):
            self.assertIn(text, self.source)
        self.assertIn("自动发送为次级方式", self.source)

    def test_collect_modules_and_native_charts_are_present(self):
        for text in ("省份 / 城市", "至少填写一个关键词或一个公司 URL / brandId", "关键词×城市组合",
                     "列表采集", "JD 详情", "仅重试缺失 JD"):
            self.assertIn(text, self.source)
        self.assertIn("combinations.value > 20", self.source)
        self.assertIn("const hasTasks", self.source)
        self.assertIn("status.run_id", self.source)
        self.assertIn("const SvgBars", self.source)
        self.assertIn('<svg viewBox="0 0 100 12"', self.source)
        self.assertNotIn("chart.js", self.source.lower())

    def test_layout_has_responsive_constraints(self):
        self.assertIn("table-layout: fixed", self.style)
        self.assertIn("overflow-x: auto", self.style)
        self.assertIn("@media (max-width: 820px)", self.style)
        self.assertIn("@media (max-width: 560px)", self.style)


if __name__ == "__main__":
    unittest.main()
