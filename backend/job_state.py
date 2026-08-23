"""岗位收藏、排除恢复与猎头标记。"""
import re

from .db import get_db, now_iso


_HEADHUNTER_COMPANY_RE = re.compile(r"^某.+公司$")
_VALID_JOB_STATUSES = {"active", "delisted", "hr_inactive", "excluded"}


def _job(job_key: str):
    row = get_db().execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
    if row is None:
        raise ValueError("岗位不存在")
    return row


def _job_dict(row) -> dict:
    out = dict(row)
    auto = bool(out.get("is_headhunter"))
    override = out.get("headhunter_override")
    out["is_headhunter"] = auto
    out["effective_headhunter"] = auto if override is None else bool(override)
    out["favorite"] = bool(out.get("favorite_at"))
    return out


def detect_headhunter(company: str, hr_title: str = "", native_flag=None) -> tuple[bool, str]:
    """按原生字段、HR 职位、匿名公司名的优先级判断猎头岗位。"""
    if native_flag is not None:
        return bool(native_flag), "平台原生标志" if native_flag else "平台原生标志：否"
    if "猎头" in (hr_title or ""):
        return True, "招聘者职位包含“猎头”"
    if _HEADHUNTER_COMPANY_RE.fullmatch((company or "").strip()):
        return True, "公司名称为匿名“某…公司”"
    return False, ""


def update_headhunter_detection(job_key: str, native_flag=None,
                                hr_title: str = None, company: str = None) -> dict:
    row = _job(job_key)
    title = row["hr_title"] if hr_title is None else hr_title
    company_name = row["company"] if company is None else company
    detected, reason = detect_headhunter(company_name, title, native_flag)
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET hr_title=?, is_headhunter=?, headhunter_reason=? WHERE job_key=?",
        (title or "", int(detected), reason, job_key))
    conn.commit()
    return _job_dict(_job(job_key))


def set_headhunter_override(job_key: str, value=None) -> dict:
    """value 为 True/False 时人工覆盖；None 恢复自动判断。"""
    _job(job_key)
    if value not in (None, True, False, 0, 1):
        raise ValueError("猎头人工标记必须是 true、false 或 null")
    stored = None if value is None else int(bool(value))
    conn = get_db()
    conn.execute("UPDATE jobs SET headhunter_override=? WHERE job_key=?",
                 (stored, job_key))
    conn.commit()
    return _job_dict(_job(job_key))


def favorite_job(job_key: str, favorite: bool = True) -> dict:
    row = _job(job_key)
    if favorite and row["status"] == "excluded":
        raise ValueError("已排除岗位不能收藏，请先恢复岗位")
    conn = get_db()
    conn.execute("UPDATE jobs SET favorite_at=? WHERE job_key=?",
                 (now_iso() if favorite else None, job_key))
    conn.commit()
    return _job_dict(_job(job_key))


def exclude_job(job_key: str) -> dict:
    row = _job(job_key)
    if row["status"] == "excluded":
        return _job_dict(row)
    previous = row["status"] if row["status"] in _VALID_JOB_STATUSES else "active"
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET status='excluded', status_before_excluded=?, excluded_at=?, "
        "favorite_at=NULL WHERE job_key=?", (previous, ts, job_key))
    conn.commit()
    return _job_dict(_job(job_key))


def restore_job(job_key: str) -> dict:
    row = _job(job_key)
    if row["status"] != "excluded":
        return _job_dict(row)
    previous = row["status_before_excluded"]
    if previous not in _VALID_JOB_STATUSES or previous == "excluded":
        previous = "active"
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET status=?, status_before_excluded='', excluded_at=NULL "
        "WHERE job_key=?", (previous, job_key))
    conn.commit()
    return _job_dict(_job(job_key))


def list_excluded(limit: int = 100, offset: int = 0) -> dict:
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='excluded'").fetchone()["c"]
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status='excluded' "
        "ORDER BY excluded_at DESC, job_key LIMIT ? OFFSET ?",
        (max(1, min(int(limit), 500)), max(0, int(offset)))).fetchall()
    return {"total": total, "items": [_job_dict(row) for row in rows]}
