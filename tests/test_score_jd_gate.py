"""无 JD 岗位不参与岗位评分/匹配度评分：入队过滤与提示契约。

无 JD 的岗位缺少精评素材，批量评分时由后端直接剔除（skipped_no_jd 单独回报），
force 也绕不过；工作台分析/招呼语闸门在后续「工作台生成治理」中另行扩展。
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-score-jd-gate-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

ROOT = Path(__file__).resolve().parents[1]

from backend import config, llm, main, resumes  # noqa: E402
from backend.ai_tasks import AITaskScheduler  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402
from backend.scoring import l2  # noqa: E402


class NoJdGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-score-jd-case-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()
        self.resume = resumes.update_resume(
            resumes.get_default_resume()["id"], resume_text="完整项目简历" * 80)
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True)
        main._ai_scheduler = None
        with main._score_flights_lock:
            main._score_flights.clear()
        with l2._INFLIGHT_LOCK:
            l2._INFLIGHT.clear()

    def tearDown(self):
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True, timeout=1)
        main._ai_scheduler = None
        self.tmp.cleanup()

    def add_job(self, job_key: str):
        ts = now_iso()
        get_db().execute(
            "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,'active',?,?)",
            (job_key, f"岗位 {job_key}", "示例科技", ts, ts),
        )
        get_db().commit()

    def set_jd(self, job_key: str, jd: str):
        get_db().execute(
            "INSERT INTO job_details(job_key,jd,fetched_at) VALUES(?,?,?)",
            (job_key, jd, now_iso()))
        get_db().commit()

    def save_complete(self, job_key: str):
        resumes.save_job_score(
            job_key, self.resume["id"], self.resume["revision"],
            job_score=78, match_score=82, composite=80.4, priority="P0",
            l2_detail={"summary": "已完成"}, l2_source="llm")

    def enqueue(self, job_keys, **extra):
        body = {"kind": "score", "job_keys": job_keys,
                "resume_id": self.resume["id"], **extra}
        return main.ai_tasks_enqueue("jobs", body)

    def test_mixed_batch_filters_jobs_without_usable_jd(self):
        # 已评分但无 JD 的岗位记入「已有评分」跳过，不与缺 JD 重复计数
        for key in ("no-jd-row", "empty-jd", "ws-jd", "real-jd", "scored-nojd"):
            self.add_job(key)
        self.set_jd("empty-jd", "")
        self.set_jd("ws-jd", "  \n ")
        self.set_jd("real-jd", "负责核心链路开发，要求熟悉 Python")
        self.save_complete("scored-nojd")

        calls = []
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: calls.append(task) or {"ok": True},
            max_concurrency=1)
        with patch.object(llm, "configured", return_value=True):
            result = self.enqueue(
                ["no-jd-row", "empty-jd", "ws-jd", "real-jd", "scored-nojd"])

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["tasks"][0]["job_key"], "real-jd")
        self.assertEqual(result["skipped_no_jd"], ["no-jd-row", "empty-jd", "ws-jd"])
        self.assertEqual(result["skipped_no_jd_count"], 3)
        self.assertEqual(result["skipped_existing"], ["scored-nojd"])
        self.assertTrue(main._ai_scheduler.wait_for_idle(timeout=2))
        self.assertEqual([task["job_key"] for task in calls], ["real-jd"])

    def test_all_missing_jd_is_noop_even_without_llm_configured(self):
        for key in ("a", "b"):
            self.add_job(key)

        calls = []
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: calls.append(task) or {"ok": True})
        with patch.object(llm, "configured", return_value=False):
            result = self.enqueue(["a", "b"])

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_no_jd_count"], 2)
        self.assertEqual(calls, [])

    def test_force_skips_missing_jd_too(self):
        self.add_job("done")
        self.add_job("no-jd")
        self.set_jd("done", "真实职位描述")
        self.save_complete("done")
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: {"ok": True}, max_concurrency=1)

        with patch.object(llm, "configured", return_value=True):
            forced = self.enqueue(["done", "no-jd"], force=True)

        self.assertEqual(forced["skipped_existing_count"], 0)
        self.assertEqual(forced["skipped_no_jd"], ["no-jd"])
        self.assertEqual([task["job_key"] for task in forced["tasks"]], ["done"])
        self.assertTrue(forced["tasks"][0]["payload"]["force"])

    def test_analysis_kind_gated_too(self):
        # 工作台分析与招呼语也受本闸门约束（早先「分析不受影响」的断言随闸门扩展作废）
        self.add_job("favorite-njd")
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: {"ok": True}, max_concurrency=1)

        result = main.ai_tasks_enqueue("workbench", {
            "kind": "analysis", "job_keys": ["favorite-njd"],
            "resume_id": self.resume["id"],
        })

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_no_jd"], ["favorite-njd"])


class FrontendJdGateContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        cls.index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_run_score_reports_no_jd_skips_and_keeps_legacy_copy(self):
        self.assertIn("result.skipped_no_jd_count", self.app)
        self.assertIn("`所选 ${noJd} 个岗位均缺少职位描述（JD），补齐后才能参与评分`",
                      self.app)
        # M18 既有提示文案必须保留
        self.assertIn("已有评分，无需重复生成", self.app)

    def test_cache_version_does_not_hold_stale_values(self):
        # 静态缓存版本演进后，本用例只约束不再驻留旧值，当前值由最新前端改动钉住
        self.assertNotIn("?v=m22-composite", self.index)
        self.assertNotIn("?v=m23-jd-gate", self.index)


if __name__ == "__main__":
    unittest.main()
