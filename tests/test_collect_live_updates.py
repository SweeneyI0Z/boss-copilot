"""采集实时更新信号测试：增量落库必须驱动 data_version 递增并透出到状态。

背景：采集运行中，新岗位/JD 每次增量入库都应递增 data_version 并在
/api/collect/status 透出——前端轮询检测到变化后按最小间隔刷新
总览看板、数据分析与岗位列表，用户不再需要手动切换页面才能看到新数据。

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

from backend import collector  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402


class CollectDataVersionTests(unittest.TestCase):
    def setUp(self):
        init_db()
        # job_run_items 对 collect_runs 与 jobs 均有外键约束，先建最小记录
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO collect_runs(id,kind,started_at,status) "
            "VALUES(1,'search',?,'running')", (now_iso(),))
        for key in ("job-a", "job-b"):
            conn.execute(
                "INSERT OR IGNORE INTO jobs(job_key,title,company,first_seen_at,last_seen_at) "
                "VALUES(?,'岗位','公司',?,?)", (key, now_iso(), now_iso()))
        conn.commit()

    def test_status_exposes_data_version(self):
        status = collector.status()
        self.assertIn("data_version", status)
        self.assertIsInstance(status["data_version"], int)

    def test_attaching_jobs_bumps_data_version(self):
        before = collector.status()["data_version"]
        collector._attach_run_jobs(1, {"job-a", "job-b"}, "collection_snapshot")
        self.assertEqual(collector.status()["data_version"], before + 1)

    def test_empty_attach_does_not_bump(self):
        """无岗位落库不算数据变更，不触发前端刷新信号。"""
        before = collector.status()["data_version"]
        collector._attach_run_jobs(1, set(), "collection_snapshot")
        collector._attach_run_jobs(1, {""}, "collection_snapshot")
        self.assertEqual(collector.status()["data_version"], before)

    def test_repeated_attach_keeps_bumping_for_live_updates(self):
        """同一岗位重复导入（JD 补齐等）仍递增：信号语义是“有数据落库”。"""
        collector._attach_run_jobs(1, {"job-a"}, "collection")
        first = collector.status()["data_version"]
        collector._attach_run_jobs(1, {"job-a"}, "collection_snapshot")
        self.assertEqual(collector.status()["data_version"], first + 1)

    def test_explicit_bump_increments_monotonically(self):
        before = collector.status()["data_version"]
        collector._bump_data_version()
        collector._bump_data_version()
        self.assertEqual(collector.status()["data_version"], before + 2)


if __name__ == "__main__":
    unittest.main()
