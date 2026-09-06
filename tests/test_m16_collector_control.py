"""M16 测试：采集暂停、恢复、取消与运行计时控制。"""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# 测试库隔离到临时目录，绝不碰 ~/.boss-copilot 真实数据
_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m16-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import collector, config  # noqa: E402
from backend.db import close_db, get_db, init_db  # noqa: E402


class _FakeProcess:
    def __init__(self):
        self.signals = []

    def poll(self):
        return None

    def send_signal(self, value):
        self.signals.append(value)


class CollectorControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m16-case-")
        self.root = Path(self.tmp.name)
        self.original_result_dir = collector.RESULT_DIR
        self.original_scraper_dir = collector.SCRAPER_DIR
        config.DATA_DIR = self.root
        config.DB_PATH = self.root / "copilot.db"
        config.COLLECT_RESULT_DIR = self.root / "job-result"
        collector.RESULT_DIR = config.COLLECT_RESULT_DIR
        collector.SCRAPER_DIR = self.root
        init_db()
        with collector._state_condition:
            collector._state.update({
                "running": False, "current": "", "phase": "", "log": [],
                "run_id": None, "cancel": False, "risk_signal": "",
                "paused": False, "process": None, "pause_started_at": None,
                "paused_seconds": 0.0, "worker_ident": None,
                "eta_model": None,
                "progress": {"list_total": 0, "list_completed": 0,
                             "detail_total": 0, "detail_completed": 0},
            })
            collector._state_condition.notify_all()

    def tearDown(self):
        with collector._state_condition:
            process = collector._state.get("process")
            collector._signal_process(process, getattr(signal, "SIGCONT", None))
            collector._state.update({
                "running": False, "current": "", "phase": "", "run_id": None,
                "cancel": False, "paused": False, "process": None,
                "pause_started_at": None, "paused_seconds": 0.0,
                "worker_ident": None, "eta_model": None,
            })
            collector._state_condition.notify_all()
        if process is not None and process.poll() is None and hasattr(process, "terminate"):
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
        collector.RESULT_DIR = self.original_result_dir
        collector.SCRAPER_DIR = self.original_scraper_dir
        close_db()
        self.tmp.cleanup()

    def _begin_run(self):
        task = collector._normalize_task({
            "type": "search", "keyword": "AI", "city": "深圳", "pages": 1,
        })
        run_id = collector._begin_run("config", {"tasks": [task]}, [task])
        return run_id

    @unittest.skipUnless(os.name == "posix", "SIGSTOP/SIGCONT 仅适用于 Unix")
    def test_pause_resume_updates_memory_process_database_and_status(self):
        run_id = self._begin_run()
        process = _FakeProcess()
        with collector._state_condition:
            collector._state["process"] = process

        paused = collector.pause()

        self.assertTrue(paused["paused"])
        self.assertEqual(process.signals, [signal.SIGSTOP])
        self.assertTrue(collector._state["paused"])
        self.assertEqual(get_db().execute(
            "SELECT paused FROM collect_runs WHERE id=?", (run_id,)).fetchone()["paused"], 1)
        self.assertTrue(collector.status()["paused"])

        resumed = collector.resume()

        self.assertFalse(resumed["paused"])
        self.assertEqual(process.signals, [signal.SIGSTOP, signal.SIGCONT])
        self.assertFalse(collector._state["paused"])
        self.assertEqual(get_db().execute(
            "SELECT paused FROM collect_runs WHERE id=?", (run_id,)).fetchone()["paused"], 0)
        self.assertFalse(collector.status()["paused"])

    @unittest.skipUnless(os.name == "posix", "SIGSTOP/SIGCONT 仅适用于 Unix")
    def test_cancel_resumes_paused_process_and_finish_cleans_control_state(self):
        run_id = self._begin_run()
        process = _FakeProcess()
        with collector._state_condition:
            collector._state["process"] = process
        collector.pause()

        result = collector.cancel()

        self.assertTrue(result["ok"])
        self.assertFalse(result["paused"])
        self.assertEqual(process.signals, [signal.SIGSTOP, signal.SIGCONT])
        self.assertTrue(collector._state["cancel"])
        self.assertFalse(collector._state["paused"])
        row = get_db().execute(
            "SELECT cancel_requested,paused FROM collect_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual((row["cancel_requested"], row["paused"]), (1, 0))

        collector._finish_run(run_id, {"cancelled": True}, "cancelled")

        self.assertFalse(collector._state["running"])
        self.assertFalse(collector._state["paused"])
        self.assertIsNone(collector._state["process"])
        self.assertEqual(get_db().execute(
            "SELECT paused FROM collect_runs WHERE id=?", (run_id,)).fetchone()["paused"], 0)

    def test_task_gap_does_not_advance_while_paused(self):
        self._begin_run()
        started = threading.Event()
        finished = threading.Event()

        def wait_for_gap():
            started.set()
            collector._wait_between_tasks()
            finished.set()

        with patch.object(collector, "ITEM_GAP_SEC", 0.12):
            thread = threading.Thread(target=wait_for_gap, daemon=True)
            thread.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.03)
            collector.pause()
            time.sleep(0.16)
            self.assertFalse(finished.is_set())
            collector.resume()
            thread.join(1)

        self.assertTrue(finished.is_set())

    def test_phase_transition_waits_until_resume(self):
        run_id = self._begin_run()
        collector.pause()
        finished = threading.Event()

        def change_phase():
            collector._set_phase(run_id, "details")
            finished.set()

        thread = threading.Thread(target=change_phase, daemon=True)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(finished.is_set())
        self.assertEqual(get_db().execute(
            "SELECT phase FROM collect_runs WHERE id=?", (run_id,)).fetchone()["phase"],
            "list")

        collector.resume()
        thread.join(1)

        self.assertTrue(finished.is_set())
        self.assertEqual(get_db().execute(
            "SELECT phase FROM collect_runs WHERE id=?", (run_id,)).fetchone()["phase"],
            "details")

    def test_control_during_startup_never_changes_previous_run(self):
        old_run = get_db().execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('config','{}','{}','2026-01-01','2026-01-01','succeeded')"
        ).lastrowid
        get_db().commit()
        with collector._state_condition:
            collector._state.update({
                "running": True, "run_id": None, "current": "启动中",
                "phase": "starting", "paused": False,
            })

        result = collector.pause()

        self.assertFalse(result["ok"])
        self.assertFalse(result["paused"])
        self.assertEqual(get_db().execute(
            "SELECT paused FROM collect_runs WHERE id=?", (old_run,)).fetchone()["paused"], 0)
        current = collector.status()
        self.assertTrue(current["running"])
        self.assertIsNone(current["run_id"])

    def test_resume_thread_start_failure_rolls_back_run_and_state(self):
        scraper_python = self.root / "python"
        scraper_script = self.root / "scraper.py"
        scraper_python.touch()
        scraper_script.touch()

        task = collector._normalize_task({
            "type": "search", "keyword": "AI", "city": "深圳", "pages": 1,
        })
        run_id = collector._begin_run("config", {"tasks": [task]}, [task])
        # _begin_run 会把内存置为运行中；这里模拟该计划早已被中断、工作线程已退出。
        with collector._state_condition:
            collector._state.update({"running": False, "current": "", "phase": "",
                                     "run_id": None})
            collector._state_condition.notify_all()
        list_file = self.root / "boss_jobs_list.json"
        list_file.write_text('{"jobs": []}', encoding="utf-8")
        conn = get_db()
        conn.execute("UPDATE collect_run_tasks SET status='list_succeeded',list_file=? "
                     "WHERE run_id=?", (str(list_file), run_id))
        conn.execute("UPDATE collect_runs SET status='interrupted',finished_at=?,paused=0 "
                     "WHERE id=?", (collector.now_iso(), run_id))
        conn.commit()
        conn.execute(
            "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
            "VALUES('missing-jd','岗位','公司','active',?,?)",
            (collector.now_iso(), collector.now_iso()))
        conn.execute(
            "INSERT INTO job_run_items(run_id,job_key,source,created_at) "
            "VALUES(?,?,?,?)", (run_id, "missing-jd", "test", collector.now_iso()))
        conn.commit()

        with patch.object(collector, "SCRAPER_PY", scraper_python), \
                patch.object(collector, "SCRAPER_SCRIPT", scraper_script), \
                patch.object(collector.threading, "Thread",
                             side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                collector.resume_missing(source_run_id=run_id)

        self.assertFalse(collector._state["running"])
        self.assertIsNone(collector._state["run_id"])
        self.assertFalse(collector._state["paused"])
        self.assertIsNone(collector._state["process"])
        row = get_db().execute(
            "SELECT status,phase,finished_at,paused FROM collect_runs WHERE id=?",
            (run_id,)).fetchone()
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["phase"], "finished")
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(row["paused"], 0)
        # 没有新建采集计划
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM collect_runs").fetchone()["c"], 1)

    def test_external_stream_thread_cannot_replace_collector_process(self):
        self._begin_run()
        owner = _FakeProcess()
        external = _FakeProcess()
        with collector._state_condition:
            collector._state["worker_ident"] = threading.get_ident()
            collector._state["process"] = owner
        result = {}

        def register_external():
            result["controlled"] = collector._register_process(external)

        thread = threading.Thread(target=register_external)
        thread.start()
        thread.join(1)

        self.assertFalse(result["controlled"])
        self.assertIs(collector._state["process"], owner)

    def test_worker_thread_start_failure_finishes_run_and_clears_state(self):
        scraper_python = self.root / "python"
        scraper_script = self.root / "scraper.py"
        scraper_python.touch()
        scraper_script.touch()
        task = {"type": "search", "keyword": "AI", "city": "深圳", "pages": 1}

        with patch.object(collector, "SCRAPER_PY", scraper_python), \
                patch.object(collector, "SCRAPER_SCRIPT", scraper_script), \
                patch.object(threading.Thread, "start",
                             side_effect=RuntimeError("thread error")):
            with self.assertRaisesRegex(RuntimeError, "thread error"):
                collector.start("config", [task])

        run = get_db().execute(
            "SELECT status,phase,paused FROM collect_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((run["status"], run["phase"], run["paused"]),
                         ("failed", "finished", 0))
        self.assertFalse(collector._state["running"])
        self.assertFalse(collector._state["paused"])
        self.assertIsNone(collector._state["process"])
        self.assertIsNone(collector._state["worker_ident"])

    @unittest.skipUnless(os.name == "posix", "SIGSTOP/SIGCONT 仅适用于 Unix")
    def test_streamed_process_pause_does_not_consume_timeout(self):
        run_id = self._begin_run()
        ready = threading.Event()
        result = {}
        ticks = self.root / "ticks.txt"
        command = [
            sys.executable, "-u", "-c",
            "import pathlib,sys,time\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "print('ready', flush=True)\n"
            "with path.open('w', encoding='utf-8') as stream:\n"
            "    for _ in range(25):\n"
            "        stream.write('x')\n"
            "        stream.flush()\n"
            "        time.sleep(0.01)\n"
            "print('done', flush=True)\n",
            str(ticks),
        ]

        def on_output(line):
            if line == "ready":
                ready.set()

        def run_stream():
            try:
                with collector._state_condition:
                    collector._state["worker_ident"] = threading.get_ident()
                result["value"] = collector._run_scraper_streamed(
                    command, 0.35, os.environ.copy(), on_output=on_output)
            except Exception as error:  # pragma: no cover - 失败内容由主线程断言
                result["error"] = error

        thread = threading.Thread(target=run_stream, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))
        deadline = time.monotonic() + 1
        while (not ticks.exists() or ticks.stat().st_size < 3) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ticks.exists())
        try:
            collector.pause()
            time.sleep(0.05)
            paused_size = ticks.stat().st_size
            time.sleep(0.14)
            self.assertEqual(ticks.stat().st_size, paused_size)
            self.assertTrue(thread.is_alive())
        finally:
            collector.resume()
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result, result.get("error"))
        lines, returncode = result["value"]
        self.assertEqual(returncode, 0)
        self.assertEqual(lines, ["ready", "done"])
        self.assertIsNone(collector._state["process"])
        collector._finish_run(run_id, {}, "succeeded")


if __name__ == "__main__":
    unittest.main()
