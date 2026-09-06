"""run.py 统一入口测试：服务参数解析、引擎再入派发与 uvicorn 启动参数。

run.py 是打包 exe 的唯一入口，锁定三类契约：
1. 服务模式参数：可选端口（默认 8787，与 start.bat 对齐）与 --no-browser；
2. 引擎再入模式：--run-engine 后的全部参数交给采集引擎 argparse，
   子进程端到端可跑（--help 返回 0 且暴露 boss-copilot 增强参数）；
3. uvicorn 以 app 对象启动（冻结后无法按模块字符串再导入）。

跑法：.venv/bin/python -m unittest discover tests -v
回归要求：任何 backend/ 改动后必须全量通过。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 测试库隔离到临时目录，绝不碰 ~/.boss-copilot 真实数据
_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-test-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import run  # noqa: E402

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"


class ParseServiceArgsTests(unittest.TestCase):
    """服务模式参数：端口默认/覆盖/非法值与 --no-browser。"""

    def test_defaults(self):
        self.assertEqual(run.parse_service_args([]), (8787, True))

    def test_positional_port(self):
        self.assertEqual(run.parse_service_args(["9000"]), (9000, True))

    def test_no_browser_flag(self):
        self.assertEqual(run.parse_service_args(["--no-browser"]), (8787, False))
        self.assertEqual(run.parse_service_args(["9000", "--no-browser"]),
                         (9000, False))

    def test_invalid_token_rejected(self):
        with self.assertRaises(SystemExit):
            run.parse_service_args(["abc"])

    def test_out_of_range_port_rejected(self):
        with self.assertRaises(SystemExit):
            run.parse_service_args(["70000"])
        with self.assertRaises(SystemExit):
            run.parse_service_args(["0"])


class RunServiceTests(unittest.TestCase):
    """服务模式：以 app 对象启动 uvicorn（冻结态无法按模块字符串导入）。"""

    def test_uvicorn_runs_app_object_with_host_port(self):
        with mock.patch("uvicorn.run") as uvicorn_run:
            run.run_service(9000, open_browser=False)
        uvicorn_run.assert_called_once()
        args, kwargs = uvicorn_run.call_args
        self.assertEqual(kwargs.get("host"), run.DEFAULT_HOST)
        self.assertEqual(kwargs.get("port"), 9000)

    def test_main_routes_to_engine_mode_without_uvicorn(self):
        """--run-engine 派发：不再启动服务，而是以引擎脚本为 __main__ 执行。"""
        executed = {}

        def fake_run_path(path, run_name=None):
            executed["path"] = path
            executed["run_name"] = run_name
            executed["argv"] = list(sys.argv)

        with mock.patch.object(run, "runpy") as runpy_mock, \
                mock.patch("uvicorn.run") as uvicorn_run:
            runpy_mock.run_path.side_effect = fake_run_path
            run.main([config.ENGINE_REENTRY_FLAG, "--keyword", "AI"])
        self.assertIsNone(uvicorn_run.call_args)
        self.assertEqual(executed["run_name"], "__main__")
        self.assertEqual(Path(executed["path"]), config.SCRAPER_SCRIPT)
        self.assertEqual(executed["argv"],
                         [str(config.SCRAPER_SCRIPT), "--keyword", "AI"])


class EngineReentryEndToEndTests(unittest.TestCase):
    """子进程端到端：run.py --run-engine --help 等价于直接跑引擎脚本。"""

    def test_engine_help_via_reentry(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "run.py"),
             config.ENGINE_REENTRY_FLAG, "--help"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=60, cwd=str(_REPO_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        # 与 test_builtin_scraper_engine 的契约对齐：增强参数仍在
        self.assertIn("--company", proc.stdout)
        self.assertIn("--dup-stop-ratio", proc.stdout)


if __name__ == "__main__":
    unittest.main()
