"""数据分析多标签交叉筛选与高阶指标（分位数/漏斗/榜单）测试。"""
import os
import tempfile
import unittest
from pathlib import Path


_TEST_HOME = tempfile.mkdtemp(prefix="boss-copilot-analytics-cross-")
os.environ["BOSS_COPILOT_HOME"] = _TEST_HOME

from backend import analytics, config, workflow  # noqa: E402
from backend.db import close_db, get_db, init_db, now_iso  # noqa: E402
from backend.resumes import get_default_resume  # noqa: E402


class AnalyticsCrossTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-analytics-case-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        init_db()
        self.resume = get_default_resume()

    def tearDown(self):
        close_db()
        self.tmp.cleanup()

    @staticmethod
    def add_job(job_key: str, **values):
        ts = now_iso()
        fields = {
            "title": f"岗位 {job_key}", "company": "示例公司", "status": "active",
            "industry": "人工智能", "salary_min": 20, "salary_max": 30,
            "experience": "1-3年", "degree": "本科",
        }
        fields.update(values)
        columns = ["job_key", *fields, "first_seen_at", "last_seen_at"]
        args = [job_key, *fields.values(), ts, ts]
        get_db().execute(
            f"INSERT INTO jobs({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})", args)
        get_db().commit()

    @staticmethod
    def add_run(enabled: bool = True) -> int:
        ts = now_iso()
        run_id = get_db().execute(
            "INSERT INTO collect_runs(kind,params,stats,started_at,finished_at,status,"
            "enabled) VALUES('search','{}','{}',?,?,'succeeded',?)",
            (ts, ts, 1 if enabled else 0)).lastrowid
        get_db().commit()
        return run_id

    @staticmethod
    def attach(run_id: int, job_key: str, keyword: str, seen_at: str = "",
               city: str = "深圳", city_code: str = "101280600"):
        """为岗位登记一条可靠采集命中（来源关键词 + 城市归属）。"""
        ts = seen_at or now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO job_run_items(run_id,job_key,source,created_at) "
            "VALUES(?,?,'test',?)", (run_id, job_key, ts))
        conn.execute(
            "INSERT INTO job_collection_hits(run_id,job_key,search_key,keyword,"
            "city,city_code,is_active,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,1,?,?)",
            (run_id, job_key, f"{run_id}:{keyword}", keyword,
             city, city_code, ts, ts))
        conn.commit()

    def labels(self, items):
        return [item["label"] for item in items]

    def option_map(self, result, field):
        return {item["label"]: item for item in result["meta"]["options"][field]}


class LegacyCompatTests(AnalyticsCrossTestCase):
    def test_unfiltered_aggregate_keeps_legacy_keys_and_values(self):
        self.add_job("a")                       # 月薪中点 25
        self.add_job("b", salary_min=30, salary_max=40,
                     experience="3-5年", degree="硕士")  # 中点 35
        self.add_job("c", salary_min=None, salary_max=None)

        result = analytics.aggregate()

        summary = result["summary"]
        self.assertEqual(summary["jobs"], 3)
        self.assertEqual(summary["monthly_salary_avg_k"], 30.0)
        self.assertEqual(summary["annual_salary_avg_k"], 360.0)
        # 存量键全部保留，语义不变。
        for key in ("salary_monthly", "salary_annual", "experience", "degree",
                    "industry", "scale", "job_score", "priority", "by_keyword",
                    "by_city", "trend"):
            self.assertIn(key, result)
        self.assertEqual(
            self.labels(result["salary_monthly"]),
            ["10K以下", "10-20K", "20-30K", "30-50K", "50K以上", "未标注"])
        self.assertEqual(result["by_keyword"], [])
        self.assertEqual(result["meta"]["keywords"], [])
        # 新增键就位且互不干扰存量读取方。
        for key in ("cross", "funnel", "top_companies", "top_skills"):
            self.assertIn(key, result)
        self.assertIn("experience", result["meta"]["options"])
        # 默认总览 scope 判断不受维度参数缺省影响。
        self.assertEqual(summary["scope"], "all_current")


