"""多简历档案与按修订评分。

旧版 ``profile`` 表继续作为默认简历的兼容镜像；新功能只以本模块为写入口。
"""
import json

from .db import get_db, now_iso


_UNSET = object()
_SCORE_FIELDS = {
    "l1_score", "l1_detail", "match_rough", "composite_rough", "l1_source",
    "job_score", "match_score", "composite", "priority", "l2_detail",
    "l2_source", "l2_stale",
}
_JSON_FIELDS = {"l1_detail", "l2_detail"}


def _json_dump(value, default) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            value = default
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value, default):
    try:
        parsed = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _resume_dict(row) -> dict:
    out = dict(row)
    out["expectations"] = _json_load(out.get("expectations"), {})
    out["skill_profile"] = _json_load(out.get("skill_profile"), {})
    out["is_default"] = bool(out.get("is_default"))
    out["archived"] = bool(out.get("archived_at"))
    return out


def _score_dict(row) -> dict:
    out = dict(row)
    out["l1_detail"] = _json_load(out.get("l1_detail"), {})
    out["l2_detail"] = _json_load(out.get("l2_detail"), {})
    out["l2_stale"] = bool(out.get("l2_stale"))
    return out


def _require_resume(resume_id: int, include_archived: bool = False):
    row = get_db().execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
    if row is None or (row["archived_at"] and not include_archived):
        raise ValueError("简历不存在或已归档")
    return row


def _sync_legacy_profile(row) -> None:
    """默认简历同步给旧模块，迁移完成前保持单档案调用兼容。"""
    get_db().execute(
        "UPDATE profile SET resume_text=?, expectations=?, updated_at=? WHERE id=1",
        (row["resume_text"], row["expectations"], row["updated_at"]))


def list_resumes(include_archived: bool = False) -> list[dict]:
    where = "" if include_archived else "WHERE archived_at IS NULL"
    rows = get_db().execute(
        f"SELECT * FROM resumes {where} ORDER BY is_default DESC, updated_at DESC, id DESC"
    ).fetchall()
    return [_resume_dict(row) for row in rows]


def get_resume(resume_id: int) -> dict:
    return _resume_dict(_require_resume(int(resume_id), include_archived=True))


