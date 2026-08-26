"""岗位求职阶段：统一汇总人工标记与已有招呼、投递、面试事实。"""
from .db import get_db, now_iso
from .resumes import get_default_resume, get_resume


VALID_STAGES = {"greeted", "applied", "interviewed"}
STAGE_ORDER = ("greeted", "applied", "interviewed")
QUERY_CHUNK_SIZE = 400


def _resolve_resume(resume_id=None) -> dict:
    return get_default_resume() if resume_id is None else get_resume(int(resume_id))


def _require_job(job_key: str) -> None:
    if get_db().execute(
            "SELECT 1 FROM jobs WHERE job_key=?", (job_key,)).fetchone() is None:
        raise ValueError("岗位不存在")


def _chunks(values: list[str]):
    for start in range(0, len(values), QUERY_CHUNK_SIZE):
        yield values[start:start + QUERY_CHUNK_SIZE]


def _manual_rows(job_keys: list[str], resume_id: int) -> dict[str, dict]:
    result = {}
    for chunk in _chunks(job_keys):
        marks = ",".join("?" for _ in chunk)
        rows = get_db().execute(
            f"SELECT * FROM job_workflow_states WHERE resume_id=? "
            f"AND job_key IN ({marks})", [resume_id, *chunk]).fetchall()
        result.update({row["job_key"]: dict(row) for row in rows})
    return result


def _fact_keys(job_keys: list[str], resume_id: int, table: str,
               condition: str) -> set[str]:
    """按块读取已确认业务事实，避免长岗位列表超过 SQLite 参数上限。"""
    result = set()
    for chunk in _chunks(job_keys):
        marks = ",".join("?" for _ in chunk)
        rows = get_db().execute(
            f"SELECT DISTINCT job_key FROM {table} WHERE resume_id=? "
            f"AND job_key IN ({marks}) AND {condition}",
            [resume_id, *chunk]).fetchall()
        result.update(row["job_key"] for row in rows)
    return result


def states_for_jobs(job_keys, resume_id=None) -> dict[str, dict]:
    """批量返回当前简历的三阶段状态，已有业务事实优先点亮。"""
    source_keys = [job_keys] if isinstance(job_keys, str) else job_keys
    keys = list(dict.fromkeys(str(key) for key in source_keys if key))
    resume = _resolve_resume(resume_id)
    result = {key: {"greeted": False, "applied": False, "interviewed": False}
              for key in keys}
    if not keys:
        return result
    manual = _manual_rows(keys, resume["id"])
    for key, row in manual.items():
        if row.get("greeted_at"):
            result[key]["greeted"] = True
        if row.get("applied_at"):
            result[key].update({"greeted": True, "applied": True})
        if row.get("interviewed_at"):
            result[key].update(
                {"greeted": True, "applied": True, "interviewed": True})

    greeted = _fact_keys(
        keys, resume["id"], "greetings",
        "(delivery_status='confirmed' OR status='sent')")
    applied = _fact_keys(
        keys, resume["id"], "applications",
        "status IN ('platform_confirmed','manual_confirmed')")
    interviewed = _fact_keys(
        keys, resume["id"], "interviews", "status='finished'")
    for key in greeted:
        result[key]["greeted"] = True
    for key in applied:
        result[key].update({"greeted": True, "applied": True})
    for key in interviewed:
        result[key].update(
            {"greeted": True, "applied": True, "interviewed": True})
    return result


def get_state(job_key: str, resume_id=None) -> dict:
    _require_job(job_key)
    resume = _resolve_resume(resume_id)
    state = states_for_jobs([job_key], resume["id"])[job_key]
    return {"job_key": job_key, "resume_id": resume["id"], **state}


def set_stage(job_key: str, stage: str, enabled: bool, resume_id=None) -> dict:
    """人工更新阶段；推进时补齐前置阶段，回退时清除后续阶段。"""
    if stage not in VALID_STAGES:
        raise ValueError("求职阶段无效")
    _require_job(job_key)
    resume = _resolve_resume(resume_id)
    conn = get_db()
    row = conn.execute(
        "SELECT greeted_at,applied_at,interviewed_at FROM job_workflow_states "
        "WHERE job_key=? AND resume_id=?", (job_key, resume["id"])).fetchone()
    values = {name: row[f"{name}_at"] if row else None for name in STAGE_ORDER}
    ts = now_iso()
    stage_index = STAGE_ORDER.index(stage)
    if enabled:
        for name in STAGE_ORDER[:stage_index + 1]:
            values[name] = values[name] or ts
    else:
        for name in STAGE_ORDER[stage_index:]:
            values[name] = None
    conn.execute(
        "INSERT INTO job_workflow_states(job_key,resume_id,greeted_at,applied_at,"
        "interviewed_at,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(job_key,resume_id) DO UPDATE SET greeted_at=excluded.greeted_at,"
        "applied_at=excluded.applied_at,interviewed_at=excluded.interviewed_at,"
        "updated_at=excluded.updated_at",
        (job_key, resume["id"], values["greeted"], values["applied"],
         values["interviewed"], ts))
    conn.commit()
    return {**get_state(job_key, resume["id"]), "updated_at": ts}
