"""在线采集执行器：组合任务串行抓列表，再统一补齐缺失 JD。

列表文件始终按子任务精确导入；一次成功列表对应一个稳定来源快照。详情阶段
失败不会撤销已导入列表，也不会触发错误的下架 diff，可稍后单独重试缺失 JD。
"""
import ctypes
import json
import math
import os
import queue
import re
import signal
import statistics
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, importer, sync
from .boss import cdp
from .cities import resolve_city
from .db import get_db, get_setting, now_iso

SCRAPER_DIR = config.SCRAPER_DIR
SCRAPER_PY = config.SCRAPER_PY
SCRAPER_SCRIPT = config.SCRAPER_SCRIPT
RESULT_DIR = config.COLLECT_RESULT_DIR
# 列表任务间隔秒数：None＝按设置 collect_pace 解析档位间隔（M20 默认均衡档 60s）；
# 数值＝强制覆盖（测试用旧钩子 patch.object(collector, "ITEM_GAP_SEC", …)）。
ITEM_GAP_SEC = None
MAX_SEARCH_COMBINATIONS = 20
MAX_COMPANY_PAGES = 30
ETA_DEFAULT_LIST_SECONDS_PER_PAGE = 45.0
ETA_DEFAULT_DETAIL_SECONDS_PER_JOB = 12.0
ETA_DEFAULT_JOBS_PER_PAGE = 15.0
ETA_SEED_WEIGHT = 3.0
ETA_SAMPLE_LIMIT = 12
ETA_MAX_LIST_SECONDS_PER_PAGE = 900.0
ETA_MAX_DETAIL_SECONDS_PER_JOB = 300.0
ETA_MAX_JOBS_PER_PAGE = 100.0
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


def _new_progress() -> dict:
    """构造兼容旧字段的岗位级采集进度。"""
    return {
        "list_total": 0, "list_completed": 0,
        "detail_total": 0, "detail_completed": 0,
        "jobs_discovered": 0, "jobs_total": 0,
        "jd_total": 0, "jd_completed": 0,
        "percent": 0, "eta_seconds": None,
    }


_state_lock = threading.RLock()
_state_condition = threading.Condition(_state_lock)
_state = {
    "running": False, "current": "", "phase": "", "log": [],
    "run_id": None, "cancel": False, "risk_signal": "",
    "paused": False, "process": None, "pause_started_at": None,
    "paused_seconds": 0.0, "worker_ident": None,
    "fetch_details": False,
    "phase_started_at": None, "phase_pause_baseline": 0.0,
    "eta_model": None,
    "progress": _new_progress(),
    "data_version": 0,
}


def _median(values, fallback: float) -> float:
    cleaned = []
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            cleaned.append(parsed)
    return float(statistics.median(cleaned)) if cleaned else float(fallback)


