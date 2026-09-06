"""M8 测试：LLM 连通性、单/双账号路由、登录态提示与岗位详情入口。"""
import json
import os
import ntpath
import sys
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
        get_db().execute("DELETE FROM account_states")
        get_db().commit()

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

    def test_low_level_stopped_login_state_has_friendly_hint(self):
        with patch.object(cdp, "is_running", return_value=False):
            state = cdp.login_state("account_a")
        self.assertFalse(state["running"])
        self.assertIsNone(state["logged_in"])
        self.assertIn("Chrome 未启动", state["hint"])

    def test_login_marker_wins_over_user_marker(self):
        state = cdp._classify_login_dom("account_a", {"login": True, "user": True})
        self.assertFalse(state["logged_in"])

    def test_unknown_dom_never_claims_logged_in(self):
        state = cdp._classify_login_dom("account_a", {"login": False, "user": False})
        self.assertIsNone(state["logged_in"])

    def test_authenticated_geek_route_is_logged_in(self):
        state = cdp._classify_login_dom(
            "collect", {"login": False, "user": False, "authenticated": True})
        self.assertTrue(state["logged_in"])

    def test_dom_probe_ignores_hidden_login_panel(self):
        self.assertIn(".some(visible)", cdp.LOGIN_STATE_JS)

    def test_check_temporarily_starts_opens_stops_and_saves(self):
        detected = {"account": "account_a", "running": True,
                    "logged_in": False, "hint": "未登录"}
        with patch.object(cdp, "is_running", return_value=False), \
             patch.object(cdp, "launch", return_value={"ok": True,
                                                        "already_running": False}), \
             patch.object(cdp, "_navigate_login_page", return_value={"ok": True}), \
             patch.object(cdp, "login_state", return_value=detected), \
             patch.object(cdp, "stop", return_value={"ok": True}) as stop:
            result = cdp.check_login_state("account_a", wait_sec=0)
        self.assertFalse(result["logged_in"])
        self.assertFalse(result["running"])
        self.assertTrue(result["auto_started"])
        stop.assert_called_once_with("account_a")
        self.assertFalse(cdp.saved_login_state("account_a")["logged_in"])

    def test_check_does_not_stop_preexisting_chrome(self):
        detected = {"account": "account_a", "running": True,
                    "logged_in": True, "hint": ""}
        with patch.object(cdp, "is_running", return_value=True), \
             patch.object(cdp, "login_state", return_value=detected), \
             patch.object(cdp, "launch") as launch, \
             patch.object(cdp, "_navigate_login_page") as navigate, \
             patch.object(cdp, "stop") as stop:
            result = cdp.check_login_state("account_a", wait_sec=0)
        self.assertTrue(result["logged_in"])
        self.assertFalse(result["auto_started"])
        launch.assert_not_called()
        navigate.assert_not_called()
        stop.assert_not_called()

    def test_check_failure_still_stops_and_saves_unknown(self):
        with patch.object(cdp, "is_running", return_value=False), \
             patch.object(cdp, "launch", return_value={"ok": True,
                                                        "already_running": False}), \
             patch.object(cdp, "_navigate_login_page",
                          side_effect=RuntimeError("navigate")), \
             patch.object(cdp, "stop", return_value={"ok": True}) as stop:
            result = cdp.check_login_state("account_a", wait_sec=0)
        self.assertIsNone(result["logged_in"])
        self.assertIn("检测失败", result["hint"])
        stop.assert_called_once_with("account_a")
        self.assertIsNone(cdp.saved_login_state("account_a")["logged_in"])

    def test_status_returns_saved_state_without_profile_path(self):
        cdp._save_login_state("account_a", {"logged_in": False, "hint": "未登录"})
        with patch.object(cdp, "is_running", return_value=False):
            status = cdp.status()["account_a"]
        self.assertFalse(status["login_state"]["logged_in"])
        self.assertNotIn("profile", status)

    def test_status_refreshes_saved_state_for_running_chrome(self):
        cdp._save_login_state("collect", {"logged_in": False, "hint": "未登录"})
        live = {"account": "collect", "running": True,
                "logged_in": True, "hint": ""}
        with patch.object(cdp, "is_running", side_effect=lambda port: port == 9222), \
             patch.object(cdp, "browser_version", return_value="Chrome/test"), \
             patch.object(cdp, "login_state", return_value=live):
            status = cdp.status()["collect"]
        self.assertTrue(status["login_state"]["logged_in"])
        self.assertTrue(cdp.saved_login_state("collect")["logged_in"])

    def test_scraper_receives_selected_cdp_port(self):
        completed = SimpleNamespace(returncode=0, stdout="完成\n", stderr="")
        with patch.object(collector.subprocess, "run", return_value=completed) as run:
            collector._run_scraper(["--keyword", "AI"], timeout=10, cdp_port=9223)
        command = run.call_args.args[0]
        port_index = command.index("--cdp-port")
        output_index = command.index("--output")
        self.assertEqual(command[port_index + 1], "9223")
        self.assertEqual(Path(command[output_index + 1]).parent, collector.RESULT_DIR)

    def test_open_boss_job_page_uses_communication_chrome(self):
        calls = []

        class FakeSocket:
            def send(self, raw):
                calls.append(json.loads(raw))

            def recv(self):
                message = calls[-1]
                result = {"targetId": "job-target"} \
                    if message["method"] == "Target.createTarget" else {}
                return json.dumps({"id": message["id"], "result": result})

            def close(self):
                pass

        fake_websocket = SimpleNamespace(create_connection=lambda *_args, **_kwargs: FakeSocket())
        with patch.object(cdp, "launch", return_value={"ok": True}), \
             patch.object(cdp, "_http_get_json", return_value={
                 "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/browser/test"}), \
             patch.dict(sys.modules, {"websocket": fake_websocket}):
            result = cdp.open_boss_job_page("https://www.zhipin.com/job_detail/test.html")
        self.assertEqual(result, {"ok": True, "account": "account_a", "port": 9223})
        self.assertEqual(calls[0]["method"], "Target.createTarget")
        self.assertEqual(calls[0]["params"]["url"], "https://www.zhipin.com/job_detail/test.html")
        self.assertEqual(calls[1], {"id": 2, "method": "Target.activateTarget",
                                    "params": {"targetId": "job-target"}})

    def test_open_boss_job_page_rejects_non_boss_link(self):
        with patch.object(cdp, "launch") as launch:
            result = cdp.open_boss_job_page("https://example.com/job")
        self.assertFalse(result["ok"])
        self.assertIn("有效的 BOSS", result["error"])
        launch.assert_not_called()


