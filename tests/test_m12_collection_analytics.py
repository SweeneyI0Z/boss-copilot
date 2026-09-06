"""M12 测试：模块化采集、精确导入、来源关系、详情补齐与分析聚合。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import analytics, cities, collector, config, importer, main, sync
from backend.db import close_db, get_db, init_db, now_iso


class M12DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m12-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        config.COLLECT_RESULT_DIR = root / "job-result"
        collector.RESULT_DIR = config.COLLECT_RESULT_DIR
        init_db()
        with collector._state_lock:
            collector._state.update({
                "running": False, "current": "", "phase": "", "log": [],
                "run_id": None, "cancel": False, "risk_signal": "",
                "eta_model": None,
                "progress": {"list_total": 0, "list_completed": 0,
                             "detail_total": 0, "detail_completed": 0},
            })

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    def write_list(self, name, jobs, **meta):
        path = Path(self.tmp.name) / name
        payload = {"keyword": meta.get("keyword", "AI"),
                   "city": meta.get("city", "深圳"), "filters": {},
                   "scraped_at": meta.get("scraped_at", "2026-08-23T10:00:00+08:00"),
                   "total": len(jobs), "jobs": jobs}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def raw_job(key, title="AI工程师", company="示例科技", salary="20-30K"):
        return {"title": title, "boss_name": company, "salary": salary,
                "tags": "3-5年 | 本科", "location": "深圳·南山区",
                "company_industry": "人工智能", "company_scale": "100-499人",
                "job_link": f"https://www.zhipin.com/job_detail/{key}.html"}

    @staticmethod
    def insert_job(key, **overrides):
        values = {"title": "岗位", "company": "公司", "status": "active",
                  "salary_min": 20, "salary_max": 30, "salary_months": 14,
                  "experience": "3-5年", "degree": "本科", "industry": "人工智能",
                  "scale": "100-499人", "is_headhunter": 0,
                  "job_score": 80, "priority": "P1"}
        values.update(overrides)
        columns = ["job_key", *values, "first_seen_at", "last_seen_at"]
        args = [key, *values.values(), now_iso(), now_iso()]
        get_db().execute(
            f"INSERT INTO jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            args)
        get_db().commit()

    @staticmethod
    def run_task(task):
        conn = get_db()
        run = conn.execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,status) "
            "VALUES('search','{}','{}',?,'running')", (now_iso(),)).lastrowid
        row = conn.execute(
            "INSERT INTO collect_run_tasks(run_id,task_key,kind,keyword,province,city,"
            "city_code,filters,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (run, task["task_key"], task["kind"], task.get("keyword", ""),
             task.get("province", ""), task.get("city", ""),
             task.get("city_code", ""), json.dumps(task.get("filters") or {}),
             "running"))
        conn.commit()
        return run, row.lastrowid


class CollectConfigTests(M12DatabaseTestCase):
    def test_full_city_table_is_grouped_by_province(self):
        groups = cities.city_groups()
        names = {city["name"] for group in groups for city in group["cities"]}
        self.assertGreater(len(names), 100)
        self.assertIn("赣州", names)
        self.assertIn("长春", names)
        self.assertEqual(cities.resolve_city("赣州")["province"], "江西省")

    def test_builtin_city_snapshot_is_offline_fallback(self):
        with patch.object(config, "SCRAPER_DIR", Path(self.tmp.name) / "missing"):
            groups = cities.city_groups()
            self.assertGreaterEqual(len(groups), 30)
            self.assertEqual(cities.resolve_city("深圳")["city_code"], "101280600")

    def test_global_cities_expand_and_forward_filters(self):
        tasks = collector.build_tasks({
            "keywords": ["AI Agent", "嵌入式"],
            "cities": ["深圳", "上海"], "pages": 4,
            "filters": {"salary": 406, "degree": 203},
        }, resume_id=7)
        self.assertEqual(len(tasks), 4)
        self.assertEqual({task["city_code"] for task in tasks},
                         {"101280600", "101020100"})
        self.assertTrue(all(task["filters"] == {"salary": "406", "degree": "203"}
                            for task in tasks))
        self.assertTrue(all(task["resume_id"] == 7 for task in tasks))

    def test_at_least_one_city_and_max_twenty_combinations(self):
        with self.assertRaisesRegex(ValueError, "最多 20"):
            collector.build_tasks({"keywords": [f"关键词{i}" for i in range(11)],
                                   "cities": ["深圳", "上海"]})
        with self.assertRaisesRegex(ValueError, "至少选择一个城市"):
            collector.normalize_collect_config({"keywords": ["AI"], "cities": []})

    def test_company_target_can_run_without_keyword_or_city(self):
        tasks = collector.build_tasks({
            "keywords": [], "cities": [],
            "companies": [{"brand_id": "brand-1", "name": "目标公司"}],
        })
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["type"], "company")
        with self.assertRaisesRegex(ValueError, "1-30"):
            collector._normalize_task({"type": "company", "brand_id": "x", "pages": 31})

    def test_config_isolated_by_resume(self):
        collector.save_collect_config({"keywords": ["固件"], "cities": ["深圳"]}, 1)
        collector.save_collect_config({"keywords": ["Agent"], "cities": ["上海"]}, 2)
        self.assertEqual(collector.get_collect_config(1)["keywords"], ["固件"])
        self.assertEqual(collector.get_collect_config(2)["cities"][0]["city"], "上海")

    def test_filter_labels_are_persisted_as_boss_codes(self):
        cfg = collector.normalize_collect_config({
            "keywords": ["AI"], "cities": ["深圳"],
            "filters": {"experience": ["1-3年", "3-5年"],
                        "degree": ["不限", "本科"], "industry": ["企业服务"]},
        })
        self.assertEqual(cfg["filters"], {
            "experience": "104,105", "degree": "203", "industry": "1005"})
        with self.assertRaisesRegex(ValueError, "不支持的salary"):
            collector.normalize_collect_config({
                "keywords": ["AI"], "cities": ["深圳"],
                "filters": {"salary": "25-40K"},
            })

    def test_risk_and_login_are_classified_separately(self):
        self.assertTrue(collector.classify_failure("触发安全验证码")["risk"])
        login = collector.classify_failure("BOSS detail login expired")
        self.assertEqual(login["kind"], "login")
        self.assertFalse(login["risk"])

    def test_successful_collection_clears_old_account_risk_hint(self):
        collector._mark_account_failure("collect", {
            "kind": "risk", "risk": True, "message": "安全验证"})
        collector._mark_account_success("collect")
        row = get_db().execute(
            "SELECT logged_in,hint FROM account_states WHERE account='collect'").fetchone()
        self.assertEqual((row["logged_in"], row["hint"]), (1, ""))

    def test_zero_exit_partial_warning_is_not_treated_as_success(self):
        completed = type("Result", (), {
            "returncode": 0,
            "stdout": "抓取 5 条\n⚠️ 达到请求上限，已保留部分结果\n",
            "stderr": "",
        })()
        with patch.object(collector.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "未完整完成"):
                collector._run_scraper(["--keyword", "AI"], 10, 9222)


class ExactImporterTests(M12DatabaseTestCase):
    def test_explicit_file_does_not_scan_sibling_history(self):
        first = self.write_list("boss_jobs_first.json", [self.raw_job("first")])
        self.write_list("boss_jobs_history.json", [self.raw_job("history")])
        stats = importer.import_scraper_files([first], record_run=False)
        self.assertEqual(stats["job_keys"], ["first"])
        keys = {row["job_key"] for row in get_db().execute("SELECT job_key FROM jobs")}
        self.assertEqual(keys, {"first"})

    def test_excluded_job_is_fully_frozen(self):
        self.insert_job("excluded", title="旧标题", company="旧公司", status="excluded")
        path = self.write_list(
            "boss_jobs_one.json",
            [self.raw_job("excluded", title="新标题", company="新公司")])
        stats = importer.import_scraper_files([path], record_run=False)
        row = get_db().execute(
            "SELECT title,company,status FROM jobs WHERE job_key='excluded'").fetchone()
        self.assertEqual((row["title"], row["company"], row["status"]),
                         ("旧标题", "旧公司", "excluded"))
        self.assertEqual(stats["excluded_skipped"], 1)
        self.assertEqual(stats["job_keys"], [])

    def test_detail_only_import_updates_known_nonexcluded_job(self):
        self.insert_job("detail")
        self.insert_job("frozen", status="excluded")
        path = Path(self.tmp.name) / "boss_details_one.json"
        path.write_text(json.dumps([
            {"job_link": "https://www.zhipin.com/job_detail/detail.html",
             "jd": "有效JD", "skill_tags": ["Python"]},
            {"job_link": "https://www.zhipin.com/job_detail/frozen.html",
             "jd": "不应写入"},
        ], ensure_ascii=False), encoding="utf-8")
        stats = importer.import_scraper_details(str(path))
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["excluded_skipped"], 1)
        self.assertEqual(get_db().execute(
            "SELECT jd FROM job_details WHERE job_key='detail'").fetchone()["jd"], "有效JD")

    def test_xlsx_never_adds_scores_to_excluded_job(self):
        self.insert_job("excludedxlsx", status="excluded", job_score=80)
        overview = [{"序号": 1, "岗位名称": "新标题", "公司": "新公司",
                     "薪资": "30-40K", "岗位链接":
                     "https://www.zhipin.com/job_detail/excludedxlsx.html"}]
        details = [{"序号": 1, "JD全文": "不应更新的JD"}]
        l1_rows = [{"序号": 1, "岗位粗分": 99, "综合粗分": 99}]
        l2_rows = [{"序号": 1, "岗位评分": 99, "综合分": 99, "P级": "P0"}]
        workbook = {name: object() for name in ("岗位总览", "JD详情", "L1初筛", "L2评级")}
        with patch("openpyxl.load_workbook", return_value=workbook), \
                patch.object(importer, "_sheet_rows",
                             side_effect=[overview, details, l1_rows, l2_rows]), \
                patch("backend.resumes.save_imported_baseline") as save_baseline:
            result = importer.import_xlsx("missing-source.xlsx")
        self.assertEqual(result["excluded_skipped"], 1)
        save_baseline.assert_not_called()
        row = get_db().execute(
            "SELECT title,job_score FROM jobs WHERE job_key='excludedxlsx'").fetchone()
        self.assertEqual((row["title"], row["job_score"]), ("岗位", 80))


class SourceRelationshipTests(M12DatabaseTestCase):
    def test_job_delists_only_after_every_source_disappears(self):
        self.insert_job("shared")
        task_a = collector._normalize_task({"type": "search", "keyword": "AI",
                                            "city": "深圳", "pages": 1})
        task_b = collector._normalize_task({"type": "search", "keyword": "Python",
                                            "city": "深圳", "pages": 1})
        run_a1, task_a1 = self.run_task(task_a)
        sync.record_source_success(run_a1, task_a1, task_a, {"shared"})
        run_b1, task_b1 = self.run_task(task_b)
        sync.record_source_success(run_b1, task_b1, task_b, {"shared"})

        run_a2, task_a2 = self.run_task(task_a)
        result_a = sync.record_source_success(run_a2, task_a2, task_a, set())
        self.assertEqual(result_a["delisted"], [])
        self.assertEqual(get_db().execute(
            "SELECT status FROM jobs WHERE job_key='shared'").fetchone()["status"], "active")

        run_b2, task_b2 = self.run_task(task_b)
        result_b = sync.record_source_success(run_b2, task_b2, task_b, set())
        self.assertEqual(result_b["delisted"], ["shared"])
        self.assertEqual(get_db().execute(
            "SELECT status FROM jobs WHERE job_key='shared'").fetchone()["status"],
            "delisted")

    def test_excluded_job_never_gets_new_hit(self):
        self.insert_job("excluded", status="excluded")
        task = collector._normalize_task({"type": "search", "keyword": "AI",
                                          "city": "深圳", "pages": 1})
        run_id, task_id = self.run_task(task)
        result = sync.record_source_success(run_id, task_id, task, {"excluded"})
        self.assertEqual(result["fresh"], 0)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) count FROM job_collection_hits").fetchone()["count"], 0)

    def test_reducing_pages_never_delists_deeper_page_jobs(self):
        self.insert_job("deep")
        ten_pages = collector._normalize_task({
            "type": "search", "keyword": "AI", "city": "深圳", "pages": 10})
        one_page = collector._normalize_task({
            "type": "search", "keyword": "AI", "city": "深圳", "pages": 1})
        self.assertEqual(ten_pages["search_key"], one_page["search_key"])
        run1, task1 = self.run_task(ten_pages)
        sync.record_source_success(run1, task1, ten_pages, {"deep"})
        run2, task2 = self.run_task(one_page)
        result = sync.record_source_success(run2, task2, one_page, set())
        self.assertEqual(result["delisted"], [])
        self.assertFalse(result["missing_diff_applied"])
        run3, task3 = self.run_task(one_page)
        again = sync.record_source_success(run3, task3, one_page, set())
        self.assertEqual(again["delisted"], [])
        self.assertEqual(get_db().execute(
            "SELECT status FROM jobs WHERE job_key='deep'").fetchone()["status"], "active")
        run4, task4 = self.run_task(ten_pages)
        full_refresh = sync.record_source_success(run4, task4, ten_pages, set())
        self.assertTrue(full_refresh["missing_diff_applied"])
        self.assertEqual(full_refresh["delisted"], ["deep"])


class TwoPhaseCollectorTests(M12DatabaseTestCase):
    def test_lists_are_independent_then_details_are_merged_once(self):
        tasks = collector._normalize_tasks([
            {"type": "search", "keyword": "AI", "city": "深圳", "pages": 1},
            {"type": "search", "keyword": "Agent", "city": "深圳", "pages": 1},
        ])
        run_id = collector._begin_run("config", {"tasks": tasks}, tasks)
        calls = []

        def fake_scraper(args, timeout, cdp_port, output_path=None, detail_output=None,
                         on_output=None, on_snapshot=None):
            calls.append(list(args))
            if "--input" in args:
                merged = json.loads(Path(args[args.index("--input") + 1]).read_text(
                    encoding="utf-8"))
                self.assertEqual(len(merged["jobs"]), 3)
                details = [{"job_link": job["job_link"], "title": job["title"],
                            "company": job["boss_name"], "salary": job["salary"],
                            "jd": f"{job['title']} 的完整JD"}
                           for job in merged["jobs"]]
                partial = []
                for index, detail in enumerate(details, 1):
                    partial.append(detail)
                    Path(detail_output).write_text(
                        json.dumps(partial, ensure_ascii=False), encoding="utf-8")
                    if on_output:
                        on_output(f"[{index}/{len(details)}] 公司 - {detail['title']}")
                    if on_snapshot:
                        on_snapshot(Path(detail_output))
                    self.assertEqual(get_db().execute(
                        "SELECT COUNT(*) count FROM job_details").fetchone()["count"], index)
            else:
                keyword = args[args.index("--keyword") + 1]
                unique = "one" if keyword == "AI" else "two"
                jobs = [self.raw_job(unique, title=keyword), self.raw_job("shared")]
                Path(output_path).write_text(json.dumps({
                    "keyword": keyword, "city": "深圳", "filters": {},
                    "scraped_at": "2026-08-23T10:00:00+08:00", "jobs": jobs,
                }, ensure_ascii=False), encoding="utf-8")
                if on_snapshot:
                    on_snapshot(Path(output_path))
            return "完成"

        with patch.object(collector.cdp, "account_for", return_value="collect"), \
                patch.object(collector.cdp, "launch", return_value={"ok": True}), \
                patch.object(collector, "_run_scraper", side_effect=fake_scraper), \
                patch.object(collector, "ITEM_GAP_SEC", 0):
            collector._worker(run_id, "config", tasks, False, True)

        self.assertEqual(len(calls), 3, calls)
        self.assertEqual(sum("--input" in args for args in calls), 1)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) count FROM jobs").fetchone()["count"], 3)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) count FROM job_details").fetchone()["count"], 3)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) count FROM job_collection_hits WHERE is_active=1"
        ).fetchone()["count"], 4)
        run = get_db().execute(
            "SELECT status FROM collect_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(collector.status()["progress"]["detail_completed"], 3)

    def test_streamed_process_reports_lines_and_file_snapshots(self):
        output = Path(self.tmp.name) / "stream.json"
        lines = []
        snapshots = []
        code = (
            "import pathlib,sys,time; "
            "print('开始详情', flush=True); "
            "pathlib.Path(sys.argv[1]).write_text('[1]', encoding='utf-8'); "
            "print('完成一条', flush=True); time.sleep(0.3)"
        )
        # 流式机制测试不依赖外部仓库：SCRAPER_DIR 仅作为子进程 cwd，替身保证本机
        # 未部署 ../boss-zhipin-scraper 时（Windows 常见）同样可跑
        with patch.object(collector, "SCRAPER_DIR", Path(self.tmp.name)):
            result, returncode = collector._run_scraper_streamed(
                [sys.executable, "-u", "-c", code, str(output)], 5,
                os.environ.copy(), output, lines.append,
                lambda path: snapshots.append(path.read_text(encoding="utf-8")))
        self.assertEqual(returncode, 0)
        self.assertEqual(result, ["开始详情", "完成一条"])
        self.assertIn("完成一条", lines)
        self.assertEqual(snapshots[-1], "[1]")

    def test_status_restores_missing_detail_retry_after_process_restart(self):
        self.insert_job("missing-jd")
        task = collector._normalize_task({"type": "search", "keyword": "AI",
                                          "city": "深圳", "pages": 1})
        run_id, task_id = self.run_task(task)
        sync.record_source_success(run_id, task_id, task, {"missing-jd"})
        get_db().execute(
            "UPDATE collect_run_tasks SET list_file='saved-list.json',status='partial' "
            "WHERE id=?", (task_id,))
        get_db().execute(
            "UPDATE collect_runs SET status='partial',finished_at=?,phase='finished' WHERE id=?",
            (now_iso(), run_id))
        get_db().commit()
        with collector._state_lock:
            collector._state.update({"running": False, "run_id": None, "current": "",
                                     "phase": "", "cancel": False})
        status = collector.status()
        self.assertEqual(status["run_id"], run_id)
        self.assertTrue(status["retry_details_available"])
        self.assertEqual(status["progress"]["detail_total"], 1)

        retry_run = get_db().execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status) "
            "VALUES('detail_retry',?,'{}',?,?, 'partial')",
            (json.dumps({"source_run_id": run_id}), now_iso(), now_iso())).lastrowid
        get_db().execute(
            "INSERT INTO collect_run_tasks(run_id,task_key,kind,status) "
            "VALUES(?,?,'detail_retry','failed')", (retry_run, f"retry:{run_id}"))
        get_db().commit()
        with collector._state_lock:
            collector._state["run_id"] = retry_run
        after_retry_failure = collector.status()
        self.assertEqual(after_retry_failure["source_run_id"], run_id)
        self.assertTrue(after_retry_failure["retry_details_available"])


class AnalyticsTests(M12DatabaseTestCase):
    def test_analytics_uses_only_reliable_hits_and_deduplicates_jobs(self):
        self.insert_job("one", salary_min=20, salary_max=30, salary_months=14,
                        experience="3-5年", degree="本科", industry="人工智能",
                        scale="100-499人", is_headhunter=1, priority="P1", job_score=82)
        self.insert_job("two", salary_min=10, salary_max=20, salary_months=12,
                        experience="1-3年", degree="大专", industry="互联网",
                        scale="20-99人", priority="P2", job_score=68)
        self.insert_job("manual", salary_min=50, salary_max=60, is_headhunter=1)
        task_ai = collector._normalize_task({"type": "search", "keyword": "AI",
                                             "city": "深圳", "pages": 1})
        task_py = collector._normalize_task({"type": "search", "keyword": "Python",
                                             "city": "深圳", "pages": 1})
        run_ai, id_ai = self.run_task(task_ai)
        sync.record_source_success(run_ai, id_ai, task_ai, {"one", "two"})
        run_py, id_py = self.run_task(task_py)
        sync.record_source_success(run_py, id_py, task_py, {"one"})

        result = analytics.aggregate()
        self.assertEqual(result["summary"]["jobs"], 3)
        self.assertEqual(result["summary"]["active_relations"], 3)
        self.assertEqual(result["summary"]["reliable_jobs"], 2)
        self.assertEqual(result["summary"]["unattributed_jobs"], 1)
        self.assertEqual(result["summary"]["scope"], "all_current")
        self.assertEqual(result["summary"]["headhunter_jobs"], 2)
        self.assertEqual(result["summary"]["headhunter_rate"], 66.7)
        by_keyword = {item["label"]: item["count"] for item in result["by_keyword"]}
        self.assertEqual(by_keyword, {"AI": 2, "Python": 1})
        self.assertTrue(result["trend"])

        filtered = analytics.aggregate({"keyword": "Python"})
        self.assertEqual(filtered["summary"]["jobs"], 1)
        self.assertEqual(filtered["summary"]["reliable_jobs"], 1)
        self.assertEqual(filtered["summary"]["unattributed_jobs"], 0)
        self.assertEqual(filtered["summary"]["scope"], "reliable_filtered")
        self.assertEqual(filtered["summary"]["monthly_salary_avg_k"], 25.0)

    def test_default_analytics_uses_current_jobs_when_no_source_hits_exist(self):
        self.insert_job("active")
        self.insert_job("inactive", status="hr_inactive", industry="机器人")
        self.insert_job("delisted", status="delisted")
        self.insert_job("excluded", status="excluded")

        result = analytics.aggregate()

        self.assertEqual(result["summary"]["jobs"], 2)
        self.assertEqual(result["summary"]["reliable_jobs"], 0)
        self.assertEqual(result["summary"]["unattributed_jobs"], 2)
        self.assertEqual(result["summary"]["source_coverage_rate"], 0)
        self.assertEqual(result["summary"]["scope"], "all_current")
        self.assertEqual(
            {item["label"]: item["count"] for item in result["industry"]},
            {"人工智能": 1, "机器人": 1},
        )
        self.assertEqual(analytics.aggregate({"keyword": "AI"})["summary"]["jobs"], 0)

    def test_historical_date_filter_includes_reliable_inactive_snapshot(self):
        self.insert_job("history")
        task = collector._normalize_task({"type": "search", "keyword": "AI",
                                          "city": "深圳", "pages": 1})
        run_id, task_id = self.run_task(task)
        sync.record_source_success(run_id, task_id, task, {"history"})
        get_db().execute(
            "UPDATE job_collection_hits SET is_active=0,last_seen_at='2025-01-02T10:00:00+08:00' "
            "WHERE job_key='history'")
        get_db().commit()
        result = analytics.aggregate({"date_from": "2025-01-01", "date_to": "2025-01-03"})
        self.assertEqual(result["summary"]["jobs"], 1)


class ApiIntegrationTests(M12DatabaseTestCase):
    def test_collect_config_api_returns_full_city_options(self):
        result = main.collect_config_save({
            "keywords": ["AI Agent"], "cities": ["深圳"], "pages": 2,
            "filters": {"salary": "20-50K", "degree": ["本科"]},
        })
        self.assertEqual(result["config"]["filters"],
                         {"salary": "406", "degree": "203"})
        self.assertGreater(sum(len(group["cities"])
                               for group in result["city_options"]), 100)

    def test_analytics_api_ignores_jobs_without_reliable_hits(self):
        self.insert_job("reliable")
        self.insert_job("legacy-only")
        task = collector._normalize_task({"type": "search", "keyword": "AI",
                                          "city": "深圳", "pages": 1})
        run_id, task_id = self.run_task(task)
        sync.record_source_success(run_id, task_id, task, {"reliable"})
        result = main.analytics_read(keyword="AI")
        self.assertEqual(result["summary"]["jobs"], 1)

    def test_job_keyword_filter_uses_only_current_source_relations(self):
        self.insert_job("shared")
        ai = collector._normalize_task({"type": "search", "keyword": "AI",
                                        "city": "深圳", "pages": 1})
        python = collector._normalize_task({"type": "search", "keyword": "Python",
                                            "city": "深圳", "pages": 1})
        run1, task1 = self.run_task(ai)
        sync.record_source_success(run1, task1, ai, {"shared"})
        run2, task2 = self.run_task(ai)
        sync.record_source_success(run2, task2, ai, set())
        run3, task3 = self.run_task(python)
        sync.record_source_success(run3, task3, python, {"shared"})
        self.assertEqual(main.list_jobs(keyword="AI")["total"], 0)
        self.assertEqual(main.list_jobs(keyword="Python")["total"], 1)

    def test_file_import_records_success_and_source_timestamp(self):
        path = self.write_list("boss_jobs_source.json", [self.raw_job("source")],
                               scraped_at="2026-08-20T12:00:00+08:00")
        importer.import_scraper_files([path], record_run=True)
        row = get_db().execute(
            "SELECT status,data_source_at FROM collect_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["data_source_at"], "2026-08-20T12:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
