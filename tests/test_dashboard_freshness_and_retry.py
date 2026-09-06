"""看板采集新鲜度与采集记录续采：中断采集的数据不被误报过期，缺 JD 可在原计划上继续。"""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-fresh-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import collector, config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import collection_runs, dashboard  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402

init_db()


def _clear_data():
    # 全量回归时其他模块会改写全局 DATA_DIR/DB_PATH，这里每个用例重新指向本模块目录。
    config.DATA_DIR = Path(_TEST_HOME)
    config.DB_PATH = Path(_TEST_HOME) / "copilot.db"
    init_db()
    conn = get_db()
    for table in ("job_run_items", "collect_run_tasks", "job_details", "jobs",
                  "collect_runs"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _job(key, status="active", last_seen_at=None):
    ts = last_seen_at or now_iso()
    get_db().execute(
        "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
        "VALUES(?,?,?,?,?,?)", (key, "嵌入式软件工程师", "公司A", status, ts, ts))


def _jd(job_key, jd_text):
    get_db().execute(
        "INSERT INTO job_details(job_key,jd,fetched_at) VALUES(?,?,?)",
        (job_key, jd_text, now_iso()))


def _run(kind="config", status="interrupted", paused=0, finished=True,
         started_at=None, finished_at=None):
    started = started_at or now_iso()
    cur = get_db().execute(
        "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status,"
        "phase,paused) VALUES(?,?,?,?,?,?,?,?)",
        (kind, "{}", "{}", started,
         finished_at or (started if finished else None), status,
         "finished" if finished else "list", paused))
    get_db().commit()
    return cur.lastrowid


def _own(run_id, job_key):
    get_db().execute(
        "INSERT OR IGNORE INTO job_run_items(run_id,job_key,source,created_at) "
        "VALUES(?,?,?,?)", (run_id, job_key, "test", now_iso()))
    get_db().commit()


def _four_days_ago() -> str:
    moment = datetime.now(timezone.utc).astimezone() - timedelta(days=4)
    return moment.isoformat(timespec="seconds")


class DashboardFreshnessTests(unittest.TestCase):
    def setUp(self):
        _clear_data()

    def test_interrupted_run_with_fresh_jobs_is_not_stale(self):
        """被中断的采集已写入当天岗位数据时，看板不得报“3 天未更新”。"""
        run_id = _run(status="interrupted")
        _job("j1")
        _own(run_id, "j1")
        collection = dashboard.get_dashboard()["system"]["collection"]
        self.assertFalse(collection["stale"])
        self.assertTrue(collection["collected"])
        self.assertEqual(collection["hint"], "数据处于有效期内")

    def test_old_jobs_without_recent_collection_stay_stale(self):
        old = _four_days_ago()
        run_id = _run(status="succeeded", started_at=old, finished_at=old)
        _job("j1", last_seen_at=old)
        _own(run_id, "j1")
        collection = dashboard.get_dashboard()["system"]["collection"]
        self.assertTrue(collection["stale"])
        self.assertEqual(collection["hint"], "数据已超过 3 天，请更新")

    def test_empty_library_reports_not_collected(self):
        collection = dashboard.get_dashboard()["system"]["collection"]
        self.assertTrue(collection["stale"])
        self.assertFalse(collection["collected"])
        self.assertEqual(collection["hint"], "尚未采集数据")

    def test_disabled_source_data_does_not_count_as_fresh(self):
        run_id = _run(status="interrupted")
        _job("j1")
        _own(run_id, "j1")
        get_db().execute("UPDATE collect_runs SET enabled=0 WHERE id=?", (run_id,))
        get_db().commit()
        collection = dashboard.get_dashboard()["system"]["collection"]
        self.assertTrue(collection["stale"])


class RunMissingJdCountTests(unittest.TestCase):
    def setUp(self):
        _clear_data()

    def test_list_runs_counts_eligible_jobs_without_jd(self):
        run_id = _run(status="partial")
        _job("j1")
        _job("j2")
        _job("j3", status="excluded")
        _jd("j2", "岗位描述")
        for key in ("j1", "j2", "j3"):
            _own(run_id, key)
        items = collection_runs.list_runs()
        item = next(row for row in items if row["id"] == run_id)
        self.assertEqual(item["missing_jd_count"], 1)
        self.assertEqual(item["owned_item_count"], 3)
        self.assertEqual(item["with_jd_count"], 1)

    def test_run_without_jobs_has_zero_missing(self):
        run_id = _run(status="succeeded")
        item = next(row for row in collection_runs.list_runs() if row["id"] == run_id)
        self.assertEqual(item["missing_jd_count"], 0)


class StalePausedRunTests(unittest.TestCase):
    def setUp(self):
        _clear_data()
        with collector._state_condition:
            collector._state.update({"running": False, "run_id": None,
                                     "paused": False})

    def test_resume_marks_stale_paused_run_interrupted(self):
        """内存无运行任务时，“暂停中”的遗留记录在继续动作时被标记为中断。"""
        run_id = _run(status="running", paused=1, finished=False)
        result = collector.resume()
        self.assertTrue(result["ok"])
        self.assertFalse(result["running"])
        row = get_db().execute(
            "SELECT status,paused,finished_at,phase FROM collect_runs WHERE id=?",
            (run_id,)).fetchone()
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["paused"], 0)
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(row["phase"], "finished")

    def test_cancel_marks_stale_paused_run_interrupted(self):
        run_id = _run(status="running", paused=1, finished=False)
        result = collector.cancel()
        self.assertTrue(result["ok"])
        self.assertFalse(result["running"])
        row = get_db().execute(
            "SELECT status,paused FROM collect_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["paused"], 0)

    def test_resume_keeps_unaffected_runs(self):
        """没有残留状态时不改写任何记录。"""
        run_id = _run(status="succeeded", paused=0, finished=True)
        collector.resume()
        row = get_db().execute(
            "SELECT status,paused,finished_at FROM collect_runs WHERE id=?",
            (run_id,)).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["paused"], 0)
        self.assertIsNotNone(row["finished_at"])


