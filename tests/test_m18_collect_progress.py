"""M18 测试：采集计划命名、JD 完整度与岗位级运行进度。"""
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m18-collect-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import collection_runs, collector, config, strategy  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402


class CollectProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m18-case-")
        self.root = Path(self.tmp.name)
        self.original_result_dir = collector.RESULT_DIR
        config.DATA_DIR = self.root
        config.DB_PATH = self.root / "copilot.db"
        config.COLLECT_RESULT_DIR = self.root / "job-result"
        collector.RESULT_DIR = config.COLLECT_RESULT_DIR
        init_db()
        self._reset_state()

    def tearDown(self):
        self._reset_state()
        collector.RESULT_DIR = self.original_result_dir
        self.tmp.cleanup()

    @staticmethod
    def _reset_state():
        with collector._state_condition:
            collector._state.update({
                "running": False, "current": "", "phase": "", "log": [],
                "run_id": None, "cancel": False, "risk_signal": "",
                "paused": False, "process": None, "pause_started_at": None,
                "paused_seconds": 0.0, "worker_ident": None,
                "fetch_details": False,
                "phase_started_at": None, "phase_pause_baseline": 0.0,
                "eta_model": None,
                "progress": collector._new_progress(),
            })
            collector._state_condition.notify_all()

    @staticmethod
    def _task(keyword="AI"):
        return collector._normalize_task({
            "type": "search", "keyword": keyword, "city": "深圳", "pages": 1,
        })

    @staticmethod
    def _insert_job(key, jd=None):
        conn = get_db()
        ts = now_iso()
        conn.execute(
            "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,'active',?,?)", (key, f"岗位{key}", "示例公司", ts, ts))
        if jd is not None:
            conn.execute(
                "INSERT INTO job_details(job_key,jd,fetched_at) VALUES(?,?,?)",
                (key, jd, ts))
        conn.commit()

    @staticmethod
    def _raw_job(key, title=None):
        return {
            "title": title or f"岗位{key}", "boss_name": "示例公司",
            "salary": "20-30K", "location": "深圳",
            "job_link": f"https://www.zhipin.com/job_detail/{key}.html",
        }

    def test_strategy_forwards_stream_callbacks(self):
        callback = object()
        cancelled = object()
        plan = {"searches": [{"keyword": "AI", "city": "深圳", "pages": 3}]}
        with patch.object(strategy.llm, "chat_json", return_value=plan) as chat_json:
            result = strategy.generate_plan(
                "完整简历" * 30, {}, on_delta=callback, cancelled=cancelled)
        self.assertEqual(result["searches"][0]["keyword"], "AI")
        self.assertIs(chat_json.call_args.kwargs["on_delta"], callback)
        self.assertIs(chat_json.call_args.kwargs["cancelled"], cancelled)

    def test_config_run_name_uses_date_resume_and_unique_run_id(self):
        resume = get_db().execute(
            "SELECT id FROM resumes WHERE is_default=1").fetchone()
        long_name = "嵌入式/人工智能求职简历超长名称用于截断验证"
        get_db().execute("UPDATE resumes SET name=? WHERE id=?", (long_name, resume["id"]))
        get_db().commit()

        names = []
        ids = []
        for keyword in ("AI", "固件"):
            task = self._task(keyword)
            run_id = collector._begin_run(
                "config", {"resume_id": resume["id"], "tasks": [task]}, [task])
            ids.append(run_id)
            params = json.loads(get_db().execute(
                "SELECT params FROM collect_runs WHERE id=?", (run_id,)).fetchone()["params"])
            names.append(params["name"])
            collector._finish_run(run_id, {}, "succeeded")

        expected_resume = re.sub(r"[\r\n\t/\\]+", "-", long_name)[:20]
        self.assertRegex(names[0], rf"^\d{{8}}-{re.escape(expected_resume)}-{ids[0]:04d}$")
        self.assertTrue(names[1].endswith(f"-{ids[1]:04d}"))
        self.assertNotEqual(names[0], names[1])

    def test_start_returns_authoritative_plan_name(self):
        fake_python = self.root / "python"
        fake_script = self.root / "scraper.py"
        fake_python.touch()
        fake_script.touch()
        resume = get_db().execute(
            "SELECT id FROM resumes WHERE is_default=1").fetchone()
        with patch.object(collector, "SCRAPER_PY", fake_python), \
                patch.object(collector, "SCRAPER_SCRIPT", fake_script), \
                patch.object(threading.Thread, "start", return_value=None):
            result = collector.start(
                "config", [self._task()], resume_id=resume["id"], fetch_details=False)
        params = json.loads(get_db().execute(
            "SELECT params FROM collect_runs WHERE id=?", (result["run_id"],)
        ).fetchone()["params"])
        self.assertEqual(result["name"], params["name"])
        collector._finish_run(result["run_id"], {}, "cancelled")

    def test_list_runs_splits_blank_and_complete_jd(self):
        self._insert_job("missing")
        self._insert_job("blank", " \n\t　 ")
        self._insert_job("complete", "负责嵌入式软件开发与设备联调。")
        conn = get_db()
        run_id = conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('config',?, '{}', ?, ?, 'succeeded')",
            (json.dumps({"name": "20260826-简历-0001"}, ensure_ascii=False),
             now_iso(), now_iso())).lastrowid
        legacy_id = conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('config','{}',?, ?, ?, 'succeeded')",
            (json.dumps({"total": 2}), now_iso(), now_iso())).lastrowid
        conn.commit()
        collection_runs.attach_jobs(run_id, ["missing", "blank", "complete"])

        listed = {item["id"]: item for item in collection_runs.list_runs()}
        current = listed[run_id]
        self.assertEqual(current["plan_name"], "20260826-简历-0001")
        self.assertTrue(current["completeness_known"])
        self.assertEqual(current["with_jd_count"], 1)
        self.assertEqual(current["list_only_count"], 2)
        self.assertRegex(listed[legacy_id]["plan_name"],
                         rf"^\d{{8}}-未命名简历-{legacy_id:04d}$")
        self.assertFalse(listed[legacy_id]["completeness_known"])
        self.assertIsNone(listed[legacy_id]["with_jd_count"])
        self.assertIsNone(listed[legacy_id]["list_only_count"])

    def test_stage_percent_eta_and_pause_are_same_unit(self):
        tasks = [self._task(str(index)) for index in range(4)]
        eta_model = collector._new_eta_model(tasks, {
            "list_seconds_per_page": 10,
            "detail_seconds_per_job": 5,
            "jobs_per_page": 3,
        })
        eta_model["list_samples"] = [{"seconds": 10, "pages": 1}]
        with collector._state_condition:
            collector._state.update({
                "running": True, "current": "AI / 深圳", "phase": "list",
                "run_id": 1, "cancel": False, "risk_signal": "", "log": [],
                "paused": False, "pause_started_at": None, "paused_seconds": 0.0,
                "phase_started_at": 100.0, "phase_pause_baseline": 0.0,
                "eta_model": eta_model,
                "progress": {**collector._new_progress(),
                             "list_total": 4, "list_completed": 1,
                             "jobs_discovered": 7},
            })
            with patch.object(collector, "ITEM_GAP_SEC", 0), \
                    patch.object(collector.time, "monotonic", return_value=110.0):
                list_progress = collector._state_snapshot()["progress"]
        self.assertEqual(list_progress["percent"], 25)
        self.assertEqual(list_progress["eta_seconds"], 30)
        self.assertEqual(list_progress["jobs_discovered"], 7)

        with collector._state_condition:
            collector._state["fetch_details"] = True
            with patch.object(collector, "ITEM_GAP_SEC", 0), \
                    patch.object(collector.time, "monotonic", return_value=110.0):
                weighted_list = collector._state_snapshot()["progress"]
        self.assertEqual(weighted_list["percent"], 12)
        self.assertEqual(weighted_list["eta_seconds"], 110)

        with collector._state_condition:
            detail_model = collector._new_eta_model(tasks, {
                "list_seconds_per_page": 10,
                "detail_seconds_per_job": 10,
                "jobs_per_page": 3,
            })
            detail_model.update({
                "detail_started_at": 200.0, "detail_pause_baseline": 0.0,
                "detail_last_at": 220.0, "detail_last_pause_baseline": 0.0,
                "detail_last_completed": 2,
            })
            collector._state.update({
                "phase": "details", "phase_started_at": 200.0,
                "phase_pause_baseline": 0.0,
                "eta_model": detail_model,
                "progress": {**collector._new_progress(),
                             "detail_total": 4, "detail_completed": 2,
                             "jobs_total": 6, "jd_total": 6, "jd_completed": 4},
            })
            with patch.object(collector.time, "monotonic", return_value=220.0):
                detail_progress = collector._state_snapshot()["progress"]
        self.assertEqual(detail_progress["percent"], 83)
        self.assertEqual(detail_progress["eta_seconds"], 20)
        self.assertEqual((detail_progress["jd_completed"], detail_progress["jd_total"]),
                         (4, 6))

    def test_interrupted_history_keeps_weighted_list_progress(self):
        self._insert_job("partial")
        conn = get_db()
        run_id = conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('config',?,'{}',?,?,'interrupted')",
            (json.dumps({"fetch_details": True}), now_iso(), now_iso())).lastrowid
        conn.commit()
        collection_runs.attach_jobs(run_id, ["partial"])
        run = conn.execute("SELECT * FROM collect_runs WHERE id=?", (run_id,)).fetchone()
        progress, _ = collector._finished_progress(run, [
            {"kind": "search", "status": "list_succeeded", "list_file": ""},
            {"kind": "search", "status": "queued", "list_file": ""},
        ], run_id)
        self.assertEqual(progress["percent"], 25)

    def test_eta_counts_remaining_gap_and_counts_down(self):
        tasks = [self._task("AI"), self._task("Agent")]
        model = collector._new_eta_model(tasks, {
            "list_seconds_per_page": 10,
            "detail_seconds_per_job": 5,
            "jobs_per_page": 3,
        })
        model.update({
            "gap_after_index": 0, "gap_started_at": 100.0,
            "gap_pause_baseline": 0.0,
            "list_samples": [{"seconds": 10, "pages": 1}],
        })
        with collector._state_condition:
            collector._state.update({
                "running": True, "phase": "list", "fetch_details": False,
                "paused": False, "pause_started_at": None, "paused_seconds": 0.0,
                "eta_model": model,
                "progress": {**collector._new_progress(),
                             "list_total": 2, "list_completed": 1},
            })
            with patch.object(collector, "ITEM_GAP_SEC", 120), \
                    patch.object(collector.time, "monotonic", return_value=130.0):
                first = collector._state_snapshot()["progress"]["eta_seconds"]
            with patch.object(collector, "ITEM_GAP_SEC", 120), \
                    patch.object(collector.time, "monotonic", return_value=150.0):
                later = collector._state_snapshot()["progress"]["eta_seconds"]
        self.assertEqual(first, 100)
        self.assertEqual(later, 80)

    def test_cancelled_list_eta_excludes_queued_tasks_and_future_gaps(self):
        tasks = [self._task("AI"), self._task("Agent")]
        model = collector._new_eta_model(tasks, {
            "list_seconds_per_page": 10,
            "detail_seconds_per_job": 10,
            "jobs_per_page": 10,
        })
        model.update({
            "current_task_index": 0, "current_task_started_at": 100.0,
            "current_task_pause_baseline": 0.0,
        })
        with collector._state_condition:
            collector._state.update({
                "running": True, "phase": "list", "fetch_details": True,
                "cancel": True, "paused": False, "pause_started_at": None,
                "paused_seconds": 0.0, "eta_model": model,
                "progress": {**collector._new_progress(),
                             "list_total": 2, "list_completed": 0},
            })
            with patch.object(collector, "ITEM_GAP_SEC", 120), \
                    patch.object(collector.time, "monotonic", return_value=105.0):
                eta = collector._state_snapshot()["progress"]["eta_seconds"]
        self.assertEqual(eta, 5)

    def test_failed_list_task_is_not_used_as_speed_sample(self):
        model = collector._new_eta_model([self._task("AI")], {
            "list_seconds_per_page": 10,
            "detail_seconds_per_job": 5,
            "jobs_per_page": 3,
        })
        with collector._state_condition:
            collector._state.update({
                "paused": False, "pause_started_at": None, "paused_seconds": 0.0,
                "eta_model": model,
            })
            with patch.object(collector.time, "monotonic", return_value=100.0):
                collector._eta_start_list_task(0)
            with patch.object(collector.time, "monotonic", return_value=160.0):
                collector._eta_finish_list_task(0, usable=False)
        self.assertEqual(model["list_samples"], [])

    def test_eta_startup_is_seeded_and_pause_freezes_countdown(self):
        task = self._task("AI")
        model = collector._new_eta_model([task], {
            "list_seconds_per_page": 60,
            "detail_seconds_per_job": 5,
            "jobs_per_page": 3,
        })
        model.update({
            "current_task_index": 0, "current_task_started_at": 300.0,
            "current_task_pause_baseline": 0.0,
        })
        with collector._state_condition:
            collector._state.update({
                "running": True, "phase": "list", "fetch_details": False,
                "paused": False, "pause_started_at": None, "paused_seconds": 0.0,
                "eta_model": model,
                "progress": {**collector._new_progress(),
                             "list_total": 1, "list_completed": 0},
            })
            with patch.object(collector.time, "monotonic", return_value=305.0):
                early = collector._state_snapshot()["progress"]["eta_seconds"]
            collector._state.update({"paused": True, "pause_started_at": 310.0})
            with patch.object(collector.time, "monotonic", return_value=320.0):
                paused_first = collector._state_snapshot()["progress"]["eta_seconds"]
            with patch.object(collector.time, "monotonic", return_value=350.0):
                paused_later = collector._state_snapshot()["progress"]["eta_seconds"]
        self.assertEqual(early, 55)
        self.assertEqual(paused_first, 50)
        self.assertEqual(paused_later, paused_first)

    def test_eta_includes_detail_work_before_phase_switch(self):
        task = self._task("AI")
        model = collector._new_eta_model([task], {
            "list_seconds_per_page": 10,
            "detail_seconds_per_job": 10,
            "jobs_per_page": 4,
        })
        model["list_samples"] = [{"seconds": 10, "pages": 1}]
        with collector._state_condition:
            collector._state.update({
                "running": True, "phase": "list", "fetch_details": True,
                "paused": False, "pause_started_at": None, "paused_seconds": 0.0,
                "eta_model": model,
                "progress": {**collector._new_progress(),
                             "list_total": 1, "list_completed": 1,
                             "jobs_discovered": 4, "jobs_total": 4,
                             "jd_total": 4, "jd_completed": 1},
            })
            with patch.object(collector.time, "monotonic", return_value=100.0):
                before = collector._state_snapshot()["progress"]["eta_seconds"]
            model.update({
                "detail_started_at": 100.0, "detail_pause_baseline": 0.0,
                "detail_last_at": 100.0, "detail_last_pause_baseline": 0.0,
            })
            collector._state.update({
                "phase": "details",
                "progress": {**collector._state["progress"],
                             "detail_total": 3, "detail_completed": 0},
            })
            with patch.object(collector.time, "monotonic", return_value=100.0):
                after = collector._state_snapshot()["progress"]["eta_seconds"]
        self.assertEqual(before, 30)
        self.assertEqual(after, before)

    def test_historical_timing_provides_robust_priors(self):
        conn = get_db()
        timing = {
            "list_active_seconds": 20, "list_page_units": 2,
            "detail_active_seconds": 60, "detail_completed": 3,
            "jobs_discovered": 12,
        }
        conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('config','{}',?,?,?,'succeeded')",
            (json.dumps({"timing": timing}), now_iso(), now_iso()))
        conn.commit()

        priors = collector._historical_eta_priors()

        self.assertEqual(priors["list_seconds_per_page"], 10)
        self.assertEqual(priors["detail_seconds_per_job"], 20)
        self.assertEqual(priors["jobs_per_page"], 6)

    def test_invalid_historical_timing_falls_back_without_crashing(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('config','{}',?,?,?,'succeeded')",
            (json.dumps({"timing": {
                "list_active_seconds": "Infinity", "list_page_units": 1,
                "detail_active_seconds": "NaN", "detail_completed": 2,
                "jobs_discovered": 999999,
            }}), now_iso(), now_iso()))
        conn.commit()

        priors = collector._historical_eta_priors()
        model = collector._new_eta_model([], {
            "list_seconds_per_page": float("inf"),
            "detail_seconds_per_job": float("nan"),
            "jobs_per_page": 999999,
        })

        self.assertEqual(
            priors["list_seconds_per_page"], collector.ETA_DEFAULT_LIST_SECONDS_PER_PAGE)
        self.assertEqual(
            priors["detail_seconds_per_job"], collector.ETA_DEFAULT_DETAIL_SECONDS_PER_JOB)
        self.assertEqual(priors["jobs_per_page"], collector.ETA_DEFAULT_JOBS_PER_PAGE)
        self.assertEqual(
            model["list_seconds_per_page"], collector.ETA_DEFAULT_LIST_SECONDS_PER_PAGE)
        self.assertEqual(
            model["detail_seconds_per_job"], collector.ETA_DEFAULT_DETAIL_SECONDS_PER_JOB)
        self.assertEqual(model["jobs_per_page"], collector.ETA_DEFAULT_JOBS_PER_PAGE)

    def test_partial_detail_timing_excludes_trailing_stall(self):
        model = collector._new_eta_model([self._task("AI")], {
            "list_seconds_per_page": 10,
            "detail_seconds_per_job": 10,
            "jobs_per_page": 3,
        })
        model.update({
            "detail_started_at": 100.0, "detail_pause_baseline": 0.0,
            "detail_samples": [8.0, 12.0],
        })
        with collector._state_condition:
            collector._state.update({
                "paused": False, "pause_started_at": None, "paused_seconds": 0.0,
                "eta_model": model,
                "progress": {**collector._new_progress(), "detail_completed": 2},
            })
            report = collector._timing_report_locked(now=200.0)
        self.assertEqual(report["detail_active_seconds"], 20)
        self.assertEqual(report["detail_completed"], 2)
        self.assertEqual(report["detail_phase_seconds"], 100)

    def test_finished_run_persists_pause_aware_timing_samples(self):
        task = self._task("AI")
        run_id = collector._begin_run(
            "config", {"fetch_details": False, "tasks": [task]}, [task])
        with collector._state_condition:
            collector._state["eta_model"]["list_samples"] = [
                {"seconds": 18.5, "pages": 1}]
            collector._state["progress"].update({
                "list_completed": 1, "jobs_discovered": 9,
            })

        collector._finish_run(run_id, {"items": []}, "succeeded")

        stats = json.loads(get_db().execute(
            "SELECT stats FROM collect_runs WHERE id=?", (run_id,)).fetchone()["stats"])
        self.assertEqual(stats["timing"]["list_active_seconds"], 18.5)
        self.assertEqual(stats["timing"]["list_page_units"], 1)
        self.assertEqual(stats["timing"]["jobs_discovered"], 9)

    def test_snapshot_job_keys_are_deduplicated_across_tasks(self):
        tasks = [self._task("AI"), self._task("Agent")]
        resume = get_db().execute(
            "SELECT id FROM resumes WHERE is_default=1").fetchone()
        run_id = collector._begin_run(
            "config", {"resume_id": resume["id"], "tasks": tasks}, tasks)

        def fake_scraper(args, timeout, cdp_port, output_path=None,
                         detail_output=None, on_output=None, on_snapshot=None):
            keyword = args[args.index("--keyword") + 1]
            unique = "one" if keyword == "AI" else "two"
            output_path.write_text(json.dumps({
                "keyword": keyword, "city": "深圳", "filters": {},
                "jobs": [self._raw_job(unique, keyword), self._raw_job("shared")],
            }, ensure_ascii=False), encoding="utf-8")
            if on_snapshot:
                on_snapshot(output_path)
            return "完成"

        with patch.object(collector.cdp, "account_for", return_value="collect"), \
                patch.object(collector.cdp, "launch", return_value={"ok": True}), \
                patch.object(collector, "_run_scraper", side_effect=fake_scraper), \
                patch.object(collector, "ITEM_GAP_SEC", 0):
            collector._worker(run_id, "config", tasks, False, False)

        progress = collector.status()["progress"]
        self.assertEqual(progress["jobs_discovered"], 3)
        self.assertEqual(progress["jobs_total"], 3)
        self.assertEqual(progress["jd_total"], 3)
        self.assertEqual(progress["jd_completed"], 0)
        self.assertEqual(progress["percent"], 100)
        self.assertEqual(len(collector._run_job_keys(run_id)), 3)


if __name__ == "__main__":
    unittest.main()
