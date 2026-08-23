"""M8 测试：LLM 连通性、单/双账号路由、登录态提示与岗位详情入口。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m8-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend import collector, llm, main  # noqa: E402
from backend.boss import cdp  # noqa: E402
from backend.db import get_db, init_db, set_setting  # noqa: E402


def fake_client(reply="OK", error=None):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=reply))])

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    return client, calls


class LLMConnectionTests(unittest.TestCase):
    def test_uses_unsaved_form_values(self):
        client, calls = fake_client("OK")
        result = llm.test_connection("https://llm.example/v1", "secret", "model-x",
                                     client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "model-x")
        self.assertEqual(result["reply"], "OK")
        self.assertEqual(calls[0]["model"], "model-x")
        self.assertEqual(calls[0]["max_tokens"], 8)

    def test_missing_fields_rejected_before_request(self):
        client, calls = fake_client()
        with self.assertRaisesRegex(llm.LLMError, "API Key"):
            llm.test_connection("https://llm.example/v1", "", "model-x",
                                client=client)
        self.assertEqual(calls, [])

    def test_provider_error_wrapped(self):
        client, _ = fake_client(error=RuntimeError("unauthorized"))
        with self.assertRaisesRegex(llm.LLMError, "连接失败.*unauthorized"):
            llm.test_connection("https://llm.example/v1", "bad", "model-x",
                                client=client)

    def test_api_endpoint_forwards_form_values(self):
        body = main.LLMTestIn(base_url="http://x", api_key="k", model="m")
        expected = {"ok": True, "model": "m", "reply": "OK", "latency_ms": 1}
        with patch("backend.llm.test_connection", return_value=expected) as call:
            self.assertEqual(main.test_llm_connection(body), expected)
        call.assert_called_once_with("http://x", "k", "m")


class AccountModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        set_setting("dual_account_enabled", True)

    def test_dual_account_routes_collection_to_collect_account(self):
        self.assertEqual(cdp.account_for("collect"), "collect")
        self.assertEqual(cdp.account_for("communication"), "account_a")

    def test_single_account_routes_both_to_communication_account(self):
        set_setting("dual_account_enabled", False)
        self.assertEqual(cdp.account_for("collect"), "account_a")
        self.assertEqual(cdp.account_for("communication"), "account_a")
        with patch.object(cdp, "is_running", return_value=False):
            status = cdp.status()
        self.assertFalse(status["collect"]["enabled"])
        self.assertEqual(status["account_a"]["roles"], ["采集", "沟通"])
        self.assertEqual(status["account_a"]["label"], "沟通号")

    def test_invalid_switch_value_rejected(self):
        with self.assertRaisesRegex(Exception, "必须是布尔值"):
            main.write_settings({"dual_account_enabled": "false"})

    def test_stopped_login_state_has_friendly_hint(self):
        with patch.object(cdp, "is_running", return_value=False):
            state = cdp.login_state("account_a")
        self.assertFalse(state["running"])
        self.assertIsNone(state["logged_in"])
        self.assertIn("Chrome 未启动", state["hint"])

    def test_scraper_receives_selected_cdp_port(self):
        completed = SimpleNamespace(returncode=0, stdout="完成\n", stderr="")
        with patch.object(collector.subprocess, "run", return_value=completed) as run:
            collector._run_scraper(["--keyword", "AI"], timeout=10, cdp_port=9223)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--cdp-port", "9223"])


class JobDetailAndFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        conn = get_db()
        conn.execute("DELETE FROM job_details")
        conn.execute("DELETE FROM jobs")
        conn.execute(
            "INSERT INTO jobs(job_key, title, company, first_seen_at, last_seen_at) "
            "VALUES(?,?,?,?,?)", ("jd-key", "AI 工程师", "示例公司", "t", "t"))
        conn.execute(
            "INSERT INTO job_details(job_key, jd, fetched_at) VALUES(?,?,?)",
            ("jd-key", "负责大模型应用开发", "t"))
        conn.commit()

    def test_job_detail_contains_full_jd(self):
        detail = main.job_detail("jd-key")
        self.assertEqual(detail["jd"], "负责大模型应用开发")

    def test_frontend_uses_component_click_handler_and_friendly_states(self):
        source = (config.BASE_DIR / "frontend" / "app.js").read_text()
        self.assertIn('@click="openJob(j)"', source)
        self.assertIn("encodeURIComponent(j.job_key)", source)
        self.assertIn("职位描述（JD）", source)
        self.assertIn("测试连通性", source)
        self.assertIn("账号管理", source)
        self.assertIn("const desired = event.target.checked", source)
        self.assertNotIn("alert(JSON.stringify(r))", source)


if __name__ == "__main__":
    unittest.main()
