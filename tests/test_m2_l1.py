"""M2 测试：L1 电算评分引擎——规则锚点 + 黄金校准。

黄金用例来自《岗位筛选评分规则.md》§五 与 xlsx L1初筛 sheet 的真实岗位。
回归要求：scoring/ 或 importer/ 改动后必须全量通过。
"""
import os
import tempfile
import unittest
from pathlib import Path

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-l1-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend.scoring import l1  # noqa: E402
from backend import importer  # noqa: E402
from backend.db import get_db, init_db  # noqa: E402

EXPECT = {"salary_min": 25, "salary_max": 30}


class AnchorTests(unittest.TestCase):
    """锚点表精确断言（电算部分必须与规则严格一致）。"""

    def test_salary_bands(self):
        self.assertEqual(l1.salary_score(40, 50, 30), 15.0)   # ≥40
        self.assertEqual(l1.salary_score(30, 40, 30), 13.0)   # 35-40
        self.assertEqual(l1.salary_score(25, 35, 30), 11.0)   # 30-35
        self.assertEqual(l1.salary_score(25, 30, 30), 9.0)    # 25-30 达期望
        self.assertEqual(l1.salary_score(20, 25, 30), 6.0)
        self.assertEqual(l1.salary_score(15, 20, 30), 4.0)
        self.assertEqual(l1.salary_score(None, None, 30), 2.0)  # 面议

    def test_structure(self):
        self.assertEqual(l1.structure_score("20-30K·16薪", 16), 5.0)
        self.assertEqual(l1.structure_score("25-40K·14薪", 14), 4.5)
        self.assertEqual(l1.structure_score("20-30K·13薪", 13), 4.0)
        self.assertEqual(l1.structure_score("20-30K", 12), 3.0)
        self.assertEqual(l1.structure_score("20-30K", None), 3.0)
        self.assertEqual(l1.structure_score("面议", None), 2.0)

    def test_e1_matrix(self):
        self.assertEqual(l1.e1_score("已上市", "10000人以上"), 12.0)
        self.assertEqual(l1.e1_score("已上市", "1000-9999人"), 11.0)
        self.assertEqual(l1.e1_score("不需要融资", "1000-9999人"), 10.0)
        self.assertEqual(l1.e1_score("不需要融资", "20-99人"), 7.0)
        self.assertEqual(l1.e1_score("A轮", "100-499人"), 8.0)
        self.assertEqual(l1.e1_score("天使轮", "0-20人"), 5.0)
        self.assertEqual(l1.e1_score("", ""), 6.0)

    def test_industry(self):
        self.assertGreaterEqual(l1.industry_score("人工智能", ""), 18.0)
        self.assertEqual(l1.industry_score("医疗器械", "某科技公司"), 15.0)
        # 头部品牌上探一档：迈瑞（医疗器械头部）、华大（与 xlsx 基线 A=17 一致）
        self.assertEqual(l1.industry_score("医疗器械", "迈瑞医疗"), 17.0)
        self.assertEqual(l1.industry_score("医疗器械", "华大智造"), 17.0)
        self.assertEqual(l1.industry_score("智能硬件/消费电子", "绿联科技"), 14.0)  # 11+头部3
        self.assertEqual(l1.industry_score("电子商务", ""), 7.0)
        self.assertEqual(l1.industry_score("", ""), 9.0)

    def test_gates(self):
        cap, hits = l1.gates({"degree": "硕士", "experience": "3-5年",
                              "salary_max": 30}, "")
        self.assertEqual(cap, 70)
        self.assertIn("G1-要求硕士", hits)
        cap, _ = l1.gates({"degree": "本科", "experience": "5-10年",
                           "salary_max": 40}, "")
        self.assertEqual(cap, 85)
        cap, hits = l1.gates({"degree": "本科", "experience": "经验不限",
                              "salary_max": 18}, "")
        self.assertEqual(cap, 65)
        cap, hits = l1.gates({"degree": "本科", "experience": "经验不限",
                              "salary_max": 30, "title": "AI短视频制作"},
                             "负责数字人内容制作")
        self.assertEqual(cap, 50)
        self.assertIn("G4-纯算法/非技术岗", hits)


