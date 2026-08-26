"""在线采集执行器：组合任务串行抓列表，再统一补齐缺失 JD。

列表文件始终按子任务精确导入；一次成功列表对应一个稳定来源快照。详情阶段
失败不会撤销已导入列表，也不会触发错误的下架 diff，可稍后单独重试缺失 JD。
"""
import json
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, importer, sync
from .boss import cdp
from .cities import resolve_city
from .db import get_db, now_iso

SCRAPER_DIR = config.SCRAPER_DIR
SCRAPER_PY = config.SCRAPER_PY
SCRAPER_SCRIPT = config.SCRAPER_SCRIPT
RESULT_DIR = config.COLLECT_RESULT_DIR
ITEM_GAP_SEC = 120
MAX_SEARCH_COMBINATIONS = 20
MAX_COMPANY_PAGES = 30
FILTER_KEYS = ("scale", "stage", "salary", "experience", "degree", "industry")
FILTER_VALUE_MAPS = {
    "scale": {"0-20人": "301", "20-99人": "302", "100-499人": "303",
              "500-999人": "304", "1000-9999人": "305", "10000人以上": "306"},
    "stage": {"未融资": "801", "天使轮": "802", "A轮": "803", "B轮": "804",
              "C轮": "805", "D轮及以上": "806", "已上市": "807",
              "不需要融资": "808"},
    "salary": {"不限": "0", "3K以下": "402", "3-5K": "403", "5-10K": "404",
               "10-20K": "405", "20-50K": "406", "50K以上": "407"},
    "experience": {"不限": "0", "在校生": "108", "应届生": "102",
                   "经验不限": "101", "1年以内": "103", "1-3年": "104",
                   "3-5年": "105", "5-10年": "106", "10年以上": "107"},
    "degree": {"不限": "0", "初中及以下": "209", "中专/中技": "208",
               "高中": "206", "大专": "202", "本科": "203", "硕士": "204",
               "博士": "205"},
    "industry": {"互联网": "1001", "电子商务": "1002", "电商": "1002",
                 "金融": "1003", "游戏": "1004", "企业服务": "1005",
                 "教育培训": "1006", "社交网络": "1007", "医疗健康": "1008",
                 "生活服务": "1009", "广告营销": "1010"},
}

DEFAULT_COLLECT_CONFIG = {
    "keywords": [],
    "cities": [{"province": "广东省", "city": "深圳", "city_code": "101280600"}],
    "pages": 3,
    "filters": {},
    "companies": [],
    "fetch_details": True,
}

_state_lock = threading.RLock()
_state = {
    "running": False, "current": "", "phase": "", "log": [],
    "run_id": None, "cancel": False, "risk_signal": "",
    "progress": {"list_total": 0, "list_completed": 0,
                 "detail_total": 0, "detail_completed": 0},
}


def _state_snapshot() -> dict:
    return {
        "running": _state["running"], "current": _state["current"],
        "phase": _state["phase"], "run_id": _state["run_id"],
        "cancel_requested": bool(_state["cancel"]),
        "risk_signal": _state["risk_signal"],
        "progress": dict(_state["progress"]), "log": list(_state["log"][-40:]),
    }