class DimensionFilterTests(AnalyticsCrossTestCase):
    def test_multi_value_dimension_filter_uses_or_within_dimension(self):
        self.add_job("j1")                                   # 1-3年
        self.add_job("j2")                                   # 1-3年
        self.add_job("j3", experience="5-10年")
        self.add_job("j4", experience="3-5年")

        result = analytics.aggregate({"experience": ["1-3年", "5-10年"]})

        self.assertEqual(result["summary"]["jobs"], 3)
        counts = {item["label"]: item["count"]
                  for item in result["experience"]}
        self.assertEqual(counts, {"1-3年": 2, "5-10年": 1})
        # 字符串逗号形式与列表等价（API query 参数路径）。
        string_form = analytics.aggregate({"experience": "1-3年, 5-10年"})
        self.assertEqual(string_form["summary"]["jobs"], 3)

    def test_cross_dimensions_combine_with_and_semantics(self):
        self.add_job("ai_senior", industry="人工智能", experience="3-5年")
        self.add_job("ai_junior", industry="人工智能", experience="1-3年")
        self.add_job("hw_junior", industry="消费电子", experience="1-3年")
        self.add_job("hw_lead", industry="消费电子", experience="5-10年")

        result = analytics.aggregate({
            "experience": ["1-3年"], "industry": ["人工智能"]})

        self.assertEqual(result["summary"]["jobs"], 1)
        self.assertEqual(self.labels(result["industry"]), ["人工智能"])
        self.assertEqual(self.labels(result["experience"]), ["1-3年"])

    def test_salary_range_filters_on_midpoint_and_updates_quantiles(self):
        self.add_job("low", salary_min=8, salary_max=12)     # 中点 10
        self.add_job("mid", salary_min=20, salary_max=24)    # 中点 22
        self.add_job("high", salary_min=60, salary_max=80)   # 中点 70
        self.add_job("none", salary_min=None, salary_max=None)

        above15 = analytics.aggregate({"salary_min": 15})
        self.assertEqual(above15["summary"]["jobs"], 2)
        self.assertNotIn("未标注", [item["label"] for item in above15["salary_monthly"]])
        below25 = analytics.aggregate({"salary_max": 25})
        self.assertEqual(below25["summary"]["jobs"], 2)
        between = analytics.aggregate({"salary_min": 15, "salary_max": 25})
        self.assertEqual(between["summary"]["jobs"], 1)
        # 单样本时中位数等于自身，四分位带同步收窄。
        self.assertEqual(between["summary"]["median_monthly_salary_k"], 22.0)
        self.assertEqual(between["summary"]["p25_monthly_salary_k"], 22.0)

    def test_headhunter_tri_state_filter(self):
        conn = get_db()
        self.add_job("override", headhunter_override=1)
        self.add_job("auto", is_headhunter=1)
        self.add_job("plain")
        conn.commit()

        only_hh = analytics.aggregate({"headhunter": "1"})
        self.assertEqual(only_hh["summary"]["jobs"], 2)
        self.assertEqual(only_hh["summary"]["headhunter_jobs"], 2)
        no_hh = analytics.aggregate({"headhunter": "0"})
        self.assertEqual(no_hh["summary"]["jobs"], 1)
        self.assertEqual(analytics.aggregate()["summary"]["jobs"], 3)


class FacetOptionTests(AnalyticsCrossTestCase):
    def test_facet_options_exclusive_to_own_dimension(self):
        self.add_job("b1", degree="本科", industry="电子商务")
        self.add_job("b2", degree="本科", industry="电子商务")
        self.add_job("b3", degree="本科", industry="人工智能")
        self.add_job("m1", degree="硕士", industry="人工智能")

        result = analytics.aggregate({"degree": ["硕士"]})

        # 主分布按筛选收窄：只剩硕士 1 个岗位。
        self.assertEqual(result["summary"]["jobs"], 1)
        # 学历刻面对自身免疫：候选计数仍覆盖全集 4 岗位并标出选中态。
        degree_options = self.option_map(result, "degree")
        self.assertEqual(degree_options["本科"]["count"], 3)
        self.assertEqual(degree_options["硕士"]["count"], 1)
        self.assertTrue(degree_options["硕士"]["selected"])
        self.assertFalse(degree_options["本科"]["selected"])
        # 其他维度刻面吃到学历筛选：行业候选项只余人工智能。
        industry_options = self.option_map(result, "industry")
        self.assertEqual(list(industry_options), ["人工智能"])
        self.assertEqual(industry_options["人工智能"]["count"], 1)


