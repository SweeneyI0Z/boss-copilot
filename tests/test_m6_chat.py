"""M6 测试：会话快照去重、AI 草稿生成与状态流转（CDP 全 mock）。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m6-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import chatpoll, llm  # noqa: E402
from backend.db import get_db, init_db, set_setting  # noqa: E402


def fake_client(text):
    def create(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _upsert_snapshot(conv_key, title, text):
    """模拟 poll 落库路径（绕过 CDP）。"""
    conn = get_db()
    import hashlib
    row = conn.execute("SELECT id FROM conversations WHERE boss_key=?",
                       (conv_key,)).fetchone()
    cid = row["id"] if row else conn.execute(
        "INSERT INTO conversations(boss_key, boss_name, last_message_at) VALUES(?,?,datetime('now','localtime'))",
        (conv_key, title)).lastrowid
    h = hashlib.md5(text.encode()).hexdigest()[:16]
    if not conn.execute("SELECT 1 FROM messages WHERE conversation_id=? AND msg_key=?",
                        (cid, h)).fetchone():
        conn.execute("INSERT INTO messages(conversation_id, direction, content, msg_key,"
                     " created_at) VALUES(?,?,?,?,datetime('now','localtime'))",
                     (cid, "snapshot", text, h))
    conn.commit()
    return cid


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM conversations")
        conn.commit()

    def test_snapshot_dedup(self):
        cid = _upsert_snapshot("k1", "张三|BOSS|AI工程师", "你好，看到你的经历")
        _upsert_snapshot("k1", "张三|BOSS|AI工程师", "你好，看到你的经历")  # 相同快照
        n = get_db().execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        self.assertEqual(n, 1)
        _upsert_snapshot("k1", "张三|BOSS|AI工程师", "你好，看到你的经历\n方便发份简历吗")
        n = get_db().execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        self.assertEqual(n, 2)   # 内容变化 → 新快照

    def test_conversations_view(self):
        _upsert_snapshot("k1", "张三", "msg1")
        _upsert_snapshot("k2", "李四", "msg2")
        convs = chatpoll.conversations_with_drafts()
        self.assertEqual(len(convs), 2)
        self.assertTrue(all("latest_snapshot" in c for c in convs))


class DraftTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM conversations")
        conn.execute("UPDATE profile SET resume_text=?", ("简历正文" * 100,))
        conn.commit()
        set_setting("llm_base_url", "http://x")
        set_setting("llm_api_key", "k")
        set_setting("llm_model", "m")

    def test_generate_draft(self):
        cid = _upsert_snapshot("k1", "张三", "我想要一份您的附件简历，您是否同意")
        out = chatpoll.generate_draft(cid, client=fake_client('{"reply": "已投递附件简历，请查收"}'))
        self.assertEqual(out["reply"], "已投递附件简历，请查收")
        row = get_db().execute(
            "SELECT draft_reply, draft_status FROM messages WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        self.assertEqual(row["draft_reply"], "已投递附件简历，请查收")
        self.assertEqual(row["draft_status"], "pending")

    def test_draft_requires_snapshot(self):
        cid = get_db().execute(
            "INSERT INTO conversations(boss_key, boss_name, last_message_at) "
            "VALUES('k9','空',datetime('now','localtime'))").lastrowid
        get_db().commit()
        with self.assertRaises(llm.LLMError):
            chatpoll.generate_draft(cid, client=fake_client('{"reply":"x"}'))

    def test_llm_unconfigured(self):
        set_setting("llm_base_url", "")
        cid = _upsert_snapshot("k2", "李四", "hello")
        with self.assertRaises(llm.LLMError):
            chatpoll.generate_draft(cid, client=None)


if __name__ == "__main__":
    unittest.main()
