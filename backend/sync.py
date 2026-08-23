"""同步刷新：按采集计划重跑 → diff 下架 → HR 活跃度剔除 → 简历变更重评报告。

下架判定原则：只有「同一搜索词本次重跑未再出现」的岗位才判 delisted
（不同关键词的结果集天然不同，不能跨词判缺失）。
"""
import hashlib
import json
from datetime import datetime

from . import importer
from .db import get_db, now_iso

# HR 活跃度字符串 → 是否剔除（BOSS 档位：刚刚/在线/今日/3日内/本周/半月/月前/半年前…）
_HR_ACTIVE_OK = ("刚刚", "在线", "今日", "3日内", "本周", "2周内", "半月")
_HR_STALE_PAT = "月前"


def hr_is_stale(hr_active: str) -> bool:
    """月前/半年前/一年前等档位视为不活跃；空值不判。"""
    text = hr_active or ""
    if not text:
        return False
    if _HR_STALE_PAT in text or "年" in text:
        return True
    return not any(k in text for k in _HR_ACTIVE_OK) and text not in ("", "未知")


def apply_hr_inactive() -> int:
    """把 HR 不活跃的 active 岗位标记为 hr_inactive（保留数据，仅出推荐池）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT job_key, hr_active FROM jobs WHERE status='active'").fetchall()
    n = 0
    for r in rows:
        if hr_is_stale(r["hr_active"]):
            conn.execute("UPDATE jobs SET status='hr_inactive' WHERE job_key=?",
                         (r["job_key"],))
            n += 1
    conn.commit()
    return n


def diff_delisted(query: str, fresh_keys: set) -> list:
    """同一 origin_query 下，上次在、本次不在的 active 岗位 → delisted。返回列表。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT job_key FROM jobs WHERE status IN ('active','hr_inactive') "
        "AND origin_query=?", (query,)).fetchall()
    gone = [r["job_key"] for r in rows if r["job_key"] not in fresh_keys]
    for k in gone:
        conn.execute("UPDATE jobs SET status='delisted' WHERE job_key=?", (k,))
    conn.commit()
    return gone


def tag_origin_query(keys: list, query: str) -> None:
    conn = get_db()
    conn.executemany(
        "UPDATE jobs SET origin_query=? WHERE job_key=? AND "
        "(origin_query IS NULL OR origin_query='')",
        [(query, k) for k in keys])
    conn.commit()


def source_key(task: dict) -> str:
    """生成跨运行稳定的来源键；筛选变化应视为不同来源。"""
    kind = str(task.get("type") or task.get("kind") or "search")
    if kind == "company":
        identity = str(task.get("brand_id") or task.get("url") or "").strip()
        raw = f"company|{identity}"
    else:
        filters = json.dumps(task.get("filters") or {}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        raw = "|".join(("search", str(task.get("keyword") or "").strip(),
                        str(task.get("city_code") or task.get("city") or "").strip(),
                        filters))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{kind}:{digest}"


def record_source_success(run_id: int, task_id: int, task: dict,
                          fresh_keys: set) -> dict:
    """提交一次成功列表的来源快照，并仅在全部来源失效时下架岗位。"""
    conn = get_db()
    search_key = task.get("search_key") or source_key(task)
    rows = conn.execute(
        "SELECT DISTINCT job_key FROM job_collection_hits "
        "WHERE search_key=? AND is_active=1", (search_key,)).fetchall()
    previous = {row["job_key"] for row in rows}
    current_pages = max(1, int(task.get("pages", 5 if task.get("type") == "company" else 3)))
    coverage_key = f"source_page_coverage:{search_key}"
    coverage_row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (coverage_key,)).fetchone()
    try:
        coverage_pages = int(json.loads(coverage_row["value"])) if coverage_row else None
    except (TypeError, ValueError, json.JSONDecodeError):
        coverage_pages = None
    if coverage_pages is None:
        # 升级前来源没有页深元数据，按最大安全深度处理，避免首次浅采集误删。
        coverage_pages = (30 if task.get("type") == "company" else 10) \
            if previous else current_pages
    allow_missing_diff = current_pages >= coverage_pages
    if fresh_keys:
        placeholders = ",".join("?" for _ in fresh_keys)
        eligible = conn.execute(
            f"SELECT job_key FROM jobs WHERE job_key IN ({placeholders}) "
            "AND status<>'excluded'", list(fresh_keys)).fetchall()
        fresh = {row["job_key"] for row in eligible}
    else:
        fresh = set()

    ts = now_iso()
    if allow_missing_diff:
        conn.execute(
            "UPDATE job_collection_hits SET is_active=0 WHERE search_key=? AND is_active=1",
            (search_key,))
    elif fresh:
        placeholders = ",".join("?" for _ in fresh)
        conn.execute(
            f"UPDATE job_collection_hits SET is_active=0 WHERE search_key=? "
            f"AND is_active=1 AND job_key IN ({placeholders})",
            [search_key, *sorted(fresh)])
    filters = json.dumps(task.get("filters") or {}, ensure_ascii=False, sort_keys=True)
    for job_key in sorted(fresh):
        first = conn.execute(
            "SELECT MIN(first_seen_at) first_seen FROM job_collection_hits "
            "WHERE search_key=? AND job_key=?", (search_key, job_key)).fetchone()
        first_seen = first["first_seen"] if first and first["first_seen"] else ts
        conn.execute(
            "INSERT INTO job_collection_hits("
            "task_id,run_id,job_key,search_key,keyword,province,city,city_code,filters,"
            "is_active,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(search_key,job_key,run_id) DO UPDATE SET "
            "task_id=excluded.task_id,is_active=1,last_seen_at=excluded.last_seen_at",
            (task_id, run_id, job_key, search_key, task.get("keyword", ""),
             task.get("province", ""), task.get("city", ""),
             task.get("city_code", ""), filters, 1, first_seen, ts))
        conn.execute(
            "UPDATE jobs SET status='active' WHERE job_key=? AND status='delisted'",
            (job_key,))

    missing = previous - fresh if allow_missing_diff else set()
    delisted = []
    for job_key in sorted(missing):
        still_seen = conn.execute(
            "SELECT 1 FROM job_collection_hits WHERE job_key=? AND is_active=1 LIMIT 1",
            (job_key,)).fetchone()
        if still_seen is None:
            cur = conn.execute(
                "UPDATE jobs SET status='delisted' WHERE job_key=? "
                "AND status IN ('active','hr_inactive')", (job_key,))
            if cur.rowcount:
                delisted.append(job_key)
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (coverage_key, json.dumps(max(coverage_pages, current_pages))))
    conn.commit()
    return {"search_key": search_key, "fresh": len(fresh),
            "deactivated": len(missing), "delisted": delisted,
            "coverage_pages": max(coverage_pages, current_pages),
            "missing_diff_applied": allow_missing_diff}