def status() -> dict:
    with _state_lock:
        result = _state_snapshot()
    if not result["run_id"]:
        latest = get_db().execute(
            "SELECT run_id FROM collect_run_tasks WHERE list_file<>'' "
            "ORDER BY run_id DESC,id DESC LIMIT 1").fetchone()
        if latest:
            result["run_id"] = latest["run_id"]
            result["phase"] = "finished"
    if result["run_id"]:
        run = get_db().execute(
            "SELECT kind,status,stats,params,finished_at FROM collect_runs WHERE id=?",
            (result["run_id"],)).fetchone()
        data_run_id = result["run_id"]
        if run and run["kind"] == "detail_retry":
            try:
                source_run_id = int(json.loads(run["params"] or "{}").get("source_run_id"))
            except (TypeError, ValueError, json.JSONDecodeError):
                source_run_id = None
            if source_run_id:
                data_run_id = source_run_id
                result["source_run_id"] = source_run_id
        rows = get_db().execute(
            "SELECT id,task_key,kind,keyword,province,city,city_code,status,stats,error,"
            "list_file,detail_file,started_at,finished_at FROM collect_run_tasks "
            "WHERE run_id=? ORDER BY id", (data_run_id,)).fetchall()
        tasks = []
        for row in rows:
            item = dict(row)
            try:
                item["stats"] = json.loads(item["stats"] or "{}")
            except (json.JSONDecodeError, TypeError):
                item["stats"] = {}
            tasks.append(item)
        result["tasks"] = tasks
        if not result["running"]:
            list_tasks = [task for task in tasks if task.get("list_file")]
            missing_count = get_db().execute(
                "SELECT COUNT(DISTINCT h.job_key) c FROM job_collection_hits h "
                "JOIN jobs j ON j.job_key=h.job_key "
                "LEFT JOIN job_details d ON d.job_key=h.job_key "
                "WHERE h.run_id=? AND j.status<>'excluded' "
                "AND (d.jd IS NULL OR trim(d.jd)='')", (data_run_id,)).fetchone()["c"]
            if not missing_count and any(task["status"] in ("partial", "failed")
                                         for task in list_tasks):
                keys = set()
                for task in list_tasks:
                    path = Path(task["list_file"])
                    if not path.exists():
                        continue
                    try:
                        payload = _read_list(path)
                    except RuntimeError:
                        continue
                    for raw in payload.get("jobs", []):
                        keys.add(importer.job_key_from(
                            raw.get("job_link", "") or raw.get("link", ""),
                            raw.get("title", ""),
                            raw.get("boss_name", "") or raw.get("company", ""),
                            raw.get("salary", "")))
                missing_count = len(_all_missing_jd(keys))
            result["progress"] = {
                "list_total": len(list_tasks), "list_completed": len(list_tasks),
                "detail_total": missing_count, "detail_completed": 0,
            }
            result["retry_details_available"] = bool(missing_count)
            if run:
                try:
                    stats = json.loads(run["stats"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    stats = {}
                result["last_result"] = {
                    "status": run["status"], "stats": stats,
                    "finished_at": run["finished_at"],
                }
                details = stats.get("details") or {}
                if "requested" in details:
                    partial = details.get("partial") or {}
                    result["progress"].update({
                        "detail_total": int(details.get("requested") or 0),
                        "detail_completed": int(details.get(
                            "updated", partial.get("updated", 0)) or 0),
                    })
    return result


def _log(message: str) -> None:
    with _state_lock:
        _state["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {message}")


def _set_phase(run_id: int, phase: str) -> None:
    get_db().execute("UPDATE collect_runs SET phase=? WHERE id=?", (phase, run_id))
    get_db().commit()
    with _state_lock:
        if _state["run_id"] == run_id:
            _state["phase"] = phase


def _set_progress(**values) -> None:
    with _state_lock:
        _state["progress"].update(values)


def _is_cancelled() -> bool:
    with _state_lock:
        return bool(_state["cancel"])


def _collect_config_key(resume_id) -> str:
    return f"collect_config:{int(resume_id or 0)}"


def _clean_filters(raw: dict) -> dict:
    raw = raw or {}
    cleaned = {}
    for key in FILTER_KEYS:
        value = raw.get(key)
        if value in (None, "", []):
            continue
        values = value if isinstance(value, (list, tuple, set)) else re.split(
            r"[,，]", str(value))
        codes = []
        mapping = FILTER_VALUE_MAPS[key]
        for item in values:
            text = str(item).strip()
            if not text:
                continue
            if text in mapping:
                code = mapping[text]
            elif text.isdigit():
                code = text
            else:
                raise ValueError(f"不支持的{key}筛选值: {text}")
            if code != "0" and code not in codes:
                codes.append(code)
        if codes:
            cleaned[key] = ",".join(codes)
    return cleaned


def _clean_keywords(values) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，\n]", values)
    return list(dict.fromkeys(str(value).strip() for value in (values or [])
                              if str(value).strip()))


def normalize_collect_config(body: dict, resume_id=None) -> dict:
    """校验采集配置；关键词与全局城市最多生成 20 个组合。"""
    body = body or {}
    keywords = _clean_keywords(body.get("keywords") or body.get("keyword"))
    if "cities" in body:
        raw_cities = body["cities"]
    elif "global_cities" in body:
        raw_cities = body["global_cities"]
    else:
        raw_cities = DEFAULT_COLLECT_CONFIG["cities"]
    cities = []
    seen_codes = set()
    for raw in raw_cities:
        city = resolve_city(raw)
        if city["city_code"] not in seen_codes:
            cities.append(city)
            seen_codes.add(city["city_code"])
    if keywords and not cities:
        raise ValueError("关键词采集至少选择一个城市")
    combinations = len(keywords) * len(cities)
    if combinations > MAX_SEARCH_COMBINATIONS:
        raise ValueError(
            f"关键词×城市最多 {MAX_SEARCH_COMBINATIONS} 个组合，当前为 {combinations} 个")
    pages = int(body.get("pages", DEFAULT_COLLECT_CONFIG["pages"]))
    if pages < 1 or pages > 10:
        raise ValueError("关键词采集页数必须在 1-10 之间")
    companies = []
    for raw in body.get("companies") or []:
        if not isinstance(raw, dict):
            continue
        identity = raw.get("brand_id") or raw.get("url")
        if identity:
            companies.append({key: raw[key] for key in
                              ("name", "url", "brand_id", "pages") if raw.get(key)})
    return {
        "resume_id": int(resume_id or body.get("resume_id") or 0),
        "keywords": keywords, "cities": cities, "pages": pages,
        "filters": _clean_filters(body.get("filters") or body),
        "companies": companies,
        "fetch_details": bool(body.get("fetch_details", True)),
    }


def get_collect_config(resume_id=None) -> dict:
    row = get_db().execute(
        "SELECT value FROM settings WHERE key=?", (_collect_config_key(resume_id),)).fetchone()
    if row is None:
        return normalize_collect_config(DEFAULT_COLLECT_CONFIG, resume_id)
    try:
        saved = json.loads(row["value"] or "{}")
    except (json.JSONDecodeError, TypeError):
        saved = DEFAULT_COLLECT_CONFIG
    return normalize_collect_config(saved, resume_id)


def save_collect_config(body: dict, resume_id=None) -> dict:
    saved = normalize_collect_config(body, resume_id)
    conn = get_db()
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_collect_config_key(saved["resume_id"]),
         json.dumps(saved, ensure_ascii=False, sort_keys=True)))
    conn.commit()
    return saved