class SourceLinkedFilterTests(AnalyticsCrossTestCase):
    def test_trends_keywords_cities_narrow_with_dimension_filters(self):
        run_id = self.add_run(True)
        self.add_job("java_job", degree="本科")
        self.add_job("py_job", degree="博士")
        self.attach(run_id, "java_job", "Java后端", "2026-08-24T10:00:00+08:00")
        self.attach(run_id, "py_job", "Python数据", "2026-08-25T10:00:00+08:00",
                    city="广州", city_code="101280100")

        full = analytics.aggregate()
        self.assertEqual(len(full["trends"]), 2)
        self.assertEqual(len(full["by_keyword"]), 2)
        self.assertEqual(len(full["meta"]["cities"]), 2)

        narrowed = analytics.aggregate({"degree": ["本科"]})
        self.assertEqual([item["count"] for item in narrowed["trends"]], [1])
        self.assertEqual(self.labels(narrowed["by_keyword"]), ["Java后端"])
        self.assertEqual(narrowed["meta"]["keywords"], ["Java后端"])
        self.assertEqual([city["name"] for city in narrowed["meta"]["cities"]],
                         ["深圳"])

    def test_cross_reports_rank_by_job_count(self):
        self.add_job("i1", experience="1-3年")
        self.add_job("i2", experience="1-3年", salary_min=30, salary_max=50)
        self.add_job("i3", experience="3-5年", salary_min=10, salary_max=14)

        cross = analytics.aggregate()["cross"]

        self.assertEqual([item["label"] for item in cross["experience_avg_salary"]],
                         ["1-3年", "3-5年"])  # 按岗位数降序
        exp_top = cross["experience_avg_salary"][0]
        self.assertEqual(exp_top["count"], 2)
        self.assertEqual(exp_top["value"], 32.5)   # 25 与 40 的均值

    def test_top_companies_and_skills_follow_filters(self):
        conn = get_db()
        self.add_job("s1", company="星尘科技", skills="Python|SQL")
        self.add_job("s2", company="星尘科技", skills="python|Go")
        self.add_job("s3", company="远山网络", skills="Java", industry="游戏开发")
        conn.commit()

        full = analytics.aggregate()
        companies = {item["label"]: item["value"] for item in full["top_companies"]}
        skills = {item["label"]: item["value"] for item in full["top_skills"]}
        self.assertEqual(companies["星尘科技"], 2)
        self.assertEqual(skills.get("python"), 2)   # 大小写归一并去重
        self.assertEqual(skills.get("sql"), 1)

        narrowed = analytics.aggregate({"industry": ["人工智能"]})
        companies = {item["label"]: item["value"] for item in narrowed["top_companies"]}
        self.assertNotIn("远山网络", companies)
        self.assertEqual(companies.get("星尘科技"), 2)


class QuantileTests(AnalyticsCrossTestCase):
    def add_salary_job(self, key: str, midpoint: float):
        self.add_job(key, salary_min=midpoint, salary_max=midpoint)

    def test_median_and_quartiles_odd_sample(self):
        self.add_salary_job("q1", 10)
        self.add_salary_job("q2", 20)
        self.add_salary_job("q3", 30)

        summary = analytics.aggregate()["summary"]

        self.assertEqual(summary["median_monthly_salary_k"], 20.0)
        self.assertEqual(summary["p25_monthly_salary_k"], 15.0)
        self.assertEqual(summary["p75_monthly_salary_k"], 25.0)

    def test_median_and_quartiles_even_sample(self):
        for index, mid in enumerate((10, 20, 30, 40)):
            self.add_salary_job(f"e{index}", mid)

        summary = analytics.aggregate()["summary"]

        self.assertEqual(summary["median_monthly_salary_k"], 25.0)
        self.assertEqual(summary["p25_monthly_salary_k"], 17.5)
        self.assertEqual(summary["p75_monthly_salary_k"], 32.5)

    def test_no_known_salary_yields_none_quantiles(self):
        self.add_job("ns", salary_min=None, salary_max=None)
        summary = analytics.aggregate()["summary"]
        self.assertIsNone(summary["monthly_salary_avg_k"])
        self.assertIsNone(summary["median_monthly_salary_k"])


