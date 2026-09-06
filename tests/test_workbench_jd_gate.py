"""无 JD 岗位不参与收藏工作台分析与招呼语：队列闸门、自动重算与提示契约。

与岗位评分同规则：缺素材不入队不耗 token，force 绕不过；
已有可用结果优先记入 skipped_existing；简历保存触发的自动分析同样过滤；
直连招呼语接口对「新生成」拒单、对既有结果照常复用。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-workbench-jd-gate-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

ROOT = Path(__file__).resolve().parents[1]

from backend import config, greeting, llm, main, resumes  # noqa: E402
from backend.ai_tasks import AITaskScheduler  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402


class NoJdWorkbenchGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-workbench-jd-case-")
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
        self.calls = []
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: self.calls.append(task) or {"ok": True},
            max_concurrency=1)

    def tearDown(self):
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True, timeout=1)
        main._ai_scheduler = None
        self.tmp.cleanup()

    def add_job(self, job_key: str, *, jd: str = None, favorite: bool = False):
        ts = now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO jobs(job_key,title,company,status,favorite_at,"
            "first_seen_at,last_seen_at) VALUES(?,?,'示例科技','active',?,?,?)",
            (job_key, f"岗位 {job_key}", ts if favorite else None, ts, ts))
        if jd is not None:
            conn.execute(
                "INSERT INTO job_details(job_key,jd,fetched_at) VALUES(?,?,?)",
                (job_key, jd, ts))
        conn.commit()

    def add_greeting(self, job_key: str, variants=None):
        """与 M19 同款夹具：当前修订可用招呼语（draft 即视为已完成跳过项）。"""
        ts = now_iso()
        get_db().execute(
            "INSERT INTO greetings(job_key,variants,chosen,status,created_at,"
            "updated_at,resume_id,resume_revision) VALUES(?,?,?,'draft',?,?,?,?)",
            (job_key, json.dumps(variants or [], ensure_ascii=False),
             "", ts, ts, self.resume["id"], self.resume["revision"]))
        get_db().commit()

    def enqueue_workbench(self, kind: str, job_keys, **extra):
        body = {"kind": kind, "job_keys": job_keys,
                "resume_id": self.resume["id"], **extra}
        return main.ai_tasks_enqueue("workbench", body)

    def test_analysis_batch_filters_jobs_without_usable_jd(self):
        self.add_job("an-real", jd="负责核心链路开发")
        self.add_job("an-no-row")
        self.add_job("an-blank", jd="")

        with patch.object(llm, "configured", return_value=True):
            result = self.enqueue_workbench(
                "analysis", ["an-no-row", "an-blank", "an-real"])

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["tasks"][0]["job_key"], "an-real")
        self.assertEqual(result["skipped_no_jd"], ["an-no-row", "an-blank"])
        self.assertEqual(result["skipped_existing"], [])

    def test_greeting_batch_puts_existing_result_before_jd_gate(self):
        # 已有可用招呼语即使缺 JD 也记入 skipped_existing，不重复计数
        self.add_job("gr-done")
        self.add_greeting("gr-done", variants=["专业版", "精简版", "技术版"])
        self.add_job("gr-njd")
        self.add_job("gr-ok", jd="gr-ok 职位描述")

        result = self.enqueue_workbench("greeting", ["gr-done", "gr-njd", "gr-ok"])

        self.assertEqual(result["added"], 1)
        self.assertEqual([task["job_key"] for task in result["tasks"]], ["gr-ok"])
        self.assertEqual(result["skipped_existing"], ["gr-done"])
        self.assertEqual(result["skipped_no_jd"], ["gr-njd"])

    def test_force_bypasses_existing_but_never_missing_jd(self):
        self.add_job("done-jd", jd="真实职位描述")
        self.add_greeting("done-jd", variants=["专业版", "精简版", "技术版"])
        self.add_job("no-jd-force")

        forced = self.enqueue_workbench(
            "greeting", ["done-jd", "no-jd-force"], force=True)

        self.assertEqual(forced["skipped_existing_count"], 0)
        self.assertEqual(forced["skipped_no_jd"], ["no-jd-force"])
        self.assertEqual(
            [task["job_key"] for task in forced["tasks"]], ["done-jd"])

    def test_all_missing_jd_is_noop_even_without_llm_configured(self):
        self.add_job("x1")
        self.add_job("x2", jd="   ")

        with patch.object(llm, "configured", return_value=False):
            result = self.enqueue_workbench("analysis", ["x1", "x2"])

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_no_jd"], ["x1", "x2"])
        self.assertEqual(self.calls, [])

    def test_rescore_resume_skips_no_jd_favorites_for_auto_analysis(self):
        self.add_job("fav-with-jd", jd="收藏且有 JD", favorite=True)
        self.add_job("fav-no-jd", favorite=True)
        self.add_job("plain-with-jd", jd="未收藏不参与", favorite=False)

        with patch.object(llm, "configured", return_value=True):
            main._rescore_resume(self.resume)

        tasks = main.ai_tasks_status("workbench")["tasks"]
        self.assertEqual({task["job_key"] for task in tasks}, {"fav-with-jd"})

    def test_direct_greeting_endpoint_blocks_new_generation_but_reuses(self):
        generate = Mock(return_value={"reused_existing": True})
        self.add_job("fresh-njd")
        self.add_job("direct2")
        self.add_greeting("direct2", variants=["专业版", "精简版", "技术版"])

        with patch.object(greeting, "generate", generate):
            with self.assertRaises(HTTPException) as caught:
                main.greeting_generate({
                    "job_key": "fresh-njd", "resume_id": self.resume["id"]})
            reused = main.greeting_generate({
                "job_key": "direct2", "resume_id": self.resume["id"]})

        # 仅 direct2 的复用路径触达生成器；fresh-njd 在闸门处就被拒单
        generate.assert_called_once()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("缺少职位描述", str(caught.exception.detail))
        self.assertTrue(reused["reused_existing"])
        self.assertEqual(reused["skipped_existing"], ["direct2"])


class FrontendWorkbenchGateContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        cls.index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_single_and_batch_generation_report_missing_jd(self):
        # 岗位列表批量评分 + 工作台单岗分析/招呼语与批量入口，共四处消费该字段
        self.assertGreaterEqual(self.app.count("result.skipped_no_jd_count"), 4)
        self.assertIn("'该岗位缺少职位描述（JD），补齐后才能生成分析'", self.app)
        self.assertIn("'该岗位缺少职位描述（JD），补齐后才能生成招呼语'", self.app)
        self.assertIn("`所选 ${noJd} 个岗位缺少职位描述（JD），补齐后才能${labels[type]}`",
                      self.app)

    def test_legacy_copy_still_present(self):
        self.assertIn("该岗位已有招呼语，已保留原结果", self.app)

    def test_cache_version_pins_current_tag(self):
        self.assertNotIn("?v=m24-workbench-gate", self.index)
        self.assertIn("app.js?v=win-account-fix", self.index)


if __name__ == "__main__":
    unittest.main()