def build_tasks(body: dict, resume_id=None) -> list:
    cfg = normalize_collect_config(body, resume_id)
    tasks = []
    for keyword in cfg["keywords"]:
        for city in cfg["cities"]:
            tasks.append({
                "type": "search", "keyword": keyword, **city,
                "pages": cfg["pages"], "filters": dict(cfg["filters"]),
                "resume_id": cfg["resume_id"],
            })
    tasks.extend({"type": "company", **company, "resume_id": cfg["resume_id"]}
                 for company in cfg["companies"])
    if not tasks:
        raise ValueError("采集配置至少需要一个关键词或定向公司")
    return _normalize_tasks(tasks)


def _normalize_task(raw: dict) -> dict:
    kind = str(raw.get("type") or raw.get("kind") or "search")
    if kind == "company":
        identity = raw.get("brand_id") or raw.get("url")
        if not identity:
            raise ValueError("公司任务缺少 url/brand_id")
        company_pages = int(raw.get("pages", 5))
        task = {"type": "company", "kind": "company",
                "brand_id": raw.get("brand_id", ""), "url": raw.get("url", ""),
                "name": raw.get("name", ""), "pages": company_pages,
                "keyword": raw.get("name", ""), "province": "", "city": "",
                "city_code": "", "filters": {},
                "resume_id": int(raw.get("resume_id") or 0)}
        if task["pages"] < 1 or task["pages"] > MAX_COMPANY_PAGES:
            raise ValueError(f"公司采集页数必须在 1-{MAX_COMPANY_PAGES} 之间")
    else:
        keyword = str(raw.get("keyword") or "").strip()
        if not keyword:
            raise ValueError("关键词不能为空")
        city = resolve_city(raw.get("city_code") or raw.get("city") or "深圳")
        filters = dict(raw.get("filters") or {})
        for key in FILTER_KEYS:
            if raw.get(key) not in (None, ""):
                filters[key] = raw[key]
        pages = int(raw.get("pages", 3))
        if pages < 1 or pages > 10:
            raise ValueError("关键词采集页数必须在 1-10 之间")
        task = {"type": "search", "kind": "search", "keyword": keyword,
                **city, "pages": pages, "filters": _clean_filters(filters),
                "resume_id": int(raw.get("resume_id") or 0)}
    task["search_key"] = sync.source_key(task)
    task["task_key"] = task["search_key"]
    return task


def _normalize_tasks(tasks: list) -> list:
    normalized = []
    seen = set()
    search_count = 0
    for raw in tasks:
        task = _normalize_task(raw)
        if task["task_key"] in seen:
            continue
        seen.add(task["task_key"])
        normalized.append(task)
        search_count += task["type"] == "search"
    if search_count > MAX_SEARCH_COMBINATIONS:
        raise ValueError(f"关键词×城市最多 {MAX_SEARCH_COMBINATIONS} 个组合")
    if not normalized:
        raise ValueError("没有可执行的采集任务")
    return normalized


def _begin_run(kind: str, params: dict, tasks: list) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO collect_runs(kind,params,stats,started_at,status,phase) "
        "VALUES(?,?,?,?,?,?)",
        (kind, json.dumps(params, ensure_ascii=False), "{}", now_iso(), "running", "list"))
    run_id = cur.lastrowid
    for task in tasks:
        row = conn.execute(
            "INSERT INTO collect_run_tasks("
            "run_id,task_key,kind,keyword,province,city,city_code,filters,status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, task["task_key"], task["kind"], task.get("keyword", ""),
             task.get("province", ""), task.get("city", ""),
             task.get("city_code", ""),
             json.dumps(task.get("filters") or {}, ensure_ascii=False, sort_keys=True),
             "queued"))
        task["_task_id"] = row.lastrowid
    conn.commit()
    with _state_lock:
        _state.update({"running": True, "current": kind, "phase": "list",
                       "log": [], "run_id": run_id, "cancel": False,
                       "risk_signal": "",
                       "progress": {"list_total": len(tasks), "list_completed": 0,
                                    "detail_total": 0, "detail_completed": 0}})
    return run_id


