"""内置采集引擎测试：引擎 vendor 进本仓库后，接口契约与节奏注入仍然成立。

背景：采集引擎原依赖同级外部仓库 boss-zhipin-scraper 及其独立 venv；
现已 vendor 到 vendor/boss-zhipin-scraper 并用本项目解释器直接运行。
本文件锁定：打包完整性、SCRAPER_* 节奏注入语义（缺省=上游原生节奏）、
--company 与 --dup-stop-ratio 两个 boss-copilot 增强参数。

跑法：.venv/bin/python -m unittest discover tests -v
回归要求：任何 backend/ 改动后必须全量通过。
"""
import importlib.util
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

from backend import config  # noqa: E402
config.DATA_DIR = Path(_TEST_HOME)
config.DB_PATH = Path(_TEST_HOME) / "copilot.db"


def _load_engine():
    """把 vendored 脚本当模块加载（顶层无副作用，main 有守护）。"""
    spec = importlib.util.spec_from_file_location(
        "vendored_boss_cdp_raw", config.SCRAPER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuiltinEngineContractTests(unittest.TestCase):
    """打包完整性与接口存在性。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = _load_engine()

    def test_engine_assets_are_packaged(self):
        self.assertTrue(config.SCRAPER_SCRIPT.exists())
        self.assertTrue((config.SCRAPER_DIR / "data" / "city_codes.json").exists())
        self.assertTrue((config.SCRAPER_DIR / "LICENSE").exists())
        self.assertTrue((config.SCRAPER_DIR / "README.md").exists())

    def test_cli_exposes_company_and_dup_stop(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, str(config.SCRAPER_SCRIPT), "--help"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertIn("--company", proc.stdout)
        self.assertIn("--dup-stop-ratio", proc.stdout)

    def test_company_url_parameterizes_brand_id_and_page(self):
        url = self.engine.build_company_url("123456", 2, {})
        self.assertEqual(
            url, "https://www.zhipin.com/web/geek/job?brandId=123456&page=2")

    def test_company_url_carries_filters(self):
        url = self.engine.build_company_url("123456", 1, {"salary": "406"})
        self.assertIn("brandId=123456", url)
        self.assertIn("salary=406", url)

    def test_search_url_construction_unchanged(self):
        url = self.engine.build_search_url("AI Agent", "101280600", 3,
                                           {"salary": "406", "degree": ""})
        self.assertIn("query=AI+Agent", url)
        self.assertIn("city=101280600", url)
        self.assertIn("page=3", url)
        self.assertIn("salary=406", url)
        self.assertNotIn("degree", url)


class PacingEnvInjectionTests(unittest.TestCase):
    """SCRAPER_* 环境变量：注入时覆盖节奏，未注入/非法时回退上游原生值。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = _load_engine()

    def test_injected_range_replaces_hardcoded_defaults(self):
        with mock.patch.dict(os.environ, {"SCRAPER_LIST_PAGE_GAP_MIN": "6",
                                          "SCRAPER_LIST_PAGE_GAP_MAX": "11"}), \
                mock.patch.object(self.engine.random, "uniform",
                                  return_value=7.5) as uniform:
            value = self.engine.env_range_uniform(
                "SCRAPER_LIST_PAGE_GAP_MIN", "SCRAPER_LIST_PAGE_GAP_MAX", 12, 22)
        uniform.assert_called_once_with(6, 11)
        self.assertEqual(value, 7.5)

    def test_missing_env_falls_back_to_upstream_pace(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(self.engine.random, "uniform",
                                  return_value=15.0) as uniform:
            self.engine.env_range_uniform("SCRAPER_DETAIL_GAP_MIN",
                                          "SCRAPER_DETAIL_GAP_MAX", 10, 25)
        uniform.assert_called_once_with(10, 25)

    def test_invalid_env_value_falls_back_to_defaults(self):
        with mock.patch.dict(os.environ, {"SCRAPER_DETAIL_GAP_MIN": "not-a-number"}), \
                mock.patch.object(self.engine.random, "uniform",
                                  return_value=12.0) as uniform:
            self.engine.env_range_uniform("SCRAPER_DETAIL_GAP_MIN",
                                          "SCRAPER_DETAIL_GAP_MAX", 10, 25)
        uniform.assert_called_once_with(10, 25)

    def test_reversed_env_bounds_are_normalized(self):
        with mock.patch.dict(os.environ, {"SCRAPER_LIST_PAGE_GAP_MIN": "11",
                                          "SCRAPER_LIST_PAGE_GAP_MAX": "6"}), \
                mock.patch.object(self.engine.random, "uniform",
                                  return_value=8.0) as uniform:
            self.engine.env_range_uniform("SCRAPER_LIST_PAGE_GAP_MIN",
                                          "SCRAPER_LIST_PAGE_GAP_MAX", 12, 22)
        uniform.assert_called_once_with(6, 11)

    def test_read_pause_scale_scales_reading_wait(self):
        with mock.patch.dict(os.environ, {"SCRAPER_READ_PAUSE_SCALE": "0.5"}), \
                mock.patch.object(self.engine.random, "uniform",
                                  return_value=4.0) as uniform:
            value = self.engine.paced_uniform(2.0, 5.0)
        uniform.assert_called_once_with(2.0, 5.0)
        self.assertEqual(value, 2.0)

    def test_read_pause_scale_defaults_to_native_pace(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(self.engine.random, "uniform",
                                  return_value=4.0) as uniform:
            value = self.engine.paced_uniform(2.0, 5.0)
        uniform.assert_called_once_with(2.0, 5.0)
        self.assertEqual(value, 4.0)


if __name__ == "__main__":
    unittest.main()
