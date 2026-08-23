"""M7 测试：模拟面试——出题、答题流转（追问/下一题）、报告。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m7-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import interview, llm  # noqa: E402
from backend.db import get_db, init_db, set_setting  # noqa: E402

PLAN = ('{"questions": [{"q": "RTOS优先级反转如何处理", "probe": "RTOS", "follow_up": "x"},'
        '{"q": "I2C与SPI差异", "probe": "协议", "follow_up": "y"},'
        '{"q": "Agent工具调用超时怎么设计", "probe": "Agent工程", "follow_up": "z"}]}')
TURN_NEXT = '{"feedback": "回答基本正确，缺互斥量提法", "action": "next", "content": "下一题原文"}'
TURN_FOLLOW = '{"feedback": "含糊", "action": "follow_up", "content": "能展开说吗"}'
REPORT = ('{"overall": "基础扎实，Agent经验偏薄", "score": 72, "strengths": ["协议题"],'
          '"risks": ["RTOS互斥量没提"], "prep": ["补互斥量/优先级继承"], "closing_tips": ["先答结构再细节"]}')


def fake_client(responses):
    queue = list(responses)
    def create(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=queue.pop(0)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _setup():
    init_db()
    conn = get_db()
    # 子表先清（外键约束；全量 discover 模式下各模块共用一个测试库）
    for child in ("greetings", "interviews", "messages", "conversations",
                  "job_details", "sent_log"):
        conn.execute(f"DELETE FROM {child}")
    conn.execute("DELETE FROM jobs")
    conn.execute("UPDATE profile SET resume_text=?", ("嵌入式4年经验简历正文" * 30,))
    conn.execute("INSERT INTO jobs(job_key, title, company, salary, first_seen_at,"
                 " last_seen_at, status) VALUES('ik','AI Agent工程师','某公司','25-40K',"
                 "'t','t','active')")
    conn.commit()
    set_setting("llm_base_url", "http://x")
    set_setting("llm_api_key", "k")
    set_setting("llm_model", "m")


class InterviewFlowTests(unittest.TestCase):
    def setUp(self):
        _setup()

    def test_start_creates_bank_and_first_question(self):
        s = interview.start("ik", client=fake_client([PLAN]))
        self.assertEqual(s["total_questions"], 3)
        self.assertEqual(s["first_question"], "RTOS优先级反转如何处理")
        d = interview.get(s["id"])
        roles = [t["role"] for t in d["transcript"]]
        self.assertEqual(roles, ["bank", "interviewer"])
        self.assertEqual(len(d["transcript"][0]["questions"]), 3)

    def test_answer_follow_up_then_next(self):
        s = interview.start("ik", client=fake_client([PLAN]))
        # 追问
        out = interview.answer(s["id"], "优先级反转就是...", client=fake_client([TURN_FOLLOW]))
        self.assertEqual(out["action"], "follow_up")
        self.assertEqual(out["content"], "能展开说吗")
        # 下一题：LLM 给了 content 但应被题库覆盖为未问的题
        out2 = interview.answer(s["id"], "展开说...", client=fake_client([TURN_NEXT]))
        self.assertEqual(out2["action"], "next")
        self.assertIn(out2["content"], ("I2C与SPI差异", "Agent工具调用超时怎么设计"))
        d = interview.get(s["id"])
        asked = [t["content"] for t in d["transcript"] if t["role"] == "interviewer"]
        self.assertIn("I2C与SPI差异", asked)

    def test_bank_exhausted_ends(self):
        s = interview.start("ik", client=fake_client([PLAN]))
        cli = fake_client([TURN_NEXT] * 3)
        for _ in range(3):
            interview.answer(s["id"], "答", client=cli)
        out = interview.answer(s["id"], "最后一答", client=fake_client([TURN_NEXT]))
        self.assertIn("面试结束", out["content"])

    def test_finish_report(self):
        s = interview.start("ik", client=fake_client([PLAN]))
        interview.answer(s["id"], "答一", client=fake_client([TURN_NEXT]))
        report = interview.finish(s["id"], client=fake_client([REPORT]))
        self.assertEqual(report["score"], 72)
        d = interview.get(s["id"])
        self.assertEqual(d["status"], "finished")
        # 结束后不可再答
        with self.assertRaises(llm.LLMError):
            interview.answer(s["id"], "再答", client=fake_client([TURN_NEXT]))

    def test_requires_resume_and_job(self):
        conn = get_db()
        conn.execute("UPDATE profile SET resume_text='短'")
        conn.commit()
        with self.assertRaises(llm.LLMError):
            interview.start("ik", client=fake_client([PLAN]))
        with self.assertRaises(llm.LLMError):
            interview.start("nonexistent", client=fake_client([PLAN]))


if __name__ == "__main__":
    unittest.main()