class FunnelTests(AnalyticsCrossTestCase):
    def stage_by_key(self, funnel, key):
        return next(stage for stage in funnel["stages"] if stage["key"] == key)

    def add_inbound_conversation(self, suffix: str, job_key: str):
        ts = now_iso()
        conversation_id = get_db().execute(
            "INSERT INTO conversations(boss_key,boss_name,job_key,last_message_at,"
            "unread) VALUES(?,?,?,? ,1)",
            (f"boss-{suffix}", f"HR{suffix}", job_key, ts)).lastrowid
        get_db().execute(
            "INSERT INTO messages(conversation_id,direction,content,msg_key,"
            "created_at) VALUES(?,'in','方便聊聊吗',?,?)",
            (conversation_id, f"msg-{suffix}-{job_key}", ts))
        get_db().commit()

    def test_funnel_counts_merge_manual_marks_and_reply_facts(self):
        self.add_job("p1")
        self.add_job("p2")
        self.add_job("a1", experience="3-5年")
        self.add_job("iv1")
        self.add_job("of1")
        self.add_job("idle")
        workflow.set_stage("p1", "greeted", True, self.resume["id"])
        self.add_inbound_conversation("r", "p2")
        self.add_job("x", salary_min=None, salary_max=None)
        get_db().execute(
            "INSERT INTO applications(job_key,resume_id,status,confirmed_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("a1", self.resume["id"], "platform_confirmed", now_iso(),
             now_iso(), now_iso()))
        get_db().commit()
        workflow.set_stage("iv1", "interviewed", True, self.resume["id"])
        workflow.set_stage("of1", "offered", True, self.resume["id"])
        self.add_job("zzz")

        funnel = analytics.aggregate()["funnel"]

        self.assertEqual(self.stage_by_key(funnel, "pool")["count"], 8)
        greeted = self.stage_by_key(funnel, "greeted")
        applied = self.stage_by_key(funnel, "applied")
        offered = self.stage_by_key(funnel, "offered")
        # 事实表点亮前置阶段：投递/面试/Offer 都自动算作已打招呼。
        self.assertEqual(greeted["count"], 4)      # p1/a1/iv1/of1
        self.assertEqual(applied["count"], 3)      # a1/iv1/of1
        self.assertEqual(self.stage_by_key(funnel, "replied")["count"], 1)
        self.assertEqual(offered["count"], 1)
        # 相邻转化率与占样本率自洽（Offer 的上一级是面试）。
        interviewed = self.stage_by_key(funnel, "interviewed")
        self.assertAlmostEqual(
            offered["step_rate"],
            round(offered["count"] / interviewed["count"] * 100, 1), places=6)
        self.assertEqual(greeted["pool_rate"], round(4 / 8 * 100, 1))

    def test_funnel_respects_dimension_scope(self):
        self.add_job("edu_greeted", industry="教育培训")
        self.add_job("ai_plain")
        workflow.set_stage("edu_greeted", "greeted", True, self.resume["id"])

        scoped = analytics.aggregate({"industry": ["人工智能"]})["funnel"]

        self.assertEqual(self.stage_by_key(scoped, "pool")["count"], 1)
        self.assertEqual(self.stage_by_key(scoped, "greeted")["count"], 0)


