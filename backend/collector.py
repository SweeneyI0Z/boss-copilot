"""在线采集执行器：调 boss-zhipin-scraper，后台线程串行执行。

执行链：确保采集 Chrome 启动 → subprocess 跑 scraper → 导入结果 →
打 origin_query 标签 →（同步模式）同词缺失判下架 + HR 活跃度剔除。
"""
import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import importer, sync
from .boss import cdp
from .db import get_db, now_iso

SCRAPER_DIR = Path("~/project/boss-zhipin-scraper")
SCRAPER_PY = SCRAPER_DIR / ".venv" / "bin" / "python"
SCRAPER_SCRIPT = SCRAPER_DIR / "scripts" / "boss_cdp_raw.py"
RESULT_DIR = Path.home() / ".boss-zhipin-scraper" / "job-result"
ITEM_GAP_SEC = 120          # 计划内相邻任务间隔（COLLECT.md 多关键词惯例）

_state_lock = threading.Lock()
_state = {"running": False, "current": "", "log": [], "run_id": None}


def status() -> dict:
    with _state_lock:
        return {"running": _state["running"], "current": _state["current"],
                "run_id": _state["run_id"],
                "log": list(_state["log"][-30:])}


def _log(msg: str) -> None:
    with _state_lock:
        _state["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")


def _begin_run(kind: str, params: dict) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO collect_runs(kind, params, stats, started_at) VALUES(?,?,?,?)",
        (kind, json.dumps(params, ensure_ascii=False), "{}", now_iso()))
    conn.commit()
    with _state_lock:
        _state.update({"running": True, "current": kind, "log": [],
                       "run_id": cur.lastrowid})
    return cur.lastrowid


def _finish_run(run_id: int, stats: dict) -> None:
    conn = get_db()
    conn.execute("UPDATE collect_runs SET stats=?, finished_at=? WHERE id=?",
                 (json.dumps(stats, ensure_ascii=False), now_iso(), run_id))
    conn.commit()
    with _state_lock:
        _state["running"] = False
        _state["current"] = ""