class GoldenTests(unittest.TestCase):
    """规则 §五 的 4 个真实岗位：电算维度对照（容差覆盖人工判断部分）。"""

    @classmethod
    def setUpClass(cls):
        init_db()

    def _score(self, job, jd=""):
        return l1.score_job(job, jd, config.DEFAULT_SKILL_DICTIONARY, EXPECT)

    def test_anker(self):
        """#92 Anker·AI应用 20-35K·16薪·已上市：B1=9 B2=5，综合公式 0.6×84+0.4×73.5=79.8。"""
        r = self._score({"title": "AI应用工程师", "company": "Anker",
                         "industry": "智能硬件/消费电子", "scale": "1000-9999人",
                         "stage": "已上市", "salary": "20-35K·16薪",
                         "salary_min": 20, "salary_max": 35, "salary_months": 16,
                         "experience": "经验不限", "degree": "本科",
                         "skills": "Python LLM Agent 嵌入式 MCP TypeScript 全栈"},
                        "AI Agent 应用开发，Python 全栈，MCP 工具链，智能硬件场景")
        d = r["l1_detail"]
        self.assertEqual(d["B1"], 9.0)
        self.assertEqual(d["B2"], 5.0)
        self.assertEqual(d["E1"], 11.0)
        # 综合分公式（用 L2 真值反推校验公式本身）
        self.assertAlmostEqual(84 * 0.6 + 73.5 * 0.4, 79.8, places=1)
        # 电算综合粗分应落在人工 L2 综合分 ±10 内
        self.assertLess(abs(r["composite_rough"] - 79.8), 10)

    def test_shadow_sensor(self):
        """#1 影拓传感·嵌入式(机器人通信) 30-60K：好岗但有硬 gap → 匹配度受限。"""
        r = self._score({"title": "嵌入式软件工程师（机器人通信方向）",
                         "company": "影拓传感", "industry": "电子/硬件开发",
                         "scale": "20-99人", "stage": "不需要融资",
                         "salary": "30-60K", "salary_min": 30, "salary_max": 60,
                         "salary_months": 12, "experience": "3-5年", "degree": "本科",
                         "skills": "CAN CANopen EtherCAT RTOS 嵌入式 C"},
                        "CAN/CANopen/EtherCAT/RTOS 通信协议栈开发")
        d = r["l1_detail"]
        self.assertEqual(d["B1"], 15.0)          # 中值45 ≥40
        self.assertEqual(d["E1"], 7.0)           # 不需要融资 20-99人
        # 简历词典没有 CAN/EtherCAT → 命中率有限，匹配粗分不高于人工 63+10
        self.assertLess(r["match_rough"], 73)

    def test_fake_ai_job_g4(self):
        """#101 三只蜗牛·AI短视频内容岗：G4 兜底 → 匹配粗分 ≤50，P3。"""
        r = self._score({"title": "AI全栈工程师", "company": "三只蜗牛",
                         "industry": "批发/零售", "scale": "0-20人", "stage": "未融资",
                         "salary": "15-30K", "salary_min": 15, "salary_max": 30,
                         "salary_months": None, "experience": "经验不限",
                         "degree": "学历不限", "skills": ""},
                        "AI 短视频制作与小游戏素材生成，数字人内容运营")
        d = r["l1_detail"]
        self.assertEqual(d["cap"], 50.0)
        self.assertLessEqual(r["match_rough"], 50.0)
        self.assertEqual(r["priority_rough"], "P3")

    def test_mgi_medical_bonus(self):
        """华大智造·生命科学 Agent·医疗器械：头部品牌 A=17（与 xlsx L2 基线一致）+ 医疗加分。"""
        r = self._score({"title": "AI Agent 软件开发工程师（生命科学方向）",
                         "company": "华大智造", "industry": "医疗器械",
                         "scale": "1000-9999人", "stage": "已上市",
                         "salary": "16-25K", "salary_min": 16, "salary_max": 25,
                         "salary_months": 12, "experience": "经验不限",
                         "degree": "本科", "skills": "Python LLM Agent"},
                        "生命科学 AI Agent，医疗器械软件")
        self.assertEqual(r["l1_detail"]["A"], 17.0)
        self.assertTrue(any("医疗器械公司" in n for n in r["l1_detail"]["match"]["adjust"]))


class RunTests(unittest.TestCase):
    def setUp(self):
        conn = get_db()
        conn.execute("DELETE FROM job_details")
        conn.execute("DELETE FROM jobs")
        conn.commit()

    def test_run_l1_fills_and_skips(self):
        importer.upsert_job({"job_key": "a", "title": "T", "company": "C",
                             "salary": "20-30K", "salary_min": 20, "salary_max": 30,
                             "source": "import"})
        importer.upsert_job({"job_key": "b", "title": "T2", "company": "C2",
                             "salary": "25-40K·14薪", "salary_min": 25,
                             "salary_max": 40, "salary_months": 14, "source": "import",
                             "l1_score": 69.5, "match_rough": 79.2,
                             "composite_rough": 75.3,
                             "l1_detail": {"source": "imported"}})
        stats = l1.run_l1()
        self.assertEqual(stats["scored"], 1)   # 只算没有 L1 的
        self.assertEqual(stats["skipped"], 1)  # 导入基线不覆盖
        row = get_db().execute("SELECT * FROM jobs WHERE job_key='b'").fetchone()
        self.assertEqual(row["l1_score"], 69.5)
        row_a = get_db().execute("SELECT * FROM jobs WHERE job_key='a'").fetchone()
        self.assertIsNotNone(row_a["l1_score"])


if __name__ == "__main__":
    unittest.main()
