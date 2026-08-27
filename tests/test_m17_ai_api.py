"""M17 页面 AI API、流式取消与采集删除端点契约。"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m17-api-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from fastapi import HTTPException  # noqa: E402

from backend import collection_runs, config, greeting, llm, main, resumes  # noqa: E402
from backend.ai_tasks import AITaskScheduler  # noqa: E402
from backend.db import get_db, init_db, now_iso, set_setting  # noqa: E402
from backend.scoring import l2  # noqa: E402


class PageTaskApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m17-api-case-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        config.COLLECT_RESULT_DIR = root / "job-result"
        init_db()
        self.resume = resumes.update_resume(
            resumes.get_default_resume()["id"], resume_text="完整项目简历" * 80)
        ts = now_iso()
        get_db().execute(
            "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
            "VALUES('task-job','AI 应用工程师','示例科技','active',?,?)", (ts, ts))
        # JD 闸门上线后：无 JD 的岗位不进入评分队列，本用例夹具统一带 JD
        get_db().execute(
            "INSERT INTO job_details(job_key,jd,fetched_at) VALUES('task-job',?,?)",
            ("负责核心业务模块开发", now_iso()))
        get_db().commit()
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True)
        main._ai_scheduler = None

    def tearDown(self):
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True)
        main._ai_scheduler = None
        self.tmp.cleanup()

    def test_enqueue_normalizes_both_score_buttons_and_deduplicates(self):
        release = threading.Event()

        def runner(task, cancelled):
            release.wait(2)
            return {"job_score": 80, "match_score": 82}

        main._ai_scheduler = AITaskScheduler(runner, max_concurrency=1)
        with patch.object(llm, "configured", return_value=True):
            first = main.ai_tasks_enqueue("jobs", {
                "kind": "job_score", "job_keys": ["task-job"],
                "resume_id": self.resume["id"],
            })
            second = main.ai_tasks_enqueue("jobs", {
                "kind": "match_score", "job_keys": ["task-job"],
                "resume_id": self.resume["id"],
            })
        self.assertEqual(first["kind"], "score")
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["deduplicated"], 1)
        self.assertEqual(first["tasks"][0]["id"], second["tasks"][0]["id"])
        release.set()
        self.assertTrue(main._ai_scheduler.wait_for_idle("jobs", timeout=2))
        status = main.ai_tasks_status("jobs")
        self.assertEqual(status["max_concurrency"], 1)
        self.assertEqual(status["progress"]["succeeded"], 1)

    def test_cancel_is_scoped_to_page_and_resume(self):
        release = threading.Event()

        def runner(task, cancelled):
            release.wait(2)
            return {"ok": True}

        main._ai_scheduler = AITaskScheduler(runner, max_concurrency=1)
        with patch.object(llm, "configured", return_value=True):
            main.ai_tasks_enqueue("jobs", {
                "kind": "score", "job_keys": ["task-job"],
                "resume_id": self.resume["id"],
            })
            main.ai_tasks_enqueue("workbench", {
                "kind": "analysis", "job_keys": ["task-job"],
                "resume_id": self.resume["id"],
            })
        cancelled = main.ai_tasks_cancel(
            "jobs", {"resume_id": self.resume["id"]})
        self.assertEqual(cancelled["cancelled"], 1)
        workbench = main.ai_tasks_status("workbench")
        self.assertFalse(workbench["tasks"][0]["cancel_requested"])
        release.set()
        self.assertTrue(main._ai_scheduler.wait_for_idle(timeout=2))

    def test_score_requires_llm_but_unconfigured_greeting_can_use_template(self):
        main._ai_scheduler = AITaskScheduler(lambda task, cancelled: {"ok": True})
        with patch.object(llm, "configured", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                main.ai_tasks_enqueue("jobs", {
                    "kind": "score", "job_keys": ["task-job"],
                    "resume_id": self.resume["id"],
                })
            greeting_task = main.ai_tasks_enqueue("workbench", {
                "kind": "greeting", "job_keys": ["task-job"],
                "resume_id": self.resume["id"],
            })
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(greeting_task["added"], 1)


class LLMStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m17-stream-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()
        self.resume = resumes.update_resume(
            resumes.get_default_resume()["id"], resume_text="流式评分简历" * 80)
        ts = now_iso()
        get_db().execute(
            "INSERT INTO jobs(job_key,title,company,status,l1_detail,first_seen_at,last_seen_at) "
            "VALUES('stream-job','AI 工程师','示例科技','active','{\"cap\":100}',?,?)",
            (ts, ts))
        get_db().commit()
        resumes.save_job_score(
            "stream-job", self.resume["id"], self.resume["revision"],
            l1_score=70, composite_rough=70, l1_detail={"cap": 100}, l1_source="engine")
        set_setting("llm_model", "test-model")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def stream_client(parts):
        chunks = [SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=part))]) for part in parts]
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: iter(chunks))))

    def test_stream_cancel_stops_before_score_writeback(self):
        cancelled = {"value": False}
        response = ('{"dims":{"A":10},"s":{"S1":60},"adjust":[],'
                    '"summary":"不应保存","advice":"正常投"}')

        def on_delta(_):
            cancelled["value"] = True

        with self.assertRaises(llm.LLMCancelledError):
            l2.score_job_llm(
                "stream-job", client=self.stream_client([response[:20], response[20:]]),
                resume_id=self.resume["id"], force=True, on_delta=on_delta,
                cancelled=lambda: cancelled["value"])
        score = resumes.get_job_score("stream-job", self.resume["id"])
        self.assertIsNone(score["composite"])

    def test_rate_limit_keeps_429_and_retry_after(self):
        class Provider429(RuntimeError):
            status_code = 429
            response = SimpleNamespace(status_code=429, headers={"Retry-After": "2"})

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: (_ for _ in ()).throw(Provider429("请求过多")))))
        with self.assertRaises(llm.LLMRateLimitError) as caught:
            llm.chat([{"role": "user", "content": "测试"}], client=client)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 2.0)

    def test_configured_greeting_task_surfaces_failure_instead_of_template(self):
        attempts = iter(("不是 JSON", "仍然不是", "最后仍失败"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=next(attempts)))]))))
        set_setting("llm_base_url", "https://llm.example/v1")
        set_setting("llm_api_key", "secret")
        with self.assertRaises(llm.LLMError):
            greeting.generate(
                "stream-job", client=client, resume_id=self.resume["id"],
                fallback_on_error=False)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) count FROM greetings WHERE job_key='stream-job'"
        ).fetchone()["count"], 0)


class CollectionDeleteApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m17-delete-api-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_and_delete_endpoint_require_matching_confirmation(self):
        ts = now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
            "VALUES('delete-api','岗位','公司','active',?,?)", (ts, ts))
        run_id = conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('search','{}','{\"total\":1}',?,?,'succeeded')", (ts, ts)).lastrowid
        conn.commit()
        collection_runs.attach_jobs(run_id, ["delete-api"])

        self.assertEqual(main.run_delete_preview(run_id)["exclusive_job_count"], 1)
        with self.assertRaises(HTTPException) as caught:
            main.run_delete(run_id, {"confirm_run_id": run_id + 1})
        self.assertEqual(caught.exception.status_code, 400)
        result = main.run_delete(run_id, {"confirm_run_id": run_id})
        self.assertEqual(result["deleted_jobs"], 1)


if __name__ == "__main__":
    unittest.main()
