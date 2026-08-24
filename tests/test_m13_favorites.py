"""M13 测试：双账号「感兴趣」收藏同步、增量合并、状态机与 JD 补齐。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config, favorites, importer, main
from backend.db import get_db, init_db, set_setting


class M13TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="boss-copilot-m13-")
        root = Path(self.tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / "copilot.db"
        config.COLLECT_RESULT_DIR = root / "job-result"
        init_db()
        self.reset_state()

    def tearDown(self):
        self.reset_state()
        self.tmp.cleanup()

    @staticmethod
    def reset_state():
        with favorites._state_lock:
            favorites._state.update({
                "running": False, "run_id": None, "phase": "", "current": "",
                "page": 0, "cancel": False, "log": []})

    @staticmethod
    def card(key, title="收藏工程师", company="收藏科技", salary="25-45K·15薪"):
        return {"href": f"https://www.zhipin.com/job_detail/{key}.html",
                "lines": [title, salary, "3-5年 本科", company,
                          "互联网 · 100-499人", "张先生 · 招聘者", "刚刚活跃",
                          "深圳·南山区"],
                "name": "", "salary": "", "company": "", "tags": "", "area": ""}

    @staticmethod
    def page(cards, **flags):
        return {"url": favorites.FAVORITE_URL.format(page=1), "login": False,
                "risk": False, "cards": cards, **flags}

    def fake_reader_factory(self, account_pages):
        """account_pages: {account: [第1页dict, 第2页dict, ...]}；读完后返回空页。"""

        def factory(account):
            remaining = list(account_pages.get(account, []))
            state = {"closed": False}

            class Reader:
                def read_page(self, page):
                    return remaining.pop(0) if remaining else {"cards": []}

                def close(self):
                    state["closed"] = True

            return Reader()
        return factory

    def run_sync(self, account_pages, max_pages=3):
        # 只替换 favorites 命名空间里的 time 引用：同步线程的等待被跳过，
        # 测试自身的时间函数不受影响。
        with patch.object(favorites, "time"):
            result = favorites.start_sync(
                reader_factory=self.fake_reader_factory(account_pages),
                max_pages=max_pages)
            self.assertTrue(result.get("ok"), result)
            deadline = time.time() + 5
            while favorites._state["running"] and time.time() < deadline:
                time.sleep(0.01)
        return result

    def last_run(self):
        return get_db().execute(
            "SELECT * FROM collect_runs WHERE kind='favorite_sync' "
            "ORDER BY id DESC LIMIT 1").fetchone()


class CardParsingTests(M13TestCase):
    def test_line_heuristics_map_all_fields(self):
        raw = favorites.parse_favorite_card(self.card("k1"))
        self.assertEqual(raw["title"], "收藏工程师")
        self.assertEqual(raw["salary"], "25-45K·15薪")
        self.assertEqual(raw["boss_name"], "收藏科技")
        self.assertEqual(raw["tags"], "3-5年 | 本科")
        self.assertTrue(raw["job_link"].endswith("/job_detail/k1.html"))

    def test_structured_dom_fields_win_over_lines(self):
        raw = favorites.parse_favorite_card({
            "href": "/job_detail/k2.html",
            "lines": ["随便的行", "20-30K", "广告位公司"],
            "name": "新 嵌入式专家", "salary": "30-50K·14薪",
            "company": "结构化科技", "tags": "5-10年 硕士", "area": "深圳·南山区"})
        self.assertEqual(raw["title"], "嵌入式专家")   # 徽标被清理
        self.assertEqual(raw["salary"], "30-50K·14薪")
        self.assertEqual(raw["boss_name"], "结构化科技")
        self.assertEqual(raw["tags"], "5-10年 | 硕士")
        self.assertEqual(raw["location"], "深圳·南山区")

    def test_card_without_company_or_title_is_skipped(self):
        self.assertIsNone(favorites.parse_favorite_card({"lines": [], "href": ""}))
        self.assertIsNone(favorites.parse_favorite_card(
            {"lines": ["只有标题"], "href": ""}))
        self.assertIsNone(favorites.parse_favorite_card(
            {"lines": ["20-30K"], "href": "", "company": "只有公司"}))

    def test_page_dedupes_same_link(self):
        raws = favorites.parse_favorite_page(
            self.page([self.card("k1"), self.card("k1"), self.card("k2")]))
        self.assertEqual([r["job_link"].rsplit("/", 1)[-1] for r in raws],
                         ["k1.html", "k2.html"])


class MergeTests(M13TestCase):
    def raws(self, *keys):
        return [favorites.parse_favorite_card(self.card(key)) for key in keys]

    def test_new_hit_creates_job_and_lights_favorite(self):
        stats = favorites.merge_account_hits("collect", self.raws("a1", "a2"))
        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["new_hits"], 2)
        self.assertEqual(stats["newly_favorited"], 2)
        rows = get_db().execute(
            "SELECT job_key, source, favorite_at FROM jobs ORDER BY job_key").fetchall()
        self.assertEqual({r["source"] for r in rows}, {"favorite"})
        self.assertTrue(all(r["favorite_at"] for r in rows))

    def test_second_sync_refreshes_without_duplicating(self):
        favorites.merge_account_hits("collect", self.raws("a1"))
        stats = favorites.merge_account_hits("collect", self.raws("a1"))
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["new_hits"], 0)
        self.assertEqual(stats["refreshed"], 1)
        self.assertEqual(stats["newly_favorited"], 0)
        self.assertEqual(get_db().execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"], 1)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM job_favorite_hits").fetchone()["c"], 1)

    def test_local_unfavorite_is_not_resurrected(self):
        favorites.merge_account_hits("collect", self.raws("a1"))
        get_db().execute("UPDATE jobs SET favorite_at=NULL WHERE job_key='a1'")
        get_db().commit()
        stats = favorites.merge_account_hits("collect", self.raws("a1"))
        self.assertEqual(stats["newly_favorited"], 0)
        self.assertIsNone(get_db().execute(
            "SELECT favorite_at FROM jobs WHERE job_key='a1'").fetchone()["favorite_at"])

    def test_both_accounts_share_one_job_with_two_hits(self):
        favorites.merge_account_hits("collect", self.raws("a1"))
        stats = favorites.merge_account_hits("account_a", self.raws("a1"))
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["new_hits"], 1)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM job_favorite_hits").fetchone()["c"], 2)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM jobs").fetchone()["c"], 1)

    def test_excluded_jobs_are_frozen(self):
        importer.upsert_job({"job_key": "a1", "title": "岗位", "company": "公司",
                             "source": "favorite"})
        get_db().execute("UPDATE jobs SET status='excluded' WHERE job_key='a1'")
        get_db().commit()
        stats = favorites.merge_account_hits("collect", self.raws("a1"))
        self.assertEqual(stats["excluded_skipped"], 1)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM job_favorite_hits WHERE job_key='a1'")
            .fetchone()["c"], 0)


class SyncRunTests(M13TestCase):
    def test_happy_path_syncs_both_accounts_and_records_run(self):
        result = self.run_sync({
            "collect": [self.page([self.card("c1"), self.card("c2")])],
            "account_a": [self.page([self.card("c2"), self.card("a1")])]})
        run = self.last_run()
        self.assertEqual(run["status"], "succeeded")
        stats = json.loads(run["stats"])
        self.assertEqual([a["account"] for a in stats["accounts"]],
                         ["collect", "account_a"])
        self.assertTrue(all(a["ok"] for a in stats["accounts"]))
        # 快照文件按账号各存一份：collect 2 个 + account_a 2 个 = 4 条原始记录
        files = [Path(p) for p in stats["files"].values()]
        self.assertTrue(all(p.exists() for p in files))
        self.assertEqual(sum(len(json.loads(
            p.read_text(encoding="utf-8"))["jobs"]) for p in files), 4)
        # c2 命中两个账号；favorite_at 只点亮一次，job 只有一行
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM job_favorite_hits WHERE job_key='c2'")
            .fetchone()["c"], 2)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM jobs").fetchone()["c"], 3)
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM jobs WHERE favorite_at IS NOT NULL")
            .fetchone()["c"], 3)
        states = {r["account"]: r for r in get_db().execute(
            "SELECT account, logged_in FROM account_states").fetchall()}
        self.assertEqual({a: states[a]["logged_in"] for a in states},
                         {"collect": 1, "account_a": 1})

    def test_single_account_mode_only_syncs_communication(self):
        set_setting("dual_account_enabled", False)
        self.assertEqual(favorites.sync_accounts(), ["account_a"])
        called = []

        def factory(account):
            called.append(account)
            return self.fake_reader_factory(
                {"account_a": [self.page([self.card("a1")])]})("x")

        with patch.object(favorites, "time"):
            result = favorites.start_sync(reader_factory=factory)
            self.assertTrue(result["ok"])
            self.assertEqual(result["accounts"], ["account_a"])
            deadline = time.time() + 5
            while favorites._state["running"] and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(called, ["account_a"])

    def test_login_failure_marks_partial_and_continues_other_account(self):
        self.run_sync({
            "collect": [self.page([], login=True)],
            "account_a": [self.page([self.card("a1")])]})
        run = self.last_run()
        self.assertEqual(run["status"], "partial")
        stats = json.loads(run["stats"])
        by_account = {a["account"]: a for a in stats["accounts"]}
        self.assertEqual(by_account["collect"]["kind"], "login")
        self.assertTrue(by_account["account_a"]["ok"])
        state = get_db().execute(
            "SELECT logged_in, hint FROM account_states WHERE account='collect'"
        ).fetchone()
        self.assertEqual(state["logged_in"], 0)
        self.assertIn("登录", state["hint"])

    def test_risk_signal_aborts_remaining_accounts(self):
        self.run_sync({
            "collect": [self.page([], risk=True)],
            "account_a": [self.page([self.card("a1")])]})
        run = self.last_run()
        self.assertEqual(run["status"], "failed")
        self.assertIn("风控", run["risk_signal"])
        stats = json.loads(run["stats"])
        self.assertEqual([a["account"] for a in stats["accounts"]], ["collect"])
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM jobs").fetchone()["c"], 0)

    def test_start_while_running_is_rejected(self):
        with favorites._state_lock:
            favorites._state.update({"running": True, "run_id": 999})
        result = favorites.start_sync(reader_factory=self.fake_reader_factory({}))
        self.assertFalse(result["ok"])
        self.assertIn("已在进行", result["error"])

    def test_cancel_stops_before_next_account(self):
        seen = []
        # 先取出绑定方法，避免 Reader 方法里的 self 遮蔽测试实例
        make_page, make_card = self.page, self.card

        def factory(account):
            class Reader:
                def read_page(inner, page):
                    seen.append(account)
                    with favorites._state_lock:
                        favorites._state["cancel"] = True
                    return make_page([make_card(f"{account}-1")])

                def close(inner):
                    pass
            return Reader()
        with patch.object(favorites, "time"):
            result = favorites.start_sync(reader_factory=factory)
            self.assertTrue(result["ok"])
            deadline = time.time() + 5
            while favorites._state["running"] and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(seen, ["collect"])          # 第二个账号被取消跳过
        run = self.last_run()
        self.assertEqual(run["status"], "partial")
        self.assertTrue(json.loads(run["stats"])["cancelled"])
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) c FROM jobs").fetchone()["c"], 1)

    def test_status_reports_last_run_and_account_summary(self):
        self.run_sync({"collect": [self.page([self.card("c1")])],
                       "account_a": [self.page([self.card("a1")])]})
        result = favorites.status()
        self.assertFalse(result["running"])
        self.assertEqual(result["last_result"]["status"], "succeeded")
        self.assertEqual(result["account_summary"]["collect"]["jobs"], 1)
        self.assertEqual(result["account_summary"]["account_a"]["jobs"], 1)

    def test_invalid_page_count_rejected(self):
        self.assertFalse(favorites.start_sync(max_pages=0)["ok"])
        self.assertFalse(favorites.start_sync(max_pages=11)["ok"])


class DetailRetryTests(M13TestCase):
    def test_retry_details_fills_missing_jd(self):
        self.run_sync({"collect": [self.page([self.card("d1"), self.card("d2")])],
                       "account_a": [self.page([self.card("d1")])]})
        self.assertEqual(favorites.missing_jd_count(), 2)

        def fake_scraper(args, timeout, cdp_port, output_path=None, detail_output=None):
            self.assertIsNotNone(detail_output)
            details = [{"job_link": f"https://www.zhipin.com/job_detail/d{i}.html",
                        "title": "收藏工程师", "boss_name": "收藏科技",
                        "salary": "25-45K·15薪",
                        "jd": f"岗位 d{i} 的完整职责说明。", "skill_tags": ["C语言"]}
                       for i in (1, 2)]
            Path(detail_output).write_text(json.dumps(details, ensure_ascii=False),
                                            encoding="utf-8")
            return "补齐完成"
        with patch.object(favorites, "time"), \
                patch.object(favorites.cdp, "launch", return_value={"ok": True}), \
                patch.object(favorites, "_run_scraper", side_effect=fake_scraper):
            result = favorites.retry_details()
            self.assertTrue(result["ok"], result)
            deadline = time.time() + 5
            while favorites._state["running"] and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(favorites.missing_jd_count(), 0)
        row = get_db().execute(
            "SELECT jd FROM job_details WHERE job_key='d1'").fetchone()
        self.assertIn("完整职责说明", row["jd"])

    def test_retry_without_snapshot_is_rejected(self):
        result = favorites.retry_details()
        self.assertFalse(result["ok"])


class ApiTests(M13TestCase):
    def test_jobs_list_exposes_favorite_accounts(self):
        favorites.merge_account_hits("collect",
                                     [favorites.parse_favorite_card(self.card("j1"))])
        favorites.merge_account_hits("account_a",
                                     [favorites.parse_favorite_card(self.card("j1"))])
        data = main.list_jobs(favorite="only")
        self.assertEqual(data["total"], 1)
        accounts = set(data["items"][0]["favorite_accounts"].split(","))
        self.assertEqual(accounts, {"collect", "account_a"})

    def test_sync_start_endpoint_returns_error_when_busy(self):
        with favorites._state_lock:
            favorites._state.update({"running": True, "run_id": 1})
        with self.assertRaises(Exception):
            main.favorites_sync_start({"max_pages": 2})

    def test_sync_start_endpoint_accepts_max_pages(self):
        with patch.object(favorites, "start_sync",
                          return_value={"ok": True, "run_id": 7}) as starter:
            result = main.favorites_sync_start({"max_pages": 4})
        self.assertEqual(result, {"ok": True, "run_id": 7})
        starter.assert_called_once_with(4)


if __name__ == "__main__":
    unittest.main()
