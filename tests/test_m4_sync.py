"""M4 测试：HR 活跃度判定、同词下架 diff、origin 标签、重评报告。"""
import os
import tempfile
import unittest
from pathlib import Path

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m4-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import sync  # noqa: E402
from backend.db import get_db, init_db  # noqa: E402


def _insert(key, status="active", hr="", origin="", priority=None,
            composite=None, composite_rough=None, title="T", company="C"):
    conn = get_db()
    conn.execute(
        "INSERT INTO jobs(job_key, title, company, status, hr_active, origin_query,"
        " priority, composite, composite_rough, first_seen_at, last_seen_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET"
        " status=excluded.status, hr_active=excluded.hr_active,"
        " origin_query=excluded.origin_query, priority=excluded.priority,"
        " composite=excluded.composite, composite_rough=excluded.composite_rough",
        (key, title, company, status, hr, origin, priority, composite,
         composite_rough, "t", "t"))
    conn.commit()


class HrStaleTests(unittest.TestCase):
    def test_fresh_levels(self):
        for ok in ("刚刚活跃", "在线", "今日活跃", "3日内活跃", "本周活跃", "2周内活跃"):
            self.assertFalse(sync.hr_is_stale(ok), ok)

    def test_stale_levels(self):
        for bad in ("月前活跃", "半年前活跃", "一年前活跃", "2月前活跃"):
            self.assertTrue(sync.hr_is_stale(bad), bad)

    def test_empty_not_stale(self):
        self.assertFalse(sync.hr_is_stale(""))
        self.assertFalse(sync.hr_is_stale("未知"))


class ApplyHrInactiveTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def test_marks_only_stale_active(self):
        _insert("a", hr="刚刚活跃")
        _insert("b", hr="半年前活跃")
        _insert("c", status="delisted", hr="半年前活跃")
        n = sync.apply_hr_inactive()
        self.assertEqual(n, 1)
        conn = get_db()
        self.assertEqual(conn.execute(
            "SELECT status FROM jobs WHERE job_key='b'").fetchone()["status"],
            "hr_inactive")
        self.assertEqual(conn.execute(
            "SELECT status FROM jobs WHERE job_key='c'").fetchone()["status"],
            "delisted")   # 已下架不被复活


class DelistDiffTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def test_same_query_missing_delisted(self):
        _insert("j1", origin="kw:嵌入式", title="A")
        _insert("j2", origin="kw:嵌入式", title="B")
        _insert("j3", origin="kw:AI应用", title="C")
        gone = sync.diff_delisted("kw:嵌入式", {"j1"})   # 本轮只出现 j1
        self.assertEqual(gone, ["j2"])
        conn = get_db()
        # j2 下架；j3 属于别的搜索词，不受影响
        self.assertEqual(conn.execute(
            "SELECT status FROM jobs WHERE job_key='j2'").fetchone()["status"], "delisted")
        self.assertEqual(conn.execute(
            "SELECT status FROM jobs WHERE job_key='j3'").fetchone()["status"], "active")

    def test_no_origin_never_delisted(self):
        _insert("x1", origin="")   # 历史导入无来源
        self.assertEqual(sync.diff_delisted("kw:嵌入式", set()), [])


class RescoreReportTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def test_detects_priority_change(self):
        # L2 是 P1，但当前 L1 粗分已达 P0 档 → 进重评清单
        _insert("p", priority="P1", composite=70.0, composite_rough=78.0)
        # P 级一致 → 不进清单
        _insert("q", priority="P2", composite=60.0, composite_rough=58.0)
        report = sync.rescore_report()
        keys = [c["job_key"] for c in report["changed"]]
        self.assertIn("p", keys)
        self.assertNotIn("q", keys)
        self.assertEqual(report["total_scored"], 2)


class OriginTagTests(unittest.TestCase):
    def test_tag_does_not_overwrite(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM jobs")
        conn.commit()
        _insert("t1", origin="")
        _insert("t2", origin="kw:旧词")
        sync.tag_origin_query(["t1", "t2"], "kw:新词")
        rows = {r["job_key"]: r["origin_query"] for r in
                conn.execute("SELECT job_key, origin_query FROM jobs")}
        self.assertEqual(rows["t1"], "kw:新词")
        self.assertEqual(rows["t2"], "kw:旧词")   # 已有来源不覆盖


class RescoreKeepImportedTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM job_details")
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def test_rescore_keeps_imported_baseline(self):
        from backend.scoring import l1
        from backend import importer
        importer.upsert_job({"job_key": "imp", "title": "T", "company": "C",
                             "salary": "20-30K", "salary_min": 20, "salary_max": 30,
                             "source": "import", "l1_score": 69.5, "match_rough": 79.2,
                             "composite_rough": 75.3,
                             "l1_detail": {"source": "imported"}})
        importer.upsert_job({"job_key": "eng", "title": "T2", "company": "C2",
                             "salary": "20-30K", "salary_min": 20, "salary_max": 30,
                             "source": "search", "l1_score": 50.0,
                             "l1_detail": {"engine": "l1"}})
        stats = l1.run_l1(force=True, keep_imported=True)
        self.assertEqual(stats["scored"], 1)      # 只重算引擎评的
        self.assertEqual(stats["skipped"], 1)     # 导入基线保留
        conn = get_db()
        self.assertEqual(conn.execute(
            "SELECT l1_score FROM jobs WHERE job_key='imp'").fetchone()["l1_score"],
            69.5)


if __name__ == "__main__":
    unittest.main()
