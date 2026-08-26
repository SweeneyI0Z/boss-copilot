"""M18 岗位福利、工时与外包标签识别。"""
import os
import tempfile
import unittest
from pathlib import Path


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m18-tags-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config, job_tags, main  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402
from backend.resumes import get_default_resume  # noqa: E402


TAG_FIELDS = job_tags.TAG_FIELDS


class JobTagClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m18-tags-case-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def add_job(key: str, jd: str, skills: str = "", skill_tags: str = ""):
        ts = now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO jobs(job_key,title,company,skills,status,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?, 'active',?,?)",
            (key, "后端工程师", "示例科技", skills, ts, ts))
        conn.execute(
            "INSERT INTO job_details(job_key,jd,skill_tags,fetched_at) VALUES(?,?,?,?)",
            (key, jd, skill_tags, ts))
        conn.commit()

    def assert_tags(self, result: dict, **expected):
        self.assertEqual({field: result[field] for field in TAG_FIELDS}, expected)

    def test_nfkc_and_expanded_positive_keywords(self):
        result = job_tags.classify_job_tags(
            {"title": "研发工程师", "company": "示例公司", "skills": "第三方用工"},
            "每天８小时工作制，固定周末双休，提供六险一金、补充公积金和免费班车。",
            "派驻客户现场")

        self.assert_tags(
            result, has_benefits=True, has_weekend=True,
            has_eight_hour_weekend=True, has_alternating_weekend=False,
            is_outsourcing=True)

    def test_alternating_weekend_and_negations_take_priority(self):
        result = job_tags.classify_job_tags(
            {"title": "平台工程师", "company": "示例公司"},
            "实行大小周，周末双休，每天8小时；不保证双休，不提供双休。"
            "明确非外包，工作内容包含外包供应商管理与外包采购。")

        self.assert_tags(
            result, has_benefits=False, has_weekend=False,
            has_eight_hour_weekend=False, has_alternating_weekend=True,
            is_outsourcing=False)

    def test_eight_hour_weekend_requires_both_conditions(self):
        only_hours = job_tags.classify_job_tags(
            {"title": "工程师", "company": "示例公司"}, "每天8小时工作制。")
        only_weekend = job_tags.classify_job_tags(
            {"title": "工程师", "company": "示例公司"}, "固定周末双休。")

        self.assertFalse(only_hours["has_weekend"])
        self.assertFalse(only_hours["has_eight_hour_weekend"])
        self.assertTrue(only_weekend["has_weekend"])
        self.assertFalse(only_weekend["has_eight_hour_weekend"])

    def test_required_false_positive_phrases_are_masked(self):
        cases = (
            ("不保证双休", "has_weekend"),
            ("不提供双休", "has_weekend"),
            ("非外包岗位", "is_outsourcing"),
            ("负责外包供应商管理", "is_outsourcing"),
            ("负责外包采购", "is_outsourcing"),
            ("不提供五险一金、年终奖和带薪年假", "has_benefits"),
        )
        for text, field in cases:
            with self.subTest(text=text):
                result = job_tags.classify_job_tags(
                    {"title": "工程师", "company": "示例公司"}, text)
                self.assertFalse(result[field])

        contrast = job_tags.classify_job_tags(
            {"title": "工程师", "company": "示例公司"},
            "不提供五险一金，但提供年终奖。")
        self.assertTrue(contrast["has_benefits"])

    def test_list_and_detail_share_the_same_classifier(self):
        self.add_job(
            "positive",
            "每天8小时，周休二日，提供五险二金和下午茶，属于劳务派遣岗位。",
            skills="Python", skill_tags="年度体检")
        self.add_job(
            "alternating", "一周单休一周双休轮换，每天8小时，提供带薪病假。")
        self.add_job(
            "negative", "不保证双休；不是外包岗位，负责外包服务商对接。")
        resume = get_default_resume()

        listed = main.list_jobs(limit=50, resume_id=resume["id"])["items"]
        by_key = {item["job_key"]: item for item in listed}
        for key in ("positive", "alternating", "negative"):
            detail = main.job_detail(key, resume["id"])
            self.assertEqual(
                {field: by_key[key][field] for field in TAG_FIELDS},
                {field: detail[field] for field in TAG_FIELDS})

        self.assert_tags(
            by_key["positive"], has_benefits=True, has_weekend=True,
            has_eight_hour_weekend=True, has_alternating_weekend=False,
            is_outsourcing=True)
        self.assert_tags(
            by_key["alternating"], has_benefits=True, has_weekend=False,
            has_eight_hour_weekend=False, has_alternating_weekend=True,
            is_outsourcing=False)
        self.assert_tags(
            by_key["negative"], has_benefits=False, has_weekend=False,
            has_eight_hour_weekend=False, has_alternating_weekend=False,
            is_outsourcing=False)


if __name__ == "__main__":
    unittest.main()
