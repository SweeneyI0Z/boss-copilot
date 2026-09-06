"""M16 岗位工作流与启用采集来源聚合。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m16-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import analytics, config, dashboard, workflow  # noqa: E402
from backend.db import close_db, get_db, init_db, now_iso  # noqa: E402
from backend.resumes import get_default_resume  # noqa: E402


class M16DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m16-case-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()
        self.resume = get_default_resume()

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    @staticmethod
    def add_job(job_key: str, favorite: bool = False, **values):
        ts = now_iso()
        fields = {
            "title": f"岗位 {job_key}", "company": "示例公司", "status": "active",
            "industry": "人工智能", "salary_min": 20, "salary_max": 30,
            "favorite_at": ts if favorite else None,
        }
        fields.update(values)
        columns = ["job_key", *fields, "first_seen_at", "last_seen_at"]
        args = [job_key, *fields.values(), ts, ts]
        get_db().execute(
            f"INSERT INTO jobs({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})", args)
        get_db().commit()

    @staticmethod
    def add_run(enabled: bool) -> int:
        ts = now_iso()
        run_id = get_db().execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status,"
            "enabled) VALUES('search','{}','{}',?,?,'succeeded',?)",
            (ts, ts, 1 if enabled else 0)).lastrowid
        get_db().commit()
        return run_id

    @staticmethod
    def attach(run_id: int, job_key: str, keyword: str = "", seen_at: str = ""):
        ts = seen_at or now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO job_run_items(run_id,job_key,source,created_at) "
            "VALUES(?,?,'test',?)", (run_id, job_key, ts))
        if keyword:
            conn.execute(
                "INSERT INTO job_collection_hits(run_id,job_key,search_key,keyword,"
                "city,city_code,is_active,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?,1,?,?)",
                (run_id, job_key, f"{run_id}:{keyword}", keyword,
                 "深圳", "101280600", ts, ts))
        conn.commit()

    def add_greeting_fact(self, job_key: str):
        ts = now_iso()
        get_db().execute(
            "INSERT INTO greetings(job_key,variants,status,resume_id,resume_revision,"
            "delivery_status,confirmed_at,created_at,updated_at) "
            "VALUES(?,'[]','sent',?,?,'confirmed',?,?,?)",
            (job_key, self.resume["id"], self.resume["revision"], ts, ts, ts))
        get_db().commit()

    def add_application_fact(self, job_key: str, status="platform_confirmed"):
        ts = now_iso()
        get_db().execute(
            "INSERT INTO applications(job_key,resume_id,status,confirmed_at,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?)",
            (job_key, self.resume["id"], status, ts, ts, ts))
        get_db().commit()

    def add_interview_fact(self, job_key: str):
        get_db().execute(
            "INSERT INTO interviews(job_key,status,transcript,report,created_at,resume_id,"
            "resume_revision) VALUES(?,'finished','[]','{}',?,?,?)",
            (job_key, now_iso(), self.resume["id"], self.resume["revision"]))
        get_db().commit()


class WorkflowTests(M16DatabaseTestCase):
    def test_batch_states_merge_manual_and_confirmed_facts(self):
        for key in ("greeting", "application", "interview", "manual"):
            self.add_job(key)
        self.add_greeting_fact("greeting")
        self.add_application_fact("application")
        self.add_interview_fact("interview")
        workflow.set_stage("manual", "offered", True, self.resume["id"])

        states = workflow.states_for_jobs(
            ["greeting", "application", "interview", "manual", "greeting"],
            self.resume["id"])

        self.assertEqual(states["greeting"], {
            "greeted": True, "applied": False, "interviewed": False,
            "offered": False})
        self.assertEqual(states["application"], {
            "greeted": True, "applied": True, "interviewed": False,
            "offered": False})
        self.assertEqual(states["interview"], {
            "greeted": True, "applied": True, "interviewed": True,
            "offered": False})
        self.assertEqual(states["manual"], {
            "greeted": True, "applied": True, "interviewed": True,
            "offered": True})
        self.assertEqual(workflow.states_for_jobs("greeting", self.resume["id"]),
                         {"greeting": states["greeting"]})

    def test_stage_progress_preserves_times_and_rollback_returns_effective_facts(self):
        self.add_job("flow")
        with patch.object(workflow, "now_iso", return_value="2026-08-01T10:00:00+08:00"):
            workflow.set_stage("flow", "greeted", True, self.resume["id"])
        with patch.object(workflow, "now_iso", return_value="2026-08-02T10:00:00+08:00"):
            advanced = workflow.set_stage(
                "flow", "offered", True, self.resume["id"])
        self.assertTrue(all(advanced[name] for name in workflow.STAGE_ORDER))
        row = get_db().execute(
            "SELECT * FROM job_workflow_states WHERE job_key='flow'").fetchone()
        self.assertEqual(row["greeted_at"], "2026-08-01T10:00:00+08:00")
        self.assertEqual(row["applied_at"], "2026-08-02T10:00:00+08:00")
        self.assertEqual(row["interviewed_at"], "2026-08-02T10:00:00+08:00")
        self.assertEqual(row["offered_at"], "2026-08-02T10:00:00+08:00")

        with patch.object(workflow, "now_iso", return_value="2026-08-03T10:00:00+08:00"):
            rolled_back = workflow.set_stage(
                "flow", "applied", False, self.resume["id"])
        self.assertEqual(rolled_back["greeted"], True)
        self.assertEqual(rolled_back["applied"], False)
        self.assertEqual(rolled_back["interviewed"], False)
        self.assertEqual(rolled_back["offered"], False)
        row = get_db().execute(
            "SELECT * FROM job_workflow_states WHERE job_key='flow'").fetchone()
        self.assertEqual(row["greeted_at"], "2026-08-01T10:00:00+08:00")
        self.assertIsNone(row["applied_at"])
        self.assertIsNone(row["interviewed_at"])
        self.assertIsNone(row["offered_at"])

        self.add_application_fact("flow")
        effective = workflow.set_stage("flow", "greeted", False, self.resume["id"])
        self.assertTrue(effective["greeted"])
        self.assertTrue(effective["applied"])
        self.assertFalse(effective["interviewed"])
        self.assertFalse(effective["offered"])

    def test_rollback_interview_clears_offer_but_preserves_earlier_stages(self):
        self.add_job("offer-rollback")
        workflow.set_stage("offer-rollback", "offered", True, self.resume["id"])

        state = workflow.set_stage(
            "offer-rollback", "interviewed", False, self.resume["id"])

        self.assertEqual(
            {name: state[name] for name in workflow.STAGE_ORDER},
            {"greeted": True, "applied": True, "interviewed": False,
             "offered": False})
        self.assertEqual(state["job_key"], "offer-rollback")
        self.assertEqual(state["resume_id"], self.resume["id"])
        self.assertTrue(state["updated_at"])
        row = get_db().execute(
            "SELECT * FROM job_workflow_states WHERE job_key='offer-rollback'").fetchone()
        self.assertIsNotNone(row["greeted_at"])
        self.assertIsNotNone(row["applied_at"])
        self.assertIsNone(row["interviewed_at"])
        self.assertIsNone(row["offered_at"])

    def test_init_db_adds_offer_column_to_legacy_workflow_table(self):
        self.add_job("legacy-workflow")
        conn = get_db()
        conn.execute("DROP TABLE job_workflow_states")
        conn.execute(
            "CREATE TABLE job_workflow_states ("
            "job_key TEXT NOT NULL, resume_id INTEGER NOT NULL, greeted_at TEXT, "
            "applied_at TEXT, interviewed_at TEXT, updated_at TEXT NOT NULL, "
            "PRIMARY KEY(job_key,resume_id))")
        conn.execute(
            "INSERT INTO job_workflow_states(job_key,resume_id,greeted_at,updated_at) "
            "VALUES('legacy-workflow',?,?,?)",
            (self.resume["id"], "2026-08-01T10:00:00+08:00", now_iso()))
        conn.commit()

        init_db()
        init_db()

        columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(job_workflow_states)")}
        self.assertIn("offered_at", columns)
        row = conn.execute(
            "SELECT * FROM job_workflow_states WHERE job_key='legacy-workflow'").fetchone()
        self.assertEqual(row["greeted_at"], "2026-08-01T10:00:00+08:00")
        self.assertIsNone(row["offered_at"])


class EnabledSourceAnalyticsTests(M16DatabaseTestCase):
    def test_default_filters_and_trends_exclude_disabled_sources(self):
        for key in ("enabled", "disabled", "shared", "legacy"):
            self.add_job(key)
        enabled_run = self.add_run(True)
        disabled_run = self.add_run(False)
        self.attach(enabled_run, "enabled", "启用关键词", "2026-08-24T10:00:00+08:00")
        self.attach(enabled_run, "shared", "共享关键词", "2026-08-25T10:00:00+08:00")
        self.attach(disabled_run, "disabled", "禁用关键词", "2026-08-22T10:00:00+08:00")
        self.attach(disabled_run, "shared", "禁用关键词", "2026-08-23T10:00:00+08:00")

        result = analytics.aggregate()

        self.assertEqual(result["summary"]["jobs"], 3)
        self.assertEqual(result["summary"]["active_relations"], 2)
        self.assertEqual(result["summary"]["reliable_jobs"], 2)
        self.assertEqual(result["summary"]["unattributed_jobs"], 1)
        self.assertEqual(result["meta"]["keywords"], ["共享关键词", "启用关键词"])
        self.assertEqual([item["label"] for item in result["trends"]],
                         ["2026-08-24", "2026-08-25"])
        self.assertEqual(
            analytics.aggregate({"keyword": "禁用关键词"})["summary"]["jobs"], 0)
        self.assertEqual(
            analytics.aggregate({"keyword": "启用关键词"})["summary"]["jobs"], 1)


class EnabledSourceDashboardTests(M16DatabaseTestCase):
    def test_metrics_obey_source_visibility_and_merge_manual_workflow(self):
        for key in ("enabled", "disabled", "legacy"):
            self.add_job(key, favorite=True)
        enabled_run = self.add_run(True)
        disabled_run = self.add_run(False)
        self.attach(enabled_run, "enabled")
        self.attach(disabled_run, "disabled")

        self.add_greeting_fact("enabled")
        self.add_application_fact("enabled")
        workflow.set_stage("enabled", "applied", True, self.resume["id"])
        self.add_greeting_fact("disabled")
        self.add_application_fact("disabled")
        workflow.set_stage("disabled", "applied", True, self.resume["id"])
        workflow.set_stage("legacy", "applied", True, self.resume["id"])

        metrics = dashboard.get_dashboard({"running": False})["metrics"]

        self.assertEqual(metrics["jobs"]["total"], 2)
        self.assertEqual(metrics["jobs"]["active"], 2)
        self.assertEqual(metrics["favorites"]["total"], 2)
        self.assertEqual(metrics["greetings"]["total"], 2)
        self.assertEqual(metrics["applications"]["total"], 2)
        self.assertEqual(metrics["applications"]["platform_confirmed"], 1)
        self.assertEqual(metrics["applications"]["manual_confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
