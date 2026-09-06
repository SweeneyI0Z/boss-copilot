"""账号管理探测降级测试：websocket-client 缺失时接口必须可用而非 500。

背景：Windows 用户按 start.bat 指引装依赖时容易漏装 websocket-client，
Chrome 一旦运行，账号页的状态探测就会踩到缺失依赖——此前直接抛
ModuleNotFoundError 把整个 /api/accounts 炸成 500，前端表现为
「采集号操作失败」，账号管理整体不可用。本文件锁定降级行为：
单个账号的探测失败只降级为提示，绝不拖垮页面；同时锁定 Windows
taskkill 必须强杀（/F），否则无窗口 Chrome 进程幸存、端口被占。

跑法：.venv/bin/python -m unittest discover tests -v
回归要求：任何 backend/ 改动后必须全量通过。
"""
import os
import sys
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


def _clear_account_states():
    """清空检测记录表，避免同类用例之间的持久化状态串扰。"""
    conn = get_db()
    conn.execute("DELETE FROM account_states")
    conn.commit()


def _without_websocket():
    """让 `import websocket` 暂时失败，模拟依赖未安装的环境。"""
    return mock.patch.dict(sys.modules, {"websocket": None})


class LoadWebsocketTests(unittest.TestCase):
    """依赖加载器：缺失时必须抛出带修复指引的 RuntimeError。"""

    def test_missing_dependency_raises_actionable_error(self):
        with _without_websocket():
            with self.assertRaises(RuntimeError) as ctx:
                cdp._load_websocket()
        self.assertIn("websocket-client", str(ctx.exception))
        self.assertIn("pip install", str(ctx.exception))

    def test_present_dependency_returns_module(self):
        import websocket as real_websocket
        self.assertIs(cdp._load_websocket(), real_websocket)


class OpenLoginPageDegradationTests(unittest.TestCase):
    """打开登录页：依赖缺失时返回 ok:false 的可读错误，而非 500。"""

    def test_missing_dependency_returns_ok_false(self):
        with _without_websocket(), \
                mock.patch.object(cdp, "launch",
                                  return_value={"ok": True, "port": 9222}):
            result = cdp.open_login_page("collect")
        self.assertFalse(result["ok"])
        self.assertIn("websocket-client", result["error"])

    def test_launch_failure_short_circuits_before_websocket(self):
        with _without_websocket(), \
                mock.patch.object(cdp, "launch",
                                  return_value={"ok": False, "error": "CDP 未就绪"}):
            result = cdp.open_login_page("collect")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "CDP 未就绪")


class StatusDegradationTests(unittest.TestCase):
    """/api/accounts：单账号登录态探测失败不得拖垮整个账号页。"""

    def setUp(self):
        init_db()
        _clear_account_states()

    def test_probe_failure_degrades_to_login_error_field(self):
        with mock.patch.object(cdp, "is_running", return_value=True), \
                _without_websocket():
            out = cdp.status()
        self.assertEqual(set(out), {"collect", "account_a"})
        for name, entry in out.items():
            self.assertTrue(entry["running"])
            self.assertIn("websocket-client", entry["login_error"])
            self.assertIsNone(entry["login_state"])

    def test_probe_failure_does_not_save_detection_record(self):
        """降级不是检测：不得把错误提示写成「上次检测」记录。"""
        with mock.patch.object(cdp, "is_running", return_value=True), \
                _without_websocket():
            cdp.status()
        for name in ("collect", "account_a"):
            self.assertIsNone(cdp.saved_login_state(name))

    def test_healthy_probe_still_persists_result(self):
        with mock.patch.object(cdp, "is_running", return_value=True), \
                mock.patch.object(cdp, "login_state",
                                  return_value={"account": "collect",
                                                "running": True,
                                                "logged_in": False,
                                                "hint": "未登录"}):
            out = cdp.status()
        self.assertEqual(out["collect"]["login_error"], "")
        self.assertFalse(out["collect"]["login_state"]["logged_in"])


class CheckLoginStateDegradationTests(unittest.TestCase):
    """检测登录态：依赖缺失时落库一条带修复指引的失败结果。"""

    def setUp(self):
        init_db()
        _clear_account_states()

    def test_missing_dependency_yields_hint_not_exception(self):
        with mock.patch.object(cdp, "is_running", return_value=False), \
                mock.patch.object(cdp, "launch",
                                  return_value={"ok": True, "port": 9222}), \
                _without_websocket(), \
                mock.patch.object(cdp, "stop",
                                  return_value={"ok": True, "stopped": 1}):
            result = cdp.check_login_state("collect", wait_sec=0)
        self.assertIsNone(result["logged_in"])
        self.assertIn("websocket-client", result["hint"])
        self.assertTrue(result["auto_started"])


class WindowsForceKillTests(unittest.TestCase):
    """Windows 停止 Chrome 必须 /F 强杀：优雅关闭会留下占用端口的幸存进程。"""

    def _process_listing(self, marker):
        return (f"  111\tchrome --user-data-dir={marker} --remote-debugging\n"
                f"  999\tchrome --user-data-dir=C:\\其他\\路径 --foo\n")

    def test_windows_kill_uses_force_flag(self):
        marker = str(config.ACCOUNTS["collect"]["profile_dir"])
        calls = []

        def fake_run(argv, *args, **kwargs):
            calls.append(list(argv))
            if argv[0] == "powershell":
                return mock.Mock(returncode=0,
                                 stdout=self._process_listing(marker), stderr="")
            return mock.Mock(returncode=0)

        with mock.patch.object(cdp.os, "name", "nt"), \
                mock.patch.object(cdp.subprocess, "run", side_effect=fake_run):
            result = cdp.stop("collect")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], 1)
        kill_argv = calls[1]
        self.assertEqual(kill_argv,
                         ["taskkill", "/PID", "111", "/T", "/F"])


if __name__ == "__main__":
    unittest.main()
