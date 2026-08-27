"""M20 前端新人引导教程（使用指南页 + 首访弹窗）与 README 注意事项契约。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
        cls.index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_cache_version_bumped_to_m20(self):
        self.assertIn("style.css?v=m20-guide", self.index)
        self.assertIn("app.js?v=m20-guide", self.index)
        self.assertNotIn("?v=m19-eta", self.index)

    def test_guide_route_and_nav_entry_registered(self):
        self.assertIn("['/guide', '使用指南']", self.app)
        self.assertIn("'/guide': GuideView", self.app)
        # 总览看板页头：「使用指南」按钮位于「向导」按钮左侧
        self.assertIn(
            '<a class="button" href="#/guide">使用指南</a><button @click="openGuide">向导</button>',
            self.app)

    def test_wizard_cross_view_trigger_hook(self):
        # 跨视图打开求职作战向导：模块级共享 ref + 组件挂载时消费挂起意图，
        # 避免从其他页面跳转后 watch 错过自增信号
        self.assertIn("const wizardRequest = ref(0)", self.app)
        self.assertIn("function requestWizardOpen()", self.app)
        self.assertIn("watch(wizardRequest, consumeWizardRequest)", self.app)

    def test_guide_page_covers_all_required_notes(self):
        self.assertIn("const GuideView = {", self.app)
        self.assertIn("<h2>注意事项</h2>", self.app)
        self.assertIn("<h2>推荐使用顺序</h2>", self.app)
        for note_title in (
            "非官方工具，请先评估账号风控风险",
            "仅支持 Chrome 浏览器",
            "采集号与投递号建议分开两个账号",
            "首次采集耗时较久，建议一次采齐 JD",
            "提前准备 API Key，并留意 token 消耗",
            "简历直接粘贴即可，Markdown 或纯文本都能识别",
            "推荐与 BOSS 直聘原版分工搭配",
        ):
            self.assertIn(note_title, self.app)
        # 使用顺序关键节点：右上角确认简历 → 批量评分 → 收藏工作台作战
        self.assertIn("点「匹配度评分」「岗位评分」", self.app)
        self.assertIn("竞争力分析和招呼语", self.app)
        # 概不负责声明必须出现
        self.assertIn("本项目概不负责", self.app)

    def test_launch_notice_dialog_requires_acknowledge(self):
        # 每次启动默认弹出；仅勾选「不再显示」后才持久化；关闭必须点「我已知晓」
        self.assertIn("'bc-onboarding-v1'", self.app)
        self.assertIn("function markOnboardingDone()", self.app)
        self.assertIn("function tryStartOnboarding()", self.app)
        self.assertIn("if (onboarding.suppress) markOnboardingDone()", self.app)
        self.assertIn('aria-label="启动须知"', self.app)
        self.assertIn('v-model="onboarding.suppress"', self.app)
        self.assertIn("下次启动不再显示", self.app)
        self.assertIn(">我已知晓</button>", self.app)
        self.assertIn("dismissOnboarding('wizard')", self.app)
        self.assertIn("dismissOnboarding('guide')", self.app)

    def test_onboarding_and_guide_styles_defined(self):
        for selector in (
            ".guide-note.tone-risk", ".guide-note.tone-warn", ".guide-note.tone-ok",
            ".guide-doc-foot", ".guide-flow li::before",
            ".onboarding-dialog", ".onboarding-check",
            ".onboarding-list", ".onboarding-actions", ".onboarding-risk",
        ):
            self.assertIn(selector, self.css)

    def test_readme_documents_notes_section(self):
        self.assertIn("## 注意事项", self.readme)
        self.assertIn("非官方程序", self.readme)
        self.assertIn("风控", self.readme)
        self.assertIn("概不负责", self.readme)
        self.assertIn("Chrome", self.readme)
        self.assertIn("Markdown", self.readme)
        self.assertIn("API Key", self.readme)
        self.assertIn("token", self.readme.lower())
        self.assertIn("消息机制", self.readme)


if __name__ == "__main__":
    unittest.main()
