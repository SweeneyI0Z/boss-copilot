"""M11 多简历评分、招呼语修订与平台协同证据。"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m11-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import greeting, llm, main, platform_status, resumes, sender, strategy  # noqa: E402
from backend.db import get_db, get_setting, init_db, now_iso, set_setting  # noqa: E402
from backend.scoring import l1, l2  # noqa: E402


def _fake_client(content):
    def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def _reset():
    init_db()
    conn = get_db()
    for table in ("applications", "job_resume_scores", "job_score_baselines",
                  "greetings", "sent_log", "job_details", "jobs", "resumes"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("UPDATE profile SET resume_text='',expectations='{}',updated_at=? WHERE id=1",
                 (now_iso(),))
    conn.commit()


def _job(key="m11"):
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "INSERT INTO jobs(job_key,title,company,salary,salary_min,salary_max,status,"
        "job_link,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (key, "嵌入式 AI 工程师", "示例科技", "25-35K", 25, 35, "active",
         f"https://www.zhipin.com/job_detail/{key}.html", ts, ts))
    conn.execute(
        "INSERT INTO job_details(job_key,jd,skill_tags,fetched_at) VALUES(?,?,?,?)",
        (key, "负责 RustMagic 设备端工具与 Agent 系统开发", "RustMagic", ts))
    conn.commit()


class ResumeScoringTests(unittest.TestCase):
    def setUp(self):
        _reset()
        _job()
        self.a = resumes.create_resume(
            "交叉方向", "完整简历正文" * 20,
            skill_profile={"skills": ["RustMagic"]}, make_default=True)
        self.b = resumes.create_resume(
            "传统方向", "另一份完整简历正文" * 20,
            skill_profile={"skills": ["完全无关技能"]})

    def test_l1_scores_are_isolated_by_resume_revision(self):
        l1.run_l1(force=True, resume_id=self.a["id"])
        l1.run_l1(force=True, resume_id=self.b["id"])
        score_a = resumes.get_job_score("m11", self.a["id"])
        score_b = resumes.get_job_score("m11", self.b["id"])
        self.assertGreater(score_a["match_rough"], score_b["match_rough"])
        self.assertNotEqual(score_a["resume_id"], score_b["resume_id"])

    def test_resume_change_creates_revision_and_marks_old_l2_stale(self):
        resumes.save_job_score(
            "m11", self.a["id"], self.a["revision"], composite=80,
            l2_detail={"summary": "旧评分"}, l2_source="llm")
        changed = resumes.update_resume(
            self.a["id"], resume_text="更新后的完整简历" * 20)
        self.assertEqual(changed["revision"], self.a["revision"] + 1)
        old = resumes.get_job_score("m11", self.a["id"], self.a["revision"])
        self.assertTrue(old["l2_stale"])
        self.assertIsNone(resumes.get_job_score("m11", self.a["id"], changed["revision"]))

    def test_same_job_resume_cannot_run_concurrent_l2(self):
        resumes.save_job_score(
            "m11", self.a["id"], self.a["revision"], l1_score=70,
            composite_rough=70, l1_detail={"cap": 100}, l1_source="engine")
        started, release = threading.Event(), threading.Event()
        response = ('{"dims":{"A":10},"s":{"S1":60},"adjust":[],'
                    '"summary":"完成","advice":"正常投"}')

        def create(**kwargs):
            started.set()
            release.wait(2)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=response))])

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        errors = []

        def first_call():
            try:
                l2.score_job_llm("m11", client=client, resume_id=self.a["id"], force=True)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=first_call)
        thread.start()
        self.assertTrue(started.wait(1))
        with self.assertRaisesRegex(llm.LLMError, "正在进行"):
            l2.score_job_llm("m11", client=client, resume_id=self.a["id"], force=True)
        release.set()
        thread.join(2)
        self.assertFalse(errors)

    def test_l2_finishing_after_resume_update_is_saved_stale(self):
        resumes.save_job_score(
            "m11", self.a["id"], self.a["revision"], l1_score=70,
            composite_rough=70, l1_detail={"cap": 100}, l1_source="engine")
        started, release = threading.Event(), threading.Event()
        response = ('{"dims":{"A":10},"s":{"S1":60},"adjust":[],'
                    '"summary":"旧修订结果","advice":"正常投"}')

        def create(**kwargs):
            started.set()
            release.wait(2)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=response))])

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        thread = threading.Thread(target=lambda: l2.score_job_llm(
            "m11", client=client, resume_id=self.a["id"], force=True))
        thread.start()
        self.assertTrue(started.wait(1))
        resumes.update_resume(self.a["id"], resume_text="更新中的简历" * 20)
        release.set()
        thread.join(2)
        old = resumes.get_job_score("m11", self.a["id"], self.a["revision"])
        self.assertTrue(old["l2_stale"])


class GreetingWorkflowTests(unittest.TestCase):
    def setUp(self):
        _reset()
        _job()
        self.resume = resumes.create_resume(
            "主投简历", "4年软硬件系统研发与嵌入式 AI IDE 项目经验。" * 20,
            make_default=True)
        set_setting("llm_base_url", "https://llm.example/v1")
        set_setting("llm_api_key", "secret")
        set_setting("llm_model", "model")

    def test_three_variants_bind_resume_and_manual_confirmation(self):
        response = ('{"professional":"您好，我具备4年系统研发经验，希望进一步沟通。",'
                    '"concise":"您好，我的嵌入式与AI经验和岗位匹配，期待沟通。",'
                    '"technical":"您好，我做过固件、协议与Agent工具链，期待交流。"}')
        generated = greeting.generate(
            "m11", client=_fake_client(response), resume_id=self.resume["id"])
        self.assertEqual(generated["variant_labels"],
                         ["专业完整版", "精简版", "技术聚焦版"])
        self.assertEqual(len(generated["variants"]), 3)
        self.assertEqual(generated["resume_revision"], self.resume["revision"])
        greeting.approve(generated["id"], chosen_text="人工编辑后的招呼语")
        confirmed = greeting.confirm_manual(generated["id"])
        self.assertTrue(confirmed["ok"])
        row = get_db().execute(
            "SELECT * FROM greetings WHERE id=?", (generated["id"],)).fetchone()
        self.assertEqual((row["chosen"], row["delivery_channel"], row["delivery_status"]),
                         ("人工编辑后的招呼语", "manual", "confirmed"))

    def test_old_resume_revision_cannot_enter_automatic_batch(self):
        response = ('{"professional":"专业版","concise":"精简版",'
                    '"technical":"技术版"}')
        generated = greeting.generate(
            "m11", client=_fake_client(response), resume_id=self.resume["id"])
        greeting.approve(generated["id"], 0)
        resumes.update_resume(self.resume["id"], resume_text="新版简历正文" * 20)
        self.assertEqual(greeting.pending_batch(), [])

    def test_omitted_resume_still_binds_default_revision(self):
        response = ('{"professional":"专业版","concise":"精简版",'
                    '"technical":"技术版"}')
        generated = greeting.generate("m11", client=_fake_client(response))
        self.assertEqual(generated["resume_id"], self.resume["id"])
        self.assertEqual(generated["resume_revision"], self.resume["revision"])


class ReliabilityBoundaryTests(unittest.TestCase):
    def test_settings_cannot_relax_hard_send_guards(self):
        _reset()
        main.write_settings({
            "send_daily_limit": 999, "send_daily_hard_cap": 999,
            "send_gap_min_sec": 0, "send_gap_max_sec": 999,
        })
        self.assertEqual(get_setting("send_daily_hard_cap"), 110)
        self.assertEqual(get_setting("send_daily_limit"), 110)
        self.assertEqual(get_setting("send_gap_min_sec"), 30)
        self.assertEqual(get_setting("send_gap_max_sec"), 90)

    def test_platform_probe_only_accepts_explicit_completed_state(self):
        self.assertEqual(platform_status.classify_application_text(
            "发送简历 继续沟通")["status"], "unknown")
        result = platform_status.classify_application_text("在线简历已发送")
        self.assertEqual(result["status"], "platform_confirmed")
        self.assertTrue(platform_status.classify_application_text("需要安全验证")["risk"])

    def test_platform_transport_failure_degrades_to_unknown(self):
        with patch.object(platform_status.cdp, "launch", return_value={"ok": True}), \
                patch.object(platform_status.cdp, "login_state",
                             return_value={"logged_in": True}), \
                patch.object(platform_status, "_BrowserSession",
                             side_effect=RuntimeError("websocket timeout")):
            result = platform_status.probe_job_page("https://www.zhipin.com/job_detail/x.html")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("探测失败", result["hint"])

    def test_archived_resume_cannot_create_new_artifacts(self):
        _reset()
        _job("archived")
        first = resumes.create_resume("待归档", "完整正文" * 20, make_default=True)
        resumes.create_resume("保留简历", "另一份正文" * 20)
        resumes.archive_resume(first["id"])
        with self.assertRaises(ValueError):
            l1.run_l1(force=True, resume_id=first["id"])
        with self.assertRaises(ValueError):
            resumes.save_job_score("archived", first["id"], l1_score=1)
        with self.assertRaises(llm.LLMError):
            greeting.generate("archived", resume_id=first["id"])

    def test_sender_source_enables_focus_emulation(self):
        import inspect
        self.assertIn("Emulation.setFocusEmulationEnabled",
                      inspect.getsource(sender.send_batch))

    def test_collect_plan_is_isolated_by_resume(self):
        strategy.save_plan({"searches": [{"keyword": "固件"}]}, resume_id=1)
        strategy.save_plan({"searches": [{"keyword": "Agent"}]}, resume_id=2)
        self.assertEqual(strategy.get_plan(1)["searches"][0]["keyword"], "固件")
        self.assertEqual(strategy.get_plan(2)["searches"][0]["keyword"], "Agent")

    def test_on_demand_probe_persists_only_explicit_platform_confirmation(self):
        _reset()
        _job("probe")
        resume = resumes.create_resume("探测简历", "完整简历" * 30, make_default=True)
        with patch("backend.platform_status.probe_job_page", return_value={
                "status": "platform_confirmed", "evidence": "简历已发送", "risk": False}):
            result = main.application_probe("probe", {"resume_id": resume["id"]})
        self.assertEqual(result["status"], "platform_confirmed")
        self.assertEqual(result["probe_evidence"], "简历已发送")


if __name__ == "__main__":
    unittest.main()