def get_default_resume() -> dict:
    row = get_db().execute(
        "SELECT * FROM resumes WHERE is_default=1 AND archived_at IS NULL "
        "ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise ValueError("尚未设置默认简历")
    return _resume_dict(row)


def create_resume(name: str, resume_text: str = "", expectations=None,
                  skill_profile=None, boss_resume_label: str = "",
                  make_default: bool = False) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("简历名称不能为空")
    conn = get_db()
    ts = now_iso()
    if make_default:
        conn.execute("UPDATE resumes SET is_default=0 WHERE is_default=1")
    cur = conn.execute(
        "INSERT INTO resumes(name, resume_text, expectations, skill_profile, "
        "boss_resume_label, revision, is_default, created_at, updated_at) "
        "VALUES(?,?,?,?,?,1,?,?,?)",
        (name, resume_text or "", _json_dump(expectations, {}),
         _json_dump(skill_profile, {}), (boss_resume_label or "").strip(),
         int(make_default), ts, ts))
    row = conn.execute("SELECT * FROM resumes WHERE id=?", (cur.lastrowid,)).fetchone()
    if make_default:
        _sync_legacy_profile(row)
    conn.commit()
    return _resume_dict(row)


def update_resume(resume_id: int, *, name=_UNSET, resume_text=_UNSET,
                  expectations=_UNSET, skill_profile=_UNSET,
                  boss_resume_label=_UNSET) -> dict:
    """更新档案；只有会影响评分的内容变化才创建新修订。"""
    conn = get_db()
    old = _require_resume(int(resume_id))
    values = dict(old)
    if name is not _UNSET:
        value = (name or "").strip()
        if not value:
            raise ValueError("简历名称不能为空")
        values["name"] = value
    if resume_text is not _UNSET:
        values["resume_text"] = resume_text or ""
    if expectations is not _UNSET:
        values["expectations"] = _json_dump(expectations, {})
    if skill_profile is not _UNSET:
        values["skill_profile"] = _json_dump(skill_profile, {})
    if boss_resume_label is not _UNSET:
        values["boss_resume_label"] = (boss_resume_label or "").strip()

    score_changed = any(values[field] != old[field]
                        for field in ("resume_text", "expectations", "skill_profile"))
    metadata_changed = any(values[field] != old[field]
                           for field in ("name", "boss_resume_label"))
    if not score_changed and not metadata_changed:
        return _resume_dict(old)
    revision = old["revision"] + 1 if score_changed else old["revision"]
    ts = now_iso()
    conn.execute(
        "UPDATE resumes SET name=?, resume_text=?, expectations=?, skill_profile=?, "
        "boss_resume_label=?, revision=?, updated_at=? WHERE id=?",
        (values["name"], values["resume_text"], values["expectations"],
         values["skill_profile"], values["boss_resume_label"], revision, ts, resume_id))
    if score_changed:
        conn.execute(
            "UPDATE job_resume_scores SET l2_stale=1, updated_at=? "
            "WHERE resume_id=? AND resume_revision<? AND l2_source<>''",
            (ts, resume_id, revision))
    row = conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
    if row["is_default"]:
        _sync_legacy_profile(row)
    conn.commit()
    return _resume_dict(row)


def save_revision(resume_id: int, resume_text: str, expectations=None,
                  skill_profile=None, boss_resume_label=None, name=None) -> dict:
    """保存编辑器内容；省略的可选字段沿用当前值。"""
    kwargs = {"resume_text": resume_text}
    if expectations is not None:
        kwargs["expectations"] = expectations
    if skill_profile is not None:
        kwargs["skill_profile"] = skill_profile
    if boss_resume_label is not None:
        kwargs["boss_resume_label"] = boss_resume_label
    if name is not None:
        kwargs["name"] = name
    return update_resume(resume_id, **kwargs)


def set_default(resume_id: int) -> dict:
    conn = get_db()
    _require_resume(int(resume_id))
    conn.execute("UPDATE resumes SET is_default=0 WHERE is_default=1")
    conn.execute("UPDATE resumes SET is_default=1 WHERE id=?", (resume_id,))
    row = conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
    _sync_legacy_profile(row)
    conn.commit()
    return _resume_dict(row)


def archive_resume(resume_id: int) -> dict:
    conn = get_db()
    row = _require_resume(int(resume_id), include_archived=True)
    if row["archived_at"]:
        return _resume_dict(row)
    candidates = conn.execute(
        "SELECT * FROM resumes WHERE archived_at IS NULL AND id<>? "
        "ORDER BY is_default DESC, updated_at DESC, id DESC", (resume_id,)).fetchall()
    if not candidates:
        raise ValueError("至少需要保留一份未归档简历")
    ts = now_iso()
    conn.execute(
        "UPDATE resumes SET archived_at=?, is_default=0, updated_at=? WHERE id=?",
        (ts, ts, resume_id))
    if row["is_default"]:
        replacement = candidates[0]
        conn.execute("UPDATE resumes SET is_default=1 WHERE id=?", (replacement["id"],))
        replacement = conn.execute(
            "SELECT * FROM resumes WHERE id=?", (replacement["id"],)).fetchone()
        _sync_legacy_profile(replacement)
    conn.commit()
    return get_resume(resume_id)


def restore_resume(resume_id: int, make_default: bool = False) -> dict:
    conn = get_db()
    row = _require_resume(int(resume_id), include_archived=True)
    conn.execute("UPDATE resumes SET archived_at=NULL, updated_at=? WHERE id=?",
                 (now_iso(), resume_id))
    conn.commit()
    return set_default(resume_id) if make_default else get_resume(resume_id)


def current_revision(resume_id: int = None) -> int:
    row = get_default_resume() if resume_id is None else get_resume(resume_id)
    if row["archived"]:
        raise ValueError("已归档简历没有当前评分版本")
    return int(row["revision"])


def get_job_score(job_key: str, resume_id: int = None,
                  resume_revision: int = None):
    resume = get_default_resume() if resume_id is None else get_resume(resume_id)
    revision = int(resume_revision or resume["revision"])
    row = get_db().execute(
        "SELECT * FROM job_resume_scores WHERE job_key=? AND resume_id=? "
        "AND resume_revision=?", (job_key, resume["id"], revision)).fetchone()
    return _score_dict(row) if row else None


def save_job_score(job_key: str, resume_id: int = None,
                   resume_revision: int = None, **score) -> dict:
    unknown = set(score) - _SCORE_FIELDS
    if unknown:
        raise ValueError(f"未知评分字段: {', '.join(sorted(unknown))}")
    if not score:
        raise ValueError("至少需要一个评分字段")
    resume = get_default_resume() if resume_id is None else get_resume(resume_id)
    revision = int(resume_revision or resume["revision"])
    if revision < 1 or revision > int(resume["revision"]):
        raise ValueError("简历修订号无效")
    conn = get_db()
    if conn.execute("SELECT 1 FROM jobs WHERE job_key=?", (job_key,)).fetchone() is None:
        raise ValueError("岗位不存在")
    ts = now_iso()
    fields = []
    values = []
    for field, value in score.items():
        fields.append(field)
        values.append(_json_dump(value, {}) if field in _JSON_FIELDS else value)
    insert_cols = ["job_key", "resume_id", "resume_revision", *fields,
                   "created_at", "updated_at"]
    insert_values = [job_key, resume["id"], revision, *values, ts, ts]
    updates = ", ".join(f"{field}=excluded.{field}" for field in fields)
    conn.execute(
        f"INSERT INTO job_resume_scores({', '.join(insert_cols)}) "
        f"VALUES({', '.join('?' for _ in insert_cols)}) "
        f"ON CONFLICT(job_key,resume_id,resume_revision) DO UPDATE SET "
        f"{updates}, updated_at=excluded.updated_at", insert_values)

    # 默认简历当前评分镜像到 jobs，供尚未迁移的列表和报告使用。
    has_baseline = conn.execute(
        "SELECT 1 FROM job_score_baselines WHERE job_key=?", (job_key,)).fetchone()
    if (resume["is_default"] and revision == int(resume["revision"])
            and has_baseline is None):
        legacy_fields = [field for field in fields if field in {
            "l1_score", "l1_detail", "match_rough", "composite_rough", "job_score",
            "match_score", "composite", "priority", "l2_detail", "l2_source",
        }]
        if legacy_fields:
            legacy_values = [values[fields.index(field)] for field in legacy_fields]
            conn.execute(
                "UPDATE jobs SET " + ", ".join(f"{field}=?" for field in legacy_fields)
                + " WHERE job_key=?", [*legacy_values, job_key])
    conn.commit()
    return get_job_score(job_key, resume["id"], revision)


def mark_l2_stale(resume_id: int, before_revision: int = None) -> int:
    resume = get_resume(resume_id)
    cutoff = int(before_revision or resume["revision"])
    cur = get_db().execute(
        "UPDATE job_resume_scores SET l2_stale=1, updated_at=? "
        "WHERE resume_id=? AND resume_revision<? AND l2_source<>'' AND l2_stale=0",
        (now_iso(), resume_id, cutoff))
    get_db().commit()
    return cur.rowcount


def save_imported_baseline(job_key: str, **score) -> dict:
    """只写首次导入值；后续导入和评分引擎都不能覆盖基线。"""
    allowed = {"l1_score", "l1_detail", "match_rough", "composite_rough",
               "job_score", "match_score", "composite", "priority", "l2_detail"}
    unknown = set(score) - allowed
    if unknown:
        raise ValueError(f"未知基线字段: {', '.join(sorted(unknown))}")
    conn = get_db()
    if conn.execute("SELECT 1 FROM jobs WHERE job_key=?", (job_key,)).fetchone() is None:
        raise ValueError("岗位不存在")
    fields = list(score)
    values = [_json_dump(score[f], {}) if f in _JSON_FIELDS else score[f] for f in fields]
    cols = ["job_key", *fields, "source", "imported_at"]
    conn.execute(
        f"INSERT OR IGNORE INTO job_score_baselines({', '.join(cols)}) "
        f"VALUES({', '.join('?' for _ in cols)})",
        [job_key, *values, "imported", now_iso()])
    conn.commit()
    return get_job_baseline(job_key)


def get_job_baseline(job_key: str):
    row = get_db().execute(
        "SELECT * FROM job_score_baselines WHERE job_key=?", (job_key,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["l1_detail"] = _json_load(out.get("l1_detail"), {})
    out["l2_detail"] = _json_load(out.get("l2_detail"), {})
    return out