class PathMigrationTests(unittest.TestCase):
    def test_legacy_directories_move_into_unified_data_root(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            home, data = root / "home", root / "data"
            old_collect = home / ".boss-zhipin-scraper" / "chrome-profile"
            old_result = home / ".boss-zhipin-scraper" / "job-result"
            old_communication = data / "chrome-profile-a"
            for directory, filename in ((old_collect, "cookie"),
                                        (old_result, "jobs.json"),
                                        (old_communication, "state")):
                directory.mkdir(parents=True)
                (directory / filename).write_text("x")

            report = config.migrate_legacy_data(data_dir=data, home_dir=home,
                                                check_ports=False)
            self.assertEqual({item["status"] for item in report}, {"moved"})
            self.assertTrue((data / "chrome-profile-collect" / "cookie").exists())
            self.assertTrue((data / "job-result" / "jobs.json").exists())
            self.assertTrue((data / "chrome-profile-communication" / "state").exists())
            self.assertFalse(old_collect.exists())

    def test_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            home, data = root / "home", root / "data"
            source = home / ".boss-zhipin-scraper" / "job-result"
            target = data / "job-result"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "old.json").write_text("old")
            (target / "new.json").write_text("new")
            report = config.migrate_legacy_data(data_dir=data, home_dir=home,
                                                check_ports=False)
            item = next(x for x in report if x["source"] == str(source))
            self.assertEqual(item["status"], "skipped_target_exists")
            self.assertTrue((source / "old.json").exists())
            self.assertEqual((target / "new.json").read_text(), "new")

    def test_windows_chrome_path_uses_local_app_data(self):
        env = {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}
        path = config.default_chrome_path(system="Windows", environ=env)
        self.assertEqual(path, ntpath.join(env["LOCALAPPDATA"], "Google", "Chrome",
                                           "Application", "chrome.exe"))


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

    def test_open_boss_endpoint_uses_saved_job_link(self):
        get_db().execute(
            "UPDATE jobs SET job_link=? WHERE job_key=?",
            ("https://www.zhipin.com/job_detail/test.html", "jd-key"))
        get_db().commit()
        expected = {"ok": True, "account": "account_a", "port": 9223}
        with patch.object(cdp, "open_boss_job_page", return_value=expected) as open_page:
            self.assertEqual(main.job_open_boss("jd-key"), expected)
        open_page.assert_called_once_with("https://www.zhipin.com/job_detail/test.html")

    def test_frontend_uses_component_click_handler_and_friendly_states(self):
        source = (config.BASE_DIR / "frontend" / "app.js").read_text()
        self.assertIn('@click="toggleExpanded(job)"', source)
        self.assertIn("职位描述（JD）", source)
        self.assertIn("测试连通性", source)
        self.assertIn("账号管理", source)
        self.assertIn("演示模式：已模拟打开", source)
        self.assertIn('@click="openBoss(activeJob)"', source)
        self.assertIn("event.target.checked", source)
        self.assertIn("上次检测", source)
        self.assertNotIn("{{a.profile}}", source)
        self.assertNotIn("~/.boss-zhipin-scraper/job-result/", source)
        self.assertNotIn("alert(JSON.stringify(r))", source)


if __name__ == "__main__":
    unittest.main()
