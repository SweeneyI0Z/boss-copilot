"""M17 前端静态契约：采集删除、页面级 AI 任务与工作台交互。"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendTaskContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "frontend" / "app.js").read_text()
        cls.css = (ROOT / "frontend" / "style.css").read_text()
        cls.jobs = cls._section("const JobsView =", "// -- 页面：收藏工作台")
        cls.workbench = cls._section("const JobCardView =", "// -- 页面：账号管理")
        cls.collect = cls._section("const CollectView =", "// -- 页面：数据分析")

    @classmethod
    def _section(cls, start: str, end: str) -> str:
        begin = cls.app.index(start)
        finish = cls.app.index(end, begin)
        return cls.app[begin:finish]

    def test_guide_busy_disabled_bindings_are_explicit_booleans(self):
        expressions = re.findall(
            r':disabled="([^"]*guide\.busy[^"]*)"', self.app)
        self.assertGreaterEqual(len(expressions), 8)
        unsafe = [expression for expression in expressions if not re.search(
            r'(?:!!guide\.busy|Boolean\(guide\.busy\))', expression)]
        self.assertEqual(
            unsafe, [], msg=f"guide.busy 必须先布尔化：{unsafe}")
        self.assertNotIn(':disabled="guide.busy"', self.app)

    def test_collection_delete_previews_confirms_deletes_and_refreshes(self):
        for contract in (
                "deletePreview: id => apiRequest(`/api/runs/${id}/delete-preview`)",
                "method: 'DELETE'", "confirm_run_id: id",
                "async function deleteRunData(run)", "apiClient.runs.deletePreview(run.id)",
                "title: `删除采集数据", "confirmText: '永久删除'",
                "tone: 'danger'", "apiClient.runs.delete(run.id)",
                '@click="deleteRunData(run)"'):
            self.assertIn(contract, self.app)

        body = self.collect[
            self.collect.index("async function deleteRunData(run)"):
            self.collect.index("    return { store")]
        self.assertLess(body.index("deletePreview(run.id)"),
                        body.index("requestConfirm({"))
        self.assertLess(body.index("requestConfirm({"),
                        body.index("runs.delete(run.id)"))
        self.assertIn("if (!accepted) return", body)
        self.assertIn(
            "refreshRuns(), refreshDashboard(), refreshAnalytics(), refreshJobs()",
            body)
        self.assertIn("独占岗位", body)
        self.assertIn("共享岗位", body)

    def test_jobs_resume_selector_task_progress_and_score_states(self):
        self.assertIn('<span>当前简历</span><select '
                      'v-model.number="store.selectedResumeId">', self.jobs)
        self.assertIn("const taskSummary = computed(() => aiSummary('jobs'))", self.jobs)
        self.assertIn('class="ai-task-progress"', self.jobs)
        self.assertIn("taskSummary.percent+'%'", self.jobs)
        self.assertIn("动态并发", self.jobs)
        self.assertIn("return aiTaskFor(page, job.job_key, kinds) ? '评分中'",
                      self.app)
        self.assertIn("job[field] ?? '未评分'", self.app)
        self.assertGreaterEqual(self.jobs.count("scoreLabel(job,"), 6)

    def test_jobs_show_only_highest_workflow_tag_and_readable_jd(self):
        self.assertEqual(self.jobs.count("{{workflowLabel(job)}}"), 1)
        for old_tag in ('v-if="job.contacted"', 'v-if="job.applied"',
                        'v-if="job.interviewed"', 'v-if="job.offered"'):
            self.assertNotIn(old_tag, self.jobs)
        for stage in ("if (job.offered) return 'OFFER'",
                      "if (job.interviewed) return '已面试'",
                      "if (job.applied) return '已投递'",
                      "if (job.contacted) return '已打招呼'",
                      "return '待开始'"):
            self.assertIn(stage, self.app)
        self.assertRegex(
            self.css,
            r'\.job-detail\s+\.jd-copy\s*\{[^}]*font-size:\s*16px;',
        )

    def test_two_pages_have_independent_sse_task_channels(self):
        self.assertIn(
            "ai: { jobs: EMPTY_AI_PAGE('jobs'), "
            "workbench: EMPTY_AI_PAGE('workbench') }", self.app)
        for endpoint in (
                "`/api/ai/tasks/${page}`",
                "`/api/ai/tasks/${page}/cancel`",
                "`/api/ai/tasks/${page}/stream`"):
            self.assertIn(endpoint, self.app)
        self.assertIn("for (const page of ['jobs', 'workbench'])", self.app)
        self.assertIn("new EventSource(apiClient.ai.streamUrl(page))", self.app)
        self.assertIn("source.addEventListener('snapshot'", self.app)
        self.assertIn("applyAiSnapshot(page, JSON.parse(event.data), true)", self.app)
        self.assertIn("connectAiStreams()", self.app)
        self.assertIn("closeAiStreams()", self.app)

    def test_switching_resume_cancels_both_page_queues(self):
        watcher = self.app[self.app.index(
            "watch(() => store.selectedResumeId", self.app.index("const App =")):]
        self.assertIn("apiClient.ai.cancel('jobs', { resume_id: previous })", watcher)
        self.assertIn(
            "apiClient.ai.cancel('workbench', { resume_id: previous })", watcher)
        self.assertIn("store.aiTracking.jobs = []", watcher)
        self.assertIn("store.aiTracking.workbench = []", watcher)
        self.assertIn("已停止旧简历的", watcher)

    def test_workbench_batches_enqueue_without_serial_ai_loop(self):
        batch = self.workbench[
            self.workbench.index("async function executeBatch"):
            self.workbench.index("function retryFailed")]
        self.assertIn("if (type === 'analysis' || type === 'greeting')", batch)
        self.assertIn(
            "enqueueAi('workbench', type, "
            "targets.map(job => job.job_key), true)", batch)
        self.assertNotIn("await generateAnalysis(job, true)", batch)
        self.assertNotIn("await generateGreeting(job, true)", batch)
        self.assertIn(
            '<button :disabled="!selectedKeys.length" '
            '@click="executeBatch(\'analysis\')">', self.workbench)
        self.assertIn(
            '<button :disabled="!selectedKeys.length" '
            '@click="executeBatch(\'greeting\')">', self.workbench)

    def test_workbench_keeps_three_greeting_variants_and_copy_open_action(self):
        for contract in (
                "(greeting.variants || []).slice(0, 3)",
                "(greeting.variant_labels || []).slice(0, 3)",
                "v-for=\"(variant,index) in activeJob.greetingVariants\"",
                "selectGreetingVariant(activeJob,index)",
                "Number(job.greetingSelectedIndex || 0)",
                "async function copyAndOpenBoss(job)",
                "navigator.clipboard.writeText(job.greeting.trim())",
                "apiClient.jobs.openBoss(job.job_key)",
                "复制并打开 BOSS"):
            self.assertIn(contract, self.app)
        copy_action = self.workbench[
            self.workbench.index("async function copyAndOpenBoss"):
            self.workbench.index("async function waitForTaskIds")]
        self.assertLess(copy_action.index("clipboard.writeText"),
                        copy_action.index("jobs.openBoss"))

    def test_workbench_status_advice_safety_and_failure_retry(self):
        for contract in (
                '<option value="pending">待开始</option>',
                '<option value="offered">OFFER</option>',
                "setWorkflowStage(activeJob,'pending')",
                "setWorkflowStage(activeJob,'offered')",
                '<h3>应聘建议</h3>',
                'class="advice-copy markdown-body"',
                'v-html="renderMarkdown(activeJob.advice)"',
                "function retryFailed(job)",
                "job.actionErrorType === 'greeting'",
                '@click="retryFailed(activeJob)"',
                "重新生成"):
            self.assertIn(contract, self.workbench)
        self.assertNotIn('<h3>AI 应聘建议</h3>', self.workbench)
        for heading in ("### 总体判断", "### 投递建议", "### 匹配优势",
                        "### 面试前准备", "### 简历侧重点"):
            self.assertIn(heading, self.app)

        bindings = re.findall(r'v-html="([^"]+)"', self.app)
        self.assertCountEqual(
            bindings, ["rendered", "renderMarkdown(activeJob.advice)"])
        self.assertIn("DOMPurify.sanitize", self.app)
        self.assertIn("FORBID_TAGS", self.app)
        self.assertIn("FORBID_ATTR", self.app)


if __name__ == "__main__":
    unittest.main()
