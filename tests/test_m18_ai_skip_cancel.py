"""M18 AI 任务幂等、修订去重与取消结果保留。"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m18-ai-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config, llm, main, resumes  # noqa: E402
from backend.ai_tasks import AITaskScheduler  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402
from backend.scoring import l2  # noqa: E402


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class AiSkipCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m18-ai-case-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()
        self.resume = resumes.update_resume(
            resumes.get_default_resume()["id"], resume_text="完整项目简历" * 80)
        self.release = threading.Event()
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True)
        main._ai_scheduler = None
        with main._score_flights_lock:
            main._score_flights.clear()
        with l2._INFLIGHT_LOCK:
            l2._INFLIGHT.clear()

    def tearDown(self):
        self.release.set()
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

    def save_complete(self, job_key: str, *, source="llm", revision=None):
        resumes.save_job_score(
            job_key, self.resume["id"], revision or self.resume["revision"],
            job_score=78, match_score=82, composite=80.4, priority="P0",
            l2_detail={"summary": f"{source} 已完成"}, l2_source=source,
            l2_stale=int(bool(revision and revision != self.resume["revision"])),
        )

    def test_mixed_batch_only_enqueues_missing_current_revision(self):
        for key in ("done", "imported", "missing", "old-revision"):
            self.add_job(key)
        self.save_complete("done")
        resumes.save_imported_baseline(
            "imported", composite=76, job_score=75, match_score=77,
            l2_detail={"summary": "导入评分"})
        self.save_complete("imported", source="imported")
        self.save_complete("old-revision", revision=self.resume["revision"] - 1)

        calls = []
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: calls.append(task) or {"ok": True},
            max_concurrency=1)
        with patch.object(llm, "configured", return_value=True):
            result = main.ai_tasks_enqueue("jobs", {
                "kind": "score",
                "job_keys": ["done", "imported", "missing", "old-revision"],
                "resume_id": self.resume["id"],
            })

        self.assertEqual(result["skipped_existing"], ["done", "imported"])
        self.assertEqual(result["skipped_existing_count"], 2)
        self.assertEqual(result["added"], 2)
        self.assertEqual(
            {task["job_key"] for task in result["tasks"]},
            {"missing", "old-revision"})
        self.assertTrue(all(task["payload"]["force"] is False
                            for task in result["tasks"]))
        self.assertTrue(main._ai_scheduler.wait_for_idle("jobs", timeout=2))
        self.assertEqual(len(calls), 2)

    def test_all_existing_is_noop_without_llm_for_both_pages(self):
        for key in ("done", "imported"):
            self.add_job(key)
        self.save_complete("done")
        self.save_complete("imported", source="imported")
        calls = []
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: calls.append(task) or {"ok": True})

        with patch.object(llm, "configured", return_value=False):
            jobs = main.ai_tasks_enqueue("jobs", {
                "kind": "score", "job_keys": ["done", "imported"],
                "resume_id": self.resume["id"],
            })
            workbench = main.ai_tasks_enqueue("workbench", {
                "kind": "analysis", "job_keys": ["done", "imported"],
                "resume_id": self.resume["id"],
            })

        self.assertEqual(jobs["added"], 0)
        self.assertEqual(workbench["added"], 0)
        self.assertEqual(jobs["skipped_existing_count"], 2)
        self.assertEqual(workbench["skipped_existing_count"], 2)
        self.assertEqual(calls, [])

    def test_force_must_be_explicit_and_worker_reuses_existing(self):
        self.add_job("done")
        self.save_complete("done")
        task = {
            "id": "reuse", "page": "jobs", "kind": "score",
            "job_key": "done", "resume_id": self.resume["id"],
            "payload": {"resume_revision": self.resume["revision"]},
        }
        with patch.object(main.scoring_l1, "run_l1") as run_l1, \
                patch.object(l2, "score_job_llm") as score_job:
            reused = main._run_score_artifact(task, lambda: False)
        self.assertTrue(reused["reused_existing"])
        run_l1.assert_not_called()
        score_job.assert_not_called()

        main._ai_scheduler = AITaskScheduler(lambda task, cancelled: {"ok": True})
        with patch.object(llm, "configured", return_value=True):
            forced = main.ai_tasks_enqueue("jobs", {
                "kind": "score", "job_keys": ["done"],
                "resume_id": self.resume["id"], "force": True,
            })
        self.assertEqual(forced["added"], 1)
        self.assertEqual(forced["skipped_existing_count"], 0)
        self.assertTrue(forced["tasks"][0]["payload"]["force"])

    def test_cancel_endpoint_accepts_task_id_and_stays_on_page(self):
        started = {"jobs": threading.Event(), "workbench": threading.Event()}

        def runner(task, cancelled):
            started[task["page"]].set()
            while not self.release.wait(0.005):
                if cancelled():
                    return {"cancelled": True}
            return {"ok": True}

        main._ai_scheduler = AITaskScheduler(runner, max_concurrency=1)
        jobs_task = main._ai_scheduler.enqueue(
            "jobs", "score", "same", self.resume["id"],
            {"resume_revision": self.resume["revision"]})
        workbench_task = main._ai_scheduler.enqueue(
            "workbench", "analysis", "same", self.resume["id"],
            {"resume_revision": self.resume["revision"]})
        self.assertTrue(started["jobs"].wait(1))
        self.assertTrue(started["workbench"].wait(1))

        result = main.ai_tasks_cancel("jobs", {"task_id": jobs_task["id"]})
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(
            main.ai_tasks_cancel("jobs", {"task_id": workbench_task["id"]})["cancelled"],
            0)
        self.assertFalse(main.ai_tasks_status("workbench")["tasks"][0]["cancel_requested"])
        self.release.set()
        self.assertTrue(main._ai_scheduler.wait_for_idle(timeout=2))

    def test_new_revision_is_not_deduplicated_by_cancelling_old_task(self):
        started = threading.Event()

        def runner(task, cancelled):
            started.set()
            self.release.wait(2)
            return {"ok": True}

        scheduler = AITaskScheduler(runner, max_concurrency=1)
        main._ai_scheduler = scheduler
        first = scheduler.enqueue(
            "jobs", "score", "same", self.resume["id"],
            {"resume_revision": 1, "force": False})
        self.assertTrue(started.wait(1))
        self.assertEqual(scheduler.cancel(first["id"]), 1)
        second = scheduler.enqueue(
            "jobs", "score", "same", self.resume["id"],
            {"resume_revision": 2, "force": False})
        duplicate = scheduler.enqueue(
            "jobs", "score", "same", self.resume["id"],
            {"resume_revision": 2, "force": True})
        self.assertFalse(second["deduplicated"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertNotEqual(first["id"], second["id"])
        self.release.set()
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))

    def test_l2_expected_revision_is_checked_and_used_inflight(self):
        self.add_job("revision-job")
        calls = []

        def unexpected_create(**kwargs):
            calls.append(kwargs)
            raise AssertionError("旧修订不应调用模型")

        invalid_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=unexpected_create)))
        with self.assertRaises(llm.LLMCancelledError):
            l2.score_job_llm(
                "revision-job", client=invalid_client,
                resume_id=self.resume["id"],
                resume_revision=self.resume["revision"] - 1, force=True)
        self.assertEqual(calls, [])

        started = threading.Event()
        response = ('{"dims":{"A":10},"s":{"S1":60},"adjust":[],'
                    '"summary":"完成","advice":"正常投"}')

        def create(**kwargs):
            started.set()
            self.release.wait(2)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=response))])

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        errors = []
        thread = threading.Thread(target=lambda: self._run_l2_thread(
            client, errors), daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        with l2._INFLIGHT_LOCK:
            self.assertIn(
                ("revision-job", self.resume["id"], self.resume["revision"]),
                l2._INFLIGHT)
        self.release.set()
        thread.join(2)
        self.assertEqual(errors, [])

    def _run_l2_thread(self, client, errors):
        try:
            l2.score_job_llm(
                "revision-job", client=client, resume_id=self.resume["id"],
                resume_revision=self.resume["revision"], force=True)
        except Exception as error:  # pragma: no cover - 失败内容由主线程断言
            errors.append(error)

    def test_cancelling_remaining_task_keeps_completed_score(self):
        self.add_job("completed")
        self.add_job("pending")
        pending_started = threading.Event()

        def runner(task, cancelled):
            if task["job_key"] == "completed":
                resumes.save_job_score(
                    "completed", self.resume["id"], self.resume["revision"],
                    job_score=88, match_score=86, composite=86.8,
                    priority="P0", l2_detail={"summary": "已完成"},
                    l2_source="llm")
                return {"composite": 86.8}
            pending_started.set()
            while not cancelled():
                time.sleep(0.005)
            return {"cancelled": True}

        main._ai_scheduler = AITaskScheduler(runner, max_concurrency=1)
        main._ai_scheduler.enqueue(
            "jobs", "score", "completed", self.resume["id"],
            {"resume_revision": self.resume["revision"]})
        pending = main._ai_scheduler.enqueue(
            "jobs", "score", "pending", self.resume["id"],
            {"resume_revision": self.resume["revision"]})
        self.assertTrue(pending_started.wait(1))
        self.assertEqual(
            main.ai_tasks_cancel("jobs", {"task_id": pending["id"]})["cancelled"],
            1)
        self.assertTrue(main._ai_scheduler.wait_for_idle("jobs", timeout=2))

        self.assertEqual(
            resumes.get_job_score("completed", self.resume["id"])["composite"],
            86.8)
        self.assertIsNone(resumes.get_job_score("pending", self.resume["id"]))
        statuses = {task["job_key"]: task["status"]
                    for task in main.ai_tasks_status("jobs")["tasks"]}
        self.assertEqual(statuses, {"completed": "succeeded", "pending": "cancelled"})


if __name__ == "__main__":
    unittest.main()
