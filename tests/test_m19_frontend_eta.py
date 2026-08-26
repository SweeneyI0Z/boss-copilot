"""M19 前端任务 ETA 与招呼语幂等反馈契约。"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendEtaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
        cls.index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_page_progress_uses_backend_eta_for_jobs_and_workbench(self):
        self.assertIn("etaSeconds: taskEtas.length ? Math.max(...taskEtas)", self.app)
        self.assertIn("store.ai[page].progress && store.ai[page].progress.eta_seconds", self.app)
        self.assertIn("if (value === null || value === undefined || value === '') return null", self.app)
        self.assertEqual(self.app.count("formatEta(taskSummary.etaSeconds)"), 2)

    def test_active_analysis_and_greeting_show_progress_and_eta(self):
        self.assertIn("taskProgressPercent(activeGreetingTask(activeJob))", self.app)
        self.assertIn("taskEtaSeconds(activeGreetingTask(activeJob),'workbench')", self.app)
        self.assertIn("taskProgressPercent(activeAnalysisTask(activeJob))", self.app)
        self.assertIn("taskEtaSeconds(activeAnalysisTask(activeJob),'workbench')", self.app)
        self.assertIn(".generation-progress", self.css)

    def test_partial_stream_progress_contributes_to_page_bar(self):
        self.assertIn("const progressUnits = tasks.reduce", self.app)
        self.assertIn("Math.round(progressUnits / tasks.length * 100)", self.app)

    def test_existing_greeting_refreshes_instead_of_claiming_generation(self):
        self.assertIn("if (greeting.stale) continue", self.app)
        self.assertIn("if (skipped) await refreshGreetings()", self.app)
        self.assertIn("该岗位已有招呼语，已保留原结果", self.app)

    def test_cache_version_is_advanced(self):
        self.assertIn("style.css?v=m19-eta", self.index)
        self.assertIn("app.js?v=m19-eta", self.index)


if __name__ == "__main__":
    unittest.main()