def update_run_task(task_id: int, status: str, *, stats: dict = None,
                    error: str = "", list_file: str = None,
                    detail_file: str = None, finished: bool = False) -> None:
    """集中更新采集子任务，避免各阶段覆盖已累计的统计。"""
    conn = get_db()
    row = conn.execute(
        "SELECT stats, started_at FROM collect_run_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return
    try:
        merged = json.loads(row["stats"] or "{}")
    except (json.JSONDecodeError, TypeError):
        merged = {}
    if stats:
        merged.update(stats)
    sets = ["status=?", "stats=?", "error=?"]
    args = [status, json.dumps(merged, ensure_ascii=False), error[:500]]
    if not row["started_at"] and status not in ("queued", "cancelled"):
        sets.append("started_at=?")
        args.append(now_iso())
    if list_file is not None:
        sets.append("list_file=?")
        args.append(list_file)
    if detail_file is not None:
        sets.append("detail_file=?")
        args.append(detail_file)
    if finished:
        sets.append("finished_at=?")
        args.append(now_iso())
    args.append(task_id)
    conn.execute(f"UPDATE collect_run_tasks SET {', '.join(sets)} WHERE id=?", args)
    conn.commit()


def jobs_from_run(keys: list) -> set:
    return set(keys)


def rescore_report() -> dict:
    """简历/L1 重算后，报告 P 级发生变化的岗位（L2 是否重评由人决定）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT job_key, title, company, composite, priority, composite_rough "
        "FROM jobs WHERE composite IS NOT NULL").fetchall()
    changed = []
    for r in rows:
        rough_p = ("P0" if r["composite_rough"] >= 75 else
                   "P1" if r["composite_rough"] >= 65 else
                   "P2" if r["composite_rough"] >= 55 else "P3") \
            if r["composite_rough"] is not None else None
        if rough_p and r["priority"] and rough_p != r["priority"]:
            changed.append({"job_key": r["job_key"], "title": r["title"],
                            "company": r["company"], "l2_priority": r["priority"],
                            "l1_priority_now": rough_p,
                            "composite": r["composite"],
                            "composite_rough": r["composite_rough"]})
    return {"changed": changed, "total_scored": len(rows)}


def sync_report(run_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM collect_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return {}
    return {"id": row["id"], "kind": row["kind"],
            "stats": json.loads(row["stats"] or "{}"),
            "started_at": row["started_at"], "finished_at": row["finished_at"]}
