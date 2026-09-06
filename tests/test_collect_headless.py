"""采集无头模式测试：自动采集无窗口运行，登录/手动启动保持弹窗。

背景：采集号 Chrome 此前一律有头启动，自动采集时窗口会抢占用户桌面。
本文件锁定无头化行为：
- 自动采集以 headless 启动采集号 Chrome；实测 Chrome 152 无头 UA 仍带
  HeadlessChrome 标记（会被 BOSS 风控直接识别），launch() 必须两段式
  修正 UA 并缓存；
- 用户主动要窗口（「启动」「打开登录页」）时，即便无头实例上采集仍在
  进行，也必须先关闭无头实例再以有头模式弹窗；
- 采集只复用运行中实例（无论有头无头），绝不重启打扰用户；
- 检测登录态临时启动的 Chrome 走无头，导航前必须开焦点仿真
  （BOSS SPA 无焦点不渲染）。

跑法：.venv/bin/python -m unittest discover tests -v
回归要求：任何 backend/ 改动后必须全量通过。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 测试库隔离到临时目录，绝不碰 ~/.boss-copilot 真实数据
_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-test-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"

from backend.boss import cdp  # noqa: E402
from backend.db import get_db, init_db  # noqa: E402

_MARKED_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) HeadlessChrome/152.0.0.0 Safari/537.36")
_FIXED_UA = _MARKED_UA.replace("HeadlessChrome", "Chrome")
_CLEAN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/160.0.0.0 Safari/537.36")


class _FakeCdp:
    """CDP 端口状态机替身：stop 释放端口，Popen 启动后视为就绪。"""

    def __init__(self, running=False):
        self.running = running
        self.popens = []
        self.stops = []

    def is_running(self, port):
        return self.running

    def stop(self, account):
        self.stops.append(account)
        self.running = False
        return {"ok": True, "stopped": 1}

    def popen(self, cmd, **kwargs):
        self.running = True
        self.popens.append(cmd)
        return mock.Mock()


class ChromeLaunchCommandTests(unittest.TestCase):
    """启动命令构造：无头标记只应出现在无头模式。"""

    def setUp(self):
        self.conf = {"cdp_port": 9222, "profile_dir": Path("C:/x/profile-collect")}

    def test_visible_command_has_no_headless_flags(self):
        cmd = cdp.chrome_launch_command(self.conf)
        self.assertIn("--remote-debugging-port=9222", cmd)
        self.assertIn(f"--user-data-dir={self.conf['profile_dir']}", cmd)
        self.assertIn("--remote-allow-origins=*", cmd)
        self.assertNotIn("--headless=new", cmd)
        self.assertFalse(any(a.startswith("--user-agent=") for a in cmd))

    def test_headless_command_carries_headless_flags(self):
        cmd = cdp.chrome_launch_command(self.conf, headless=True)
        self.assertIn("--headless=new", cmd)
        # 无头默认 800x600 视口会让 BOSS SPA 渲染异常，必须显式给尺寸
        self.assertIn("--window-size=1440,900", cmd)

    def test_user_agent_override_appended(self):
        cmd = cdp.chrome_launch_command(self.conf, headless=True,
                                        user_agent=_FIXED_UA)
        self.assertIn(f"--user-agent={_FIXED_UA}", cmd)


class HeadlessInstanceDetectionTests(unittest.TestCase):
    """按进程命令行判断运行中的实例是否无头（重启决策的依据）。"""

    def test_detects_headless_by_command_line(self):
        conf = config.ACCOUNTS["collect"]
        listing = (f"  111\tchrome.exe --headless=new "
                   f"--user-data-dir={conf['profile_dir']} "
                   f"--remote-debugging-port=9222\n")
        with mock.patch.object(cdp, "_chrome_process_lines", return_value=listing):
            self.assertTrue(cdp._headless_instance_running(conf))

    def test_visible_or_foreign_instances_are_not_headless(self):
        conf = config.ACCOUNTS["collect"]
        listing = (f"  111\tchrome.exe --user-data-dir={conf['profile_dir']}\n"
                   f"  222\tchrome.exe --headless=new --user-data-dir=C:\\其他\n")
        with mock.patch.object(cdp, "_chrome_process_lines", return_value=listing):
            self.assertFalse(cdp._headless_instance_running(conf))


class LaunchHeadlessTests(unittest.TestCase):
    """launch() 的无头语义：两段式 UA 修正、缓存、复用与重启边界。"""

    def setUp(self):
        cdp._headless_user_agent_cache = ""
        self.fake = _FakeCdp(running=False)
        patches = [mock.patch.object(cdp, "is_running", self.fake.is_running),
                   mock.patch.object(cdp, "stop", self.fake.stop),
                   mock.patch.object(cdp.subprocess, "Popen", self.fake.popen)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_headless_launch_rewrites_ua_and_caches(self):
        with mock.patch.object(cdp, "_http_get_json",
                               return_value={"User-Agent": _MARKED_UA}):
            out = cdp.launch("collect", wait_sec=1, headless=True)
        self.assertTrue(out["ok"])
        self.assertFalse(out["already_running"])
        # 两段式：先无 UA 启动探测，再带修正 UA 重启
        self.assertEqual(len(self.fake.popens), 2)
        self.assertIn("--headless=new", self.fake.popens[0])
        self.assertIn("--headless=new", self.fake.popens[1])
        self.assertIn(f"--user-agent={_FIXED_UA}", self.fake.popens[1])
        self.assertEqual(self.fake.stops, ["collect"])
        self.assertEqual(cdp._headless_user_agent_cache, _FIXED_UA)

    def test_headless_launch_reuses_cached_user_agent(self):
        cdp._headless_user_agent_cache = _FIXED_UA
        with mock.patch.object(cdp, "_http_get_json") as http:
            out = cdp.launch("collect", wait_sec=1, headless=True)
        self.assertTrue(out["ok"])
        # 缓存命中：单次启动直达，无需二段探测
        self.assertEqual(len(self.fake.popens), 1)
        self.assertIn(f"--user-agent={_FIXED_UA}", self.fake.popens[0])
        self.assertEqual(self.fake.stops, [])
        http.assert_not_called()

    def test_headless_launch_skips_restart_when_ua_already_clean(self):
        with mock.patch.object(cdp, "_http_get_json",
                               return_value={"User-Agent": _CLEAN_UA}):
            out = cdp.launch("collect", wait_sec=1, headless=True)
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.fake.popens), 1)
        self.assertEqual(self.fake.stops, [])

    def test_headless_launch_reuses_running_visible_instance(self):
        """采集撞上运行中的有头实例：复用，绝不重启打扰用户。"""
        self.fake.running = True
        with mock.patch.object(cdp, "_headless_instance_running",
                               return_value=False):
            out = cdp.launch("collect", headless=True)
        self.assertTrue(out["ok"])
        self.assertTrue(out["already_running"])
        self.assertFalse(out["headless"])
        self.assertEqual(self.fake.stops, [])
        self.assertEqual(self.fake.popens, [])

    def test_visible_launch_restarts_running_headless_instance(self):
        """用户要窗口撞上无头实例（采集可能进行中）：必须弹窗优先。"""
        self.fake.running = True
        with mock.patch.object(cdp, "_headless_instance_running",
                               return_value=True):
            out = cdp.launch("collect", wait_sec=1)
        self.assertTrue(out["ok"])
        self.assertFalse(out["already_running"])
        self.assertEqual(self.fake.stops, ["collect"])
        self.assertEqual(len(self.fake.popens), 1)
        self.assertNotIn("--headless=new", self.fake.popens[0])

    def test_visible_launch_reuses_running_visible_instance(self):
        self.fake.running = True
        with mock.patch.object(cdp, "_headless_instance_running",
                               return_value=False):
            out = cdp.launch("collect")
        self.assertTrue(out["ok"])
        self.assertTrue(out["already_running"])
        self.assertEqual(self.fake.stops, [])
        self.assertEqual(self.fake.popens, [])


class NavigateLoginPageTests(unittest.TestCase):
    """登录页导航：必须先开焦点仿真再导航，无头实例才能渲染 SPA。"""

    def test_focus_emulation_precedes_navigation(self):
        sent = []

        class FakeWS:
            def send(self, raw):
                sent.append(json.loads(raw))

            def recv(self):
                return "{}"

            def close(self):
                pass

        fake_module = mock.Mock()
        fake_module.create_connection.return_value = FakeWS()
        targets = [{"type": "page", "webSocketDebuggerUrl": "ws://x"}]
        with mock.patch.object(cdp, "_load_websocket", return_value=fake_module), \
                mock.patch.object(cdp, "_http_get_json", return_value=targets):
            out = cdp._navigate_login_page("collect")
        self.assertTrue(out["ok"])
        self.assertEqual([m["method"] for m in sent],
                         ["Emulation.setFocusEmulationEnabled", "Page.navigate"])
        self.assertEqual(sent[1]["params"]["url"], cdp.LOGIN_URL)

    def test_open_login_page_launches_visible(self):
        """「打开登录页」必须有头启动（无头实例会被重启弹窗），不传 headless。"""
        with mock.patch.object(cdp, "launch",
                               return_value={"ok": True, "port": 9222}) as launch, \
                mock.patch.object(cdp, "_navigate_login_page",
                                  return_value={"ok": True, "port": 9222}):
            cdp.open_login_page("collect")
        launch.assert_called_once_with("collect")


class CheckLoginStateHeadlessTests(unittest.TestCase):
    """检测登录态：临时实例必须无头，完成即停，绝不弹窗。"""

    def setUp(self):
        init_db()
        conn = get_db()
        conn.execute("DELETE FROM account_states")
        conn.commit()

    def test_temp_launch_is_headless_and_stopped_after(self):
        with mock.patch.object(cdp, "is_running", return_value=False), \
                mock.patch.object(cdp, "launch",
                                  return_value={"ok": True, "port": 9222}) as launch, \
                mock.patch.object(cdp, "_navigate_login_page",
                                  return_value={"ok": True, "port": 9222}), \
                mock.patch.object(cdp, "login_state",
                                  return_value={"account": "collect",
                                                "running": True,
                                                "logged_in": True, "hint": ""}), \
                mock.patch.object(cdp, "stop",
                                  return_value={"ok": True, "stopped": 1}) as stop:
            result = cdp.check_login_state("collect", wait_sec=0)
        launch.assert_called_once_with("collect", headless=True)
        self.assertTrue(result["auto_started"])
        self.assertTrue(result["logged_in"])
        stop.assert_called_once_with("collect")


class CollectorLaunchSiteTests(unittest.TestCase):
    """采集启动点必须以无头请求实例：锁定 collector 对 launch 的调用契约。"""

    def test_collection_workers_request_headless_launch(self):
        import inspect
        from backend import collector
        source = inspect.getsource(collector)
        self.assertEqual(source.count("cdp.launch(account, headless=True)"), 2,
                         "采集主流程与重试 worker 都必须以无头启动采集 Chrome")
        self.assertNotIn("cdp.launch(account)\n", source)


if __name__ == "__main__":
    unittest.main()