def _bounded_positive(value, fallback: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > maximum:
        return float(fallback)
    return parsed


def _historical_eta_priors() -> dict:
    """从近期采集统计读取稳健先验；旧运行没有 timing 时自然回退默认值。"""
    rows = get_db().execute(
        "SELECT stats FROM collect_runs WHERE status IN ('succeeded','partial') "
        "ORDER BY id DESC LIMIT 100").fetchall()
    list_rates = []
    detail_rates = []
    job_rates = []
    for row in rows:
        try:
            timing = (json.loads(row["stats"] or "{}") or {}).get("timing") or {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not timing:
            continue
        try:
            list_pages = float(timing.get("list_page_units") or 0)
            list_seconds = float(timing.get("list_active_seconds") or 0)
            detail_completed = int(timing.get("detail_completed") or 0)
            detail_seconds = float(timing.get("detail_active_seconds") or 0)
            jobs_discovered = int(timing.get("jobs_discovered") or 0)
        except (TypeError, ValueError):
            continue
        list_rate = list_seconds / list_pages if list_pages > 0 else 0
        detail_rate = detail_seconds / detail_completed if detail_completed > 0 else 0
        job_rate = jobs_discovered / list_pages if list_pages > 0 else 0
        if 0 < list_rate <= ETA_MAX_LIST_SECONDS_PER_PAGE and len(list_rates) < 20:
            list_rates.append(list_rate)
        if 0 < detail_rate <= ETA_MAX_DETAIL_SECONDS_PER_JOB and len(detail_rates) < 20:
            detail_rates.append(detail_rate)
        if 0 < job_rate <= ETA_MAX_JOBS_PER_PAGE and len(job_rates) < 20:
            job_rates.append(job_rate)
        if min(len(list_rates), len(detail_rates), len(job_rates)) >= 20:
            break
    return {
        "list_seconds_per_page": _median(
            list_rates, ETA_DEFAULT_LIST_SECONDS_PER_PAGE),
        "detail_seconds_per_job": _median(
            detail_rates, ETA_DEFAULT_DETAIL_SECONDS_PER_JOB),
        "jobs_per_page": _median(job_rates, ETA_DEFAULT_JOBS_PER_PAGE),
    }


def _new_eta_model(tasks: list, priors: dict = None) -> dict:
    priors = dict(priors or _historical_eta_priors())
    pages = [max(1, int(task.get("pages") or 1)) for task in (tasks or [])]
    return {
        "task_pages": pages,
        "list_seconds_per_page": _bounded_positive(
            priors.get("list_seconds_per_page"), ETA_DEFAULT_LIST_SECONDS_PER_PAGE,
            ETA_MAX_LIST_SECONDS_PER_PAGE),
        "detail_seconds_per_job": _bounded_positive(
            priors.get("detail_seconds_per_job"), ETA_DEFAULT_DETAIL_SECONDS_PER_JOB,
            ETA_MAX_DETAIL_SECONDS_PER_JOB),
        "jobs_per_page": _bounded_positive(
            priors.get("jobs_per_page"), ETA_DEFAULT_JOBS_PER_PAGE,
            ETA_MAX_JOBS_PER_PAGE),
        "list_samples": [],
        "current_task_index": None, "current_task_started_at": None,
        "current_task_pause_baseline": 0.0,
        "gap_after_index": None, "gap_started_at": None,
        "gap_pause_baseline": 0.0, "gaps_completed": 0,
        "detail_started_at": None, "detail_pause_baseline": 0.0,
        "detail_last_at": None, "detail_last_pause_baseline": 0.0,
        "detail_last_completed": 0, "detail_samples": [],
    }


def _blended_rate(seed: float, samples: list) -> float:
    """用中位数抑制偶发慢任务，并保留先验避免首个样本造成大跳变。"""
    recent = []
    for value in (samples or [])[-ETA_SAMPLE_LIMIT:]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            recent.append(parsed)
    if not recent:
        return max(1.0, float(seed))
    observed = statistics.median(recent)
    weight = min(float(len(recent)), 6.0)
    return max(1.0, (float(seed) * ETA_SEED_WEIGHT + observed * weight)
               / (ETA_SEED_WEIGHT + weight))


def _active_elapsed_locked(started_at, pause_baseline: float, now: float) -> float:
    if started_at is None:
        return 0.0
    paused = max(0.0, _paused_duration_locked(now) - float(pause_baseline or 0.0))
    return max(0.0, float(now) - float(started_at) - paused)


def _phase_elapsed_locked(now: float = None):
    started_at = _state.get("phase_started_at")
    if started_at is None:
        return None
    now = time.monotonic() if now is None else float(now)
    baseline = float(_state.get("phase_pause_baseline") or 0.0)
    paused = max(0.0, _paused_duration_locked(now) - baseline)
    return max(0.0, now - float(started_at) - paused)


def _list_eta_locked(progress: dict, now: float, model: dict) -> float:
    pages = list(model.get("task_pages") or [])
    total_tasks = len(pages)
    if not total_tasks:
        return 0.0
    completed_tasks = min(total_tasks, max(0, int(progress.get("list_completed") or 0)))
    rate = _blended_rate(model["list_seconds_per_page"], [
        sample["seconds"] / sample["pages"] for sample in model.get("list_samples") or []
        if sample.get("seconds", 0) > 0 and sample.get("pages", 0) > 0
    ])
    remaining = 0.0
    current_index = model.get("current_task_index")
    cancel_requested = bool(_state.get("cancel"))
    if cancel_requested:
        remaining_indexes = ([current_index]
                             if current_index is not None
                             and completed_tasks <= current_index < total_tasks else [])
    else:
        remaining_indexes = range(completed_tasks, total_tasks)
    for index in remaining_indexes:
        estimated = pages[index] * rate
        if index == current_index:
            elapsed = _active_elapsed_locked(
                model.get("current_task_started_at"),
                model.get("current_task_pause_baseline", 0.0), now)
            # 超过先验时仍保留一个小尾部，避免 ETA 在任务实际完成前归零。
            estimated = max(estimated, elapsed + rate * 0.25)
            remaining += max(0.0, estimated - elapsed)
        else:
            remaining += estimated

    active_gap = not cancel_requested and model.get("gap_after_index") is not None
    gap_remaining = 0.0
    if active_gap:
        gap_elapsed = _active_elapsed_locked(
            model.get("gap_started_at"), model.get("gap_pause_baseline", 0.0), now)
        gap_remaining = max(0.0, task_gap_seconds() - gap_elapsed)
    gaps_total = max(0, total_tasks - 1)
    future_gaps = (0 if cancel_requested else max(
        0, gaps_total - int(model.get("gaps_completed") or 0)
        - (1 if active_gap else 0)))
    remaining += gap_remaining + future_gaps * task_gap_seconds()

    if _state.get("fetch_details") and not cancel_requested:
        all_pages = max(1, sum(pages))
        completed_pages = sum(pages[:completed_tasks])
        known_jobs = max(0, int(progress.get("jobs_discovered") or 0))
        known_complete = min(known_jobs, max(0, int(progress.get("jd_completed") or 0)))
        seeded_jobs = model["jobs_per_page"] * all_pages
        if completed_tasks >= total_tasks:
            projected_jobs = known_jobs
        elif completed_pages > 0:
            observed_projection = known_jobs * all_pages / completed_pages
            confidence = min(0.8, completed_pages / all_pages)
            projected_jobs = max(known_jobs, seeded_jobs * (1.0 - confidence)
                                 + observed_projection * confidence)
        else:
            projected_jobs = max(known_jobs, seeded_jobs)
        missing_ratio = ((known_jobs - known_complete) / known_jobs
                         if known_jobs else 1.0)
        projected_missing = max(known_jobs - known_complete,
                                int(round(projected_jobs * missing_ratio)))
        detail_rate = _blended_rate(
            model["detail_seconds_per_job"], model.get("detail_samples") or [])
        remaining += projected_missing * detail_rate
    return remaining


def _detail_eta_locked(progress: dict, now: float, model: dict) -> float:
    total = max(0, int(progress.get("detail_total") or 0))
    if not total:
        total = max(0, int(progress.get("jobs_total") or 0)
                    - int(progress.get("jd_completed") or 0))
    completed = min(total, max(0, int(progress.get("detail_completed") or 0)))
    remaining_items = max(0, total - completed)
    if not remaining_items:
        return 0.0
    rate = _blended_rate(
        model["detail_seconds_per_job"], model.get("detail_samples") or [])
    since_progress = _active_elapsed_locked(
        model.get("detail_last_at") or model.get("detail_started_at"),
        model.get("detail_last_pause_baseline")
        if model.get("detail_last_at") is not None
        else model.get("detail_pause_baseline", 0.0), now)
    estimated = remaining_items * rate
    return max(rate * 0.25, estimated - since_progress)


def _progress_snapshot_locked(now: float = None) -> dict:
    progress = _new_progress()
    progress.update(_state.get("progress") or {})
    phase = _state.get("phase") or ""
    now = time.monotonic() if now is None else float(now)
    if phase == "list":
        completed = progress["list_completed"]
        total = progress["list_total"]
        ratio = min(1.0, completed / total) if total else 0.0
        progress["percent"] = int(round(
            ratio * (50 if _state.get("fetch_details") else 100)))
    elif phase == "details":
        completed = progress["detail_completed"]
        total = progress["detail_total"]
        jd_completed = max(0, int(progress.get("jd_completed") or 0))
        jd_total = max(0, int(progress.get("jd_total") or 0))
        ratio = min(1.0, jd_completed / jd_total) if jd_total else 1.0
        progress["percent"] = min(100, 50 + int(round(ratio * 50)))
    elif phase == "finished":
        progress["eta_seconds"] = None
        progress["percent"] = max(0, min(100, int(progress.get("percent") or 0)))
        return progress
    else:
        progress["percent"] = 0
        progress["eta_seconds"] = None
        return progress

    model = _state.get("eta_model")
    if not model:
        progress["eta_seconds"] = None
    elif phase == "list":
        progress["eta_seconds"] = max(0, int(round(
            _list_eta_locked(progress, now, model))))
    else:
        progress["eta_seconds"] = max(0, int(round(
            _detail_eta_locked(progress, now, model))))
    return progress


def _state_snapshot() -> dict:
    return {
        "running": _state["running"], "current": _state["current"],
        "phase": _state["phase"], "run_id": _state["run_id"],
        "paused": bool(_state["paused"]),
        "cancel_requested": bool(_state["cancel"]),
        "risk_signal": _state["risk_signal"],
        "progress": _progress_snapshot_locked(), "log": list(_state["log"][-40:]),
        "data_version": int(_state.get("data_version") or 0),
    }


def _bump_data_version() -> None:
    """数据已增量落库（新岗位或 JD 更新）；前端据此节流刷新活跃视图。"""
    with _state_condition:
        _state["data_version"] = int(_state.get("data_version") or 0) + 1


def _job_completeness(keys) -> tuple[set, set]:
    """返回仍可用岗位集合及其中已具备非空 JD 的岗位集合。"""
    wanted = {str(key) for key in (keys or []) if key}
    if not wanted:
        return set(), set()
    conn = get_db()
    eligible = set()
    complete = set()
    ordered = sorted(wanted)
    for offset in range(0, len(ordered), 500):
        chunk = ordered[offset:offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT j.job_key,j.status,d.jd FROM jobs j LEFT JOIN job_details d "
            f"ON d.job_key=j.job_key WHERE j.job_key IN ({placeholders})", chunk)
        for row in rows:
            if row["status"] == "excluded":
                continue
            eligible.add(row["job_key"])
            if str(row["jd"] or "").strip():
                complete.add(row["job_key"])
    return eligible, complete


def _update_discovered_progress(keys) -> tuple[set, set]:
    """同步当前已发现岗位与已有 JD，供列表阶段估算详情工作量。"""
    eligible, complete = _job_completeness(keys)
    _set_progress(
        jobs_discovered=len(eligible), jobs_total=len(eligible),
        jd_total=len(eligible), jd_completed=len(complete))
    return eligible, complete


def _run_job_keys(run_id: int) -> set:
    return {row["job_key"] for row in get_db().execute(
        "SELECT job_key FROM job_run_items WHERE run_id=?", (int(run_id),))}


def _attach_run_jobs(run_id: int, job_keys, source: str) -> None:
    """即时记录本轮已落库岗位；来源快照是否成功仍由 sync 单独判断。"""
    keys = list(dict.fromkeys(str(key) for key in (job_keys or []) if key))
    if not keys:
        return
    conn = get_db()
    ts = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO job_run_items(run_id,job_key,source,created_at) "
        "VALUES(?,?,?,?)", [(int(run_id), key, source, ts) for key in keys])
    conn.commit()
    _bump_data_version()


def _finished_progress(run, tasks: list, data_run_id: int) -> tuple[dict, int]:
    """从持久化数据恢复已结束任务的岗位级进度。"""
    list_tasks = [task for task in tasks if task.get("kind") != "detail_retry"]
    list_completed = sum(task.get("status") not in ("queued", "running")
                         for task in list_tasks)
    keys = _run_job_keys(data_run_id)
    if not keys and any(task.get("list_file") for task in list_tasks):
        # 兼容升级前的部分采集：只读明确保存过的列表文件，不猜其他目录。
        for task in list_tasks:
            path = Path(task.get("list_file") or "")
            if not path.is_file():
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
    eligible, complete = _job_completeness(keys)
    missing_count = len(eligible - complete)
    try:
        stats = json.loads(run["stats"] or "{}") if run else {}
    except (json.JSONDecodeError, TypeError):
        stats = {}
    try:
        params = json.loads(run["params"] or "{}") if run else {}
    except (json.JSONDecodeError, TypeError):
        params = {}
    fetch_details = bool(params.get("fetch_details", True))
    details = stats.get("details") or {}
    details_attempted = "requested" in details
    partial = details.get("partial") or {}
    detail_total = int(details.get("requested") or 0)
    detail_completed = int(details.get(
        "updated", partial.get("updated", 0)) or 0)
    if not detail_total and missing_count:
        detail_total = missing_count
    status_value = str(run["status"] or "") if run else ""
    if status_value == "succeeded":
        percent = 100
    elif list_tasks:
        list_ratio = min(1.0, list_completed / len(list_tasks))
        if fetch_details and details_attempted:
            jd_ratio = min(1.0, len(complete) / len(eligible)) if eligible else 1.0
            percent = min(100, 50 + int(round(jd_ratio * 50)))
        else:
            percent = int(round(list_ratio * (50 if fetch_details else 100)))
    else:
        percent = 0
    progress = _new_progress()
    progress.update({
        "list_total": len(list_tasks), "list_completed": list_completed,
        "detail_total": detail_total, "detail_completed": detail_completed,
        "jobs_discovered": len(eligible), "jobs_total": len(eligible),
        "jd_total": len(eligible), "jd_completed": len(complete),
        "percent": percent, "eta_seconds": None,
    })
    return progress, missing_count


def status() -> dict:
    with _state_lock:
        result = _state_snapshot()
    if not result["run_id"] and not result["running"]:
        latest = get_db().execute(
            "SELECT run_id FROM collect_run_tasks WHERE list_file<>'' "
            "ORDER BY run_id DESC,id DESC LIMIT 1").fetchone()
        if latest:
            result["run_id"] = latest["run_id"]
            result["phase"] = "finished"
    if result["run_id"]:
        run = get_db().execute(
            "SELECT kind,status,stats,params,finished_at,paused "
            "FROM collect_runs WHERE id=?",
            (result["run_id"],)).fetchone()
        if run:
            result["paused"] = bool(run["paused"])
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
            result["progress"], missing_count = _finished_progress(
                run, tasks, data_run_id)
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
    return result


def _log(message: str) -> None:
    with _state_lock:
        _state["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {message}")


def _paused_duration_locked(now: float = None) -> float:
    """返回本轮累计暂停时长；调用方必须持有 ``_state_lock``。"""
    total = float(_state.get("paused_seconds") or 0.0)
    started_at = _state.get("pause_started_at")
    if _state.get("paused") and started_at is not None:
        total += (time.monotonic() if now is None else now) - started_at
    return total


def _wait_for_resume() -> bool:
    """暂停时阻塞当前阶段；返回 False 表示等待期间收到取消。"""
    with _state_condition:
        while (_state.get("running") and _state.get("paused")
               and not _state.get("cancel")):
            _state_condition.wait(timeout=0.5)
        return not bool(_state.get("cancel"))


# 暂停/恢复动作统一入口：POSIX 是真正的 SIGSTOP/SIGCONT；Windows 无此信号
# （getattr 退化为占位语义值），由 _signal_process 映射到进程挂起/恢复 API。
_SIG_PAUSE = getattr(signal, "SIGSTOP", "suspend")
_SIG_RESUME = getattr(signal, "SIGCONT", "resume")


def _windows_suspend_process(process, suspend: bool) -> bool:
    """Windows 下挂起或恢复整个 scraper 子进程（等价 SIGSTOP/SIGCONT）。

    走 ntdll 的 NtSuspendProcess/NtResumeProcess，属 stdlib ctypes 可达的
    唯一官方级挂起原语；API 缺失（如 Wine）或打不开句柄时返回 False 降级。
    """
    try:
        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        # PROCESS_SUSPEND_RESUME：NtSuspendProcess/NtResumeProcess 所需最小权限。
        handle = kernel32.OpenProcess(0x0800, False, process.pid)
        if not handle:
            return False
        try:
            suspend_or_resume = (ntdll.NtSuspendProcess if suspend
                                 else ntdll.NtResumeProcess)
            return suspend_or_resume(handle) == 0  # NTSTATUS SUCCESS == 0
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        return False


def _signal_process(process, process_signal) -> bool:
    """向仍存活的 scraper 子进程发送暂停或恢复信号，跨平台语义一致。"""
    if process is None or process_signal is None:
        return False
    try:
        if process.poll() is not None:
            return False
        if os.name == "nt":
            return _windows_suspend_process(process, process_signal == _SIG_PAUSE)
        process.send_signal(process_signal)
        return True
    except OSError:
        return False


def _register_process(process) -> bool:
    with _state_condition:
        if (not _state.get("running")
                or _state.get("worker_ident") != threading.get_ident()):
            return False
        _state["process"] = process
        if _state.get("paused"):
            _signal_process(process, _SIG_PAUSE)
        return True


def _clear_process(process) -> None:
    with _state_condition:
        if _state.get("process") is process:
            _state["process"] = None


def _cleanup_worker_control(run_id: int) -> None:
    """工作线程退出时只清理属于本轮的内存控制句柄。"""
    with _state_condition:
        if _state.get("run_id") != run_id:
            return
        if _state.get("paused"):
            _signal_process(_state.get("process"), _SIG_RESUME)
        _state["paused"] = False
        _state["process"] = None
        _state["pause_started_at"] = None
        _state["paused_seconds"] = 0.0
        _state["worker_ident"] = None
        _state["phase_started_at"] = None
        _state["phase_pause_baseline"] = 0.0
        _state["eta_model"] = None
        _state_condition.notify_all()


def _set_phase(run_id: int, phase: str) -> bool:
    """阶段切换与暂停原子互斥，避免暂停后继续进入下一阶段。"""
    with _state_condition:
        while (_state.get("running") and _state.get("paused")
               and not _state.get("cancel")):
            _state_condition.wait(timeout=0.5)
        if _state.get("cancel"):
            return False
        get_db().execute("UPDATE collect_runs SET phase=? WHERE id=?", (phase, run_id))
        get_db().commit()
        if _state["run_id"] == run_id:
            now = time.monotonic()
            _state["phase"] = phase
            _state["phase_started_at"] = now
            _state["phase_pause_baseline"] = _paused_duration_locked(now)
    return True


def _set_progress(**values) -> None:
    with _state_lock:
        _state["progress"].update(values)


def _eta_start_list_task(index: int) -> None:
    with _state_lock:
        model = _state.get("eta_model")
        if not model:
            return
        now = time.monotonic()
        model["current_task_index"] = int(index)
        model["current_task_started_at"] = now
        model["current_task_pause_baseline"] = _paused_duration_locked(now)


def _eta_finish_list_task(index: int, usable: bool = True) -> None:
    with _state_lock:
        model = _state.get("eta_model")
        if not model or model.get("current_task_index") != int(index):
            return
        now = time.monotonic()
        duration = _active_elapsed_locked(
            model.get("current_task_started_at"),
            model.get("current_task_pause_baseline", 0.0), now)
        pages = model.get("task_pages") or []
        page_count = pages[index] if index < len(pages) else 1
        if usable and duration > 0:
            model["list_samples"].append({"seconds": duration, "pages": page_count})
            model["list_samples"] = model["list_samples"][-ETA_SAMPLE_LIMIT:]
        model["current_task_index"] = None
        model["current_task_started_at"] = None


def _eta_start_gap(index: int) -> None:
    with _state_lock:
        model = _state.get("eta_model")
        if not model:
            return
        now = time.monotonic()
        model["gap_after_index"] = int(index)
        model["gap_started_at"] = now
        model["gap_pause_baseline"] = _paused_duration_locked(now)


def _eta_finish_gap(index: int, completed: bool) -> None:
    with _state_lock:
        model = _state.get("eta_model")
        if not model or model.get("gap_after_index") != int(index):
            return
        if completed:
            model["gaps_completed"] = max(
                int(model.get("gaps_completed") or 0), int(index) + 1)
        model["gap_after_index"] = None
        model["gap_started_at"] = None


def _eta_start_details() -> None:
    with _state_lock:
        model = _state.get("eta_model")
        if not model:
            return
        now = time.monotonic()
        baseline = _paused_duration_locked(now)
        model["detail_started_at"] = now
        model["detail_pause_baseline"] = baseline
        model["detail_last_at"] = now
        model["detail_last_pause_baseline"] = baseline
        model["detail_last_completed"] = 0


def _eta_record_detail_progress(completed: int) -> None:
    with _state_lock:
        model = _state.get("eta_model")
        if not model:
            return
        completed = max(0, int(completed or 0))
        previous = max(0, int(model.get("detail_last_completed") or 0))
        if completed <= previous:
            return
        now = time.monotonic()
        elapsed = _active_elapsed_locked(
            model.get("detail_last_at") or model.get("detail_started_at"),
            model.get("detail_last_pause_baseline", 0.0), now)
        delta = completed - previous
        if elapsed > 0:
            model["detail_samples"].extend([elapsed / delta] * delta)
            model["detail_samples"] = model["detail_samples"][-ETA_SAMPLE_LIMIT:]
        model["detail_last_completed"] = completed
        model["detail_last_at"] = now
        model["detail_last_pause_baseline"] = _paused_duration_locked(now)


def _timing_report_locked(now: float = None) -> dict:
    model = _state.get("eta_model")
    if not model:
        return {}
    now = time.monotonic() if now is None else float(now)
    list_samples = model.get("list_samples") or []
    detail_phase_seconds = _active_elapsed_locked(
        model.get("detail_started_at"), model.get("detail_pause_baseline", 0.0), now)
    detail_samples = []
    for value in model.get("detail_samples") or []:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            detail_samples.append(parsed)
    progress = _state.get("progress") or {}
    return {
        "list_active_seconds": round(sum(
            float(sample.get("seconds") or 0) for sample in list_samples), 3),
        "list_page_units": sum(
            int(sample.get("pages") or 0) for sample in list_samples),
        # 只持久化已完成岗位之间的有效样本，避免 partial 末尾超时污染先验。
        "detail_active_seconds": round(sum(detail_samples), 3),
        "detail_completed": len(detail_samples),
        "detail_phase_seconds": round(detail_phase_seconds, 3),
        "detail_result_completed": max(
            0, int(progress.get("detail_completed") or 0)),
        "jobs_discovered": max(0, int(progress.get("jobs_discovered") or 0)),
    }


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


def _config_run_name(conn, run_id: int, params: dict, started_at: str) -> str:
    """配置采集使用固定短名称；流水号直接复用永不回退的运行 ID。"""
    resume_id = int(params.get("resume_id") or 0)
    row = conn.execute(
        "SELECT name FROM resumes WHERE id=? AND archived_at IS NULL", (resume_id,)
    ).fetchone() if resume_id else None
    if row is None:
        row = conn.execute(
            "SELECT name FROM resumes WHERE is_default=1 AND archived_at IS NULL "
            "ORDER BY id LIMIT 1").fetchone()
    resume_name = str(row["name"] if row else "未命名简历").strip()
    resume_name = re.sub(r"[\r\n\t/\\]+", "-", resume_name) or "未命名简历"
    resume_name = resume_name[:20]
    date_text = str(started_at or now_iso())[:10].replace("-", "")
    return f"{date_text}-{resume_name}-{int(run_id):04d}"


def _saved_run_name(run_id: int) -> str:
    row = get_db().execute(
        "SELECT params FROM collect_runs WHERE id=?", (int(run_id),)).fetchone()
    if row is None:
        return ""
    try:
        params = json.loads(row["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(params.get("name") or "")


def _begin_run(kind: str, params: dict, tasks: list) -> int:
    conn = get_db()
    params = dict(params or {})
    started_at = now_iso()
    cur = conn.execute(
        "INSERT INTO collect_runs(kind,params,stats,started_at,status,phase) "
        "VALUES(?,?,?,?,?,?)",
        (kind, json.dumps(params, ensure_ascii=False), "{}", started_at, "running", "list"))
    run_id = cur.lastrowid
    if kind == "config":
        params["name"] = _config_run_name(conn, run_id, params, started_at)
        conn.execute("UPDATE collect_runs SET params=? WHERE id=?",
                     (json.dumps(params, ensure_ascii=False), run_id))
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
    eta_model = _new_eta_model(tasks)
    with _state_condition:
        phase_started_at = time.monotonic()
        progress = _new_progress()
        progress["list_total"] = len(tasks)
        _state.update({"running": True, "current": kind, "phase": "list",
                       "log": [], "run_id": run_id, "cancel": False,
                       "risk_signal": "", "paused": False, "process": None,
                       "pause_started_at": None, "paused_seconds": 0.0,
                       "worker_ident": None,
                       "fetch_details": bool(params.get("fetch_details", True)),
                       "phase_started_at": phase_started_at,
                       "phase_pause_baseline": 0.0, "eta_model": eta_model,
                       "progress": progress})
        _state_condition.notify_all()
    return run_id


def _finish_run(run_id: int, report: dict, run_status: str,
                data_source_at: str = "", risk_signal: str = "") -> None:
    with _state_condition:
        while (_state.get("run_id") == run_id and _state.get("running")
               and _state.get("paused") and not _state.get("cancel")):
            _state_condition.wait(timeout=0.5)
        conn = get_db()
        timing = _timing_report_locked()
        if timing:
            report["timing"] = timing
        conn.execute(
            "UPDATE collect_runs SET stats=?,finished_at=?,status=?,phase=?,"
            "data_source_at=?,risk_signal=?,paused=0 WHERE id=?",
            (json.dumps(report, ensure_ascii=False), now_iso(), run_status, "finished",
             data_source_at or None, risk_signal, run_id))
        conn.commit()
        if _state["run_id"] == run_id:
            current = _progress_snapshot_locked()
            current["percent"] = 100 if run_status == "succeeded" else current["percent"]
            current["eta_seconds"] = None
            _state["progress"] = current
            _state["running"] = False
            _state["current"] = ""
            _state["phase"] = "finished"
            _state["risk_signal"] = risk_signal
            _state["paused"] = False
            _state["process"] = None
            _state["pause_started_at"] = None
            _state["paused_seconds"] = 0.0
            _state["worker_ident"] = None
            _state_condition.notify_all()


def resolve_collect_pace() -> dict:
    """读设置 collect_pace 返回档位定义；未知值回退稳妥档。"""
    default = config.DEFAULT_SETTINGS.get("collect_pace", "standard")
    pace_key = str(get_setting("collect_pace", default))
    return config.COLLECT_PACES.get(pace_key, config.COLLECT_PACES["standard"])


def task_gap_seconds() -> float:
    """列表任务间隔：显式覆盖优先（测试钩子），否则按当前档位。"""
    if ITEM_GAP_SEC is not None:
        return float(ITEM_GAP_SEC)
    return float(resolve_collect_pace()["task_gap_sec"])


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
    # 采集档位（M20）：均衡/快速档压缩引擎随机等待并开启重复内容早停；
    # 稳妥档 env 为空字典、不传早停 flag＝引擎原生节奏。favorites 的
    # JD 补齐复用本函数，自动跟随同一档位。
    pace = resolve_collect_pace()
    if pace.get("dup_stop_ratio"):
        command.extend(["--dup-stop-ratio", f'{pace["dup_stop_ratio"]}'])
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # Windows 控制台默认 GBK：强制 scraper 子进程按 UTF-8 读写流，与父进程读取端对齐。
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(pace["env"])
    watch_path = Path(detail_output or output) if on_snapshot else None
    if on_output is None and on_snapshot is None:
        proc = subprocess.run(command, cwd=str(SCRAPER_DIR), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
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
    # 引擎为保留部分列表，会捕获某些 RuntimeError 后以 0 退出；这类结果可导入，
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
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        bufsize=1, env=env)
    controlled = _register_process(proc)
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
    started_at = time.monotonic()
    with _state_lock:
        pause_baseline = _paused_duration_locked(started_at) if controlled else 0.0
    signature = initial_signature
    stream_finished = False
    try:
        while not stream_finished:
            # 子进程被挂起期间不消费 scraper 超时预算；取消只在当前子进程结束后生效。
            if controlled:
                _wait_for_resume()
            with _state_lock:
                now = time.monotonic()
                paused_delta = (_paused_duration_locked(now) - pause_baseline
                                if controlled else 0.0)
                timed_out = now >= started_at + timeout + paused_delta
            if timed_out:
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
        if controlled:
            _clear_process(proc)


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


def _wait_between_tasks(task_index: int = 0) -> bool:
    _eta_start_gap(task_index)
    started_at = time.monotonic()
    gap_sec = task_gap_seconds()
    with _state_lock:
        pause_baseline = _paused_duration_locked(started_at)
    completed = False
    try:
        while True:
            _wait_for_resume()
            if _is_cancelled():
                return False
            with _state_lock:
                now = time.monotonic()
                paused_delta = _paused_duration_locked(now) - pause_baseline
                remaining = started_at + gap_sec + paused_delta - now
            if remaining <= 0:
                completed = True
                return True
            time.sleep(min(0.5, remaining))
    finally:
        _eta_finish_gap(task_index, completed)


def _all_missing_jd(keys: set) -> set:
    eligible, complete = _job_completeness(keys)
    return eligible - complete


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
                      cdp_port: int, report: dict, job_keys=None,
                      all_job_keys=None) -> tuple[str, str]:
    all_keys = {str(key) for key in (job_keys or []) if key}
    if not all_keys:
        for path in files:
            data = _read_list(path)
            for raw in data.get("jobs", []):
                key = importer.job_key_from(
                    raw.get("job_link", "") or raw.get("link", ""),
                    raw.get("title", ""),
                    raw.get("boss_name", "") or raw.get("company", ""),
                    raw.get("salary", ""))
                all_keys.add(key)
    eligible, complete_before = _job_completeness(all_keys)
    missing = eligible - complete_before
    initial_complete = len(complete_before)
    display_eligible, display_complete = _job_completeness(
        all_job_keys if all_job_keys is not None else eligible)
    _set_progress(
        jobs_discovered=len(display_eligible), jobs_total=len(display_eligible),
        jd_total=len(display_eligible), jd_completed=len(display_complete),
        detail_total=len(missing), detail_completed=0)
    _eta_start_details()
    if not missing:
        for task_id in task_ids:
            sync.update_run_task(task_id, "succeeded", stats={"missing_jd": 0},
                                 finished=True)
        report["details"] = {"requested": 0, "updated": 0,
                             "jobs_total": len(display_eligible),
                             "jd_completed": len(display_complete)}
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
        _bump_data_version()
        _, complete_now = _job_completeness(eligible)
        _, display_complete_now = _job_completeness(display_eligible)
        current_completed = max(0, len(complete_now) - initial_complete)
        if current_completed > completed:
            completed = current_completed
            _eta_record_detail_progress(completed)
            _set_progress(detail_completed=completed,
                          jd_completed=len(display_complete_now))
            _log(f"JD 已入库 {completed}/{total}，岗位列表可立即查看")

    try:
        out = _run_scraper(["--input", str(merged), "--detail"],
                           timeout=max(900, total * 90 + 300), cdp_port=cdp_port,
                           output_path=merged, detail_output=detail,
                           on_output=stream_output, on_snapshot=import_snapshot)
        if out:
            _log(out)
        detail_stats = importer.import_scraper_details(str(detail))
        _, complete_now = _job_completeness(eligible)
        _, display_complete_now = _job_completeness(display_eligible)
        completed = max(0, len(complete_now) - initial_complete)
        _eta_record_detail_progress(completed)
        _set_progress(detail_completed=completed,
                      jd_completed=len(display_complete_now))
        for task_id in task_ids:
            sync.update_run_task(task_id, "succeeded",
                                 stats={"detail_updated": completed},
                                 detail_file=str(detail), finished=True)
        report["details"] = {"requested": total, **detail_stats,
                             "updated": completed,
                             "jobs_total": len(display_eligible),
                             "jd_completed": len(display_complete_now)}
        return source_at, ""
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        partial = importer.import_scraper_details(str(detail)) if detail.exists() else {
            "updated": 0}
        _, complete_now = _job_completeness(eligible)
        _, display_complete_now = _job_completeness(display_eligible)
        completed = max(0, len(complete_now) - initial_complete)
        _eta_record_detail_progress(completed)
        _set_progress(detail_completed=completed,
                      jd_completed=len(display_complete_now))
        failure = classify_failure(error)
        for task_id in task_ids:
            sync.update_run_task(task_id, "partial",
                                 stats={"detail_updated": completed},
                                 error=failure["message"], detail_file=str(detail),
                                 finished=True)
        report["details"] = {"requested": total, "partial": partial,
                             "updated": completed,
                             "jobs_total": len(display_eligible),
                             "jd_completed": len(display_complete_now),
                             "error": failure["message"]}
        _log(f"详情阶段失败，已保留部分结果: {failure['message']}")
        return source_at, failure["message"] if failure["risk"] else ""


def _worker(run_id: int, kind: str, tasks: list, sync_mode: bool,
            fetch_details: bool) -> None:
    report = {"items": [], "touched": 0, "delisted_jobs": 0,
              "partial": False, "cancelled": False}
    successful_files = []
    successful_task_ids = []
    discovered_keys = set()
    detail_job_keys = set()
    source_times = []
    risk_signal = ""
    account = ""
    try:
        with _state_condition:
            if _state.get("run_id") == run_id:
                _state["worker_ident"] = threading.get_ident()
        account = cdp.account_for("collect")
        account_label = cdp.config.ACCOUNTS[account]["label"]
        cdp_port = cdp.config.ACCOUNTS[account]["cdp_port"]
        launched = cdp.launch(account, headless=True)
        if launched.get("ok"):
            # 无头启动：采集全程不弹窗口；复用运行中实例时保持其原有模式
            _log(f"{account_label} Chrome: "
                 f"{'复用运行中实例' if launched.get('already_running') else '已启动'}"
                 + ("（无头）" if launched.get("headless") else ""))
        else:
            _log(f"{account_label} Chrome: {launched.get('error', '启动失败')}")
        if not launched.get("ok"):
            raise RuntimeError(f"{account_label} Chrome 无法启动（CDP 未就绪）")

        for index, task in enumerate(tasks):
            task_id = task["_task_id"]
            if not _wait_for_resume() or _is_cancelled():
                report["cancelled"] = True
                sync.update_run_task(task_id, "cancelled", finished=True)
                continue
            label = (task.get("name") or task.get("brand_id") or task.get("url")) \
                if task["type"] == "company" else f"{task['keyword']} / {task['city']}"
            _log(f"列表 {index + 1}/{len(tasks)}: {label}")
            sync.update_run_task(task_id, "running")
            list_file = _result_path(run_id, task_id, task["type"] == "company")
            live_list_count = 0
            task_succeeded = False
            _eta_start_list_task(index)

            def import_list_snapshot(path: Path) -> None:
                nonlocal live_list_count
                live_stats = importer.import_scraper_files([path], record_run=False)
                snapshot_keys = set(live_stats.get("job_keys") or [])
                discovered_keys.update(snapshot_keys)
                _attach_run_jobs(run_id, snapshot_keys, "collection_snapshot")
                _update_discovered_progress(discovered_keys)
                current_count = len(snapshot_keys)
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
                current_keys = set(stats.get("job_keys") or [])
                discovered_keys.update(current_keys)
                detail_job_keys.update(current_keys)
                _attach_run_jobs(run_id, current_keys, "collection")
                _update_discovered_progress(discovered_keys)
                _mark_account_success(account)
                relation = sync.record_source_success(
                    run_id, task_id, task, current_keys)
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
                task_succeeded = True
                _log(f"{label} 完成: 新增 {stats['created']} / 刷新 {stats['updated']} / "
                     f"排除跳过 {stats['excluded_skipped']}")
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                failure = classify_failure(error)
                partial_stats = _import_partial(list_file)
                partial_keys = set(partial_stats.get("job_keys") or [])
                discovered_keys.update(partial_keys)
                _attach_run_jobs(run_id, partial_keys, "collection_partial")
                _update_discovered_progress(discovered_keys)
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
                _eta_finish_list_task(index, usable=task_succeeded)
                _set_progress(list_completed=index + 1)

            if index < len(tasks) - 1 and not risk_signal and not _is_cancelled():
                _log(f"任务间隔等待 {task_gap_seconds():.0f}s（采集节奏：{resolve_collect_pace()['label']}）")
                if not _wait_between_tasks(index):
                    report["cancelled"] = True

        eligible_jobs, complete_jobs = _update_discovered_progress(discovered_keys)
        _wait_for_resume()
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
            if not _set_phase(run_id, "details"):
                report["cancelled"] = True
                for task_id in successful_task_ids:
                    sync.update_run_task(
                        task_id, "partial", error="采集已取消，未执行详情阶段",
                        finished=True)
            else:
                detail_source, detail_risk = _run_detail_phase(
                    run_id, successful_files, successful_task_ids, cdp_port, report,
                    job_keys=detail_job_keys, all_job_keys=discovered_keys)
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

        if not _wait_for_resume():
            report["cancelled"] = True
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
    except Exception as error:
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
    finally:
        _cleanup_worker_control(run_id)


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
        _state.update({"running": True, "current": "启动中", "phase": "starting",
                       "run_id": None, "cancel": False, "paused": False,
                       "process": None, "pause_started_at": None,
                       "paused_seconds": 0.0, "worker_ident": None,
                       "fetch_details": bool(fetch_details),
                       "phase_started_at": None, "phase_pause_baseline": 0.0,
                       "eta_model": None, "progress": _new_progress()})
    try:
        run_id = _begin_run(kind, {"tasks": normalized, "sync": sync_mode,
                                   "fetch_details": fetch_details,
                                   "resume_id": int(resume_id or 0)}, normalized)
    except Exception:
        with _state_condition:
            _state.update({"running": False, "current": "", "phase": "",
                           "run_id": None, "paused": False, "process": None,
                           "pause_started_at": None, "paused_seconds": 0.0,
                           "worker_ident": None, "phase_started_at": None,
                           "phase_pause_baseline": 0.0, "eta_model": None})
            _state_condition.notify_all()
        raise
    thread = threading.Thread(
        target=_worker, args=(run_id, kind, normalized, sync_mode, fetch_details),
        daemon=True)
    try:
        thread.start()
    except Exception as error:
        _finish_run(run_id, {"error": f"后台线程启动失败: {error}"}, "failed")
        raise
    result = {"ok": True, "run_id": run_id}
    name = _saved_run_name(run_id)
    if name:
        result["name"] = name
    return result


def start_config(body: dict, resume_id=None, sync_mode: bool = False) -> dict:
    """按模块化配置展开全局关键词×城市任务。"""
    try:
        cfg = normalize_collect_config(body, resume_id)
        tasks = build_tasks(cfg, resume_id)
    except (TypeError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    return start("config", tasks, sync_mode=sync_mode,
                 fetch_details=cfg["fetch_details"], resume_id=cfg["resume_id"])


def _finish_stale_paused_run() -> None:
    """内存已无运行任务时，把遗留“暂停中”的采集记录标记为中断。

    进程重启由启动钩子兜底；这里覆盖工作线程异常退出等造成的状态残留，
    避免界面永久停在“已暂停”且继续/取消都不产生任何效果。
    """
    row = get_db().execute(
        "SELECT id FROM collect_runs WHERE paused=1 AND finished_at IS NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return
    get_db().execute(
        "UPDATE collect_runs SET paused=0,status='interrupted',phase='finished',"
        "finished_at=? WHERE id=?", (now_iso(), row["id"]))
    get_db().commit()
    _log(f"采集记录 #{row['id']} 残留“暂停中”状态，已标记为中断")


def cancel() -> dict:
    with _state_condition:
        if not _state["running"]:
            _finish_stale_paused_run()
            return {"ok": True, "running": False}
        if not _state.get("run_id"):
            return {"ok": False, "running": True, "paused": False,
                    "error": "采集正在启动，请稍后再取消"}
        was_paused = bool(_state.get("paused"))
        if was_paused:
            now = time.monotonic()
            started_at = _state.get("pause_started_at")
            if started_at is not None:
                _state["paused_seconds"] = float(
                    _state.get("paused_seconds") or 0.0) + now - started_at
            _signal_process(_state.get("process"), _SIG_RESUME)
            _state["paused"] = False
            _state["pause_started_at"] = None
        _state["cancel"] = True
        run_id = _state["run_id"]
        get_db().execute(
            "UPDATE collect_runs SET cancel_requested=1,paused=0 WHERE id=?", (run_id,))
        get_db().commit()
        _state_condition.notify_all()
    message = ("已恢复暂停中的子进程并请求取消；当前子进程结束后停止"
               if was_paused else "已请求取消；当前子进程结束后停止")
    _log(message)
    return {"ok": True, "running": True, "paused": False, "run_id": run_id}


def pause() -> dict:
    """暂停当前采集，并真实挂起正在运行的 scraper 子进程（Windows 同样生效）。"""
    with _state_condition:
        if not _state["running"]:
            return {"ok": True, "running": False, "paused": False}
        run_id = _state["run_id"]
        if not run_id:
            return {"ok": False, "running": True, "paused": False,
                    "error": "采集正在启动，请稍后再暂停"}
        if _state.get("cancel"):
            return {"ok": False, "running": True, "paused": False,
                    "cancel_requested": True, "run_id": run_id,
                    "error": "采集已请求取消，不能再暂停"}
        if not _state.get("paused"):
            _state["paused"] = True
            _state["pause_started_at"] = time.monotonic()
            _signal_process(_state.get("process"), _SIG_PAUSE)
            get_db().execute("UPDATE collect_runs SET paused=1 WHERE id=?", (run_id,))
            get_db().commit()
            changed = True
        else:
            changed = False
    if changed:
        _log("采集已暂停")
    return {"ok": True, "running": True, "paused": True, "run_id": run_id}


def resume() -> dict:
    """继续当前采集，并恢复被挂起的 scraper 子进程（Windows 同样生效）。"""
    with _state_condition:
        if not _state["running"]:
            _finish_stale_paused_run()
            return {"ok": True, "running": False, "paused": False}
        run_id = _state["run_id"]
        if not run_id:
            return {"ok": False, "running": True, "paused": False,
                    "error": "采集正在启动，请稍后再继续"}
        if _state.get("paused"):
            now = time.monotonic()
            started_at = _state.get("pause_started_at")
            if started_at is not None:
                _state["paused_seconds"] = float(
                    _state.get("paused_seconds") or 0.0) + now - started_at
            _signal_process(_state.get("process"), _SIG_RESUME)
            _state["paused"] = False
            _state["pause_started_at"] = None
            get_db().execute("UPDATE collect_runs SET paused=0 WHERE id=?", (run_id,))
            get_db().commit()
            _state_condition.notify_all()
            changed = True
        else:
            changed = False
    if changed:
        _log("采集已继续")
    return {"ok": True, "running": True, "paused": False, "run_id": run_id}


def _resume_worker(run_id: int, task_ids: list, files: list) -> None:
    """在原采集计划上继续详情阶段；结束时仍写回该计划自己的状态。"""
    report = {"resume": True}
    account = ""
    try:
        with _state_condition:
            if _state.get("run_id") == run_id:
                _state["worker_ident"] = threading.get_ident()
        account = cdp.account_for("collect")
        port = cdp.config.ACCOUNTS[account]["cdp_port"]
        launched = cdp.launch(account, headless=True)
        if not launched.get("ok"):
            raise RuntimeError("采集 Chrome 无法启动（CDP 未就绪）")
        if not _set_phase(run_id, "details"):
            report["cancelled"] = True
            _finish_run(run_id, report, "cancelled")
            return
        _, risk = _run_detail_phase(run_id, files, task_ids, port, report)
        if _is_cancelled():
            report["cancelled"] = True
            gained = int((report.get("details") or {}).get("updated") or 0)
            _finish_run(run_id, report, "partial" if gained else "cancelled", "", risk)
            return
        status_value = "partial" if report.get("details", {}).get("error") else "succeeded"
        _finish_run(run_id, report, status_value, "", risk)
    except Exception as error:
        failure = classify_failure(error)
        if account:
            _mark_account_failure(account, failure)
        report["error"] = failure["message"]
        _finish_run(run_id, report, "failed", "",
                    failure["message"] if failure["risk"] else "")
    finally:
        _cleanup_worker_control(run_id)


def _prepare_resume_plan(source_run_id: int = None) -> dict:
    """校验并准备“继续采集”：进度快照、任务与列表文件都取自原计划。"""
    if source_run_id is None:
        row = get_db().execute(
            "SELECT t.run_id FROM collect_run_tasks t JOIN collect_runs r "
            "ON r.id=t.run_id WHERE t.list_file<>'' AND r.kind<>'detail_retry' "
            "ORDER BY t.run_id DESC LIMIT 1").fetchone()
        source_run_id = int(row["run_id"]) if row else 0
    source_run_id = int(source_run_id or 0)
    run = get_db().execute(
        "SELECT id,kind,status,stats,params,paused,finished_at FROM collect_runs "
        "WHERE id=?", (source_run_id,)).fetchone()
    if run is None:
        raise ValueError("没有可继续的采集计划")
    if run["finished_at"] is None or str(run["status"] or "") in ("running", "paused"):
        raise ValueError("该采集计划仍在运行或尚未结束，不能重复继续")
    tasks = [dict(row) for row in get_db().execute(
        "SELECT id,status,list_file FROM collect_run_tasks "
        "WHERE run_id=? ORDER BY id", (source_run_id,)).fetchall()]
    files = [Path(task["list_file"]) for task in tasks
             if task["list_file"] and Path(task["list_file"]).exists()]
    if not files:
        raise ValueError("该采集计划没有可用的列表文件，无法继续")
    eligible, complete = _job_completeness(_run_job_keys(source_run_id))
    if not eligible - complete:
        raise ValueError("该采集计划没有缺失的职位描述，无需继续")
    progress, _ = _finished_progress(run, tasks, source_run_id)
    try:
        params = json.loads(run["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        params = {}
    label = str(params.get("name") or f"{run['kind']} #{source_run_id}")
    return {"run_id": source_run_id, "task_ids": [task["id"] for task in tasks],
            "files": files, "progress": progress,
            "missing": len(eligible - complete), "label": label}


def resume_missing(source_run_id: int = None) -> dict:
    """继续一条已结束的采集计划：不新建记录，在原有进度上只补齐缺失的 JD。"""
    if not SCRAPER_PY.exists() or not SCRAPER_SCRIPT.exists():
        return {"ok": False, "error": f"未找到 scraper: {SCRAPER_PY}"}
    with _state_lock:
        if _state["running"]:
            return {"ok": False, "error": "已有采集任务在运行",
                    "status": _state_snapshot()}
    try:
        plan = _prepare_resume_plan(source_run_id)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    run_id = plan["run_id"]
    with _state_condition:
        _state.update({"running": True, "current": plan["label"], "phase": "details",
                       "run_id": run_id, "cancel": False, "risk_signal": "",
                       "paused": False, "process": None, "pause_started_at": None,
                       "paused_seconds": 0.0, "worker_ident": None,
                       "fetch_details": True, "log": [],
                       "phase_started_at": time.monotonic(),
                       "phase_pause_baseline": 0.0,
                       "eta_model": _new_eta_model([]),
                       "progress": plan["progress"]})
        _state_condition.notify_all()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE collect_runs SET status='running',phase='details',finished_at=NULL,"
            "paused=0,cancel_requested=0 WHERE id=?", (run_id,))
        conn.commit()
        thread = threading.Thread(target=_resume_worker,
                                  args=(run_id, plan["task_ids"], plan["files"]),
                                  daemon=True)
        thread.start()
    except Exception:
        # 回滚内存占位与记录状态，避免启动位被一次失败的继续动作占用。
        with _state_condition:
            _state.update({"running": False, "current": "", "phase": "finished",
                           "run_id": None, "paused": False, "process": None,
                           "pause_started_at": None, "paused_seconds": 0.0,
                           "worker_ident": None, "phase_started_at": None,
                           "phase_pause_baseline": 0.0, "eta_model": None})
            _state_condition.notify_all()
        conn.execute(
            "UPDATE collect_runs SET status='interrupted',phase='finished',"
            "finished_at=?,paused=0 WHERE id=? AND status='running'",
            (now_iso(), run_id))
        conn.commit()
        raise
    return {"ok": True, "run_id": run_id, "missing": plan["missing"]}


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
