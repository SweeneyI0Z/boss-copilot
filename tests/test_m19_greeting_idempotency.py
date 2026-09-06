"""M19 招呼语按当前简历修订幂等生成。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m19-greeting-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config, greeting, llm, main, resumes  # noqa: E402
from backend.ai_tasks import AITaskScheduler  # noqa: E402
from backend.db import close_db, get_db, init_db, now_iso, set_setting  # noqa: E402


def _client(response: str):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=response))]))))


class GreetingIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="boss-copilot-m19-greeting-case-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()
        self.resume = resumes.update_resume(
            resumes.get_default_resume()["id"], resume_text="完整项目简历" * 80)
        set_setting("llm_base_url", "https://llm.example/v1")
        set_setting("llm_api_key", "secret")
        set_setting("llm_model", "test-model")
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True)
        main._ai_scheduler = None

    def tearDown(self):
        if main._ai_scheduler is not None:
            main._ai_scheduler.close(wait=True, timeout=1)
        main._ai_scheduler = None
        # unittest discover 会在 M19 后继续运行早期里程碑用例；清掉外键记录，
        # 避免线程本地连接仍指向即将删除的临时库时污染后续测试。
        conn = get_db()
        conn.execute("DELETE FROM greetings")
        conn.execute("DELETE FROM job_details")
        conn.execute("DELETE FROM jobs")
        conn.commit()
        close_db()
        self.tmp.cleanup()

    def add_job(self, job_key: str):
        ts = now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO jobs(job_key,title,company,status,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,'active',?,?)",
            (job_key, f"岗位 {job_key}", "示例科技", ts, ts))
        # JD 闸门上线后：无 JD 的岗位不进入评分、分析与招呼语队列，本用例夹具统一带 JD
        conn.execute(
            "INSERT INTO job_details(job_key,jd,fetched_at) VALUES(?,?,?)",
            (job_key, f"{job_key} 职位描述：负责核心模块开发", ts))
        conn.commit()

    def add_greeting(self, job_key: str, *, variants=None, chosen="", revision=None):
        ts = now_iso()
        cur = get_db().execute(
            "INSERT INTO greetings(job_key,variants,chosen,status,created_at,updated_at,"
            "resume_id,resume_revision) VALUES(?,?,?,'draft',?,?,?,?)",
            (job_key, json.dumps(variants or [], ensure_ascii=False), chosen, ts, ts,
             self.resume["id"], revision or self.resume["revision"]))
        get_db().commit()
        return cur.lastrowid

    def test_completion_requires_current_revision_and_usable_content(self):
        for key in ("three", "chosen", "partial", "old"):
            self.add_job(key)
        self.add_greeting("three", variants=["专业版", "精简版", "技术版"])
        self.add_greeting("chosen", variants=[], chosen="人工保留的有效文案")
        self.add_greeting("partial", variants=["只有一版", "第二版"])
        self.add_greeting(
            "old", variants=["旧专业版", "旧精简版", "旧技术版"],
            revision=self.resume["revision"] - 1)

        completed = greeting.completed_job_keys(
            ["three", "chosen", "partial", "old"],
            self.resume["id"], self.resume["revision"])

        self.assertEqual(completed, {"three", "chosen"})

    def test_generate_reuses_without_model_and_force_can_regenerate(self):
        self.add_job("reuse")
        greeting_id = self.add_greeting(
            "reuse", variants=["原专业版", "原精简版", "原技术版"])
        blocked_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: self.fail(
                "已有招呼语时不应再次调用模型"))))

        reused = greeting.generate(
            "reuse", client=blocked_client, resume_id=self.resume["id"])
        self.assertEqual(reused["id"], greeting_id)
        self.assertTrue(reused["reused_existing"])
        self.assertEqual(reused["source"], "existing")

        response = ('{"professional":"新专业版","concise":"新精简版",'
                    '"technical":"新技术版"}')
        regenerated = greeting.generate(
            "reuse", client=_client(response), resume_id=self.resume["id"], force=True)
        self.assertEqual(regenerated["id"], greeting_id)
        self.assertFalse(regenerated["reused_existing"])
        self.assertEqual(regenerated["variants"], ["新专业版", "新精简版", "新技术版"])

    def test_task_api_skips_single_and_mixed_batch(self):
        for key in ("three", "chosen", "missing"):
            self.add_job(key)
        self.add_greeting("three", variants=["专业版", "精简版", "技术版"])
        self.add_greeting("chosen", chosen="已编辑文案")
        calls = []
        main._ai_scheduler = AITaskScheduler(
            lambda task, cancelled: calls.append(task) or {"ok": True},
            max_concurrency=1)

        with patch.object(llm, "configured", return_value=False):
            single = main.ai_tasks_enqueue("workbench", {
                "kind": "greeting", "job_keys": ["three"],
                "resume_id": self.resume["id"],
            })
            mixed = main.ai_tasks_enqueue("workbench", {
                "kind": "greeting", "job_keys": ["three", "chosen", "missing"],
                "resume_id": self.resume["id"],
            })

        self.assertEqual(single["added"], 0)
        self.assertEqual(single["skipped_existing"], ["three"])
        self.assertEqual(single["skipped_existing_count"], 1)
        self.assertEqual(mixed["skipped_existing"], ["three", "chosen"])
        self.assertEqual(mixed["skipped_existing_count"], 2)
        self.assertEqual(mixed["added"], 1)
        self.assertTrue(main._ai_scheduler.wait_for_idle("workbench", timeout=2))
        self.assertEqual([task["job_key"] for task in calls], ["missing"])

    def test_single_generate_endpoint_returns_consistent_skip_metadata(self):
        self.add_job("direct")
        self.add_greeting("direct", variants=["专业版", "精简版", "技术版"])

        result = main.greeting_generate({
            "job_key": "direct", "resume_id": self.resume["id"]})

        self.assertTrue(result["reused_existing"])
        self.assertEqual(result["skipped_existing"], ["direct"])
        self.assertEqual(result["skipped_existing_count"], 1)


if __name__ == "__main__":
    unittest.main()
