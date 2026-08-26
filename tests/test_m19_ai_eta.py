"""M19 页面级 AI 队列剩余时间估计。"""
import json
import os
import tempfile
import threading
import time
import unittest


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m19-ai-eta-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend.ai_tasks import AITaskScheduler  # noqa: E402


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class _FakeClock:
    def __init__(self):
        self._value = 0.0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._value

    def advance(self, seconds):
        with self._lock:
            self._value += float(seconds)


class _RetryAfterError(RuntimeError):
    status_code = 429
    retry_after = 2.5


class _LongRetryAfterError(RuntimeError):
    status_code = 429
    retry_after = 120


class _InfiniteRetryAfterError(RuntimeError):
    status_code = 429
    retry_after = float("inf")


class AITaskEtaTests(unittest.TestCase):
    def setUp(self):
        self.schedulers = []

    def tearDown(self):
        for scheduler in self.schedulers:
            scheduler.close(wait=True, timeout=1)

    def scheduler(self, runner, **kwargs):
        scheduler = AITaskScheduler(runner, **kwargs)
        self.schedulers.append(scheduler)
        return scheduler

    @staticmethod
    def _sse_data(event):
        return json.loads(next(
            line[6:] for line in event.splitlines()
            if line.startswith("data: ")))

    def test_eta_uses_page_concurrency_and_pages_are_independent(self):
        release = threading.Event()

        def runner(task, cancelled):
            release.wait(2)
            return task["job_key"]

        clock = _FakeClock()
        scheduler = self.scheduler(
            runner,
            max_concurrency=2,
            clock_func=clock,
            default_task_seconds={"score": 10, "analysis": 20},
        )
        jobs_stream = scheduler.event_stream("jobs", heartbeat=0.2)
        initial = self._sse_data(next(jobs_stream))
        self.assertEqual(initial["progress"]["eta_seconds"], 0)
        self.assertEqual(initial["progress"]["remaining"], 0)

        for index in range(4):
            scheduler.enqueue("jobs", "score", f"job-{index}", 1)
        for index in range(2):
            scheduler.enqueue("workbench", "analysis", f"work-{index}", 1)
        self.assertTrue(_wait_until(
            lambda: scheduler.snapshot("jobs")["active"] == 2 and
                    scheduler.snapshot("workbench")["active"] == 2))

        jobs = scheduler.snapshot("jobs")["progress"]
        workbench = scheduler.snapshot("workbench")["progress"]
        self.assertEqual(jobs["remaining"], 4)
        self.assertEqual(jobs["eta_seconds"], 20)
        self.assertEqual(jobs["average_seconds_per_task"], 10.0)
        self.assertEqual(jobs["rate"], 0.2)
        self.assertEqual(jobs["rate_per_minute"], 12.0)
        self.assertEqual(jobs["estimate_samples"], 0)
        self.assertEqual(workbench["remaining"], 2)
        self.assertEqual(workbench["eta_seconds"], 20)
        self.assertEqual(workbench["average_seconds_per_task"], 20.0)
        self.assertEqual(workbench["rate_per_minute"], 6.0)
        task_snapshots = scheduler.snapshot("jobs")["tasks"]
        self.assertEqual(
            {task["eta_seconds"] for task in task_snapshots
             if task["status"] == "running"}, {10})
        self.assertEqual(
            {task["eta_seconds"] for task in task_snapshots
             if task["status"] == "queued"}, {20})
        self.assertEqual(
            {task["eta_confidence"] for task in task_snapshots}, {"low"})

        streamed = self._sse_data(next(jobs_stream))["progress"]
        self.assertIn("eta_seconds", streamed)
        self.assertIn("rate_per_minute", streamed)
        jobs_stream.close()
        release.set()
        self.assertTrue(scheduler.wait_for_idle(timeout=2))
        for page in ("jobs", "workbench"):
            final = scheduler.snapshot(page)["progress"]
            self.assertEqual(final["remaining"], 0)
            self.assertEqual(final["eta_seconds"], 0)
            self.assertEqual(final["rate"], 0.0)
            self.assertEqual(final["rate_per_minute"], 0.0)
            self.assertTrue(all(
                task["eta_seconds"] == 0 and
                task["eta_confidence"] == "none"
                for task in scheduler.snapshot(page)["tasks"]))

    def test_recent_successes_calibrate_eta_and_stream_progress_refines_it(self):
        clock = _FakeClock()
        release = threading.Event()
        second_started = threading.Event()

        def runner(task, cancelled):
            if task["job_key"] == "sample":
                clock.advance(4)
                return "sampled"
            second_started.set()
            release.wait(2)
            return "done"

        scheduler = self.scheduler(
            runner,
            max_concurrency=1,
            clock_func=clock,
            default_task_seconds={"score": 10, "analysis": 20},
        )
        scheduler.enqueue("jobs", "score", "sample", 1)
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))

        running = scheduler.enqueue("jobs", "score", "running", 1)
        scheduler.enqueue("jobs", "score", "queued", 1)
        self.assertTrue(second_started.wait(1))
        progress = scheduler.snapshot("jobs")["progress"]
        # 单个 4 秒样本只占 1/3 权重，默认 10 秒平滑到 8 秒。
        self.assertEqual(progress["average_seconds_per_task"], 8.0)
        self.assertEqual(progress["estimate_samples"], 1)
        self.assertEqual(progress["eta_seconds"], 16)

        clock.advance(4)
        scheduler.update_task(running["id"], progress=0.5)
        refined = scheduler.snapshot("jobs")["progress"]
        self.assertEqual(refined["eta_seconds"], 12)
        refined_tasks = {
            task["job_key"]: task for task in scheduler.snapshot("jobs")["tasks"]
        }
        self.assertEqual(refined_tasks["running"]["eta_seconds"], 4)
        self.assertEqual(refined_tasks["queued"]["eta_seconds"], 12)

        # jobs 的耗时样本不能泄漏到 workbench。
        scheduler.enqueue("workbench", "analysis", "isolated", 1)
        self.assertTrue(_wait_until(
            lambda: scheduler.snapshot("workbench")["active"] == 1))
        isolated = scheduler.snapshot("workbench")["progress"]
        self.assertEqual(isolated["average_seconds_per_task"], 20.0)
        self.assertEqual(isolated["estimate_samples"], 0)

        release.set()
        self.assertTrue(scheduler.wait_for_idle(timeout=2))

    def test_retry_after_is_included_and_reduced_concurrency_updates_rate(self):
        clock = _FakeClock()
        retrying = threading.Event()
        continue_retry = threading.Event()
        attempts = 0

        def runner(task, cancelled):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _RetryAfterError("请求过多")
            return "done"

        def sleep_for_retry(delay):
            retrying.set()
            continue_retry.wait(2)
            clock.advance(delay)

        scheduler = self.scheduler(
            runner,
            max_concurrency=2,
            max_retries=1,
            sleep_func=sleep_for_retry,
            clock_func=clock,
            default_task_seconds={"score": 10},
        )
        scheduler.enqueue("jobs", "score", "limited", 1)
        self.assertTrue(retrying.wait(1))
        snapshot = scheduler.snapshot("jobs")
        self.assertEqual(snapshot["tasks"][0]["status"], "retrying")
        self.assertEqual(snapshot["tasks"][0]["eta_seconds"], 13)
        self.assertEqual(snapshot["effective_concurrency"], 1)
        self.assertEqual(snapshot["progress"]["eta_seconds"], 13)
        self.assertEqual(snapshot["progress"]["rate_per_minute"], 6.0)

        continue_retry.set()
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))
        final = scheduler.snapshot("jobs")["progress"]
        self.assertEqual(final["remaining"], 0)
        self.assertEqual(final["eta_seconds"], 0)

    def test_retry_after_is_not_capped_and_retry_resets_stream_progress(self):
        clock = _FakeClock()
        retry_sleeping = threading.Event()
        allow_retry = threading.Event()
        retry_started = threading.Event()
        finish_retry = threading.Event()
        delays = []
        attempts = 0
        scheduler = None

        def runner(task, cancelled):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                scheduler.update_task(
                    task["id"], progress=0.9,
                    message="首轮流式输出", partial_output="旧内容")
                raise _LongRetryAfterError("稍后重试")
            retry_started.set()
            finish_retry.wait(2)
            return "done"

        def sleep_for_retry(delay):
            delays.append(delay)
            retry_sleeping.set()
            allow_retry.wait(2)

        scheduler = self.scheduler(
            runner,
            max_concurrency=1,
            max_retries=1,
            backoff_cap=30,
            sleep_func=sleep_for_retry,
            clock_func=clock,
            default_task_seconds={"score": 10},
        )
        scheduler.enqueue("jobs", "score", "long-retry", 1)
        self.assertTrue(retry_sleeping.wait(1))
        retrying = scheduler.snapshot("jobs")
        self.assertEqual(delays, [120.0])
        self.assertEqual(retrying["tasks"][0]["status"], "retrying")
        self.assertEqual(retrying["tasks"][0]["eta_seconds"], 130)
        self.assertEqual(retrying["progress"]["eta_seconds"], 130)

        allow_retry.set()
        self.assertTrue(retry_started.wait(1))
        restarted = scheduler.snapshot("jobs")["tasks"][0]
        self.assertEqual(restarted["status"], "running")
        self.assertIsNone(restarted["progress"])
        self.assertEqual(restarted["message"], "")
        self.assertEqual(restarted["partial_output"], "")
        self.assertEqual(restarted["eta_seconds"], 10)

        finish_retry.set()
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))

    def test_concurrent_retries_obey_reduced_page_concurrency(self):
        first_attempts = threading.Barrier(3)
        release_retries = threading.Event()
        retry_started = threading.Event()
        lock = threading.Lock()
        attempts = {}
        retry_active = 0
        retry_peak = 0

        def runner(task, cancelled):
            nonlocal retry_active, retry_peak
            key = task["job_key"]
            with lock:
                attempts[key] = attempts.get(key, 0) + 1
                attempt = attempts[key]
            if attempt == 1:
                first_attempts.wait(1)
                raise _RetryAfterError("并发限流")
            with lock:
                retry_active += 1
                retry_peak = max(retry_peak, retry_active)
                retry_started.set()
            release_retries.wait(2)
            with lock:
                retry_active -= 1
            return "done"

        scheduler = self.scheduler(
            runner,
            max_concurrency=3,
            max_retries=1,
            recovery_successes=99,
            sleep_func=lambda delay: None,
        )
        for index in range(3):
            scheduler.enqueue("jobs", "score", f"limited-{index}", 1)
        self.assertTrue(retry_started.wait(1))
        time.sleep(0.03)
        with lock:
            self.assertEqual(retry_peak, 1)
            self.assertEqual(sum(value >= 2 for value in attempts.values()), 1)
        self.assertEqual(scheduler.snapshot("jobs")["effective_concurrency"], 1)

        release_retries.set()
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))
        with lock:
            self.assertEqual(retry_peak, 1)
            self.assertEqual(set(attempts.values()), {2})

    def test_retry_backoff_does_not_block_queued_task_request_slot(self):
        clock = _FakeClock()
        retry_sleeping = threading.Event()
        allow_retry = threading.Event()
        queued_started = threading.Event()
        release_queued = threading.Event()
        attempts = {}

        def runner(task, cancelled):
            key = task["job_key"]
            attempts[key] = attempts.get(key, 0) + 1
            if key == "retrying" and attempts[key] == 1:
                raise _LongRetryAfterError("长退避")
            if key == "queued":
                queued_started.set()
                release_queued.wait(2)
            return "done"

        def sleep_for_retry(delay):
            retry_sleeping.set()
            allow_retry.wait(2)

        scheduler = self.scheduler(
            runner,
            max_concurrency=2,
            max_retries=1,
            recovery_successes=99,
            sleep_func=sleep_for_retry,
            clock_func=clock,
            default_task_seconds={"score": 10},
        )
        scheduler.enqueue("jobs", "score", "retrying", 1)
        self.assertTrue(retry_sleeping.wait(1))
        scheduler.enqueue("jobs", "score", "queued", 1)
        self.assertTrue(queued_started.wait(1))

        snapshot = scheduler.snapshot("jobs")
        tasks = {task["job_key"]: task for task in snapshot["tasks"]}
        self.assertEqual(snapshot["effective_concurrency"], 1)
        self.assertEqual(tasks["queued"]["status"], "running")
        self.assertEqual(tasks["queued"]["eta_seconds"], 10)
        self.assertEqual(tasks["retrying"]["eta_seconds"], 130)
        self.assertEqual(snapshot["progress"]["eta_seconds"], 130)

        release_queued.set()
        allow_retry.set()
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))

    def test_infinite_retry_after_falls_back_to_exponential_delay(self):
        delays = []
        attempts = 0

        def runner(task, cancelled):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _InfiniteRetryAfterError("非法 Retry-After")
            return "done"

        scheduler = self.scheduler(
            runner,
            max_concurrency=1,
            max_retries=1,
            backoff_base=3,
            sleep_func=delays.append,
        )
        scheduler.enqueue("jobs", "score", "invalid-delay", 1)
        self.assertTrue(scheduler.wait_for_idle("jobs", timeout=2))
        self.assertEqual(delays, [3.0])
        self.assertEqual(
            scheduler.snapshot("jobs")["progress"]["succeeded"], 1)

    def test_cancelled_runner_keeps_slot_release_time_in_queued_task_eta(self):
        clock = _FakeClock()
        release = threading.Event()
        started = threading.Event()

        def runner(task, cancelled):
            if task["job_key"] == "running":
                started.set()
                release.wait(2)
            return "done"

        scheduler = self.scheduler(
            runner,
            max_concurrency=1,
            clock_func=clock,
            default_task_seconds={"analysis": 10},
        )
        running = scheduler.enqueue("workbench", "analysis", "running", 1)
        scheduler.enqueue("workbench", "analysis", "queued", 1)
        self.assertTrue(started.wait(1))
        self.assertEqual(scheduler.cancel(task_id=running["id"]), 1)

        snapshot = scheduler.snapshot("workbench")
        tasks = {task["job_key"]: task for task in snapshot["tasks"]}
        self.assertEqual(snapshot["progress"]["remaining"], 2)
        self.assertEqual(snapshot["progress"]["eta_seconds"], 12)
        self.assertEqual(tasks["running"]["eta_seconds"], 2)
        self.assertEqual(tasks["queued"]["eta_seconds"], 12)

        release.set()
        self.assertTrue(scheduler.wait_for_idle("workbench", timeout=2))

    def test_cancellation_and_failure_converge_without_polluting_samples(self):
        release = threading.Event()
        started = threading.Event()

        def blocking_runner(task, cancelled):
            started.set()
            release.wait(2)
            return "done"

        scheduler = self.scheduler(
            blocking_runner,
            max_concurrency=1,
            default_task_seconds={"greeting": 10},
        )
        running = scheduler.enqueue("workbench", "greeting", "running", 1)
        scheduler.enqueue("workbench", "greeting", "queued", 1)
        self.assertTrue(started.wait(1))
        self.assertEqual(
            scheduler.snapshot("workbench")["progress"]["eta_seconds"], 20)

        self.assertEqual(scheduler.cancel(page="workbench"), 2)
        cancelling = scheduler.snapshot("workbench")["progress"]
        self.assertEqual(cancelling["remaining"], 1)
        self.assertEqual(cancelling["eta_seconds"], 2)
        self.assertEqual(cancelling["rate_per_minute"], 0.0)
        self.assertTrue(next(
            task for task in scheduler.snapshot("workbench")["tasks"]
            if task["id"] == running["id"])["cancel_requested"])

        release.set()
        self.assertTrue(scheduler.wait_for_idle("workbench", timeout=2))
        cancelled = scheduler.snapshot("workbench")["progress"]
        self.assertEqual(cancelled["completed"], 2)
        self.assertEqual(cancelled["cancelled"], 2)
        self.assertEqual(cancelled["remaining"], 0)
        self.assertEqual(cancelled["eta_seconds"], 0)
        self.assertEqual(cancelled["estimate_samples"], 0)

        failing = self.scheduler(
            lambda task, cancelled: (_ for _ in ()).throw(
                RuntimeError("模型失败")),
            default_task_seconds={"score": 10},
        )
        failing.enqueue("jobs", "score", "broken", 1)
        self.assertTrue(failing.wait_for_idle("jobs", timeout=2))
        failed = failing.snapshot("jobs")["progress"]
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(failed["remaining"], 0)
        self.assertEqual(failed["eta_seconds"], 0)
        self.assertEqual(failed["estimate_samples"], 0)


if __name__ == "__main__":
    unittest.main()
