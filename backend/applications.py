"""岗位投递证据记录。

只记录 BOSS 页面明确可见的确认结果或用户人工确认；探测失败永远不会猜测已投递。
"""
from .db import get_db, now_iso
from .resumes import get_default_resume, get_resume


VALID_STATUSES = {"unknown", "platform_confirmed", "manual_confirmed"}


def _resolve_resume(resume_id: int = None) -> dict:
    return get_default_resume() if resume_id is None else get_resume(int(resume_id))


def _require_job(job_key: str) -> None:
    if get_db().execute("SELECT 1 FROM jobs WHERE job_key=?", (job_key,)).fetchone() is None:
        raise ValueError("岗位不存在")


def _application_dict(row):
    return dict(row) if row else None


def get_application(job_key: str, resume_id: int = None):
    resume = _resolve_resume(resume_id)
    row = get_db().execute(
        "SELECT * FROM applications WHERE job_key=? AND resume_id=?",
        (job_key, resume["id"])).fetchone()
    if row:
        return _application_dict(row)
    return {
        "id": None, "job_key": job_key, "resume_id": resume["id"],
        "status": "unknown", "probe_evidence": "", "probe_error": "",
        "last_probed_at": None, "confirmed_at": None,
        "created_at": None, "updated_at": None,
    }


def _ensure_application(job_key: str, resume_id: int) -> None:
    ts = now_iso()
    get_db().execute(
        "INSERT OR IGNORE INTO applications(job_key, resume_id, status, created_at, "
        "updated_at) VALUES(?,?,'unknown',?,?)", (job_key, resume_id, ts, ts))


def record_probe_result(job_key: str, resume_id: int = None,
                        platform_confirmed=None, evidence: str = "",
                        error: str = "") -> dict:
    """保存一次按需探测；未确认或失败不会降级已有的确认状态。"""
    _require_job(job_key)
    resume = _resolve_resume(resume_id)
    conn = get_db()
    _ensure_application(job_key, resume["id"])
    current = conn.execute(
        "SELECT status FROM applications WHERE job_key=? AND resume_id=?",
        (job_key, resume["id"])).fetchone()["status"]
    status = "platform_confirmed" if platform_confirmed is True else current
    if status not in VALID_STATUSES:
        status = "unknown"
    ts = now_iso()
    confirmed_at = ts if status == "platform_confirmed" else None
    conn.execute(
        "UPDATE applications SET status=?, probe_evidence=?, probe_error=?, "
        "last_probed_at=?, confirmed_at=COALESCE(?, confirmed_at), updated_at=? "
        "WHERE job_key=? AND resume_id=?",
        (status, (evidence or "")[:1000], (error or "")[:300], ts,
         confirmed_at, ts, job_key, resume["id"]))
    conn.commit()
    return get_application(job_key, resume["id"])


def confirm_application(job_key: str, resume_id: int = None) -> dict:
    """用户在 BOSS 原平台完成操作后人工确认投递。"""
    return set_application_status(job_key, resume_id, "manual_confirmed")


def set_application_status(job_key: str, resume_id: int = None,
                           status: str = "unknown") -> dict:
    if status not in VALID_STATUSES:
        raise ValueError("投递状态无效")
    _require_job(job_key)
    resume = _resolve_resume(resume_id)
    conn = get_db()
    _ensure_application(job_key, resume["id"])
    ts = now_iso()
    conn.execute(
        "UPDATE applications SET status=?, confirmed_at=?, updated_at=? "
        "WHERE job_key=? AND resume_id=?",
        (status, ts if status != "unknown" else None, ts, job_key, resume["id"]))
    conn.commit()
    return get_application(job_key, resume["id"])


def list_applications(status: str = "", resume_id: int = None,
                      limit: int = 100, offset: int = 0) -> dict:
    where = ["1=1"]
    args = []
    if status:
        if status not in VALID_STATUSES:
            raise ValueError("投递状态无效")
        where.append("a.status=?")
        args.append(status)
    if resume_id is not None:
        resume = _resolve_resume(resume_id)
        where.append("a.resume_id=?")
        args.append(resume["id"])
    cond = " AND ".join(where)
    conn = get_db()
    total = conn.execute(
        f"SELECT COUNT(*) c FROM applications a WHERE {cond}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT a.*, j.title, j.company, r.name resume_name FROM applications a "
        f"JOIN jobs j ON j.job_key=a.job_key JOIN resumes r ON r.id=a.resume_id "
        f"WHERE {cond} ORDER BY a.updated_at DESC LIMIT ? OFFSET ?",
        [*args, max(1, min(int(limit), 500)), max(0, int(offset))]).fetchall()
    return {"total": total, "items": [dict(row) for row in rows]}
