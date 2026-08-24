"""双账号「感兴趣」收藏同步：只读导航 BOSS 推荐页感兴趣 Tab，增量合并本地收藏。

定位与边界：
- 来源固定为 https://www.zhipin.com/web/geek/recommend?tab=4&sub=1&tag=4&page=N，
  逐页翻页读取岗位卡片；全程零点击、零注入请求（只 Page.navigate + DOM 读取）。
- 双账号各同步一份：采集号(9222)与沟通号(9223)的「感兴趣」列表互相独立。
  沟通号仅在此处做只读访问（用户明确要求的例外；发送等写操作仍只走沟通号护栏）。
- 增量合并语义：新命中的岗位 upsert 入库并点亮本地收藏（不覆盖已有收藏时间）；
  已有命中仅刷新时间；BOSS 侧取消感兴趣不会删除本地数据，也不做下架 diff。
  本地手动取消收藏不会被同步复活（只有新命中的账号-岗位对才写 favorite_at）。
- 后台标签页必须开 Emulation.setFocusEmulationEnabled（BOSS SPA 无焦点不渲染）。
- 页面卡片解析借鉴外部 scraper 公司页的成熟做法：JS 只取文本行+结构化提示，
  字段映射放在 Python 纯函数里（可单测，DOM 结构变化时只改一处启发式）。
"""
import json
import random
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config, importer
from .boss import cdp
from .db import get_db, now_iso

# 详情补齐复用 collector 的 scraper 子进程封装与失败分类（同仓库内部约定）。
from .collector import _run_scraper, classify_failure

FAVORITE_URL = "https://www.zhipin.com/web/geek/recommend?tab=4&sub=1&tag=4&page={page}"
DEFAULT_MAX_PAGES = 5
MAX_PAGES_LIMIT = 10
PAGE_LOAD_TIMEOUT_SEC = 25
PAGE_GAP_SEC = (2.5, 4.5)      # 同账号翻页间隔
ACCOUNT_GAP_SEC = (8, 15)      # 两个账号之间的切换间隔
RISK_TEXT_RE = re.compile("验证码|安全验证|访问异常|请求过于频繁|操作频繁|异常流量|稍后再试")

_SALARY_RE = re.compile(r"^\d[\d\.]*\s*-\s*\d[\d\.]*\s*[Kk元]")
_EXP_RE = re.compile(r"^(经验不限|在校/应届|应届生|在校生|\d+年以内|\d+-\d+年|\d+年以上)$")
_DEGREE_SET = {"学历不限", "初中", "中专/中技", "高中", "大专", "本科", "硕士", "博士",
               "MBA/EMBA"}
_COMPANY_META_RE = re.compile(
    r"^(不需要融资|未融资|天使轮|[A-D]轮及以上|已上市|\d+-?\d*人以上?|\d+-\d+人)$")
_ACTIVITY_RE = re.compile(r"活跃|在线|刚发布|今日更新|刚刚|刚更新")
_TITLE_BADGE_RE = re.compile(r"^(新|热|急|荐|顶)\s*")

EXTRACT_JS = r"""
(function () {
  var out = {url: location.href, login: false, risk: false, cards: []};
  var visible = function (el) {
    return !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
  };
  var panel = document.querySelector(
    '.sign-wrap,.login-register-content,[class*=login-register],input[placeholder*=手机号]');
  out.login = /\/web\/user\//.test(location.href) || visible(panel);
  var pick = function (root, selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var el = root.querySelector(selectors[i]);
      var text = el ? (el.textContent || '').trim() : '';
      if (text) return text;
    }
    return '';
  };
  var nodes = document.querySelectorAll('li.job-card-box, .job-card-box, li.job-card');
  for (var i = 0; i < nodes.length; i++) {
    var card = nodes[i];
    var a = card.querySelector('a[href*="job_detail"]');
    var raw = (card.innerText || '').split('\n');
    var lines = [];
    for (var j = 0; j < raw.length; j++) {
      var t = raw[j].trim();
      if (t) lines.push(t);
    }
    out.cards.push({
      href: a ? (a.getAttribute('href') || '') : '',
      lines: lines,
      name: pick(card, ['.job-name', '.job-title', '.job-name-text']),
      salary: pick(card, ['.salary', '.job-salary', '.job-salary-bottom']),
      company: pick(card, ['.company-name', '.company-card', '.boss-name']),
      tags: pick(card, ['.job-info .tag-list', '.tag-list', '.job-tags']),
      area: pick(card, ['.job-area', '.job-area-wrapper'])
    });
  }
  var body = document.body ? document.body.innerText : '';
  out.risk = /验证码|安全验证|访问异常|请求过于频繁|操作频繁|异常流量|稍后再试/.test(
    body.slice(0, 4000));
  return JSON.stringify(out);
})()
"""