class ResumeMissingTests(unittest.TestCase):
    """继续采集：不新建计划，在原记录上恢复运行并沿原进度补齐缺失 JD。"""

    def setUp(self):
        _clear_data()
        with collector._state_condition:
            collector._state.update({
                "running": False, "run_id": None, "cancel": False, "paused": False,
                "process": None, "worker_ident": None,
            })

    def tearDown(self):
        with collector._state_condition:
            collector._state.update({"running": False, "run_id": None,
                                     "cancel": False, "paused": False,
                                     "process": None})
            collector._state_condition.notify_all()

    def _seed_resumable_run(self):
        run_id = _run(status="interrupted")
        _job("j1")
        _job("j2")
        _jd("j2", "已有描述")
        _own(run_id, "j1")
        _own(run_id, "j2")
        list_file = Path(_TEST_HOME) / "boss_jobs_resume.json"
        list_file.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        cur = get_db().execute(
            "INSERT INTO collect_run_tasks(run_id,task_key,kind,keyword,status,list_file) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, "search:t1", "search", "kw", "list_succeeded", str(list_file)))
        get_db().commit()
        return run_id, cur.lastrowid

    def _wait_worker_done(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with collector._state_condition:
                if not collector._state["running"]:
                    return True
            time.sleep(0.02)
        return False

    def test_resume_continues_original_plan_without_new_record(self):
        run_id, task_id = self._seed_resumable_run()
        with patch.object(collector.cdp, "account_for", return_value="collect"), \
                patch.object(collector.cdp, "launch", return_value={"ok": True}), \
                patch.object(collector, "_run_detail_phase",
                             return_value=("", "")) as phase:
            result = collector.resume_missing(run_id)
            self.assertTrue(result["ok"])
            self.assertEqual(result["run_id"], run_id)
            # 未新建采集计划，原计划进入运行中的详情阶段
            self.assertEqual(get_db().execute(
                "SELECT COUNT(*) c FROM collect_runs").fetchone()["c"], 1)
            row = get_db().execute(
                "SELECT status,phase,finished_at FROM collect_runs WHERE id=?",
                (run_id,)).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["phase"], "details")
            self.assertIsNone(row["finished_at"])
            self.assertTrue(collector._state["running"])
            self.assertEqual(collector._state["run_id"], run_id)
            # 进度条沿原计划继续：1/2 已有 JD → 75%
            with collector._state_condition:
                progress = collector._progress_snapshot_locked()
            self.assertEqual(progress["percent"], 75)
            self.assertTrue(self._wait_worker_done())
            # 续跑任务复用原计划的任务与列表文件（异步线程完成后才可断言）
            args, _ = phase.call_args
            self.assertEqual(args[0], run_id)
            self.assertEqual(args[2], [task_id])
        row = get_db().execute(
            "SELECT status,finished_at FROM collect_runs WHERE id=?",
            (run_id,)).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertIsNotNone(row["finished_at"])
        self.assertFalse(collector._state["running"])

    def test_resume_rejects_when_nothing_missing(self):
        run_id = _run(status="interrupted")
        _job("j1")
        _jd("j1", "完整描述")
        _own(run_id, "j1")
        result = collector.resume_missing(run_id)
        self.assertFalse(result["ok"])
        row = get_db().execute(
            "SELECT status,finished_at FROM collect_runs WHERE id=?",
            (run_id,)).fetchone()
        self.assertEqual(row["status"], "interrupted")
        self.assertIsNotNone(row["finished_at"])
        self.assertFalse(collector._state["running"])

    def test_resume_rejects_while_another_task_running(self):
        run_id, _ = self._seed_resumable_run()
        with collector._state_condition:
            collector._state["running"] = True
        try:
            result = collector.resume_missing(run_id)
        finally:
            with collector._state_condition:
                collector._state["running"] = False
        self.assertFalse(result["ok"])
        self.assertIn("已有采集任务在运行", result["error"])

    def test_resume_rejects_unfinished_run(self):
        run_id = _run(status="running", paused=0, finished=False)
        result = collector.resume_missing(run_id)
        self.assertFalse(result["ok"])
        self.assertFalse(collector._state["running"])
        row = get_db().execute(
            "SELECT status FROM collect_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "running")


if __name__ == "__main__":
    unittest.main()
