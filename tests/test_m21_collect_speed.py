"""M21 测试：采集节奏三档（collect_pace）与提速参数注入。

覆盖：档位解析与回退、任务间隔按档位生效且旧测试钩子 ITEM_GAP_SEC 覆盖仍优先、
_run_scraper 按档位注入 SCRAPER_* 环境变量与 --dup-stop-ratio、
设置接口对新白名单 key 的校验、前端设置页含采集节奏控件。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m20-collect-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from fastapi import HTTPException  # noqa: E402

from backend import collector, config, main  # noqa: E402
from backend.db import get_db, get_setting, init_db, set_setting  # noqa: E402


def _clear_pace_setting():
    with get_db() as conn:
        conn.execute("DELETE FROM settings WHERE key='collect_pace'")
        conn.commit()


class PaceResolveTests(unittest.TestCase):
    """档位映射表与 task_gap_seconds 解析。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m20-pace-")
        config.DATA_DIR = Path(self.tmp.name)
        config.DB_PATH = self.tmp.name + "/copilot.db"
        init_db()
        _clear_pace_setting()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        _clear_pace_setting()

    def test_default_setting_is_balanced(self):
        self.assertEqual(config.DEFAULT_SETTINGS["collect_pace"], "balanced")

    def test_three_profiles_share_shape(self):
        self.assertEqual(set(config.COLLECT_PACES),
                         {"standard", "balanced", "fast"})
        gaps = {key: pace["task_gap_sec"]
                for key, pace in config.COLLECT_PACES.items()}
        self.assertEqual(gaps, {"standard": 120, "balanced": 60, "fast": 20})
        for key, pace in config.COLLECT_PACES.items():
            if key == "standard":
                self.assertEqual(pace["env"], {})
                self.assertIsNone(pace["dup_stop_ratio"])
            else:
                self.assertTrue(pace["env"])
                self.assertGreater(pace["dup_stop_ratio"], 0)

    def test_task_gap_follows_setting_without_row_falls_to_balanced(self):
        with patch.object(collector, "ITEM_GAP_SEC", None):
            self.assertEqual(collector.task_gap_seconds(), 60)

    def test_task_gap_for_each_explicit_pace(self):
        for pace_key, expected in (("standard", 120), ("balanced", 60),
                                   ("fast", 20)):
            set_setting("collect_pace", pace_key)
            with patch.object(collector, "ITEM_GAP_SEC", None), \
                 self.subTest(pace=pace_key):
                self.assertEqual(collector.task_gap_seconds(), expected)

    def test_unknown_pace_falls_back_to_standard(self):
        set_setting("collect_pace", "lightning")
        with patch.object(collector, "ITEM_GAP_SEC", None):
            self.assertEqual(collector.resolve_collect_pace(),
                             config.COLLECT_PACES["standard"])

    def test_legacy_item_gap_override_wins_over_pace(self):
        # 既有测试以 patch.object(collector, "ITEM_GAP_SEC", x) 强制间隔，语义保持不变
        set_setting("collect_pace", "fast")
        with patch.object(collector, "ITEM_GAP_SEC", 7):
            self.assertEqual(collector.task_gap_seconds(), 7.0)


class ScraperEnvInjectionTests(unittest.TestCase):
    """_run_scraper 按档位改写命令与环境变量；favorites 复用同函数自动生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m20-env-")
        config.DATA_DIR = Path(self.tmp.name)
        config.DB_PATH = self.tmp.name + "/copilot.db"
        config.COLLECT_RESULT_DIR = Path(self.tmp.name) / "job-result"
        init_db()
        _clear_pace_setting()
        self.original_result_dir = collector.RESULT_DIR
        collector.RESULT_DIR = config.COLLECT_RESULT_DIR
        fake = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        patcher = patch.object(collector.subprocess, "run",
                               return_value=fake)
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        collector.RESULT_DIR = self.original_result_dir
        _clear_pace_setting()

    def _invocation(self):
        args = self.run_mock.call_args.args
        kwargs = self.run_mock.call_args.kwargs
        return list(args[0]), kwargs.get("env") or {}

    def test_standard_injects_nothing_native_rhythm(self):
        set_setting("collect_pace", "standard")
        collector._run_scraper(["--keyword", "AI"], timeout=60, cdp_port=9222)
        command, env = self._invocation()
        self.assertNotIn("--dup-stop-ratio", command)
        for key in config.COLLECT_PACES["balanced"]["env"]:
            self.assertNotIn(key, env)

    def test_balanced_compresses_env_and_enables_dup_stop(self):
        set_setting("collect_pace", "balanced")
        collector._run_scraper(["--keyword", "AI"], timeout=60, cdp_port=9222)
        command, env = self._invocation()
        expected = config.COLLECT_PACES["balanced"]
        idx = command.index("--dup-stop-ratio")
        self.assertEqual(command[idx + 1], str(expected["dup_stop_ratio"]))
        for key, value in expected["env"].items():
            self.assertEqual(env[key], value,
                             f"{key} 未按均衡档注入")

    def test_fast_profile_env_matches_mapping(self):
        set_setting("collect_pace", "fast")
        collector._run_scraper(["--input", "merged.json", "--detail"],
                               timeout=60, cdp_port=9222)
        command, env = self._invocation()
        for key, value in config.COLLECT_PACES["fast"]["env"].items():
            self.assertEqual(env[key], value)
        idx = command.index("--dup-stop-ratio")
        self.assertEqual(command[idx + 1],
                         str(config.COLLECT_PACES["fast"]["dup_stop_ratio"]))


class SettingsApiTests(unittest.TestCase):
    """设置接口接受并持久化 collect_pace，拒绝非法档位。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m20-api-")
        config.DATA_DIR = Path(self.tmp.name)
        config.DB_PATH = self.tmp.name + "/copilot.db"
        init_db()
        _clear_pace_setting()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        _clear_pace_setting()

    def test_write_settings_accepts_valid_paces(self):
        for pace_key in ("standard", "balanced", "fast"):
            main.write_settings({"collect_pace": pace_key})
            self.assertEqual(get_setting("collect_pace"), pace_key,
                             f"档位 {pace_key} 未持久化")

    def test_write_settings_rejects_unknown_pace(self):
        with self.assertRaises(HTTPException) as ctx:
            main.write_settings({"collect_pace": "lightning"})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("standard/balanced/fast", ctx.exception.detail)


class FrontendPaceControlTests(unittest.TestCase):
    """设置页包含采集节奏三档下拉与说明（无构建静态断言）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = (Path(__file__).resolve().parents[1]
                   / "frontend" / "app.js").read_text(encoding="utf-8")

    def test_settings_roundtrip_collect_pace(self):
        self.assertIn("collectPace:", self.app)
        self.assertIn("collect_pace: settings.collectPace", self.app)

    def test_select_offers_all_profiles_with_hints(self):
        for text in ("采集节奏", "'standard'", "'balanced'", "'fast'",
                     "任务间隔 120s", "任务间隔 60s", "任务间隔 20s"):
            self.assertIn(text, self.app)
        self.assertIn('v-model="draft.collectPace"', self.app)


if __name__ == "__main__":
    unittest.main()
