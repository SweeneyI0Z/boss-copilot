"""M17 采集来源恢复与二次确认物理删除。"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


# 测试库必须在导入 backend 前隔离，绝不接触真实数据。
_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m17-collection-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import collection_runs, config  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402


class CollectionDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m17-delete-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        config.COLLECT_RESULT_DIR = root / "job-result"
        config.COLLECT_RESULT_DIR.mkdir(parents=True)
        init_db()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def add_job(job_key: str, source: str = "search") -> None:
        ts = now_iso()
        get_db().execute(
            "INSERT INTO jobs(job_key,title,company,source,status,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,'active',?,?)",
            (job_key, f"岗位-{job_key}", "示例公司", source, ts, ts))
        get_db().commit()

    @staticmethod
    def add_run(kind="search", *, params=None, stats=None, status="succeeded",
                finished=True, paused=False) -> int:
        ts = now_iso()
        cursor = get_db().execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status,"
            "phase,paused) VALUES(?,?,?,?,?,?,?,?)",
            (kind, json.dumps(params or {}, ensure_ascii=False),
             json.dumps(stats or {}, ensure_ascii=False), ts, ts if finished else None,
             status, "finished" if finished else "list", 1 if paused else 0))
        get_db().commit()
        return cursor.lastrowid

    @staticmethod
    def raw_job(job_key: str) -> dict:
        return {
            "title": f"岗位-{job_key}", "boss_name": "示例公司", "salary": "20-30K",
            "job_link": f"https://www.zhipin.com/job_detail/{job_key}.html",
        }

    def test_recover_stats_keys_and_migrated_json_files_is_idempotent(self):
        for key in ("from-stats", "json-a", "json-b"):
            self.add_job(key, "import" if key == "from-stats" else "search")
        stats_run = self.add_run(
            "json_import", stats={"total": 1, "job_keys": ["from-stats"]})

        first = config.COLLECT_RESULT_DIR / "boss_jobs_legacy_first.json"
        second = config.COLLECT_RESULT_DIR / "boss_company_jobs_legacy_second.json"
        first.write_text(json.dumps(
            {"jobs": [self.raw_job("json-a")]}, ensure_ascii=False), encoding="utf-8")
        second.write_text(json.dumps(
            {"jobs": [self.raw_job("json-a"), self.raw_job("json-b")]},
            ensure_ascii=False), encoding="utf-8")
        json_run = self.add_run(
            "json_import", params={"dir": "/已经迁移且不存在的旧目录"},
            stats={"total": 3, "files": [first.name, second.name]})

        listed = {item["id"]: item for item in collection_runs.list_runs()}
        self.assertTrue(listed[stats_run]["has_source_ownership"])
        self.assertEqual(listed[stats_run]["owned_item_count"], 1)
        self.assertTrue(listed[json_run]["has_source_ownership"])
        self.assertEqual(listed[json_run]["owned_item_count"], 2)
        self.assertEqual({row["job_key"] for row in get_db().execute(
            "SELECT job_key FROM job_run_items WHERE run_id=?", (json_run,))},
            {"json-a", "json-b"})
        self.assertEqual(collection_runs.recover_legacy_ownership()["relations_added"], 0)

    def test_recover_xlsx_from_unique_complete_fingerprint(self):
        for key in ("xlsx-a", "xlsx-b"):
            self.add_job(key, "import")
        fingerprint = {"total": 2, "with_jd": 2, "with_l2": 1}
        source = self.add_run(
            "xlsx_import", stats={**fingerprint, "created": 0, "updated": 2})
        target = self.add_run(
            "xlsx_import", stats={**fingerprint, "created": 2, "updated": 0})
        different = self.add_run(
            "xlsx_import", stats={"total": 2, "with_jd": 2, "with_l2": 0})
        collection_runs.attach_jobs(source, ["xlsx-a", "xlsx-b"], "exact")

        collection_runs.recover_legacy_ownership()

        target_keys = {row["job_key"] for row in get_db().execute(
            "SELECT job_key FROM job_run_items WHERE run_id=?", (target,))}
        self.assertEqual(target_keys, {"xlsx-a", "xlsx-b"})
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) count FROM job_run_items WHERE run_id=?",
            (different,)).fetchone()["count"], 0)

    def add_exclusive_children(self, job_key: str, run_id: int) -> None:
        conn = get_db()
        ts = now_iso()
        resume = conn.execute(
            "SELECT id,revision FROM resumes WHERE is_default=1").fetchone()
        conn.execute(
            "INSERT INTO job_details(job_key,jd,fetched_at) VALUES(?,?,?)",
            (job_key, "独占岗位 JD", ts))
        conn.execute(
            "INSERT INTO greetings(job_key,variants,status,created_at,resume_id,"
            "resume_revision) VALUES(?,?,'sent',?,?,?)",
            (job_key, '["你好"]', ts, resume["id"], resume["revision"]))
        conn.execute(
            "INSERT INTO job_score_baselines(job_key,imported_at) VALUES(?,?)",
            (job_key, ts))
        conn.execute(
            "INSERT INTO interviews(job_key,status,transcript,created_at,resume_id,"
            "resume_revision) VALUES(?,'finished','[]',?,?,?)",
            (job_key, ts, resume["id"], resume["revision"]))
        conn.execute(
            "INSERT INTO conversations(boss_key,boss_name,job_key) VALUES(?,?,?)",
            (f"boss-{job_key}", "招聘者", job_key))
        conn.execute(
            "INSERT INTO sent_log(day,job_key,company,ok,created_at) "
            "VALUES(date('now','localtime'),?,'示例公司',1,?)", (job_key, ts))
        conn.execute(
            "INSERT INTO job_resume_scores(job_key,resume_id,resume_revision,created_at,"
            "updated_at) VALUES(?,?,?,?,?)",
            (job_key, resume["id"], resume["revision"], ts, ts))
        conn.execute(
            "INSERT INTO applications(job_key,resume_id,status,created_at,updated_at) "
            "VALUES(?,?,'manual_confirmed',?,?)", (job_key, resume["id"], ts, ts))
        conn.execute(
            "INSERT INTO job_workflow_states(job_key,resume_id,greeted_at,updated_at) "
            "VALUES(?,?,?,?)", (job_key, resume["id"], ts, ts))
        conn.execute(
            "INSERT INTO job_favorite_hits(job_key,account,first_seen_at,last_seen_at) "
            "VALUES(?,'collect',?,?)", (job_key, ts, ts))
        task_id = conn.execute(
            "INSERT INTO collect_run_tasks(run_id,task_key,status) VALUES(?,?,'succeeded')",
            (run_id, f"task-{job_key}")).lastrowid
        conn.execute(
            "INSERT INTO job_collection_hits(task_id,run_id,job_key,search_key,is_active,"
            "first_seen_at,last_seen_at) VALUES(?,?,?,?,1,?,?)",
            (task_id, run_id, job_key, f"search-{job_key}", ts, ts))
        conn.commit()

    def test_delete_removes_exclusive_job_and_preserves_shared_job_and_guardrail_log(self):
        for key in ("exclusive", "shared"):
            self.add_job(key)
        target = self.add_run(stats={"total": 2})
        other = self.add_run(stats={"total": 1})
        collection_runs.attach_jobs(target, ["exclusive", "shared"], "target")
        collection_runs.attach_jobs(other, ["shared"], "other")
        self.add_exclusive_children("exclusive", target)
        get_db().execute(
            "INSERT INTO job_details(job_key,jd,fetched_at) VALUES('shared','共享 JD',?)",
            (now_iso(),))
        get_db().commit()

        preview = collection_runs.preview_delete(target)
        self.assertEqual(preview["exclusive_job_count"], 1)
        self.assertEqual(preview["shared_job_count"], 1)
        self.assertEqual(preview["unattributed_job_count"], 0)
        self.assertEqual(preview["exclusive_jobs"], 1)
        self.assertEqual(preview["shared_jobs"], 1)

        result = collection_runs.delete_run(target, target)

        self.assertEqual(result["deleted_jobs"], 1)
        self.assertEqual(result["preserved_shared_jobs"], 1)
        self.assertIsNone(get_db().execute(
            "SELECT 1 FROM collect_runs WHERE id=?", (target,)).fetchone())
        self.assertIsNone(get_db().execute(
            "SELECT 1 FROM jobs WHERE job_key='exclusive'").fetchone())
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM jobs WHERE job_key='shared'").fetchone())
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM job_details WHERE job_key='shared'").fetchone())
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM job_run_items WHERE run_id=? AND job_key='shared'",
            (other,)).fetchone())
        for table in ("job_details", "greetings", "job_score_baselines", "interviews",
                      "job_resume_scores", "applications", "job_workflow_states",
                      "job_favorite_hits", "job_collection_hits"):
            self.assertEqual(get_db().execute(
                f"SELECT COUNT(*) count FROM {table} WHERE job_key='exclusive'"
            ).fetchone()["count"], 0, table)
        self.assertIsNone(get_db().execute(
            "SELECT job_key FROM conversations WHERE boss_key='boss-exclusive'"
        ).fetchone()["job_key"])
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) count FROM sent_log WHERE job_key='exclusive'"
        ).fetchone()["count"], 1)
        self.assertEqual(get_db().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unattributed_legacy_run_deletes_only_record_with_warning(self):
        self.add_job("unattributed")
        run_id = self.add_run("sync", stats={"total": 7})

        preview = collection_runs.preview_delete(run_id)
        self.assertEqual(preview["exclusive_job_count"], 0)
        self.assertEqual(preview["unattributed_job_count"], 7)

        result = collection_runs.delete_run(run_id, run_id)
        self.assertEqual(result["deleted_jobs"], 0)
        self.assertEqual(result["unattributed_jobs"], 7)
        self.assertTrue(result["warning"])
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM jobs WHERE job_key='unattributed'").fetchone())

    def test_delete_requires_matching_confirmation_and_finished_unreferenced_run(self):
        finished = self.add_run()
        with self.assertRaisesRegex(ValueError, "ID 不匹配"):
            collection_runs.delete_run(finished, finished + 1)
        if finished == 1:
            with self.assertRaisesRegex(ValueError, "ID 不匹配"):
                collection_runs.delete_run(finished, True)
        with self.assertRaisesRegex(ValueError, "不存在"):
            collection_runs.preview_delete(999999)

        blocked = [
            self.add_run(status="queued"),
            self.add_run(status="running", finished=False),
            self.add_run(status="succeeded", paused=True),
            self.add_run(status="succeeded", finished=False),
        ]
        for run_id in blocked:
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                collection_runs.delete_run(run_id, run_id)

        source = self.add_run()
        self.add_run("detail_retry", params={"source_run_id": source},
                     status="running", finished=False)
        with self.assertRaisesRegex(ValueError, "补采任务"):
            collection_runs.delete_run(source, source)
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM collect_runs WHERE id=?", (source,)).fetchone())

    def test_delete_failure_rolls_back_run_and_explicit_child_cleanup(self):
        self.add_job("rollback")
        run_id = self.add_run(stats={"total": 1})
        collection_runs.attach_jobs(run_id, ["rollback"])
        self.add_exclusive_children("rollback", run_id)
        get_db().execute("""
            CREATE TRIGGER abort_rollback_job BEFORE DELETE ON jobs
            WHEN OLD.job_key='rollback'
            BEGIN
              SELECT RAISE(ABORT, '测试删除回滚');
            END;
        """)
        get_db().commit()

        with self.assertRaises(sqlite3.IntegrityError):
            collection_runs.delete_run(run_id, run_id)

        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM collect_runs WHERE id=?", (run_id,)).fetchone())
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM jobs WHERE job_key='rollback'").fetchone())
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM job_details WHERE job_key='rollback'").fetchone())
        self.assertIsNotNone(get_db().execute(
            "SELECT 1 FROM greetings WHERE job_key='rollback'").fetchone())


if __name__ == "__main__":
    unittest.main()