def _finish_run(run_id: int, report: dict, run_status: str,
                data_source_at: str = "", risk_signal: str = "") -> None:
    conn = get_db()
    conn.execute(
        "UPDATE collect_runs SET stats=?,finished_at=?,status=?,phase=?,"
        "data_source_at=?,risk_signal=? WHERE id=?",
        (json.dumps(report, ensure_ascii=False), now_iso(), run_status, "finished",
         data_source_at or None, risk_signal, run_id))
    conn.commit()
    with _state_lock:
        if _state["run_id"] == run_id:
            _state["running"] = False
            _state["current"] = ""
            _state["phase"] = "finished"
            _state["risk_signal"] = risk_signal


def _result_path(run_id: int, task_id: int, company: bool = False) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "boss_company_jobs" if company else "boss_jobs"
    return RESULT_DIR / f"{prefix}_run{run_id}_task{task_id}.json"


def _run_scraper(args: list, timeout: int, cdp_port: int,
                 output_path: Path = None, detail_output: Path = None,
                 on_output=None, on_snapshot=None) -> str:
    """跑 scraper 子进程；需要实时进度时流式读取输出并监听结果文件。"""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    output = output_path or RESULT_DIR / (
        f"boss_jobs_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
    command = [str(SCRAPER_PY), str(SCRAPER_SCRIPT), *args,
               "--cdp-port", str(cdp_port), "--output", str(output)]
    if detail_output is not None:
        command.extend(["--detail-output", str(detail_output)])
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    watch_path = Path(detail_output or output) if on_snapshot else None
    if on_output is None and on_snapshot is None:
        proc = subprocess.run(command, cwd=str(SCRAPER_DIR), capture_output=True,
                              text=True, timeout=timeout, env=env)
        stdout = (proc.stdout or "").strip().splitlines()
        stderr = (proc.stderr or "").strip().splitlines()
        returncode = proc.returncode
        failure_lines = stderr + stdout
    else:
        stdout, returncode = _run_scraper_streamed(
            command, timeout, env, watch_path, on_output, on_snapshot)
        failure_lines = stdout
    if returncode != 0:
        tail = " / ".join(failure_lines[-12:])
        raise RuntimeError(f"scraper 退出码 {returncode}: {tail}")
    # 外部脚本为保留部分列表，会捕获某些 RuntimeError 后以 0 退出；这类结果可导入，
    # 但绝不能被当作完整来源快照执行缺失 diff。
    warnings = [line.strip() for line in stdout
                if "⚠️" in line and "继续执行实际职位搜索" not in line]
    if warnings:
        raise RuntimeError("scraper 未完整完成: " + " / ".join(warnings[-3:]))
    return " / ".join(stdout[-3:])


def _file_signature(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _run_scraper_streamed(command: list, timeout: int, env: dict,
                           watch_path: Path = None, on_output=None,
                           on_snapshot=None) -> tuple[list[str], int]:
    """持续消费子进程输出；原子结果文件每次变化后通知调用方即时入库。"""
    proc = subprocess.Popen(
        command, cwd=str(SCRAPER_DIR), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
    lines = []
    output_queue = queue.Queue()
    finished = object()
    initial_signature = _file_signature(watch_path) if watch_path else None

    def read_output():
        try:
            for raw in proc.stdout:
                output_queue.put(raw.rstrip())
        finally:
            output_queue.put(finished)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    signature = initial_signature
    stream_finished = False
    try:
        while not stream_finished:
            if time.monotonic() >= deadline:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(command, timeout,
                                                output="\n".join(lines))
            try:
                line = output_queue.get(timeout=0.2)
            except queue.Empty:
                line = None
            if line is finished:
                stream_finished = True
            elif line is not None:
                lines.append(line)
                if on_output:
                    try:
                        on_output(line)
                    except Exception as error:
                        raise RuntimeError(f"处理 scraper 实时输出失败: {error}") from error
            if watch_path and on_snapshot:
                current = _file_signature(watch_path)
                if current is not None and current != signature:
                    signature = current
                    try:
                        on_snapshot(watch_path)
                    except Exception as error:
                        raise RuntimeError(f"即时导入 scraper 结果失败: {error}") from error
        returncode = proc.wait()
        if watch_path and on_snapshot:
            current = _file_signature(watch_path)
            if current is not None and current != signature:
                try:
                    on_snapshot(watch_path)
                except Exception as error:
                    raise RuntimeError(f"即时导入 scraper 结果失败: {error}") from error
        return lines, returncode
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        reader.join(timeout=1)
        if proc.stdout:
            proc.stdout.close()


_RISK_PATTERNS = (
    "风控", "验证码", "访问异常", "账号异常", "请求过于频繁", "操作频繁",
    "安全验证", "异常流量", "稍后再试", "403", "429",
)
_LOGIN_PATTERNS = ("未登录", "登录失效", "login expired", "需要登录", "请登录")
_DETAIL_ITEM_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(.+)$")


def classify_failure(error) -> dict:
    text = str(error or "")
    lower = text.lower()
    if any(pattern.lower() in lower for pattern in _RISK_PATTERNS):
        return {"kind": "risk", "risk": True, "message": text[:500]}
    if any(pattern.lower() in lower for pattern in _LOGIN_PATTERNS):
        return {"kind": "login", "risk": False, "message": text[:500]}
    return {"kind": "error", "risk": False, "message": text[:500]}


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
    """真实列表采集成功即可确认登录可用，并清除旧的采集风控提示。"""
    get_db().execute(
        "INSERT INTO account_states(account,logged_in,hint,checked_at) VALUES(?,1,'',?) "
        "ON CONFLICT(account) DO UPDATE SET logged_in=1,hint='',checked_at=excluded.checked_at",
        (account, now_iso()))
    get_db().commit()


def _list_args(task: dict) -> list:
    if task["type"] == "company":
        identity = task.get("brand_id") or task.get("url")
        return ["--company", str(identity), "--pages", str(task["pages"]), "--no-detail"]
    args = ["--keyword", task["keyword"], "--city", task["city_code"],
            "--pages", str(task["pages"]), "--no-detail"]
    for key in FILTER_KEYS:
        if task["filters"].get(key):
            args.extend([f"--{key}", str(task["filters"][key])])
    return args


def _read_list(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"采集器未生成列表文件: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"列表文件无效: {path.name}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
        raise RuntimeError(f"列表文件结构无效: {path.name}")
    return data


def _import_partial(path: Path) -> dict:
    try:
        _read_list(path)
    except RuntimeError:
        return {}
    return importer.import_scraper_files([path], record_run=False)


def _wait_between_tasks() -> bool:
    deadline = time.monotonic() + ITEM_GAP_SEC
    while time.monotonic() < deadline:
        if _is_cancelled():
            return False
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return True


def _all_missing_jd(keys: set) -> set:
    if not keys:
        return set()
    rows = get_db().execute(
        "SELECT j.job_key,j.status,d.jd FROM jobs j LEFT JOIN job_details d "
        "ON d.job_key=j.job_key").fetchall()
    return {row["job_key"] for row in rows
            if row["job_key"] in keys and row["status"] != "excluded"
            and not str(row["jd"] or "").strip()}


def _merge_list_files(files: list[Path], output: Path,
                      only_keys: set = None) -> tuple[int, str]:
    jobs = {}
    source_times = []
    for path in files:
        data = _read_list(path)
        if data.get("scraped_at"):
            source_times.append(str(data["scraped_at"]))
        for raw in data.get("jobs", []):
            if not isinstance(raw, dict):
                continue
            key = importer.job_key_from(
                raw.get("job_link", "") or raw.get("link", ""),
                raw.get("title", ""), raw.get("boss_name", "") or raw.get("company", ""),
                raw.get("salary", ""))
            if only_keys is not None and key not in only_keys:
                continue
            jobs.setdefault(key, raw)
    payload = {"keyword": "本轮合并详情", "city": "多城市", "filters": {},
               "scraped_at": max(source_times) if source_times else now_iso(),
               "total": len(jobs), "jobs": list(jobs.values())}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(jobs), payload["scraped_at"]


def _run_detail_phase(run_id: int, files: list[Path], task_ids: list[int],
                      cdp_port: int, report: dict) -> tuple[str, str]:
    all_keys = set()
    for path in files:
        data = _read_list(path)
        for raw in data.get("jobs", []):
            key = importer.job_key_from(
                raw.get("job_link", "") or raw.get("link", ""), raw.get("title", ""),
                raw.get("boss_name", "") or raw.get("company", ""), raw.get("salary", ""))
            all_keys.add(key)
    missing = _all_missing_jd(all_keys)
    _set_progress(detail_total=len(missing), detail_completed=0)
    if not missing:
        for task_id in task_ids:
            sync.update_run_task(task_id, "succeeded", stats={"missing_jd": 0},
                                 finished=True)
        report["details"] = {"requested": 0, "updated": 0}
        return "", ""

    merged = RESULT_DIR / f"boss_jobs_run{run_id}_merged_missing.json"
    detail = RESULT_DIR / f"boss_details_run{run_id}.json"
    total, source_at = _merge_list_files(files, merged, missing)
    _log(f"详情阶段: 合并 {len(files)} 个列表，{total} 个岗位缺少 JD")
    completed = 0

    def stream_output(line: str) -> None:
        match = _DETAIL_ITEM_RE.match(line.strip())
        if not match:
            return
        current = f"JD {match.group(1)}/{match.group(2)}: {match.group(3)}"
        with _state_lock:
            if _state["run_id"] == run_id:
                _state["current"] = current
        _log(current)

    def import_snapshot(path: Path) -> None:
        nonlocal completed
        importer.import_scraper_details(str(path))
        current_completed = total - len(_all_missing_jd(missing))
        if current_completed > completed:
            completed = current_completed
            _set_progress(detail_completed=completed)
            _log(f"JD 已入库 {completed}/{total}，岗位列表可立即查看")

    try:
        out = _run_scraper(["--input", str(merged), "--detail"],
                           timeout=max(900, total * 90 + 300), cdp_port=cdp_port,
                           output_path=merged, detail_output=detail,
                           on_output=stream_output, on_snapshot=import_snapshot)
        if out:
            _log(out)
        detail_stats = importer.import_scraper_details(str(detail))
        completed = total - len(_all_missing_jd(missing))
        _set_progress(detail_completed=completed)
        for task_id in task_ids:
            sync.update_run_task(task_id, "succeeded",
                                 stats={"detail_updated": completed},
                                 detail_file=str(detail), finished=True)
        report["details"] = {"requested": total, **detail_stats,
                             "updated": completed}
        return source_at, ""
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        partial = importer.import_scraper_details(str(detail)) if detail.exists() else {
            "updated": 0}
        completed = total - len(_all_missing_jd(missing))
        _set_progress(detail_completed=completed)
        failure = classify_failure(error)
        for task_id in task_ids:
            sync.update_run_task(task_id, "partial",
                                 stats={"detail_updated": completed},
                                 error=failure["message"], detail_file=str(detail),
                                 finished=True)
        report["details"] = {"requested": total, "partial": partial,
                             "updated": completed, "error": failure["message"]}
        _log(f"详情阶段失败，已保留部分结果: {failure['message']}")
        return source_at, failure["message"] if failure["risk"] else ""


def _worker(run_id: int, kind: str, tasks: list, sync_mode: bool,
            fetch_details: bool) -> None:
    report = {"items": [], "touched": 0, "delisted_jobs": 0,
              "partial": False, "cancelled": False}
    successful_files = []
    successful_task_ids = []
    source_times = []
    risk_signal = ""
    account = ""
    try:
        account = cdp.account_for("collect")
        account_label = cdp.config.ACCOUNTS[account]["label"]
        cdp_port = cdp.config.ACCOUNTS[account]["cdp_port"]
        launched = cdp.launch(account)
        _log(f"{account_label} Chrome: "
             f"{'已启动' if launched.get('ok') else launched.get('error', '启动失败')}")
        if not launched.get("ok"):
            raise RuntimeError(f"{account_label} Chrome 无法启动（CDP 未就绪）")

        for index, task in enumerate(tasks):
            task_id = task["_task_id"]
            if _is_cancelled():
                report["cancelled"] = True
                sync.update_run_task(task_id, "cancelled", finished=True)
                continue
            label = (task.get("name") or task.get("brand_id") or task.get("url")) \
                if task["type"] == "company" else f"{task['keyword']} / {task['city']}"
            _log(f"列表 {index + 1}/{len(tasks)}: {label}")
            sync.update_run_task(task_id, "running")
            list_file = _result_path(run_id, task_id, task["type"] == "company")
            live_list_count = 0

            def import_list_snapshot(path: Path) -> None:
                nonlocal live_list_count
                live_stats = importer.import_scraper_files([path], record_run=False)
                current_count = len(live_stats.get("job_keys") or [])
                if current_count > live_list_count:
                    live_list_count = current_count
                    _log(f"{label} 已即时入库 {live_list_count} 个岗位")

            try:
                out = _run_scraper(_list_args(task),
                                   timeout=task["pages"] * 150 + 300,
                                   cdp_port=cdp_port, output_path=list_file,
                                   on_snapshot=import_list_snapshot)
                if out:
                    _log(out)
                data = _read_list(list_file)
                stats = importer.import_scraper_files([list_file], record_run=False)
                _mark_account_success(account)
                relation = sync.record_source_success(
                    run_id, task_id, task, set(stats["job_keys"]))
                stats["source"] = relation
                report["items"].append({"task_key": task["task_key"], **stats})
                report["touched"] += len(stats["job_keys"])
                report["delisted_jobs"] += len(relation["delisted"])
                successful_files.append(list_file)
                successful_task_ids.append(task_id)
                if data.get("scraped_at"):
                    source_times.append(str(data["scraped_at"]))
                sync.update_run_task(task_id, "list_succeeded", stats=stats,
                                     list_file=str(list_file))
                _log(f"{label} 完成: 新增 {stats['created']} / 刷新 {stats['updated']} / "
                     f"排除跳过 {stats['excluded_skipped']}")
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                failure = classify_failure(error)
                partial_stats = _import_partial(list_file)
                report["partial"] = bool(partial_stats) or report["partial"]
                report["items"].append({"task_key": task["task_key"],
                                        "partial": partial_stats,
                                        "error": failure["message"]})
                sync.update_run_task(task_id,
                                     "partial" if partial_stats else "failed",
                                     stats={"partial_import": partial_stats},
                                     error=failure["message"], list_file=str(list_file),
                                     finished=True)
                _log(f"{label} 失败: {failure['message']}")
                _mark_account_failure(account, failure)
                if failure["risk"]:
                    risk_signal = failure["message"]
                    break
            finally:
                _set_progress(list_completed=index + 1)

            if index < len(tasks) - 1 and not risk_signal and not _is_cancelled():
                _log(f"任务间隔等待 {ITEM_GAP_SEC}s")
                if not _wait_between_tasks():
                    report["cancelled"] = True

        if risk_signal or _is_cancelled():
            report["cancelled"] = report["cancelled"] or _is_cancelled()
            for task_id in successful_task_ids:
                sync.update_run_task(
                    task_id, "partial" if fetch_details else "succeeded",
                    error=(risk_signal or "采集已取消，未执行详情阶段") if fetch_details else "",
                    finished=True)
            for task in tasks:
                row = get_db().execute(
                    "SELECT status FROM collect_run_tasks WHERE id=?",
                    (task["_task_id"],)).fetchone()
                if row and row["status"] == "queued":
                    sync.update_run_task(task["_task_id"], "cancelled", finished=True)
        elif fetch_details and successful_files:
            _set_phase(run_id, "details")
            detail_source, detail_risk = _run_detail_phase(
                run_id, successful_files, successful_task_ids, cdp_port, report)
            if detail_source:
                source_times.append(detail_source)
            if detail_risk:
                risk_signal = detail_risk
                _mark_account_failure(account, {"kind": "risk", "risk": True,
                                                "message": detail_risk})
                report["partial"] = True
        else:
            for task_id in successful_task_ids:
                sync.update_run_task(task_id, "succeeded", finished=True)

        if sync_mode and not risk_signal and not report["cancelled"]:
            inactive = sync.apply_hr_inactive()
            report["hr_inactive"] = inactive
            _log(f"HR 活跃度剔除: {inactive} 个岗位")

        failed = len(report["items"]) - len(successful_files)
        report["failed_tasks"] = max(0, failed)
        if report["cancelled"]:
            run_status = "partial" if successful_files else "cancelled"
        elif risk_signal:
            run_status = "partial" if successful_files else "failed"
        elif failed or report["partial"] or report.get("details", {}).get("error"):
            run_status = "partial" if successful_files else "failed"
        else:
            run_status = "succeeded"
        _log("采集结束" if run_status == "succeeded" else f"采集结束: {run_status}")
        _finish_run(run_id, report, run_status,
                    max(source_times) if source_times else "", risk_signal)
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        failure = classify_failure(error)
        if account:
            _mark_account_failure(account, failure)
        report["error"] = failure["message"]
        _log(f"采集失败: {failure['message']}")
        for task in tasks:
            sync.update_run_task(task["_task_id"], "failed",
                                 error=failure["message"], finished=True)
        _finish_run(run_id, report, "failed", "",
                    failure["message"] if failure["risk"] else "")


def start(kind: str, tasks: list, sync_mode: bool = False,
          fetch_details: bool = True, resume_id=None) -> dict:
    """启动后台采集；保留旧 start(kind,tasks,sync_mode) 调用方式。"""
    if not SCRAPER_PY.exists() or not SCRAPER_SCRIPT.exists():
        return {"ok": False, "error": f"未找到 scraper: {SCRAPER_PY}"}
    try:
        normalized = _normalize_tasks(tasks)
    except (TypeError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    with _state_lock:
        if _state["running"]:
            return {"ok": False, "error": "已有采集任务在运行",
                    "status": _state_snapshot()}
        _state["running"] = True
    try:
        run_id = _begin_run(kind, {"tasks": normalized, "sync": sync_mode,
                                   "fetch_details": fetch_details,
                                   "resume_id": int(resume_id or 0)}, normalized)
    except Exception:
        with _state_lock:
            _state["running"] = False
        raise
    thread = threading.Thread(
        target=_worker, args=(run_id, kind, normalized, sync_mode, fetch_details),
        daemon=True)
    thread.start()
    return {"ok": True, "run_id": run_id}


def start_config(body: dict, resume_id=None, sync_mode: bool = False) -> dict:
    """按模块化配置展开全局关键词×城市任务。"""
    try:
        cfg = normalize_collect_config(body, resume_id)
        tasks = build_tasks(cfg, resume_id)
    except (TypeError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    return start("config", tasks, sync_mode=sync_mode,
                 fetch_details=cfg["fetch_details"], resume_id=cfg["resume_id"])


def cancel() -> dict:
    with _state_lock:
        if not _state["running"]:
            return {"ok": True, "running": False}
        _state["cancel"] = True
        run_id = _state["run_id"]
    get_db().execute(
        "UPDATE collect_runs SET cancel_requested=1 WHERE id=?", (run_id,))
    get_db().commit()
    _log("已请求取消；当前子进程结束后停止")
    return {"ok": True, "running": True, "run_id": run_id}


def _retry_worker(run_id: int, source_run_id: int, task: dict) -> None:
    report = {"source_run_id": source_run_id, "retry": True}
    account = ""
    try:
        account = cdp.account_for("collect")
        port = cdp.config.ACCOUNTS[account]["cdp_port"]
        launched = cdp.launch(account)
        if not launched.get("ok"):
            raise RuntimeError("采集 Chrome 无法启动（CDP 未就绪）")
        rows = get_db().execute(
            "SELECT list_file FROM collect_run_tasks WHERE run_id=? AND list_file<>''",
            (source_run_id,)).fetchall()
        files = [Path(row["list_file"]) for row in rows if Path(row["list_file"]).exists()]
        if not files:
            raise ValueError("原采集任务没有可用列表文件")
        _set_phase(run_id, "details")
        _, risk = _run_detail_phase(run_id, files, [task["_task_id"]], port, report)
        status_value = "partial" if report.get("details", {}).get("error") else "succeeded"
        _finish_run(run_id, report, status_value, "", risk)
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        failure = classify_failure(error)
        if account:
            _mark_account_failure(account, failure)
        report["error"] = failure["message"]
        sync.update_run_task(task["_task_id"], "failed", error=failure["message"],
                             finished=True)
        _finish_run(run_id, report, "failed", "",
                    failure["message"] if failure["risk"] else "")


def retry_missing(source_run_id: int = None) -> dict:
    """只重试历史成功列表中仍缺失的 JD，不重复搜索或来源 diff。"""
    if not SCRAPER_PY.exists() or not SCRAPER_SCRIPT.exists():
        return {"ok": False, "error": f"未找到 scraper: {SCRAPER_PY}"}
    with _state_lock:
        if _state["running"]:
            return {"ok": False, "error": "已有采集任务在运行",
                    "status": _state_snapshot()}
        _state["running"] = True
    if source_run_id is None:
        row = get_db().execute(
            "SELECT run_id FROM collect_run_tasks WHERE list_file<>'' "
            "ORDER BY run_id DESC LIMIT 1").fetchone()
        source_run_id = row["run_id"] if row else None
    if not source_run_id:
        with _state_lock:
            _state["running"] = False
        return {"ok": False, "error": "没有可重试的采集记录"}
    task = _normalize_task({"type": "company", "brand_id": f"retry-{source_run_id}",
                            "name": "仅补缺失JD", "pages": 1})
    task["kind"] = "detail_retry"
    task["task_key"] = f"detail-retry:{source_run_id}"
    run_id = _begin_run("detail_retry", {"source_run_id": source_run_id}, [task])
    thread = threading.Thread(target=_retry_worker,
                              args=(run_id, int(source_run_id), task), daemon=True)
    thread.start()
    return {"ok": True, "run_id": run_id, "source_run_id": int(source_run_id)}


def plan_tasks(resume_id=None) -> list:
    """优先使用按简历保存的新配置；没有时兼容旧 AI 采集计划。"""
    row = get_db().execute(
        "SELECT 1 FROM settings WHERE key=?", (_collect_config_key(resume_id),)).fetchone()
    if row:
        return build_tasks(get_collect_config(resume_id), resume_id)
    from .strategy import get_plan
    plan = get_plan()
    tasks = [{"type": "search", **search,
              "resume_id": int(resume_id or 0)} for search in plan.get("searches", [])]
    tasks += [{"type": "company", **company,
               "resume_id": int(resume_id or 0)} for company in plan.get("companies", [])
              if company.get("url") or company.get("brand_id")]
    return _normalize_tasks(tasks) if tasks else []
