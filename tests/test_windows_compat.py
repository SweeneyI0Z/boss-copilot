"""跨平台兼容测试：Windows venv 布局、子进程 UTF-8 编码与暂停/恢复的挂起语义。

背景：项目需兼容 macOS 与 Windows。Windows 无 SIGSTOP/SIGCONT、控制台默认 GBK，
本文件锁定三处关键行为——外部采集仓库的解释器布局、scraper 子进程的编码约定、
采集暂停/恢复在 Windows 上的等价实现。

跑法：.venv/bin/python -m unittest discover tests -v
回归要求：任何 backend/ 改动后必须全量通过。
"""
import ctypes
import os
import signal
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

from backend import collector  # noqa: E402
from backend.boss import cdp  # noqa: E402


class FakeProcess:
    """最小化 Popen/run 替身。

    stdout 为空串时既能满足 subprocess.run 的 .stdout 属性读取，也能让
    流式读取线程立即迭代到 EOF；非空时仅用于 run 分支的文本断言。
    """

    def __init__(self, returncode=None, pid=4321, stdout="", stderr=""):
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self._poll_result = returncode
        self.sent = []
        self.killed = False

    def poll(self):
        return self._poll_result

    def send_signal(self, sig):
        self.sent.append(sig)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def _stub_pace():
    """固定采集档位，避免单测触碰 settings 表。"""
    return {"label": "均衡", "task_gap_sec": 60, "env": {}, "dup_stop_ratio": None}


class ScraperPythonLayoutTests(unittest.TestCase):
    """外部采集仓库 venv 解释器：Windows 与 POSIX 布局不同。"""

    def test_windows_layout_uses_scripts_dir(self):
        python_path = config.default_scraper_python(
            r"D:\repos\boss-zhipin-scraper", is_nt=True)
        self.assertEqual(python_path.parts[-3:],
                         (".venv", "Scripts", "python.exe"))

    def test_posix_layout_uses_bin_dir(self):
        python_path = config.default_scraper_python("/opt/boss-zhipin-scraper",
                                                    is_nt=False)
        self.assertEqual(python_path.parts[-3:], (".venv", "bin", "python"))

    def test_module_constant_follows_current_os(self):
        self.assertEqual(config.SCRAPER_PY,
                         config.default_scraper_python(config.SCRAPER_DIR))


class ScraperEncodingTests(unittest.TestCase):
    """scraper 子进程必须按 UTF-8 收发，写入端经 PYTHONIOENCODING 同步约束。"""

    def test_run_scraper_reads_utf8_and_forces_child_utf8(self):
        fake = FakeProcess(returncode=0)
        with mock.patch.object(collector.subprocess, "run",
                               return_value=fake) as run, \
                mock.patch.object(collector, "resolve_collect_pace",
                                  return_value=_stub_pace()):
            collector._run_scraper(["--keyword", "测试"], timeout=5,
                                   cdp_port=9222)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        command = run.call_args.args[0]
        self.assertEqual(command[0], str(collector.SCRAPER_PY))

    def test_streamed_popen_reads_utf8_line_buffered(self):
        fake = FakeProcess(returncode=0)
        with mock.patch.object(collector.subprocess, "Popen",
                               return_value=fake) as popen, \
                mock.patch.object(collector, "resolve_collect_pace",
                                  return_value=_stub_pace()):
            collector._run_scraper(["--keyword", "测试"], timeout=5,
                                   cdp_port=9222, on_output=lambda line: None)
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["bufsize"], 1)
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")


