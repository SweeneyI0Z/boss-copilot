"""M14 主题切换静态回归：默认初版暗色风格，浅色可切换且持久化。"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ThemeToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text()
        cls.css = (ROOT / "frontend" / "style.css").read_text()
        cls.html = (ROOT / "frontend" / "index.html").read_text()

    def test_dark_is_default_with_initial_palette(self):
        # 初版暗色令牌必须落在默认 :root（而非只出现在浅色覆盖块中）
        root_block = self.css.split(":root[data-theme")[0]
        for token in ("#0f1115", "#171a21", "#1e222b", "#4f8cff", "#e6e9ef", "#34c98e"):
            self.assertIn(token, root_block)
        self.assertIn("color-scheme: dark", root_block)

    def test_light_theme_override_exists(self):
        self.assertIn(":root[data-theme=\"light\"]", self.css)
        light_block = self.css.split(":root[data-theme=\"light\"]")[1].split("}")[0]
        self.assertIn("color-scheme: light", light_block)
        self.assertIn("--bg: #f4f6f3", light_block)
        self.assertIn("--accent: #177a58", light_block)

    def test_theme_toggle_persists_and_updates_document(self):
        self.assertIn("function toggleTheme", self.app)
        self.assertIn("document.documentElement.dataset.theme", self.app)
        self.assertIn("localStorage.setItem('theme'", self.app)
        # index.html 首帧前恢复主题，避免闪色
        self.assertIn("localStorage.getItem('theme') || 'dark'", self.html)

    def test_chart_colors_are_theme_aware(self):
        # 图表颜色不再硬编码浅色 hex，改走 CSS 变量随主题切换
        self.assertIn("class=\"bar-bg\"", self.app)
        self.assertIn("'var(--chart-1)'", self.app)
        self.assertIn("'var(--chart-8)'", self.app)
        self.assertNotIn("'#177a58'", self.app)
        self.assertIn("fill: var(--bar-bg)", self.css)
        self.assertIn("--chart-1: #4f8cff", self.css)


if __name__ == "__main__":
    unittest.main()
