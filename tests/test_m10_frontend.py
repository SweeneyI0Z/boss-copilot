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
        self.assertIn("/open-boss", self.source)
        self.assertIn('@click.stop="openBoss(j)"', self.source)

    def test_job_title_cells_clip_long_text_and_keep_chevron_aligned(self):
        self.assertIn('class="chevron" aria-hidden="true"', self.source)
        self.assertIn('grid-template-columns: 16px minmax(0, 1fr)', self.style)
        self.assertIn('.job-title > div { min-width: 0; }', self.style)
        self.assertIn('.job-row.open .chevron::before', self.style)

    def test_jobcard_opens_boss_via_communication_chrome(self):
        # 收藏工作台「打开 BOSS」走后端 open-boss（沟通号 Chrome），不再默认浏览器直开
        self.assertNotIn(':href="job.job_link"', self.source)
        self.assertIn('@click="openBoss(job)"', self.source)
        self.assertIn("encodeURIComponent(job.job_key)}/open-boss`", self.source)

    def test_jobcard_multiselect_batch_greeting_skips_generated(self):
        # 多选 + 一键批量生成；已生成（未跳过）的按钮置灰防重复
        self.assertIn('v-model="selectedKeys"', self.source)
        self.assertIn("一键生成选中招呼语", self.source)
        self.assertIn("全选未生成", self.source)
        self.assertIn('请先勾选尚未生成招呼语的岗位', self.source)
        self.assertIn(':disabled="hasGreeting(job) || generating"', self.source)
        self.assertIn("g.status !== 'skipped'", self.source)
        self.assertIn("g.resume_id === Number(resumeId.value)", self.source)
        self.assertIn("招呼语已生成", self.source)

    def test_resume_and_manual_greeting_workflows_are_present(self):
        for endpoint in ("/api/resumes", "/default", "/confirm-manual"):
            self.assertIn(endpoint, self.source)
        for text in ("新建简历", "个人技能画像", "专业完整版", "精简版",
                     "技术聚焦版", "复制并打开 BOSS", "确认已发送"):
            self.assertIn(text, self.source)
        self.assertIn("自动发送为次级方式", self.source)

    def test_greeting_copy_opens_communication_chrome(self):
        # 招呼语「复制并打开 BOSS」走后端 open-boss（沟通号 Chrome）；
        # /api/greetings 列表不含 job_link，旧 window.open 写法永远不触发
        self.assertIn("encodeURIComponent(g.job_key)}/open-boss`", self.source)
        self.assertNotIn("window.open(g.job_link", self.source)
        self.assertIn("if (await openBoss(g)) manualOpened[g.id] = true", self.source)

    def test_collect_modules_and_native_charts_are_present(self):
        for text in ("省份 / 城市", "至少填写一个关键词或一个公司 URL / brandId", "关键词×城市组合",
                     "列表采集", "JD 详情", "仅重试缺失 JD"):
            self.assertIn(text, self.source)
        self.assertIn("combinations.value > 20", self.source)
        self.assertIn("const hasTasks", self.source)
        self.assertIn("status.run_id", self.source)
        self.assertIn("const SvgBars", self.source)
        self.assertIn('<svg viewBox="0 0 100 12"', self.source)
        self.assertIn("默认分析全部当前岗位", self.source)
        self.assertIn("历史导入或收藏岗位", self.source)
        self.assertIn("分析数据加载失败", self.source)
        self.assertNotIn("chart.js", self.source.lower())

    def test_layout_has_responsive_constraints(self):
        self.assertIn("table-layout: fixed", self.style)
        self.assertIn("overflow-x: auto", self.style)
        self.assertIn("@media (max-width: 820px)", self.style)
        self.assertIn("@media (max-width: 560px)", self.style)


if __name__ == "__main__":
    unittest.main()
