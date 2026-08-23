"""M5 测试：招呼语生成（LLM/模板兜底）、队列状态机、发送护栏判定。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m5-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import greeting, sender  # noqa: E402
from backend.db import get_db, init_db, set_setting  # noqa: E402


def fake_client(responses: list):
    def create(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=responses.pop(0)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _job(key="gk", status="active", l2=None):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO jobs(job_key, title, company, salary, status, "
        "first_seen_at, last_seen_at, composite, l2_detail) VALUES(?,?,?,?,?,?,?,?,?)",
        (key, "AI应用工程师", "某公司", "20-30K", status, "t", "t", 70.0,
         __import__("json").dumps(l2 or {}, ensure_ascii=False)))
    conn.commit()


class GreetingTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM greetings")
        conn.execute("DELETE FROM jobs")
        conn.execute("UPDATE profile SET resume_text=?", ("简历正文" * 100,))
        conn.commit()
        set_setting("llm_base_url", "http://x")
        set_setting("llm_api_key", "k")
        set_setting("llm_model", "m")

    def test_llm_generate_and_queue(self):
        _job()
        cli = fake_client(['{"variants": ["招呼一", "招呼二", "招呼三"]}'])
        out = greeting.generate("gk", client=cli)
        self.assertEqual(out["source"], "llm")
        self.assertEqual(out["variants"], ["招呼一", "招呼二", "招呼三"])
        q = greeting.list_queue("draft")
        self.assertEqual(len(q), 1)
        # 再次生成 → 复用队列记录（不重复插入）
        out2 = greeting.generate("gk", client=fake_client(
            ['{"variants": ["新1", "新2", "新3"]}']))
        self.assertEqual(out2["id"], out["id"])
        self.assertEqual(len(greeting.list_queue()), 1)

    def test_llm_failure_falls_back_to_template(self):
        _job(l2={"greeting_angle": "医疗器械法规+AI工具链"})
        cli = fake_client(["不是json", "还是不是json", "依然不是"])   # 重试耗尽
        out = greeting.generate("gk", client=cli)
        self.assertEqual(out["source"], "template")
        self.assertEqual(len(out["variants"]), 3)
        self.assertIn("医疗器械", out["variants"][0])

    def test_approve_flow(self):
        _job()
        out = greeting.generate("gk", client=fake_client(
            ['{"variants": ["A", "B", "C"]}']))
        self.assertTrue(greeting.approve(out["id"], 1)["ok"])
        batch = greeting.pending_batch()
        self.assertEqual(batch[0]["chosen"], "B")
        # sent 之后不可再 approve
        conn = get_db()
        conn.execute("UPDATE greetings SET status='sent' WHERE id=?", (out["id"],))
        conn.commit()
        with self.assertRaises(Exception):
            greeting.approve(out["id"], 0)

    def test_pending_excludes_inactive_jobs(self):
        _job("alive", status="active")
        _job("dead", status="delisted")
        greeting.generate("alive", client=fake_client(['{"variants":["x"]}']))
        greeting.generate("dead", client=fake_client(['{"variants":["y"]}']))
        conn = get_db()
        for k in ("alive", "dead"):
            gid = conn.execute("SELECT id FROM greetings WHERE job_key=?", (k,)).fetchone()["id"]
            greeting.approve(gid, 0)
        keys = {b["job_key"] for b in greeting.pending_batch()}
        self.assertEqual(keys, {"alive"})


class SenderGuardTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM sent_log")
        conn.execute("DELETE FROM settings WHERE key LIKE 'send_halt%'")
        conn.commit()

    def test_halt_flag(self):
        self.assertFalse(sender.halted_today())
        sender.halt("已与120位BOSS沟通")
        self.assertTrue(sender.halted_today())
        self.assertEqual(sender.halted_today(), True)

    def test_daily_count(self):
        from datetime import date
        conn = get_db()
        conn.execute("INSERT INTO sent_log(day, job_key, company, ok, created_at) "
                     "VALUES(?,?,?,?,?)",
                     (date.today().isoformat(), "k1", "C", 1, "t"))
        conn.commit()
        self.assertEqual(sender.sent_today(), 1)

    def test_company_dedup(self):
        from datetime import date
        conn = get_db()
        conn.execute("INSERT INTO sent_log(day, job_key, company, ok, created_at) "
                     "VALUES(?,?,?,1, datetime('now'))",
                     (date.today().isoformat(), "k1", "绿联科技"))
        conn.commit()
        self.assertTrue(sender.company_recently_sent("绿联科技"))
        self.assertFalse(sender.company_recently_sent("别的公司"))


if __name__ == "__main__":
    unittest.main()
