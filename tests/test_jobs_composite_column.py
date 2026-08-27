"""岗位列表综合评分列契约：列表列 + 展开详情展示 composite（0.6×匹配 + 0.4×岗位）。

后端 /api/jobs 早已按简历返回 composite 并默认按其排序，本组契约只补前端展示。
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendCompositeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        cls.index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_jobs_table_has_composite_column_and_cell(self):
        self.assertIn("{ key: 'composite', label: '综合评分' }", self.app)
        self.assertEqual(self.app.count('data-label="综合评分"'), 1)
        # 复用 scoreLabel：评分中提示与未评分占位保持同一套语义
        self.assertIn("scoreLabel(job,'composite','jobs')", self.app)
        self.assertGreaterEqual(self.app.count("scoreLabel(job,'composite','jobs')"), 2)

    def test_detail_row_colspan_tracks_new_column_count(self):
        # 列表从 8 列变 9 列，展开行跨列数必须同步，否则布局塌陷
        self.assertIn('<td colspan="9">', self.app)
        self.assertNotIn('colspan="8"', self.app)

    def test_expanded_detail_meta_shows_composite(self):
        self.assertIn("<span>综合分</span>", self.app)

    def test_composite_sortable_like_other_score_columns(self):
        # sortValue 对未知字段直接取 job[key]，综合分因此天然可排序
        self.assertIn("return job[key]", self.app)

    def test_cache_version_is_advanced(self):
        # 静态缓存版本演进后，本用例只约束不再驻留旧值，当前值由最新前端改动钉住
        self.assertNotIn("?v=m20-guide", self.index)
        self.assertNotIn("?v=m22-composite", self.index)


if __name__ == "__main__":
    unittest.main()
