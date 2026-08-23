"""M1 测试：薪资解析、job_key、upsert 去重与评分保护。

跑法：.venv/bin/python -m unittest discover tests -v
回归要求：任何 backend/ 改动后必须全量通过。
"""
import os
import tempfile
import unittest
from pathlib import Path

# 测试库隔离到临时目录，绝不碰 ~/.boss-copilot 真实数据
_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-test-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import importer  # noqa: E402
from backend.db import get_db, init_db  # noqa: E402


class ParseSalaryTests(unittest.TestCase):
    def test_monthly_k(self):
        self.assertEqual(importer.parse_salary("30-60K"), (30.0, 60.0, 12.0))

    def test_with_months(self):
        self.assertEqual(importer.parse_salary("25-40K·14薪"), (25.0, 40.0, 14.0))

    def test_daily(self):
        lo, hi, mo = importer.parse_salary("300-500元/天")
        self.assertAlmostEqual(lo, 300 * 21.75 / 1000, places=1)
        self.assertAlmostEqual(hi, 500 * 21.75 / 1000, places=1)
        self.assertEqual(mo, 12.0)

    def test_invalid_and_empty(self):
        self.assertEqual(importer.parse_salary(""), (None, None, None))
        self.assertEqual(importer.parse_salary("面议"), (None, None, None))
        self.assertEqual(importer.parse_salary(None), (None, None, None))


class JobKeyTests(unittest.TestCase):
    def test_from_detail_link(self):
        key = importer.job_key_from(
            "https://www.zhipin.com/job_detail/abc123.html?lid=x", "t", "c", "s")
        self.assertEqual(key, "abc123")

    def test_content_hash_stable(self):
        a = importer.job_key_from("", "岗位", "公司", "20-30K")
        b = importer.job_key_from("", "岗位", "公司", "20-30K")
        c = importer.job_key_from("", "岗位2", "公司", "20-30K")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class UpsertJobTests(unittest.TestCase):
    """核心语义：重复导入刷新可变字段；已有评分不被旧导入覆盖。"""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        conn = get_db()
        conn.execute("DELETE FROM job_details")
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def _job(self, **over):
        base = {
            "job_key": "k1", "title": "AI应用工程师", "company": "某公司",
            "salary": "20-35K", "hr_active": "刚刚活跃", "source": "import",
        }
        base.update(over)
        return base

    def test_insert_then_refresh(self):
        _, created1 = importer.upsert_job(self._job())
        self.assertTrue(created1)
        _, created2 = importer.upsert_job(
            self._job(hr_active="3日内活跃", salary="22-38K"))
        self.assertFalse(created2)
        row = get_db().execute("SELECT * FROM jobs WHERE job_key='k1'").fetchone()
        self.assertEqual(row["hr_active"], "3日内活跃")
        self.assertEqual(row["salary"], "22-38K")

    def test_imported_scores_not_overwritten(self):
        importer.upsert_job(self._job(
            l1_score=70.0, match_rough=80.0, composite_rough=74.0,
            job_score=73.5, match_score=84.0, composite=79.8, priority="P0",
            l2_source="imported", l2_detail={"summary": "基线"}))
        # 二次导入带不同分数 → 不覆盖已有评分
        importer.upsert_job(self._job(
            l1_score=10.0, composite=11.0, priority="P3", l2_detail={"summary": "劣化"}))
        row = get_db().execute("SELECT * FROM jobs WHERE job_key='k1'").fetchone()
        self.assertEqual(row["l1_score"], 70.0)
        self.assertEqual(row["composite"], 79.8)
        self.assertEqual(row["priority"], "P0")

    def test_jd_upsert(self):
        importer.upsert_job(self._job(jd="第一版JD"))
        importer.upsert_job(self._job(jd="第二版JD"))
        row = get_db().execute(
            "SELECT jd FROM job_details WHERE job_key='k1'").fetchone()
        self.assertEqual(row["jd"], "第二版JD")


class SheetRowsTests(unittest.TestCase):
    """表头定位容错：标题行偏移也能找到真正表头。"""

    def test_header_offset(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "岗位总览"
        ws.append(["", "标题行干扰", None])
        ws.append(["", None, "第二行干扰"])
        ws.append(["", "序号", "岗位名称", "公司"])
        ws.append(["", 1, "岗位A", "公司A"])
        rows = importer._sheet_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["公司"], "公司A")


if __name__ == "__main__":
    unittest.main()
