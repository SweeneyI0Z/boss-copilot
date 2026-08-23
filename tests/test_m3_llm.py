"""M3 测试：LLM 客户端容错、策略计划校验、L2 算术合成与写库语义。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m3-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import llm, strategy  # noqa: E402
from backend.db import get_db, init_db, set_setting  # noqa: E402
from backend.scoring import l2  # noqa: E402


def fake_client(responses: list):
    """顺序返回预设回复的假 OpenAI 客户端。"""
    calls = []

    def create(**kw):
        calls.append(kw)
        text = responses.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


class ExtractJsonTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(llm.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(llm.extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_noisy(self):
        self.assertEqual(llm.extract_json('好的，结果如下：\n{"a": {"b": 2}} 以上。'),
                         {"a": {"b": 2}})

    def test_trailing_comma(self):
        self.assertEqual(llm.extract_json('{"a": 1, "b": [1, 2,],}'),
                         {"a": 1, "b": [1, 2]})

    def test_unclosed_raises(self):
        with self.assertRaises(llm.LLMError):
            llm.extract_json('{"a": 1')


class ChatJsonRetryTests(unittest.TestCase):
    def test_retry_then_success(self):
        cli, calls = fake_client(['不是json', '{"ok": 1}'])
        out = llm.chat_json([{"role": "user", "content": "x"}], client=cli)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(len(calls), 2)   # 重试了一次


class StrategyTests(unittest.TestCase):
    PLAN = ('{"directions": ["嵌入式×AI"], "searches": ['
            '{"keyword": "嵌入式 AI", "city": "深圳", "pages": 5, "reason": "交叉方向"}],'
            '"companies": [{"name": "华大智造", "reason": "医疗×AI"}],'
            '"dictionary_patch": {"embedded": ["RTThread"]}, "notes": "ok"}')

    def setUp(self):
        init_db()
        set_setting("llm_base_url", "http://x")
        set_setting("llm_api_key", "k")
        set_setting("llm_model", "m")

    def test_generate_valid(self):
        cli, _ = fake_client([self.PLAN])
        plan = strategy.generate_plan("简历" * 100, {"salary_max": 30}, client=cli)
        self.assertEqual(plan["searches"][0]["keyword"], "嵌入式 AI")
        self.assertEqual(plan["searches"][0]["pages"], 5)

    def test_pages_clamped(self):
        bad = self.PLAN.replace('"pages": 5', '"pages": 99')
        cli, _ = fake_client([bad])
        plan = strategy.generate_plan("简历" * 100, {}, client=cli)
        self.assertEqual(plan["searches"][0]["pages"], 10)   # 上限 10

    def test_missing_searches_rejected(self):
        cli, _ = fake_client(['{"directions": []}'] * 3)
        with self.assertRaises(llm.LLMError):
            strategy.generate_plan("简历" * 100, {}, client=cli)

    def test_short_resume_rejected_before_llm(self):
        with self.assertRaises(llm.LLMError):
            strategy.generate_plan("太短", {}, client=None)


class L2FinalizeTests(unittest.TestCase):
    """算术合成：岗位分=Σdims；匹配度=min(cap, Σs+Σadjust)；综合=0.6/0.4；P 级阈值。"""

    def test_anker_golden_formula(self):
        # 用规则 §五 Anker 真值：岗位 73.5 / 匹配 84 → 综合 79.8 / P0
        dims = dict(zip(["A", "B1", "B2", "B3", "C", "D", "E1", "E2", "E3"],
                        [16, 9, 5, 2.5, 9, 5, 11, 11, 5]))
        self.assertEqual(sum(dims.values()), 73.5)
        r = l2.finalize({"dims": dims, "s": {"S1": 24, "S2": 22, "S3": 15, "S4": 12, "S5": 11},
                         "adjust": []}, cap=100)
        self.assertEqual(r["job_score"], 73.5)
        self.assertEqual(r["match_score"], 84.0)
        self.assertAlmostEqual(r["composite"], 79.8, places=1)
        self.assertEqual(r["priority"], "P0")

    def test_cap_and_clamp(self):
        # cap 只封顶不抬分：raw=40 < cap=50 → 40；raw=90 > cap=50 → 50
        r = l2.finalize({"dims": {"A": 20}, "s": {"S1": 40}, "adjust": []}, cap=50)
        self.assertEqual(r["match_score"], 40.0)
        r = l2.finalize({"dims": {"A": 20}, "s": {"S1": 90}, "adjust": []}, cap=50)
        self.assertEqual(r["match_score"], 50.0)
        r2 = l2.finalize({"dims": {}, "s": {}, "adjust": [{"delta": -999}]}, cap=100)
        self.assertEqual(r2["match_score"], 0.0)

    def test_priority_bands(self):
        def mk(match, job):
            return l2.finalize({"dims": {"A": job}, "s": {"S1": match}, "adjust": []},
                               cap=100)
        # 综合 = 0.6m + 0.4j：80/100→88 P0；70/63.75→67.25 P1；60/55→56 P2；40/40→40 P3
        self.assertEqual(mk(80, 100)["priority"], "P0")
        self.assertEqual(mk(70, 63.75)["priority"], "P1")
        self.assertEqual(mk(60, 55)["priority"], "P2")
        self.assertEqual(mk(40, 40)["priority"], "P3")


class L2WritebackTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM job_details")
        conn.execute("DELETE FROM jobs")
        conn.execute("UPDATE profile SET resume_text=?", ("简历正文" * 100,))
        conn.commit()

    def test_score_job_llm_writes_and_respects_existing(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO jobs(job_key, title, company, first_seen_at, last_seen_at,"
            " composite_rough, l1_score, l1_detail, status) VALUES(?,?,?,?,?,?,?,?,?)",
            ("jk", "AI应用工程师", "Anker", "t", "t", 76.0, 70.0,
             '{"A":16,"B1":9,"B2":5,"E1":11,"cap":100}', "active"))
        conn.commit()
        resp = ('{"type":"T3","dims":{"A":16,"B1":9,"B2":5,"B3":2.5,"C":9,"D":5,"E1":11,'
                '"E2":11,"E3":5},"s":{"S1":24,"S2":22,"S3":15,"S4":12,"S5":11},'
                '"adjust":[],"strengths":["x"],"gaps":[],"summary":"s","advice":"a"}')
        cli, _ = fake_client([resp])
        r = l2.score_job_llm("jk", client=cli)
        self.assertAlmostEqual(r["composite"], 79.8, places=1)
        row = conn.execute("SELECT * FROM jobs WHERE job_key='jk'").fetchone()
        self.assertEqual(row["l2_source"], "llm")
        self.assertEqual(row["priority"], "P0")
        # 已有 L2（composite 非空）时显式拒绝覆盖（0 行命中 → LLMError）
        conn.execute("UPDATE jobs SET composite=1.0")   # 模拟基线已存在
        conn.commit()
        cli2, calls2 = fake_client([resp])
        with self.assertRaises(llm.LLMError):
            l2.score_job_llm("jk", client=cli2)
        row2 = conn.execute("SELECT composite FROM jobs WHERE job_key='jk'").fetchone()
        self.assertEqual(row2["composite"], 1.0)


if __name__ == "__main__":
    unittest.main()
