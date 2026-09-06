"""M18 向导流式生成、采集展示、深链与取消交互契约。"""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-m18-guide-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import config, llm, main, resumes, strategy  # noqa: E402
from backend.db import close_db, init_db, set_setting  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


async def _response_text(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
    return "".join(chunks)


class StrategyStreamApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m18-stream-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()
        self.resume = resumes.update_resume(
            resumes.get_default_resume()["id"], resume_text="完整项目简历" * 80)
        set_setting("llm_base_url", "https://llm.example/v1")
        set_setting("llm_api_key", "secret")
        set_setting("llm_model", "model")

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def test_ndjson_stream_emits_deltas_then_saves_done_plan(self):
        plan = {
            "directions": ["嵌入式"],
            "searches": [{"keyword": "嵌入式工程师", "city": "深圳", "pages": 2}],
            "companies": [], "dictionary_patch": {}, "notes": "测试",
        }

        def generate(_text, _expectations, client=None, on_delta=None, cancelled=None):
            self.assertFalse(cancelled())
            on_delta('{"searches":[')
            on_delta('{"keyword":"嵌入式工程师"}]}')
            return plan

        with patch.object(strategy, "generate_plan", side_effect=generate):
            response = main.gen_strategy_stream({"resume_id": self.resume["id"]})
            lines = [json.loads(line) for line in asyncio.run(
                _response_text(response)).splitlines() if line.strip()]

        self.assertEqual([item["type"] for item in lines], ["delta", "delta", "done"])
        self.assertEqual(lines[-1]["plan"], plan)
        self.assertEqual(strategy.get_plan(self.resume["id"]), plan)

    def test_failed_stream_keeps_previous_saved_plan(self):
        previous = {"searches": [{"keyword": "旧计划", "city": "深圳", "pages": 1}]}
        strategy.save_plan(previous, self.resume["id"])
        with patch.object(strategy, "generate_plan",
                          side_effect=llm.LLMError("模型失败")):
            response = main.gen_strategy_stream({"resume_id": self.resume["id"]})
            lines = [json.loads(line) for line in asyncio.run(
                _response_text(response)).splitlines() if line.strip()]
        self.assertEqual(lines[-1]["type"], "failed")
        self.assertIn("模型失败", lines[-1]["message"])
        self.assertEqual(strategy.get_plan(self.resume["id"]), previous)

    def test_client_close_cancels_strategy_worker_promptly(self):
        started = threading.Event()
        cancelled_seen = threading.Event()

        def generate(_text, _expectations, client=None, on_delta=None, cancelled=None):
            started.set()
            while not cancelled():
                time.sleep(0.01)
            cancelled_seen.set()
            raise llm.LLMCancelledError("已取消")

        async def consume_one_then_close(response):
            iterator = response.body_iterator
            await iterator.__anext__()
            await iterator.aclose()

        with patch.object(strategy, "generate_plan", side_effect=generate):
            response = main.gen_strategy_stream({"resume_id": self.resume["id"]})
            self.assertTrue(started.wait(1))
            asyncio.run(consume_one_then_close(response))
        self.assertTrue(cancelled_seen.wait(1))


class FrontendM18ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text()
        cls.css = (ROOT / "frontend" / "style.css").read_text()

    def test_guide_has_dual_account_switch_and_full_login_actions(self):
        for text in ("双账号隔离模式", "setGuideDualAccount", "guideAccounts",
                     "guideAccountAction(account,'launch')",
                     "guideAccountAction(account,'login')",
                     "guideAccountAction(account,'check')",
                     "guideAccountAction(account,'stop')", "打开登录页", "检测登录态"):
            self.assertIn(text, self.app)
        self.assertIn("dual_account_enabled: enabled", self.app)

    def test_strategy_generation_uses_post_stream_and_abort_controller(self):
        for text in ("async function streamNdjson", "response.body.getReader()",
                     "/api/strategy/generate/stream", "generateStrategyStreaming",
                     "new AbortController()", "class=\"strategy-stream\"",
                     "正在流式生成"):
            self.assertIn(text, self.app)
        self.assertGreaterEqual(self.app.count("generateStrategyStreaming("), 3)

    def test_plan_name_is_date_resume_serial_preview_and_not_editable(self):
        self.assertIn("function planNamePreview", self.app)
        self.assertIn("return `${date}-${name}-${serial}`", self.app)
        self.assertIn("启动后自动替换为实际流水号", self.app)
        self.assertNotIn('v-model="guidePlan.name"', self.app)
        self.assertNotIn('v-model="plan.name"', self.app)

    def test_collection_history_shows_name_completeness_jobs_and_eta(self):
        for text in ("row.plan_name", "仅岗位描述 {{run.listOnlyCount}}",
                     "完整 JD {{run.withJdCount}}", "完整度无法追溯",
                     "runProgressSummary(run)", "formatEta(run.etaSeconds)",
                     "已发现 ${run.jobsDiscovered} 个岗位"):
            self.assertIn(text, self.app)
        self.assertNotIn("个步骤", self.app)

    def test_priority_and_job_analysis_use_validated_workbench_deep_link(self):
        for text in ("function parseHashRoute", "const routeJobKey = ref",
                     "function jobCardHref(job)", ':href="jobCardHref(job)"',
                     '>岗位分析</a>', "favoriteJobs.value.find(job => job.job_key === activeKey.value)",
                     "favoriteJobs.value.find(job => job.job_key === target)"):
            self.assertIn(text, self.app)
        self.assertIn("`#${path}${rawQuery ? `?${rawQuery}` : ''}`", self.app)

    def test_pending_only_applies_to_favorites_and_tags_are_specific(self):
        self.assertIn("return job.favorite ? '待开始' : ''", self.app)
        self.assertIn('v-if="workflowLabel(job)"', self.app)
        for label in ("八小时双休", "大小周", "外包", "福利"):
            self.assertIn(label, self.app)

    def test_scoring_skips_existing_and_active_tasks_can_be_cancelled(self):
        for text in ("skipped_existing_count", "已有评分，无需重复生成",
                     "async function cancelAiTasks", "async function cancelAiTask",
                     "取消评分", "取消分析", "已完成结果会保留"):
            self.assertIn(text, self.app)
        self.assertIn("async function enqueueAi(page, kind, jobKeys, force = false)", self.app)

    def test_settings_warn_about_token_consumption(self):
        self.assertIn("请注意 Token 消耗", self.app)
        self.assertIn("class=\"llm-token-warning\"", self.app)
        self.assertIn(".llm-token-warning", self.css)


if __name__ == "__main__":
    unittest.main()
