"""M17 页面级 AI 调度器：并发、去重、取消、429 自适应与 SSE。"""
import json
import os
import tempfile
import threading
import time
import unittest


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m17-ai-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend.ai_tasks import AITaskScheduler, is_rate_limit_error  # noqa: E402


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class _Status429Error(RuntimeError):
    status_code = 429


class _Response429Error(RuntimeError):
    response = type("Response", (), {"status_code": 429})()


class _RetryAfter429Error(RuntimeError):
    status_code = 429
    retry_after = 2.5


class RateLimitDetectionTests(unittest.TestCase):
    def test_recognizes_attributes_text_and_nested_error(self):
        self.assertTrue(is_rate_limit_error(_Status429Error("busy")))
        self.assertTrue(is_rate_limit_error(_Response429Error("busy")))
        self.assertTrue(is_rate_limit_error(RuntimeError("HTTP 429 Too Many Requests")))
        try:
            try:
                raise RuntimeError("rate_limit_exceeded")
            except RuntimeError as cause:
                raise ValueError("调用失败") from cause
        except ValueError as nested:
            self.assertTrue(is_rate_limit_error(nested))
        self.assertFalse(is_rate_limit_error(RuntimeError("普通网络错误")))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.schedulers = []

    def tearDown(self):
        for scheduler in self.schedulers:
            scheduler.close(wait=True, timeout=1)

    def scheduler(self, runner, **kwargs):
        scheduler = AITaskScheduler(runner, **kwargs)
        self.schedulers.append(scheduler)
        return scheduler

    def test_two_pages_each_run_at_most_five_and_do_not_block_each_other(self):
        lock = threading.Lock()
        release = threading.Event()
        reached = {"jobs": threading.Event(), "workbench": threading.Event()}
        active = {"jobs": 0, "workbench": 0}
        peaks = {"jobs": 0, "workbench": 0}

        def runner(task, cancelled):
            with lock:
                page = task["page"]
                active[page] += 1
                peaks[page] = max(peaks[page], active[page])
                if active[page] == 5:
                    reached[page].set()
            release.wait(2)
            with lock:
                active[page] -= 1
            return task["job_key"]

        scheduler = self.scheduler(runner)
        for page in ("jobs", "workbench"):
            for index in range(7):
                scheduler.enqueue(page, "analysis", f"{page}-{index}", 1)

        self.assertTrue(reached["jobs"].wait(1))
        self.assertTrue(reached["workbench"].wait(1))
        self.assertEqual(scheduler.snapshot("jobs")["active"], 5)
        self.assertEqual(scheduler.snapshot("workbench")["active"], 5)
        self.assertEqual(peaks, {"jobs": 5, "workbench": 5})

        release.set()
        self.assertTrue(scheduler.wait_for_idle(timeout=2))
        self.assertLessEqual(peaks["jobs"], 5)
        self.assertLessEqual(peaks["workbench"], 5)

    def test_running_queue_accepts_new_tasks_and_deduplicates_active_key(self):
        release = threading.Event()
        started = threading.Event()

        def runner(task, cancelled):
            started.set()
            release.wait(2)
            return {"ok": True}

        scheduler = self.scheduler(runner, max_concurrency=1)
        first = scheduler.enqueue("jobs", "score", "job-1", 3, {"force": True})
        self.assertTrue(started.wait(1))
        second = scheduler.enqueue("jobs", "score", "job-2", 3)
        duplicate = scheduler.enqueue("jobs", "score", "job-2", 3)
        self.assertFalse(second["deduplicated"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["id"], second["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(scheduler.snapshot("jobs")["progress"]["total"], 2)

        release.set()
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))

    def test_cancel_queued_and_running_tasks_never_counts_as_success(self):
        started = threading.Event()
        release = threading.Event()
        callback_seen = threading.Event()

        def runner(task, cancelled):
            started.set()
            while not release.wait(0.005):
                if cancelled():
                    callback_seen.set()
                    break
            return "即使 runner 返回，也不能记成功"

        scheduler = self.scheduler(runner, max_concurrency=1)
        running = scheduler.enqueue("workbench", "greeting", "running", 8)
        self.assertTrue(started.wait(1))
        queued = scheduler.enqueue("workbench", "greeting", "queued", 8)

        self.assertEqual(scheduler.cancel(queued["id"]), 1)
        self.assertEqual(scheduler.cancel(running["id"]), 1)
        self.assertTrue(callback_seen.wait(1))
        release.set()
        self.assertTrue(scheduler.wait_for_idle("workbench", timeout=2))

        tasks = {item["id"]: item for item in scheduler.snapshot("workbench")["tasks"]}
        self.assertEqual(tasks[queued["id"]]["status"], "cancelled")
        self.assertEqual(tasks[running["id"]]["status"], "cancelled")
        self.assertIsNone(tasks[running["id"]]["result"])
        self.assertEqual(scheduler.snapshot("workbench")["progress"]["succeeded"], 0)

    def test_429_retries_reduce_only_one_page_and_successes_restore_it(self):
        attempts = {}
        delays = []

        def runner(task, cancelled):
            key = (task["page"], task["job_key"])
            attempts[key] = attempts.get(key, 0) + 1
            if key == ("jobs", "limited") and attempts[key] == 1:
                raise _Status429Error("provider busy")
            return {"attempt": attempts[key]}

        scheduler = self.scheduler(
            runner, sleep_func=delays.append, recovery_successes=2,
            max_retries=2, backoff_base=1, backoff_cap=10)
        limited = scheduler.enqueue("jobs", "score", "limited", 1)
        scheduler.enqueue("workbench", "analysis", "normal", 1)
        self.assertTrue(scheduler.wait_for_idle(timeout=2))

        jobs = scheduler.snapshot("jobs")
        workbench = scheduler.snapshot("workbench")
        limited_task = next(item for item in jobs["tasks"]
                            if item["id"] == limited["id"])
        self.assertEqual(limited_task["attempts"], 2)
        self.assertEqual(limited_task["rate_limit_retries"], 1)
        self.assertEqual(delays, [1.0])
        self.assertEqual(jobs["effective_concurrency"], 4)
        self.assertEqual(workbench["effective_concurrency"], 5)

        scheduler.enqueue("jobs", "score", "recovery", 1)
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))
        self.assertEqual(scheduler.snapshot("jobs")["effective_concurrency"], 5)

    def test_exhausted_429_and_regular_failure_are_visible(self):
        delays = []

        def runner(task, cancelled):
            if task["job_key"] == "limited":
                raise RuntimeError("HTTP 429")
            raise RuntimeError("模型输出失败")

        scheduler = self.scheduler(
            runner, max_retries=2, sleep_func=delays.append,
            backoff_base=0.5, backoff_cap=10)
        scheduler.enqueue("jobs", "score", "limited", 1)
        scheduler.enqueue("workbench", "greeting", "broken", 1)
        self.assertTrue(scheduler.wait_for_idle(timeout=2))

        jobs_task = scheduler.snapshot("jobs")["tasks"][0]
        workbench_task = scheduler.snapshot("workbench")["tasks"][0]
        self.assertEqual(jobs_task["status"], "failed")
        self.assertEqual(jobs_task["attempts"], 3)
        self.assertIn("429", jobs_task["error"])
        self.assertEqual(delays, [0.5, 1.0])
        self.assertEqual(workbench_task["status"], "failed")
        self.assertIn("模型输出失败", workbench_task["error"])

    def test_429_prefers_retry_after_over_exponential_delay(self):
        attempts = 0
        delays = []

        def runner(task, cancelled):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _RetryAfter429Error("稍后重试")
            return {"ok": True}

        scheduler = self.scheduler(
            runner, max_retries=1, sleep_func=delays.append,
            backoff_base=1, backoff_cap=10)
        scheduler.enqueue("jobs", "score", "retry-after", 1)
        self.assertTrue(scheduler.wait_for_idle(timeout=2))
        self.assertEqual(delays, [2.5])
        self.assertEqual(scheduler.snapshot("jobs")["progress"]["succeeded"], 1)

    def test_cancel_by_page_and_resume_does_not_touch_other_page(self):
        release = threading.Event()

        def runner(task, cancelled):
            release.wait(2)
            return "done"

        scheduler = self.scheduler(runner, max_concurrency=1)
        scheduler.enqueue("jobs", "score", "old-1", 1)
        scheduler.enqueue("jobs", "score", "old-2", 1)
        scheduler.enqueue("workbench", "analysis", "keep", 1)
        self.assertTrue(_wait_until(
            lambda: scheduler.snapshot("jobs")["active"] == 1 and
                    scheduler.snapshot("workbench")["active"] == 1))

        self.assertEqual(scheduler.cancel(page="jobs", resume_id=1), 2)
        self.assertEqual(scheduler.snapshot("workbench")["tasks"][0]["cancel_requested"],
                         False)
        release.set()
        self.assertTrue(scheduler.wait_for_idle(timeout=2))
        self.assertEqual(scheduler.snapshot("workbench")["progress"]["succeeded"], 1)

    def test_snapshot_progress_updates_and_sse_uses_versions(self):
        release = threading.Event()

        def runner(task, cancelled):
            release.wait(2)
            return {"summary": "完成"}

        scheduler = self.scheduler(runner, max_concurrency=1)
        stream = scheduler.event_stream("jobs", heartbeat=0.2)
        initial = next(stream)
        self.assertIn("event: snapshot", initial)
        initial_data = json.loads(next(
            line[6:] for line in initial.splitlines() if line.startswith("data: ")))
        self.assertEqual(initial_data["progress"]["total"], 0)

        task = scheduler.enqueue("jobs", "score", "stream", 2)
        update = next(stream)
        update_data = json.loads(next(
            line[6:] for line in update.splitlines() if line.startswith("data: ")))
        self.assertEqual(update_data["progress"]["total"], 1)
        self.assertGreater(update_data["version"], initial_data["version"])

        scheduler.update_task(task["id"], progress=0.4,
                              message="正在生成", partial_output="部分内容")
        updated = scheduler.snapshot("jobs")["tasks"][0]
        self.assertEqual(updated["progress"], 0.4)
        self.assertEqual(updated["partial_output"], "部分内容")

        release.set()
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))
        final = scheduler.snapshot("jobs")
        self.assertEqual(final["progress"]["completed"], 1)
        self.assertEqual(final["progress"]["percent"], 100)
        stream.close()


if __name__ == "__main__":
    unittest.main()