class FavoriteSyncError(Exception):
    """同步失败；kind ∈ error/login/risk，决定账号状态落库与是否中断后续账号。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ── 卡片解析（纯函数，可单测）──────────────────────────────────────

def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_title(value: str) -> str:
    return _TITLE_BADGE_RE.sub("", _clean_text(value))


def _tag_fields(lines: list) -> tuple[str, str]:
    """从行文本中找经验/学历标签（"3-5年 本科" 可能合并在一行）。"""
    exp = degree = ""
    for line in lines:
        for item in line.split():
            item = item.strip()
            if not exp and _EXP_RE.match(item):
                exp = item
            elif not degree and item in _DEGREE_SET:
                degree = item
    return exp, degree


def _line_is_tags_or_meta(line: str) -> bool:
    tokens = [token for token in line.split() if token]
    if not tokens:
        return True
    if all(_EXP_RE.match(token) or token in _DEGREE_SET for token in tokens):
        return True
    return bool(_COMPANY_META_RE.match(line)) or bool(_ACTIVITY_RE.search(line))


def parse_favorite_card(card: dict) -> dict | None:
    """把单张「感兴趣」卡片映射为 scraper 兼容的原始岗位字典；识别失败返回 None。

    结构化字段（name/salary/company 来自 DOM 选择器）优先，行文本启发式兜底。
    """
    lines = [str(line).strip() for line in (card.get("lines") or [])
             if str(line).strip()]
    link = str(card.get("href") or "")
    if link.startswith("/"):
        link = f"https://www.zhipin.com{link}"

    salary = _clean_text(card.get("salary"))
    if not salary:
        salary = next((line for line in lines if _SALARY_RE.match(line)), "")
    title = _clean_title(card.get("name"))
    if not title:
        title = _clean_title(next(
            (line for line in lines if line != salary and not _SALARY_RE.match(line)),
            ""))
    company = _clean_text(card.get("company"))
    if not company:
        for line in lines:
            if line in (title, salary) or _SALARY_RE.match(line):
                continue
            if "·" in line or _line_is_tags_or_meta(line):
                continue
            company = _clean_text(line)
            break
    if not title or not company:
        return None

    tags_text = _clean_text(card.get("tags"))
    exp = degree = ""
    for item in re.split(r"[\s|·]+", tags_text):
        item = item.strip()
        if not exp and _EXP_RE.match(item):
            exp = item
        elif not degree and item in _DEGREE_SET:
            degree = item
    if not exp or not degree:
        line_exp, line_degree = _tag_fields(lines)
        exp = exp or line_exp
        degree = degree or line_degree
    return {
        "title": title,
        "salary": salary,
        "boss_name": company,
        "tags": " | ".join(item for item in (exp, degree) if item),
        "location": _clean_text(card.get("area")),
        "job_link": link,
    }


def parse_favorite_page(page_data: dict) -> list[dict]:
    """整页卡片 → 原始岗位列表（页内按链接去重）。"""
    raws = []
    seen = set()
    for card in page_data.get("cards") or []:
        if not isinstance(card, dict):
            continue
        raw = parse_favorite_card(card)
        if raw is None:
            continue
        signature = (raw.get("job_link") or "",
                     raw.get("title") or "", raw.get("boss_name") or "")
        if signature in seen:
            continue
        seen.add(signature)
        raws.append(raw)
    return raws


# ── CDP 只读页面会话 ──────────────────────────────────────────────

class FavoritePageSession:
    """账号 Chrome 里一个后台标签页（焦点仿真），逐页只读「感兴趣」Tab。"""

    def __init__(self, account: str):
        import websocket
        self.account = account
        conf = cdp.config.ACCOUNTS[account]
        launched = cdp.launch(account)
        if not launched.get("ok"):
            raise FavoriteSyncError("error", f"{conf['label']} Chrome 无法启动（CDP 未就绪）")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        version = json.loads(opener.open(
            f"http://127.0.0.1:{conf['cdp_port']}/json/version", timeout=5).read())
        self.ws = websocket.create_connection(version["webSocketDebuggerUrl"],
                                              timeout=30)
        self._id = 0
        self.tid = self.call("Target.createTarget",
                             {"url": "about:blank", "background": True})["result"]["targetId"]
        self.sid = self.call("Target.attachToTarget",
                             {"targetId": self.tid, "flatten": True})["result"]["sessionId"]
        # BOSS SPA 后台标签无焦点不渲染，必须开焦点仿真
        self.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid=self.sid)

    def call(self, method: str, params: dict, sid: str = None) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params}
        if sid:
            message["sessionId"] = sid
        self.ws.send(json.dumps(message))
        while True:
            response = json.loads(self.ws.recv())
            if response.get("id") == self._id:
                if "error" in response:
                    raise FavoriteSyncError(
                        "error", f"{method}: {response['error'].get('message')}")
                return response

    def eval_json(self, js: str) -> dict:
        result = self.call("Runtime.evaluate",
                           {"expression": js, "returnByValue": True}, sid=self.sid)
        try:
            return json.loads(result["result"]["result"]["value"] or "{}")
        except (json.JSONDecodeError, TypeError, KeyError):
            return {}

    def read_page(self, page: int) -> dict:
        """导航到第 page 页并等待卡片渲染稳定，返回 EXTRACT_JS 结果。"""
        self.call("Page.navigate", {"url": FAVORITE_URL.format(page=page)},
                  sid=self.sid)
        deadline = time.time() + PAGE_LOAD_TIMEOUT_SEC
        stable_rounds = 0
        last_count = -1
        data = {}
        while time.time() < deadline:
            data = self.eval_json(EXTRACT_JS)
            count = len(data.get("cards") or [])
            if data.get("login") or data.get("risk"):
                break
            if count and count == last_count:
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0
            last_count = count
            time.sleep(1.2)
        return data

    def close(self) -> None:
        try:
            self.call("Target.closeTarget", {"targetId": self.tid})
        except (FavoriteSyncError, OSError):
            pass
        try:
            self.ws.close()
        except OSError:
            pass


# ── 运行状态与落库 ────────────────────────────────────────────────

_state_lock = threading.RLock()
_state = {"running": False, "run_id": None, "phase": "", "current": "", "page": 0,
          "cancel": False, "log": []}


def _log(message: str) -> None:
    with _state_lock:
        _state["log"].append(f"{time.strftime('%H:%M:%S')} {message}")


def _set_state(**values) -> None:
    with _state_lock:
        _state.update(values)


def _is_cancelled() -> bool:
    with _state_lock:
        return bool(_state["cancel"])


def _snapshot() -> dict:
    with _state_lock:
        return {**{key: _state[key] for key in
                   ("running", "phase", "current", "page", "run_id", "log")},
                "cancel_requested": bool(_state["cancel"]),
                "log": list(_state["log"][-40:])}


def sync_accounts() -> list[str]:
    """参与收藏同步的账号；单账号模式下只有沟通号一份列表。"""
    accounts = ["collect", "account_a"] if cdp.dual_account_enabled() else ["account_a"]
    seen_ports = set()
    unique = []
    for account in accounts:
        port = cdp.config.ACCOUNTS[account]["cdp_port"]
        if port not in seen_ports:
            seen_ports.add(port)
            unique.append(account)
    return unique


def _mark_account_failure(account: str, failure: dict) -> None:
    if failure["kind"] not in ("risk", "login"):
        return
    hint = ("风控信号：" if failure["risk"] else "登录状态异常：") + failure["message"]
    get_db().execute(
        "INSERT INTO account_states(account,logged_in,hint,checked_at) VALUES(?,?,?,?) "
        "ON CONFLICT(account) DO UPDATE SET logged_in=excluded.logged_in,"
        "hint=excluded.hint,checked_at=excluded.checked_at",
        (account, 0 if failure["kind"] == "login" else None, hint[:500], now_iso()))
    get_db().commit()


def _mark_account_success(account: str) -> None:
    get_db().execute(
        "INSERT INTO account_states(account,logged_in,hint,checked_at) VALUES(?,1,'',?) "
        "ON CONFLICT(account) DO UPDATE SET logged_in=1,hint='',checked_at=excluded.checked_at",
        (account, now_iso()))
    get_db().commit()


def merge_account_hits(account: str, raw_jobs: list) -> dict:
    """增量合并一个账号的收藏列表：upsert 岗位 + 记录账号命中 + 点亮新收藏。"""
    conn = get_db()
    ts = now_iso()
    stats = {"total": len(raw_jobs), "created": 0, "updated": 0, "invalid": 0,
             "excluded_skipped": 0, "new_hits": 0, "refreshed": 0,
             "newly_favorited": 0}
    for raw in raw_jobs:
        job = importer._job_from_scraper(raw, "favorite", {})
        if not job["title"] or not job["company"]:
            stats["invalid"] += 1
            continue
        existing = conn.execute(
            "SELECT status, favorite_at FROM jobs WHERE job_key=?",
            (job["job_key"],)).fetchone()
        if existing is not None and existing["status"] == "excluded":
            stats["excluded_skipped"] += 1
            continue
        key, is_new = importer.upsert_job(job)
        stats["created"] += is_new
        stats["updated"] += not is_new
        hit = conn.execute(
            "SELECT id FROM job_favorite_hits WHERE job_key=? AND account=?",
            (key, account)).fetchone()
        if hit is None:
            conn.execute(
                "INSERT INTO job_favorite_hits(job_key,account,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?)", (key, account, ts, ts))
            stats["new_hits"] += 1
            # 只有新命中的账号-岗位对才点亮收藏：本地已取消的收藏不会被同步复活。
            if existing is None or existing["favorite_at"] is None:
                conn.execute(
                    "UPDATE jobs SET favorite_at=COALESCE(favorite_at,?) "
                    "WHERE job_key=? AND status<>'excluded'", (ts, key))
                stats["newly_favorited"] += 1
        else:
            conn.execute(
                "UPDATE job_favorite_hits SET last_seen_at=? WHERE job_key=? AND account=?",
                (ts, key, account))
            stats["refreshed"] += 1
    conn.commit()
    return stats


def _write_list_file(run_id: int, account: str, raw_jobs: list) -> str:
    result_dir = Path(config.COLLECT_RESULT_DIR)
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / f"boss_favorite_jobs_run{run_id}_{account}.json"
    payload = {"keyword": "感兴趣收藏", "city": "", "filters": {},
               "scraped_at": now_iso(), "total": len(raw_jobs), "jobs": raw_jobs}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


def _raw_key(raw: dict) -> str:
    return importer.job_key_from(raw.get("job_link", ""), raw.get("title", ""),
                                 raw.get("boss_name", ""), raw.get("salary", ""))


def _read_account(account: str, max_pages: int, reader_factory) -> tuple[list, int]:
    """逐页读取一个账号的感兴趣列表，跨页按 job_key 去重后按出现顺序返回。"""
    label = cdp.config.ACCOUNTS[account]["label"]
    reader = reader_factory(account)
    raw_by_key = {}
    pages_read = 0
    try:
        previous_signature = None
        for page in range(1, max_pages + 1):
            if _is_cancelled():
                break
            _set_state(page=page)
            data = reader.read_page(page)
            if data.get("login"):
                raise FavoriteSyncError(
                    "login", f"{label}未登录（到「账号管理」打开登录页完成登录）")
            if data.get("risk"):
                raise FavoriteSyncError(
                    "risk", f"{label}感兴趣页出现验证码/安全验证等风控信号")
            pages_read = page
            raws = parse_favorite_page(data)
            if not raws:
                break
            signature = frozenset(raw.get("job_link") or "" for raw in raws)
            if signature and signature == previous_signature:
                break
            previous_signature = signature
            added = 0
            for raw in raws:
                key = _raw_key(raw)
                if key in raw_by_key:
                    continue
                raw_by_key[key] = raw
                added += 1
            _log(f"{label} 第{page}页: {len(raws)} 张卡片")
            if added == 0 and page >= 2:
                break
            if page < max_pages:
                time.sleep(random.uniform(*PAGE_GAP_SEC))
    finally:
        reader.close()
    return list(raw_by_key.values()), pages_read


def _begin_run(accounts: list, max_pages: int) -> int:
    conn = get_db()
    run_id = conn.execute(
        "INSERT INTO collect_runs(kind,params,stats,started_at,status,phase) "
        "VALUES('favorite_sync',?,?,?,'running','account')",
        (json.dumps({"accounts": accounts, "max_pages": max_pages},
                    ensure_ascii=False), "{}", now_iso())).lastrowid
    conn.commit()
    with _state_lock:
        _state.update({"running": True, "run_id": run_id, "phase": "account",
                       "current": "", "page": 0, "cancel": False, "log": []})
    return run_id


def _finish_run(run_id: int, report: dict, run_status: str,
                risk_signal: str = "") -> None:
    conn = get_db()
    conn.execute(
        "UPDATE collect_runs SET stats=?,finished_at=?,status=?,phase='finished',"
        "risk_signal=? WHERE id=?",
        (json.dumps(report, ensure_ascii=False), now_iso(), run_status,
         risk_signal, run_id))
    conn.commit()
    with _state_lock:
        if _state["run_id"] == run_id:
            _state.update({"running": False, "current": "", "phase": "finished",
                           "page": 0})


def _sync_worker(run_id: int, accounts: list, max_pages: int, reader_factory) -> None:
    report = {"accounts": [], "files": {}, "cancelled": False}
    risk_signal = ""
    try:
        for index, account in enumerate(accounts):
            if _is_cancelled():
                report["cancelled"] = True
                break
            label = cdp.config.ACCOUNTS[account]["label"]
            _set_state(current=label, page=0)
            _log(f"开始同步 {label} 的感兴趣岗位")
            try:
                raw_jobs, pages = _read_account(account, max_pages, reader_factory)
                if _is_cancelled() and not raw_jobs:
                    report["cancelled"] = True
                    break
                report["files"][account] = _write_list_file(run_id, account, raw_jobs)
                stats = merge_account_hits(account, raw_jobs)
                _mark_account_success(account)
                report["accounts"].append({
                    "account": account, "label": label, "ok": True,
                    "pages": pages, **stats,
                    "file": Path(report["files"][account]).name})
                _log(f"{label} 完成: {stats['total']} 个岗位 / "
                     f"新增 {stats['created']} / 新收藏 {stats['newly_favorited']}")
            except FavoriteSyncError as error:
                failure = {"kind": error.kind, "risk": error.kind == "risk",
                           "message": error.message}
                _mark_account_failure(account, failure)
                report["accounts"].append({
                    "account": account, "label": label, "ok": False,
                    "kind": error.kind, "error": error.message})
                _log(f"{label} 同步失败: {error.message}")
                if error.kind == "risk":
                    risk_signal = error.message
                    break
            if index < len(accounts) - 1 and not risk_signal and not _is_cancelled():
                time.sleep(random.uniform(*ACCOUNT_GAP_SEC))

        if report["cancelled"] and not report["accounts"]:
            run_status = "cancelled"
        elif risk_signal and not any(item["ok"] for item in report["accounts"]):
            run_status = "failed"
        elif risk_signal or report["cancelled"] or \
                not all(item["ok"] for item in report["accounts"]):
            run_status = "partial"
        else:
            run_status = "succeeded"
        _log("收藏同步结束" if run_status == "succeeded"
             else f"收藏同步结束: {run_status}")
        _finish_run(run_id, report, run_status, risk_signal)
    except Exception as error:  # 线程兜底：任何异常都要结束运行并留下错误信息
        failure = classify_failure(error)
        report["error"] = failure["message"]
        _log(f"收藏同步异常终止: {failure['message']}")
        _finish_run(run_id, report, "failed",
                    failure["message"] if failure["risk"] else "")


def start_sync(max_pages: int = None, reader_factory=None) -> dict:
    """启动双账号收藏同步（后台线程）；reader_factory 供测试注入假页面。"""
    pages = DEFAULT_MAX_PAGES if max_pages is None else int(max_pages)
    if pages < 1 or pages > MAX_PAGES_LIMIT:
        return {"ok": False, "error": f"同步页数必须在 1-{MAX_PAGES_LIMIT} 之间"}
    accounts = sync_accounts()
    with _state_lock:
        if _state["running"]:
            return {"ok": False, "error": "收藏同步已在进行",
                    "status": _snapshot()}
    run_id = _begin_run(accounts, pages)
    thread = threading.Thread(
        target=_sync_worker,
        args=(run_id, accounts, pages, reader_factory or FavoritePageSession),
        daemon=True)
    thread.start()
    return {"ok": True, "run_id": run_id, "accounts": accounts, "max_pages": pages}


def cancel() -> dict:
    with _state_lock:
        if not _state["running"]:
            return {"ok": True, "running": False}
        _state["cancel"] = True
        run_id = _state["run_id"]
    get_db().execute("UPDATE collect_runs SET cancel_requested=1 WHERE id=?",
                     (run_id,))
    get_db().commit()
    _log("已请求取消；当前页读取结束后停止")
    return {"ok": True, "running": True, "run_id": run_id}


def _latest_run() -> dict:
    row = get_db().execute(
        "SELECT id,stats,status,finished_at,risk_signal,params FROM collect_runs "
        "WHERE kind='favorite_sync' ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {}
    try:
        stats = json.loads(row["stats"] or "{}")
    except (json.JSONDecodeError, TypeError):
        stats = {}
    try:
        params = json.loads(row["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        params = {}
    return {"run_id": row["id"], "status": row["status"],
            "finished_at": row["finished_at"], "risk_signal": row["risk_signal"],
            "stats": stats, "params": params}


def _file_keys(paths: list) -> set:
    keys = set()
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for raw in data.get("jobs") or []:
            keys.add(_raw_key(raw))
    return keys


def missing_jd_count(paths: list = None) -> int:
    """最近一次收藏同步中、库里仍缺 JD 的岗位数（供「补齐缺失 JD」按钮判断）。"""
    latest = _latest_run()
    if paths is None:
        paths = [path for path in (latest.get("stats", {}).get("files") or {}).values()]
    keys = _file_keys(paths)
    if not keys:
        return 0
    rows = get_db().execute(
        "SELECT j.job_key, j.status, d.jd FROM jobs j LEFT JOIN job_details d "
        "ON d.job_key=j.job_key").fetchall()
    return sum(1 for row in rows
               if row["job_key"] in keys and row["status"] != "excluded"
               and not str(row["jd"] or "").strip())


def _detail_retry_worker(run_id: int, source_run_id: int, merged: Path,
                         detail: Path, total: int) -> None:
    report = {"source_run_id": source_run_id, "retry": True}
    try:
        account = cdp.account_for("collect")
        port = cdp.config.ACCOUNTS[account]["cdp_port"]
        launched = cdp.launch(account)
        if not launched.get("ok"):
            raise RuntimeError("采集号 Chrome 无法启动（CDP 未就绪）")
        _set_state(phase="details", current="补齐收藏 JD")
        out = _run_scraper(["--input", str(merged), "--detail"],
                           timeout=max(900, total * 90 + 300), cdp_port=port,
                           output_path=merged, detail_output=detail)
        if out:
            _log(out)
        stats = importer.import_scraper_details(str(detail))
        report["details"] = stats
        _log(f"收藏 JD 补齐完成: 更新 {stats['updated']} 个")
        _finish_run(run_id, report, "succeeded")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        failure = classify_failure(error)
        partial = importer.import_scraper_details(str(detail)) if detail.exists() \
            else {"updated": 0}
        report["details"] = {"partial": partial, "error": failure["message"]}
        _finish_run(run_id, report,
                    "partial" if partial.get("updated") else "failed",
                    failure["message"] if failure["risk"] else "")


def retry_details(source_run_id: int = None) -> dict:
    """为最近一次收藏同步里仍缺 JD 的岗位补详情（采集号只读 job_detail 页）。"""
    with _state_lock:
        if _state["running"]:
            return {"ok": False, "error": "收藏同步任务进行中，稍后再试"}
    latest = _latest_run() if source_run_id is None else {
        "run_id": source_run_id,
        "stats": {"files": _run_files(source_run_id)}}
    files = [path for path in (latest.get("stats", {}).get("files") or {}).values()]
    files = [path for path in files if Path(path).exists()]
    if not files:
        return {"ok": False, "error": "没有可用的收藏同步快照文件"}
    keys = _file_keys(files)
    missing = missing_jd_count(files)
    if not missing:
        return {"ok": False, "error": "收藏岗位没有缺失的 JD"}
    merged = Path(config.COLLECT_RESULT_DIR) / \
        f"boss_favorite_missing_run{latest['run_id']}.json"
    jobs = []
    seen = set()
    for path in files:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for raw in data.get("jobs") or []:
            key = _raw_key(raw)
            if key in keys and key in seen:
                continue
            seen.add(key)
            jobs.append(raw)
    merged.parent.mkdir(parents=True, exist_ok=True)
    merged.write_text(json.dumps(
        {"keyword": "收藏补齐JD", "city": "", "filters": {}, "scraped_at": now_iso(),
         "total": len(jobs), "jobs": jobs}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    detail = Path(config.COLLECT_RESULT_DIR) / \
        f"boss_favorite_details_run{latest['run_id']}.json"
    conn = get_db()
    run_id = conn.execute(
        "INSERT INTO collect_runs(kind,params,stats,started_at,status,phase) "
        "VALUES('favorite_detail_retry',?,?,?,'running','details')",
        (json.dumps({"source_run_id": latest["run_id"]}, ensure_ascii=False), "{}",
         now_iso())).lastrowid
    conn.commit()
    with _state_lock:
        _state.update({"running": True, "run_id": run_id, "phase": "details",
                       "current": "补齐收藏 JD", "page": 0, "cancel": False,
                       "log": []})
    threading.Thread(target=_detail_retry_worker,
                     args=(run_id, latest["run_id"], merged, detail, missing),
                     daemon=True).start()
    return {"ok": True, "run_id": run_id, "source_run_id": latest["run_id"],
            "missing": missing}


def _run_files(run_id: int) -> dict:
    row = get_db().execute(
        "SELECT stats FROM collect_runs WHERE id=?", (run_id,)).fetchone()
    try:
        stats = json.loads(row["stats"] or "{}") if row else {}
    except (json.JSONDecodeError, TypeError):
        stats = {}
    return stats.get("files") or {}


def status() -> dict:
    """同步状态：实时进度 + 最近一次已完成运行的结果 + 各账号累计收藏数。"""
    result = _snapshot()
    running = result["running"]
    latest = _latest_run()
    if latest and not (running and latest["run_id"] == result["run_id"]):
        result["last_result"] = {
            "run_id": latest["run_id"], "status": latest["status"],
            "finished_at": latest["finished_at"],
            "risk_signal": latest["risk_signal"],
            "accounts": latest["stats"].get("accounts", [])}
        if not running:
            result["missing_jd"] = missing_jd_count()
    summary = get_db().execute(
        "SELECT account, COUNT(*) c, MAX(last_seen_at) latest FROM job_favorite_hits "
        "GROUP BY account").fetchall()
    result["account_summary"] = {
        row["account"]: {"jobs": row["c"], "last_seen_at": row["latest"]}
        for row in summary}
    return result