def _run_scraper(args: list, timeout: int, cdp_port: int) -> str:
    """跑 scraper 子进程，返回 stdout 摘要（用于日志）。"""
    proc = subprocess.run(
        [str(SCRAPER_PY), str(SCRAPER_SCRIPT)] + args
        + ["--cdp-port", str(cdp_port)],
        cwd=str(SCRAPER_DIR), capture_output=True, text=True,
        timeout=timeout, env={"PYTHONUNBUFFERED": "1",
                              "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    tail = (proc.stdout or "").strip().splitlines()[-6:]
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("scraper 退出码 %d: %s" % (proc.returncode, " / ".join(err + tail)))
    return " / ".join(tail[-3:])


def _touched_keys(since_iso: str) -> set:
    conn = get_db()
    rows = conn.execute(
        "SELECT job_key FROM jobs WHERE last_seen_at >= ?", (since_iso,)).fetchall()
    return {r["job_key"] for r in rows}


def _execute_one(search: dict, sync_mode: bool, since_iso: str,
                 report: dict, cdp_port: int) -> None:
    kw = search["keyword"]
    city = search.get("city", "深圳")
    pages = int(search.get("pages", 3))
    _log(f"采集: 「{kw}」 {city} {pages}页 …")
    args = ["--keyword", kw, "--city", city, "--pages", str(pages), "--no-detail"]
    out = _run_scraper(args, timeout=pages * 150 + 300, cdp_port=cdp_port)
    _log(out)
    since = since_iso
    stats = importer.import_scraper_json(str(RESULT_DIR))
    touched = _touched_keys(since)
    sync.tag_origin_query(sorted(touched), f"kw:{kw}")
    item = {"keyword": kw, "imported": stats, "touched": len(touched)}
    if sync_mode:
        gone = sync.diff_delisted(f"kw:{kw}", touched)
        item["delisted"] = len(gone)
        report["delisted_jobs"] += len(gone)
    report["items"].append(item)
    report["touched"] += len(touched)
    _log(f"「{kw}」完成: 导入 {stats['created']} 新 / 刷新 {stats['updated']}, 涉及 {len(touched)} 岗")


def _execute_company(c: dict, report: dict, cdp_port: int) -> None:
    name = c.get("name") or c.get("url") or ""
    _log(f"公司定向采集: {name} …（需要 brandId 或公司页 URL，跳过仅有名字的项）")
    bid = c.get("brand_id") or c.get("url")
    if not bid:
        report["items"].append({"company": name, "skipped": "缺少 url/brand_id"})
        return
    pages = int(c.get("pages", 5))
    out = _run_scraper(["--company", str(bid), "--pages", str(pages), "--no-detail"],
                       timeout=pages * 150 + 300, cdp_port=cdp_port)
    _log(out)
    since = now_iso()
    stats = importer.import_scraper_json(str(RESULT_DIR))
    touched = _touched_keys(since)
    sync.tag_origin_query(sorted(touched), f"co:{name}")
    report["items"].append({"company": name, "imported": stats})
    _log(f"{name} 完成: 导入 {stats['created']} 新 / 刷新 {stats['updated']}")


def _worker(kind: str, tasks: list, sync_mode: bool) -> None:
    run_id = _begin_run(kind, {"tasks": tasks, "sync": sync_mode})
    report = {"items": [], "touched": 0, "delisted_jobs": 0}
    since_iso = now_iso()
    try:
        account = cdp.account_for("collect")
        account_label = cdp.config.ACCOUNTS[account]["label"]
        cdp_port = cdp.config.ACCOUNTS[account]["cdp_port"]
        st = cdp.launch(account)
        _log(f"{account_label} Chrome: {'已启动' if st.get('ok') else st.get('error', '启动失败')}")
        if not st.get("ok"):
            raise RuntimeError(f"{account_label} Chrome 无法启动（CDP 未就绪）")
        for i, t in enumerate(tasks):
            if _state.get("cancel"):
                _log("已取消")
                break
            if t.get("type") == "company":
                _execute_company(t, report, cdp_port)
            else:
                _execute_one(t, sync_mode, since_iso, report, cdp_port)
            if i < len(tasks) - 1:
                _log(f"任务间隔等待 {ITEM_GAP_SEC}s …")
                time.sleep(ITEM_GAP_SEC)
        if sync_mode:
            n = sync.apply_hr_inactive()
            report["hr_inactive"] = n
            _log(f"HR 活跃度剔除: {n} 个岗位标记 hr_inactive")
        _log("全部完成")
        _finish_run(run_id, report)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        _log(f"❌ 失败: {e}")
        report["error"] = str(e)[:300]
        _finish_run(run_id, report)


def start(kind: str, tasks: list, sync_mode: bool = False) -> dict:
    """启动后台采集线程；已有任务在跑则拒绝。"""
    with _state_lock:
        if _state["running"]:
            return {"ok": False, "error": "已有采集任务在运行", "status": status()}
        _state["cancel"] = False
    if not SCRAPER_PY.exists():
        return {"ok": False, "error": f"未找到 scraper: {SCRAPER_PY}"}
    th = threading.Thread(target=_worker, args=(kind, tasks, sync_mode), daemon=True)
    th.start()
    return {"ok": True}


def cancel() -> dict:
    with _state_lock:
        _state["cancel"] = True
    return {"ok": True}


def plan_tasks() -> list:
    """把已保存的采集计划转成执行任务列表。"""
    from .strategy import get_plan
    plan = get_plan()
    tasks = [{"type": "search", **s} for s in plan.get("searches", [])]
    tasks += [{"type": "company", **c} for c in plan.get("companies", [])
              if c.get("url") or c.get("brand_id")]
    return tasks
