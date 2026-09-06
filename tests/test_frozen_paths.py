"""打包态（PyInstaller frozen）路径与采集引擎再入旗标测试。

锁定三件事：
1. frozen 模拟态下 config 的 APP_ROOT/SCRAPER_DIR/FRONTEND_DIR 全部指向
   sys._MEIPASS，且 SCRAPER_LAUNCH_FLAGS 为再入旗标（exe 自再入跑引擎）；
2. BOSS_ZHIPIN_SCRAPER_HOME 覆盖在打包态仍然优先；
3. 开发态行为与历史逐字段一致（旗标为空、命令形态不变），collector 命令
   构造在两种形态下均正确。

frozen 模拟采用独立模块名加载 config（模块级常量在 import 期计算），
不 reload 共享的 backend.config，避免污染同进程其他用例。

跑法：.venv/bin/python -m unittest discover tests -v
回归要求：任何 backend/ 改动后必须全量通过。
"""
import importlib.util
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

from backend import collector  # noqa: E402

_CONFIG_FILE = Path(config.__file__)


def _load_config_as_frozen(meipass: Path):
    """模拟 PyInstaller 冻结态加载 config：sys.frozen/_MEIPASS 只作用于本次加载。"""
    spec = importlib.util.spec_from_file_location(
        "config_frozen_simulation", _CONFIG_FILE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "frozen", True, create=True), \
            mock.patch.object(sys, "_MEIPASS", str(meipass), create=True):
        spec.loader.exec_module(module)
    return module


class FrozenConfigTests(unittest.TestCase):
    """打包态路径解析：随包资源一律落在 _MEIPASS。"""

    def test_frozen_paths_point_into_meipass(self):
        meipass = Path(_TEST_HOME) / "_internal"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOSS_ZHIPIN_SCRAPER_HOME", None)
            frozen = _load_config_as_frozen(meipass)
        self.assertTrue(frozen.FROZEN)
        self.assertEqual(frozen.APP_ROOT, meipass)
        self.assertEqual(frozen.SCRAPER_DIR,
                         meipass / "vendor" / "boss-zhipin-scraper")
        self.assertEqual(frozen.SCRAPER_SCRIPT,
                         meipass / "vendor" / "boss-zhipin-scraper" / "scripts"
                         / "boss_cdp_raw.py")
        self.assertEqual(frozen.FRONTEND_DIR, meipass / "frontend")

    def test_frozen_engine_reentry_flag(self):
        meipass = Path(_TEST_HOME) / "_internal"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOSS_ZHIPIN_SCRAPER_HOME", None)
            frozen = _load_config_as_frozen(meipass)
        self.assertEqual(frozen.SCRAPER_LAUNCH_FLAGS,
                         (config.ENGINE_REENTRY_FLAG,))
        self.assertEqual(frozen.ENGINE_REENTRY_FLAG, "--run-engine")

    def test_frozen_scraper_home_override_still_wins(self):
        override = Path(_TEST_HOME) / "other-engine"
        with mock.patch.dict(os.environ,
                             {"BOSS_ZHIPIN_SCRAPER_HOME": str(override)}):
            frozen = _load_config_as_frozen(Path(_TEST_HOME) / "_internal")
        self.assertEqual(frozen.SCRAPER_DIR, override)


class DevStateUnchangedTests(unittest.TestCase):
    """开发态行为回归：新增常量不得改变历史行为。"""

    def test_dev_flags_empty_and_app_root_is_repo(self):
        self.assertFalse(config.FROZEN)
        self.assertEqual(config.APP_ROOT, config.BASE_DIR)
        self.assertEqual(config.SCRAPER_LAUNCH_FLAGS, ())
        self.assertEqual(config.FRONTEND_DIR, config.BASE_DIR / "frontend")

    def test_collector_dev_command_shape_unchanged(self):
        """开发态命令＝解释器 + 脚本路径，与历史形态逐字段一致。"""
        output = Path(_TEST_HOME) / "job-result" / "dev_shape.json"
        with mock.patch.object(collector, "resolve_collect_pace",
                               return_value={"env": {}, "dup_stop_ratio": None,
                                             "task_gap_sec": 60.0}), \
                mock.patch.object(collector.subprocess, "run",
                                  return_value=mock.Mock(
                                      returncode=0, stdout="", stderr="")) as run_mock:
            collector._run_scraper(["--keyword", "AI"], timeout=60,
                                   cdp_port=9222, output_path=output)
        command = run_mock.call_args.args[0]
        self.assertEqual(command, [
            str(collector.SCRAPER_PY), str(collector.SCRAPER_SCRIPT),
            "--keyword", "AI", "--cdp-port", "9222", "--output", str(output)])

    def test_collector_frozen_command_carries_reentry_flag(self):
        """打包态命令＝exe 自身 + 再入旗标 + 脚本路径（cwd 仍为引擎目录）。"""
        output = Path(_TEST_HOME) / "job-result" / "frozen_shape.json"
        with mock.patch.object(collector, "SCRAPER_LAUNCH_FLAGS",
                               (config.ENGINE_REENTRY_FLAG,)), \
                mock.patch.object(collector, "resolve_collect_pace",
                                  return_value={"env": {}, "dup_stop_ratio": None,
                                                "task_gap_sec": 60.0}), \
                mock.patch.object(collector.subprocess, "run",
                                  return_value=mock.Mock(
                                      returncode=0, stdout="", stderr="")) as run_mock:
            collector._run_scraper(["--keyword", "AI"], timeout=60,
                                   cdp_port=9222, output_path=output)
        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], str(collector.SCRAPER_PY))
        self.assertEqual(command[1], "--run-engine")
        self.assertEqual(command[2], str(collector.SCRAPER_SCRIPT))
        self.assertEqual(command[3], "--keyword")
        cwd = run_mock.call_args.kwargs.get("cwd")
        self.assertEqual(str(cwd), str(collector.SCRAPER_DIR))


if __name__ == "__main__":
    unittest.main()
