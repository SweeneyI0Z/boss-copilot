"""同步刷新：按采集计划重跑 → diff 下架 → HR 活跃度剔除 → 简历变更重评报告。

下架判定原则：只有「同一搜索词本次重跑未再出现」的岗位才判 delisted
（不同关键词的结果集天然不同，不能跨词判缺失）。
"""
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