class PauseSignalRoutingTests(unittest.TestCase):
    """暂停/恢复动作：POSIX 走真实信号，Windows 走进程挂起 API。"""

    @unittest.skipUnless(os.name == "posix", "POSIX 才有 SIGSTOP/SIGCONT")
    def test_posix_sends_real_signals(self):
        paused = FakeProcess()
        self.assertTrue(collector._signal_process(paused, signal.SIGSTOP))
        self.assertEqual(paused.sent, [signal.SIGSTOP])
        resumed = FakeProcess()
        self.assertTrue(collector._signal_process(resumed, signal.SIGCONT))
        self.assertEqual(resumed.sent, [signal.SIGCONT])

    def test_dead_process_returns_false_without_side_effect(self):
        dead = FakeProcess(returncode=0)
        # Windows 无 SIGSTOP/SIGCONT，用暂停/恢复哨兵验证同一“已死进程不动手”语义
        pause = collector._SIG_PAUSE if os.name == "nt" else signal.SIGSTOP
        resume = collector._SIG_RESUME if os.name == "nt" else signal.SIGCONT
        self.assertFalse(collector._signal_process(dead, pause))
        self.assertEqual(dead.sent, [])
        self.assertFalse(collector._signal_process(None, resume))

    def test_windows_routing_maps_sentinels_to_suspend_resume(self):
        suspended_flags = []
        fake = FakeProcess()

        def record(_process, suspend):
            suspended_flags.append(suspend)
            return True

        with mock.patch.object(collector.os, "name", "nt"), \
                mock.patch.object(collector, "_windows_suspend_process",
                                  side_effect=record):
            self.assertTrue(collector._signal_process(fake, collector._SIG_PAUSE))
            self.assertTrue(collector._signal_process(fake, collector._SIG_RESUME))
            self.assertFalse(collector._signal_process(fake, None))
        self.assertEqual(suspended_flags, [True, False])
        self.assertEqual(fake.sent, [])

    def test_windows_suspend_helper_opens_frees_handle(self):
        """正常路径：OpenProcess→Nt*SuspendProcess→CloseHandle，返回 NTSTATUS 判定。"""
        fake = FakeProcess(pid=5555)
        ntdll = mock.MagicMock()
        ntdll.NtSuspendProcess.return_value = 0
        ntdll.NtResumeProcess.return_value = 0xC0000001  # 非 SUCCESS 视为失败
        kernel32 = mock.MagicMock()
        kernel32.OpenProcess.return_value = 7
        windll = mock.MagicMock()
        windll.kernel32 = kernel32
        windll.ntdll = ntdll
        with mock.patch.object(ctypes, "windll", windll, create=True):
            self.assertTrue(collector._windows_suspend_process(fake, suspend=True))
            # 恢复调用返回非 SUCCESS（NTSTATUS≠0）时必须如实报告 False
            self.assertFalse(collector._windows_suspend_process(fake, suspend=False))
        self.assertEqual(kernel32.OpenProcess.call_count, 2)
        for opened in kernel32.OpenProcess.call_args_list:
            self.assertEqual(opened.args[2], 5555)  # 第三个参数是目标 pid
        self.assertEqual(kernel32.CloseHandle.call_count, 2)

    def test_windows_suspend_helper_degrades_on_failure(self):
        """打不开句柄或系统调用报错时必须静默降级为 False，绝不向上抛。"""
        fake = FakeProcess(pid=999999)
        kernel32 = mock.MagicMock()
        kernel32.OpenProcess.return_value = 0
        windll = mock.MagicMock()
        windll.kernel32 = kernel32
        with mock.patch.object(ctypes, "windll", windll, create=True):
            self.assertFalse(collector._windows_suspend_process(fake, suspend=True))
        kernel32.OpenProcess.side_effect = OSError("access denied")
        with mock.patch.object(ctypes, "windll", windll, create=True):
            self.assertFalse(collector._windows_suspend_process(fake, suspend=True))


class CdpStopEncodingTests(unittest.TestCase):
    """stop() 按 user-data-dir 精准关闭 Chrome：进程列表必须按 UTF-8 读取。"""

    def setUp(self):
        self.marker = str(config.ACCOUNTS["collect"]["profile_dir"])

    def _process_listing(self):
        """模拟进程列表：一行目标 chrome、一行他号 chrome、一行无关进程。"""
        return (f"  111\tchrome --user-data-dir={self.marker} --remote-debugging\n"
                f"  999\tchrome --user-data-dir=C:\\其他\\路径 --foo\n"
                f"  222\tpython scripts/worker.py\n")

    def _stop_with_stubbed_run(self):
        """替换 cdp 的 subprocess.run，记录每次调用并返回伪造的进程列表。"""
        calls = []

        def fake_run(argv, *args, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            if argv[0] in ("ps", "powershell"):
                return mock.Mock(returncode=0,
                                 stdout=self._process_listing(), stderr="")
            return mock.Mock(returncode=0)

        result = None
        with mock.patch.object(cdp.subprocess, "run", side_effect=fake_run):
            result = cdp.stop("collect")
        return calls, result

    @unittest.skipUnless(os.name == "posix", "验证 POSIX 平台的 ps 分支")
    def test_posix_listing_uses_utf8_decode(self):
        calls, result = self._stop_with_stubbed_run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], 1)
        listing_argv, listing_kwargs = calls[0]
        self.assertEqual(listing_argv, ["ps", "-axo", "pid=,command="])
        self.assertEqual(listing_kwargs["encoding"], "utf-8")
        self.assertEqual(listing_kwargs["errors"], "replace")
        kill_argv, _ = calls[1]
        self.assertEqual(kill_argv, ["kill", "111"])

    def test_windows_branch_forces_powershell_utf8_output(self):
        """PowerShell 默认 GBK 输出：不强制 UTF-8 时中文用户名路径匹配失败。"""
        calls, result = None, None
        with mock.patch.object(cdp.os, "name", "nt"):
            calls, result = self._stop_with_stubbed_run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], 1)
        listing_argv, listing_kwargs = calls[0]
        self.assertEqual(listing_argv[0], "powershell")
        self.assertIn("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;",
                      listing_argv[-1])
        self.assertIn("Get-CimInstance Win32_Process", listing_argv[-1])
        self.assertEqual(listing_kwargs["encoding"], "utf-8")
        self.assertEqual(listing_kwargs["errors"], "replace")
        kill_argv, _ = calls[1]
        self.assertEqual(kill_argv[:3], ["taskkill", "/PID", "111"])


if __name__ == "__main__":
    unittest.main()

