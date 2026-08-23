"""M10 总览、岗位筛选、行内详情与收藏工作台接口。"""
import os
import tempfile
import unittest
from pathlib import Path


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m10-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import applications, job_state, main, resumes  # noqa: E402
from backend.db import get_db, init_db, now_iso  # noqa: E402


def _reset():
    init_db()
    conn = get_db()
    for table in ("applications", "job_resume_scores", "job_score_baselines",
                  "job_collection_hits", "collect_run_tasks", "job_details", "jobs"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _job(key, status="active", company="示例公司"):
    ts = now_iso()
    get_db().execute(
        "INSERT INTO jobs(job_key,title,company,salary,experience,degree,hr_active,"
        "job_link,status,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (key, f"岗位{key}", company, "20-30K", "3-5年", "本科", "今日活跃",
         f"https://www.zhipin.com/job_detail/{key}.html", status, ts, ts))
    get_db().execute(
        "INSERT INTO job_details(job_key,jd,skill_tags,fetched_at) VALUES(?,?,?,?)",
        (key, f"{key} 的完整 JD", "Python", ts))
    get_db().commit()


class JobListAndDetailTests(unittest.TestCase):
    def setUp(self):
        _reset()
        _job("active")
        _job("hunter", company="某大型智能硬件公司")
        _job("old", status="delisted")
        self.resume = resumes.get_default_resume()
        resumes.save_imported_baseline(
            "active", job_score=88, match_score=77, composite=81,
            priority="P0", l2_detail={"summary": "导入基线"})
        resumes.save_job_score(
            "active", self.resume["id"], self.resume["revision"],
            l1_score=66, match_rough=72, composite_rough=69,
            l1_detail={"engine": "l1"}, l1_source="engine")
        job_state.favorite_job("active")
        job_state.update_headhunter_detection("hunter", company="某大型智能硬件公司")

    def test_default_list_only_active_and_uses_resume_score(self):
        result = main.list_jobs(resume_id=self.resume["id"])
        self.assertEqual(result["total"], 2)
        active = next(item for item in result["items"] if item["job_key"] == "active")
        self.assertEqual(active["l1_score"], 66)
        self.assertIsNone(active["job_score"])
        self.assertTrue(active["is_favorite"])

    def test_favorite_headhunter_and_archived_filters(self):
        favorites = main.list_jobs(favorite="only", resume_id=self.resume["id"])
        self.assertEqual([item["job_key"] for item in favorites["items"]], ["active"])
        hunters = main.list_jobs(headhunter="only", resume_id=self.resume["id"])
        self.assertEqual([item["job_key"] for item in hunters["items"]], ["hunter"])
        archived = main.list_jobs(status="archived", resume_id=self.resume["id"])
        self.assertEqual([item["job_key"] for item in archived["items"]], ["old"])

    def test_detail_returns_jd_current_score_baseline_and_application(self):
        applications.confirm_application("active", self.resume["id"])
        detail = main.job_detail("active", self.resume["id"])
        self.assertEqual(detail["jd"], "active 的完整 JD")
        self.assertEqual(detail["l1_score"], 66)
        self.assertEqual(detail["imported_baseline"]["job_score"], 88)
        self.assertEqual(detail["application"]["status"], "manual_confirmed")

    def test_message_center_routes_are_not_registered(self):
        paths = {route.path for route in main.app.routes}
        self.assertFalse(any(path.startswith("/api/chat/") for path in paths))


if __name__ == "__main__":
    unittest.main()
