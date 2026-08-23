"""M9 数据底座：多简历、不可变基线、岗位状态、投递与看板统计。"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m9-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import applications, dashboard, job_state, resumes  # noqa: E402
from backend.db import get_db, init_db, now_iso, set_setting  # noqa: E402


def _clear_data():
    conn = get_db()
    for table in ("job_collection_hits", "collect_run_tasks", "applications",
                  "job_resume_scores", "job_score_baselines", "greetings", "interviews",
                  "sent_log", "job_details", "jobs", "resumes", "collect_runs",
                  "account_states"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("UPDATE profile SET resume_text='', expectations='{}', updated_at=? WHERE id=1",
                 (now_iso(),))
    conn.commit()
    init_db()


def _job(key="j1", status="active", company="公司A"):
    ts = now_iso()
    get_db().execute(
        "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
        "VALUES(?,?,?,?,?,?)", (key, "AI应用工程师", company, status, ts, ts))
    get_db().commit()


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        init_db()
        _clear_data()

    def test_schema_and_init_are_idempotent(self):
        init_db()
        init_db()
        conn = get_db()
        for table in ("resumes", "job_resume_scores", "job_score_baselines",
                      "applications", "collect_run_tasks", "job_collection_hits"):
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM resumes").fetchone()["c"], 1)

    def test_legacy_profile_scores_greeting_and_interview_migrate_once(self):
        conn = get_db()
        conn.execute("DELETE FROM resumes")
        conn.execute("DELETE FROM settings WHERE key='legacy_score_migration_completed'")
        conn.execute(
            "UPDATE profile SET resume_text=?, expectations=?",
            ("旧版简历正文", '{"city":"深圳"}'))
        _job("legacy")
        conn.execute(
            "UPDATE jobs SET l1_score=71, l1_detail=?, match_rough=80, "
            "composite_rough=75, composite=82, priority='P0', l2_source='imported' "
            "WHERE job_key='legacy'", ('{"source":"imported"}',))
        conn.execute(
            "INSERT INTO greetings(job_key,variants,status,sent_at,created_at) "
            "VALUES('legacy','[]','sent',?,?)", (now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO interviews(job_key,status,transcript,report,created_at) "
            "VALUES('legacy','active','[]','',?)", (now_iso(),))
        conn.commit()

        init_db()
        init_db()
        default = resumes.get_default_resume()
        self.assertEqual(default["resume_text"], "旧版简历正文")
        self.assertEqual(default["expectations"], {"city": "深圳"})
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM job_resume_scores").fetchone()["c"], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM job_score_baselines").fetchone()["c"], 1)
        greeting = conn.execute("SELECT * FROM greetings").fetchone()
        self.assertEqual(greeting["resume_id"], default["id"])
        self.assertEqual(greeting["delivery_channel"], "auto")
        self.assertEqual(greeting["delivery_status"], "confirmed")
        interview = conn.execute("SELECT * FROM interviews").fetchone()
        self.assertEqual(interview["resume_id"], default["id"])


class ResumeTests(unittest.TestCase):
    def setUp(self):
        init_db()
        _clear_data()
        _job()

    def test_revision_isolated_scores_and_legacy_profile_mirror(self):
        default = resumes.get_default_resume()
        resumes.save_job_score(
            "j1", default["id"], l1_score=70, l1_detail={"engine": "l1"},
            composite=80, l2_source="llm")
        changed = resumes.update_resume(
            default["id"], resume_text="新的简历正文", expectations={"city": "深圳"})
        self.assertEqual(changed["revision"], 2)
        old_score = resumes.get_job_score("j1", default["id"], 1)
        self.assertTrue(old_score["l2_stale"])
        self.assertIsNone(resumes.get_job_score("j1", default["id"], 2))
        profile = get_db().execute("SELECT * FROM profile WHERE id=1").fetchone()
        self.assertEqual(profile["resume_text"], "新的简历正文")

    def test_restart_never_migrates_legacy_score_into_new_revision(self):
        default = resumes.get_default_resume()
        resumes.save_job_score(
            "j1", default["id"], l1_score=70, composite=80, l2_source="llm")
        changed = resumes.update_resume(default["id"], resume_text="第二版简历正文")
        self.assertEqual(changed["revision"], 2)
        init_db()
        self.assertIsNone(resumes.get_job_score("j1", default["id"], 2))
        self.assertEqual(resumes.get_job_score("j1", default["id"], 1)["composite"], 80)

    def test_restart_never_moves_new_default_scores_to_legacy_resume(self):
        legacy = resumes.get_default_resume()
        second = resumes.create_resume("第二份", "Python 项目", make_default=True)
        _job("j2")
        resumes.save_job_score("j2", second["id"], l1_score=92, composite=92,
                               l2_source="llm")
        init_db()
        self.assertIsNone(resumes.get_job_score("j2", legacy["id"], 1))
        self.assertEqual(resumes.get_job_score("j2", second["id"], 1)["composite"], 92)

    def test_metadata_update_does_not_increment_revision(self):
        default = resumes.get_default_resume()
        changed = resumes.update_resume(
            default["id"], name="主简历", boss_resume_label="BOSS在线简历")
        self.assertEqual(changed["revision"], default["revision"])

    def test_plain_text_profile_fields_are_preserved(self):
        resume = resumes.create_resume(
            "文本画像", "正文", expectations="深圳，期望30K",
            skill_profile="Python，STM32\nAgent")
        self.assertEqual(resume["expectations"], {"notes": "深圳，期望30K"})
        self.assertEqual(resume["skill_profile"],
                         {"skills": ["Python", "STM32", "Agent"]})

    def test_missing_skill_profile_is_derived_from_resume_text(self):
        resume = resumes.create_resume(
            "自动画像", "使用 Python、FastAPI 开发 Agent，并负责 STM32 固件")
        self.assertTrue(resume["skill_profile"]["derived"])
        self.assertIn("Python", resume["skill_profile"]["ai_soft"])
        self.assertIn("STM32", resume["skill_profile"]["embedded"])

    def test_derived_profile_refreshes_when_resume_text_changes(self):
        resume = resumes.create_resume("自动画像", "Python FastAPI Agent")
        changed = resumes.update_resume(
            resume["id"], resume_text="STM32 固件 嵌入式",
            skill_profile=resume["skill_profile"])
        self.assertNotIn("ai_soft", changed["skill_profile"])
        self.assertIn("STM32", changed["skill_profile"]["embedded"])

    def test_manual_profile_edit_overrides_automatic_derivation(self):
        resume = resumes.create_resume("自动画像", "Python FastAPI Agent")
        changed = resumes.update_resume(
            resume["id"], resume_text="STM32 固件",
            skill_profile={**resume["skill_profile"], "skills": ["自定义能力"]})
        self.assertNotIn("derived", changed["skill_profile"])
        self.assertEqual(changed["skill_profile"]["skills"], ["自定义能力"])

    def test_json_array_profile_input_is_preserved(self):
        resume = resumes.create_resume(
            "数组画像", "正文", expectations='["深圳", "30K"]',
            skill_profile='["Python", "STM32"]')
        self.assertEqual(resume["expectations"], {"notes": ["深圳", "30K"]})
        self.assertEqual(resume["skill_profile"], {"skills": ["Python", "STM32"]})

    def test_skill_profile_rejects_non_string_array_items(self):
        with self.assertRaisesRegex(ValueError, "只接受字符串"):
            resumes.create_resume("错误画像", "正文", skill_profile=["STM32", 51])

    def test_default_switch_and_soft_archive(self):
        first = resumes.get_default_resume()
        second = resumes.create_resume("AI方向", "第二份", make_default=True)
        self.assertEqual(resumes.get_default_resume()["id"], second["id"])
        archived = resumes.archive_resume(second["id"])
        self.assertTrue(archived["archived"])
        self.assertEqual(resumes.get_default_resume()["id"], first["id"])
        with self.assertRaises(ValueError):
            resumes.archive_resume(first["id"])

    def test_imported_baseline_is_write_once(self):
        first = resumes.save_imported_baseline("j1", l1_score=70,
                                               l1_detail={"source": "imported"})
        second = resumes.save_imported_baseline("j1", l1_score=10)
        self.assertEqual(first["l1_score"], 70)
        self.assertEqual(second["l1_score"], 70)
        with self.assertRaises(sqlite3.IntegrityError):
            get_db().execute(
                "UPDATE job_score_baselines SET l1_score=1 WHERE job_key='j1'")
        get_db().rollback()

    def test_engine_score_does_not_overwrite_legacy_imported_columns(self):
        conn = get_db()
        conn.execute("UPDATE jobs SET l1_score=70, composite=80 WHERE job_key='j1'")
        conn.commit()
        resumes.save_imported_baseline("j1", l1_score=70, composite=80)
        resumes.save_job_score("j1", l1_score=10, composite=20,
                               l1_source="engine", l2_source="llm")
        row = conn.execute(
            "SELECT l1_score, composite FROM jobs WHERE job_key='j1'").fetchone()
        self.assertEqual((row["l1_score"], row["composite"]), (70, 80))
        current = resumes.get_job_score("j1")
        self.assertEqual((current["l1_score"], current["composite"]), (10, 20))


class JobStateTests(unittest.TestCase):
    def setUp(self):
        init_db()
        _clear_data()
        _job(status="delisted", company="某科技公司")

    def test_favorite_exclude_and_restore_previous_status(self):
        self.assertTrue(job_state.favorite_job("j1")["favorite"])
        excluded = job_state.exclude_job("j1")
        self.assertEqual(excluded["status"], "excluded")
        self.assertFalse(excluded["favorite"])
        restored = job_state.restore_job("j1")
        self.assertEqual(restored["status"], "delisted")

    def test_headhunter_detection_and_manual_three_state_override(self):
        auto = job_state.update_headhunter_detection("j1")
        self.assertTrue(auto["effective_headhunter"])
        manual = job_state.set_headhunter_override("j1", False)
        self.assertFalse(manual["effective_headhunter"])
        reset = job_state.set_headhunter_override("j1", None)
        self.assertTrue(reset["effective_headhunter"])
        self.assertEqual(job_state.detect_headhunter("普通公司", "资深猎头")[0], True)


class ApplicationAndDashboardTests(unittest.TestCase):
    def setUp(self):
        init_db()
        _clear_data()
        _job()
        set_setting("send_halted_day", "")
        set_setting("send_halt_reason", "")

    def test_probe_failure_never_downgrades_confirmation(self):
        confirmed = applications.record_probe_result(
            "j1", platform_confirmed=True, evidence="页面显示已投递")
        self.assertEqual(confirmed["status"], "platform_confirmed")
        failed = applications.record_probe_result(
            "j1", platform_confirmed=None, error="页面结构变化")
        self.assertEqual(failed["status"], "platform_confirmed")
        self.assertEqual(failed["probe_error"], "页面结构变化")

    def test_manual_confirmation_is_bound_to_resume(self):
        second = resumes.create_resume("第二份")
        applications.confirm_application("j1", second["id"])
        self.assertEqual(applications.get_application(
            "j1", second["id"])["status"], "manual_confirmed")
        self.assertEqual(applications.get_application("j1")["status"], "unknown")

    def test_dashboard_counts_each_confirmed_resume_delivery(self):
        default = resumes.get_default_resume()
        second = resumes.create_resume("第二份")
        applications.confirm_application("j1", default["id"])
        applications.confirm_application("j1", second["id"])
        metrics = dashboard.get_dashboard()["metrics"]["applications"]
        self.assertEqual(metrics["total"], 2)
        self.assertEqual(metrics["manual_confirmed"], 2)

    def test_dashboard_counts_confirmed_facts_and_cached_status(self):
        default = resumes.get_default_resume()
        job_state.favorite_job("j1")
        applications.confirm_application("j1", default["id"])
        ts = now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO greetings(job_key,variants,status,resume_id,resume_revision,"
            "delivery_channel,delivery_status,confirmed_at,created_at,updated_at) "
            "VALUES('j1','[]','sent',?,?,'manual','confirmed',?,?,?)",
            (default["id"], default["revision"], ts, ts, ts))
        conn.execute(
            "INSERT INTO account_states(account,logged_in,hint,checked_at) "
            "VALUES('account_a',1,'已登录',?)", (ts,))
        conn.execute(
            "INSERT INTO account_states(account,logged_in,hint,checked_at) "
            "VALUES('collect',1,'已登录',?)", (ts,))
        conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('search','{}','{}',?,?, 'succeeded')", (ts, ts))
        conn.commit()
        data = dashboard.get_dashboard({"running": False})
        self.assertEqual(data["metrics"]["jobs"]["total"], 1)
        self.assertEqual(data["metrics"]["favorites"]["total"], 1)
        self.assertEqual(data["metrics"]["applications"]["total"], 1)
        self.assertEqual(data["metrics"]["greetings"]["total"], 1)
        self.assertEqual(data["system"]["color"], "green")

    def test_dashboard_file_freshness_prefers_source_time_over_import_time(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status,"
            "data_source_at) VALUES('xlsx_import','{}','{}',?,?, 'succeeded',?)",
            (now_iso(), now_iso(), "2020-01-01T00:00:00+08:00"))
        conn.commit()
        collection = dashboard.get_dashboard()["system"]["collection"]
        self.assertEqual(collection["file_data_at"], "2020-01-01T00:00:00+08:00")
        self.assertTrue(collection["stale"])


if __name__ == "__main__":
    unittest.main()