class AnalyticsFrontendContractTests(unittest.TestCase):
    """分析页交互契约：图表联动、维度 chip 与新数据键的前端绑定。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.app = (root / "frontend" / "app.js").read_text()

    def test_dimension_chips_and_salary_presets_are_wired(self):
        # 维度 chip 点击 → toggleDimValue；薪资快捷档与自定义区间绑定在位。
        self.assertIn("@click=\"toggleDimValue(dim.key, option.label)\"", self.app)
        self.assertIn("applySalaryPreset(preset)", self.app)
        self.assertIn("salary_min: filters.salaryMin, salary_max: filters.salaryMax", self.app)

    def test_chart_click_drilldown_and_value_mapping(self):
        # SvgBars 支持 clickable/pick；公司榜与技能榜必须读 value 而非 count。
        self.assertIn("props: { items: Array, color: String, clickable: Boolean }", self.app)
        self.assertIn("@pick=\"item => pickChart(chart, item)\"", self.app)
        self.assertIn("rows: crossRows(store.analytics.top_companies)", self.app)
        self.assertIn("rows: crossRows(store.analytics.top_skills)", self.app)

    def test_keyword_and_city_chips_support_multi_select(self):
        # 关键词/城市与维度一致走 chip 多选：序列化为逗号串、候选带计数与选中态。
        self.assertIn("toggleDimValue('keywords', option.label)", self.app)
        self.assertIn("toggleDimValue('cities', option.code)", self.app)
        self.assertIn("keyword: filters.keywords.join(',')", self.app)
        self.assertIn("city_code: filters.cities.join(',')", self.app)

    def test_keyword_city_fallback_for_legacy_payload(self):
        # 旧后端返回体缺 keyword_options 时回退 meta.keywords/cities（不显示计数）。
        self.assertIn("const legacyKeywords = meta.value.keywords || []", self.app)
        self.assertIn('v-if="option.count != null"', self.app)

    def test_empty_state_contains_new_payload_keys(self):
        start = self.app.index("const EMPTY_ANALYTICS")
        block = self.app[start: self.app.index("\n}", start)]
        for key in ("options", "cross", "funnel", "top_companies", "top_skills",
                    "job_rows", "keyword_options", "city_options"):
            self.assertIn(key, block)

    def test_filtered_job_table_contract(self):
        # 末尾筛选结果清单：六列明细表走 jobRows 渲染，空值回落「未标注」。
        self.assertIn("筛选结果岗位", self.app)
        self.assertIn('v-for="row in jobRows" :key="row.job_key"', self.app)
        self.assertIn("{{salaryText(row)}}", self.app)
        self.assertIn("{{dimText(row.experience)}}", self.app)


class SourceOptionTests(AnalyticsCrossTestCase):
    """岗位关键词/城市多选筛选及其互斥候选刻面。"""

    def seed_hits(self):
        run_id = self.add_run(True)
        self.add_job("kj1", degree="本科")
        self.add_job("kp1", degree="博士")
        self.add_job("kz1", degree="本科")
        self.attach(run_id, "kj1", "Java后端", city="深圳", city_code="101280600")
        self.attach(run_id, "kp1", "Python数据", city="广州", city_code="101280100")
        self.attach(run_id, "kz1", "Java后端", city="深圳", city_code="101280600")

    def test_keyword_candidates_exclude_own_selection(self):
        self.seed_hits()

        result = analytics.aggregate({"keyword": ["Java后端"]})

        # 主统计按关键词收窄到 2 个岗位。
        self.assertEqual(result["summary"]["jobs"], 2)
        self.assertEqual(result["meta"]["keywords"], ["Java后端"])
        # 候选刻面对自身免疫：两条关键词都在列且标出选中态。
        options = {item["label"]: item for item in result["meta"]["keyword_options"]}
        self.assertEqual(options["Java后端"]["count"], 2)
        self.assertTrue(options["Java后端"]["selected"])
        self.assertEqual(options["Python数据"]["count"], 1)
        self.assertFalse(options["Python数据"]["selected"])

    def test_city_candidates_respect_other_conditions(self):
        self.seed_hits()

        result = analytics.aggregate({"city_code": ["101280600"]})

        cities = {item["code"]: item for item in result["meta"]["city_options"]}
        self.assertTrue(cities["101280600"]["selected"])
        self.assertFalse(cities["101280100"]["selected"])
        # 关键词候选吃到城市条件：只剩深圳命中的 Java后端。
        self.assertEqual([item["label"] for item in result["meta"]["keyword_options"]],
                         ["Java后端"])

    def test_multi_value_keyword_and_city_via_endpoint(self):
        from backend import main
        self.seed_hits()

        result = main.analytics_read(keyword="Java后端,Python数据",
                                     city_code="101280600,101280100")

        self.assertEqual(result["summary"]["jobs"], 3)
        selected = {item["label"] for item in result["meta"]["keyword_options"]
                    if item["selected"]}
        self.assertEqual(selected, {"Java后端", "Python数据"})
        selected_cities = {item["code"] for item in result["meta"]["city_options"]
                           if item["selected"]}
        self.assertEqual(selected_cities, {"101280600", "101280100"})

    def test_unfiltered_meta_still_lists_all(self):
        self.seed_hits()

        result = analytics.aggregate()

        self.assertEqual(result["meta"]["keywords"], ["Java后端", "Python数据"])
        self.assertEqual(len(result["meta"]["cities"]), 2)
        self.assertFalse(any(item["selected"]
                             for item in result["meta"]["keyword_options"]))


class JobRowsTests(AnalyticsCrossTestCase):
    def test_job_rows_follow_filters_and_field_mapping(self):
        self.add_job("m1", title="后端工程师", company="星尘科技", industry="人工智能",
                     salary="25-45K·14薪", experience="3-5年", degree="本科")
        self.add_job("m2", title="硬件工程师", company="南枝生物", industry="消费电子")

        rows = analytics.aggregate({"industry": ["人工智能"]})["job_rows"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            "job_key": "m1", "title": "后端工程师", "company": "星尘科技",
            "salary": "25-45K·14薪", "experience": "3-5年", "degree": "本科",
            "industry": "人工智能"})

    def test_job_rows_sorted_by_composite_then_salary(self):
        conn = get_db()
        self.add_job("low", salary_min=10, salary_max=12)
        self.add_job("high", salary_min=50, salary_max=60)
        self.add_job("scored", salary_min=1, salary_max=2)
        conn.execute(
            "INSERT INTO job_resume_scores(job_key,resume_id,resume_revision,"
            "l1_detail,created_at,updated_at,composite) VALUES(?,?,?,?,?,?,88.0)",
            ("scored", self.resume["id"], self.resume["revision"], "{}",
             now_iso(), now_iso()))
        conn.commit()

        keys = [row["job_key"] for row in analytics.aggregate()["job_rows"]]

        # 综合评分优先点亮，无评分岗位按月薪中点降序。
        self.assertEqual(keys, ["scored", "high", "low"])

    def test_job_rows_respect_limit(self):
        from unittest.mock import patch
        for index in range(4):
            self.add_job(f"cap{index}", salary_min=10 + index,
                         salary_max=20 + index)

        with patch.object(analytics, "JOB_ROWS_LIMIT", 2):
            rows = analytics.aggregate()["job_rows"]

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["job_key"] for row in rows], ["cap3", "cap2"])


class EndpointParamTests(AnalyticsCrossTestCase):
    def test_analytics_endpoint_passes_new_filter_params(self):
        from backend import main
        self.add_job("match", experience="3-5年", salary_min=20, salary_max=30)
        self.add_job("other", experience="1-3年", salary_min=60, salary_max=80)

        combined = main.analytics_read(
            experience="3-5年", salary_min=15.0, salary_max=35.0)

        self.assertEqual(combined["summary"]["jobs"], 1)
        self.assertTrue(self.option_map(combined, "experience")["3-5年"]["selected"])
        # 刻面对其他维度敏感：单看经验时未选值仍在候选列表。
        solo = main.analytics_read(experience="3-5年")
        solo_options = self.option_map(solo, "experience")
        self.assertTrue(solo_options["3-5年"]["selected"])
        self.assertFalse(solo_options["1-3年"]["selected"])

    def test_endpoint_without_new_params_stays_compatible(self):
        from backend import main
        self.add_job("plain")

        result = main.analytics_read(keyword="", city_code="",
                                     date_from="", date_to="")

        self.assertEqual(result["summary"]["jobs"], 1)
        self.assertEqual(result["summary"]["scope"], "all_current")
