"""采集历史的来源归属、启停、删除和导出。"""
import json
import re
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from . import config
from .db import get_db, now_iso


def _json_dict(raw) -> dict:
    try:
        value = json.loads(raw or "{}") if not isinstance(raw, dict) else raw
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _count_from_stats(stats: dict) -> int:
    for field in ("total", "touched"):
        raw = stats.get(field)
        if raw in (None, ""):
            continue
        try:
            count = max(0, int(raw))
        except (TypeError, ValueError):
            continue
        if count:
            return count
    return 0


def _legacy_config_name(conn, item: dict) -> str:
    """给 M18 之前没有 params.name 的配置采集补可读名称。"""
    if item.get("kind") != "config":
        return ""
    try:
        resume_id = int((item.get("params") or {}).get("resume_id") or 0)
    except (TypeError, ValueError):
        resume_id = 0
    row = conn.execute(
        "SELECT name FROM resumes WHERE id=?", (resume_id,)).fetchone() if resume_id else None
    resume_name = str(row["name"] if row else "未命名简历").strip()
    resume_name = re.sub(r"[\r\n\t/\\]+", "-", resume_name)[:20] or "未命名简历"
    date_text = str(item.get("started_at") or "")[:10].replace("-", "") or "未知日期"
    return f"{date_text}-{resume_name}-{int(item['id']):04d}"


def _insert_relations(conn, run_id: int, job_keys, source: str,
                      existing_keys: set[str]) -> int:
    added = 0
    ts = now_iso()
    for job_key in dict.fromkeys(str(key) for key in (job_keys or []) if key):
        if job_key not in existing_keys:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO job_run_items(run_id,job_key,source,created_at) "
            "VALUES(?,?,?,?)", (int(run_id), job_key, source, ts))
        added += max(0, cursor.rowcount)
    return added


def _path_values(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return []


def _legacy_json_paths(params: dict, stats: dict) -> tuple[list[Path], bool]:
    """解析旧导入声明的列表文件；声明不完整时不做部分归属恢复。"""
    directories = []
    for value in _path_values(params.get("dir")):
        directories.append(Path(value).expanduser())
    declared = [*_path_values(params.get("files")),
                *_path_values(stats.get("files"))]
    paths = []
    seen = set()
    if declared:
        for value in dict.fromkeys(declared):
            raw = Path(value).expanduser()
            candidates = [raw]
            for directory in directories:
                candidates.extend((directory / raw, directory / raw.name))
            candidates.append(Path(config.COLLECT_RESULT_DIR) / raw.name)
            found = next((path for path in candidates if path.is_file()), None)
            if found is None:
                return [], False
            marker = str(found.resolve())
            if marker not in seen:
                paths.append(found)
                seen.add(marker)
        return paths, True

    # 极早期记录可能只保存目录；保留当时“导入目录内全部列表文件”的语义。
    search_dirs = [directory for directory in directories if directory.is_dir()]
    if not search_dirs and Path(config.COLLECT_RESULT_DIR).is_dir():
        search_dirs = [Path(config.COLLECT_RESULT_DIR)]
    for directory in search_dirs:
        for pattern in ("boss_jobs_*.json", "boss_company_jobs_*.json"):
            for path in sorted(directory.glob(pattern)):
                marker = str(path.resolve())
                if marker not in seen:
                    paths.append(path)
                    seen.add(marker)
    return paths, bool(paths)


def _job_keys_from_json_files(paths: list[Path]) -> set[str] | None:
    from . import importer

    keys = set()
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        if not isinstance(jobs, list):
            return None
        for raw in jobs:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or raw.get("job_name") or "").strip()
            company = str(raw.get("boss_name") or raw.get("company") or "").strip()
            if not title or not company:
                continue
            keys.add(importer.job_key_from(
                raw.get("job_link", "") or raw.get("link", ""), title, company,
                str(raw.get("salary") or "")))
    return keys


def _xlsx_fingerprint(stats: dict):
    values = []
    for field in ("total", "with_jd", "with_l2"):
        if field not in stats:
            return None
        try:
            values.append(int(stats[field]))
        except (TypeError, ValueError):
            return None
    return tuple(values) if values[0] > 0 else None


def recover_legacy_ownership() -> dict:
    """幂等恢复 M16 之前文件导入缺失的岗位来源关系。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id,kind,params,stats FROM collect_runs "
        "WHERE kind IN ('xlsx_import','json_import') ORDER BY id").fetchall()
    if not rows:
        return {"recovered_runs": [], "relations_added": 0}

    existing_keys = {row["job_key"] for row in conn.execute("SELECT job_key FROM jobs")}
    parsed = [
        {"id": int(row["id"]), "kind": row["kind"],
         "params": _json_dict(row["params"]), "stats": _json_dict(row["stats"])}
        for row in rows
    ]
    recovered = set()
    added = 0
    owned = {}
    for row in conn.execute(
            "SELECT run_id,job_key FROM job_run_items WHERE run_id IN "
            "(SELECT id FROM collect_runs WHERE kind IN ('xlsx_import','json_import'))"):
        owned.setdefault(int(row["run_id"]), set()).add(row["job_key"])

    for item in parsed:
        stats_keys = item["stats"].get("job_keys")
        if not isinstance(stats_keys, list) or not stats_keys:
            continue
        known = owned.setdefault(item["id"], set())
        missing = [key for key in stats_keys if str(key) not in known]
        count = _insert_relations(
            conn, item["id"], missing, "stats_recovered", existing_keys)
        if count:
            recovered.add(item["id"])
            added += count
            known.update(str(key) for key in missing if str(key) in existing_keys)

    for item in parsed:
        if item["kind"] != "json_import" or owned.get(item["id"]):
            continue
        paths, complete = _legacy_json_paths(item["params"], item["stats"])
        keys = _job_keys_from_json_files(paths) if complete else None
        if keys is None or (not keys and _count_from_stats(item["stats"]) > 0):
            continue
        count = _insert_relations(
            conn, item["id"], sorted(keys), "json_file_recovered", existing_keys)
        if count:
            recovered.add(item["id"])
            added += count
            owned[item["id"]] = keys & existing_keys

    # Excel 临时副本已不存在时，只复制统计指纹一致且数量完整的已知归属。
    exact_by_fingerprint = {}
    for item in parsed:
        if item["kind"] != "xlsx_import":
            continue
        fingerprint = _xlsx_fingerprint(item["stats"])
        keys = owned.get(item["id"], set())
        if fingerprint and len(keys) == fingerprint[0]:
            exact_by_fingerprint.setdefault(fingerprint, []).append(frozenset(keys))
    for item in parsed:
        if item["kind"] != "xlsx_import" or owned.get(item["id"]):
            continue
        fingerprint = _xlsx_fingerprint(item["stats"])
        candidates = set(exact_by_fingerprint.get(fingerprint, []))
        if len(candidates) != 1:
            continue
        keys = next(iter(candidates))
        count = _insert_relations(
            conn, item["id"], sorted(keys), "xlsx_fingerprint_recovered", existing_keys)
        if count:
            recovered.add(item["id"])
            added += count
            owned[item["id"]] = set(keys)

    conn.commit()
    return {"recovered_runs": sorted(recovered), "relations_added": added}


def attach_jobs(run_id: int, job_keys, source: str = "") -> None:
    conn = get_db()
    ts = now_iso()
    for job_key in dict.fromkeys(job_keys or []):
        conn.execute(
            "INSERT OR IGNORE INTO job_run_items(run_id,job_key,source,created_at) "
            "VALUES(?,?,?,?)", (int(run_id), str(job_key), source, ts))
    conn.commit()


def visible_jobs_clause(job_alias: str = "j") -> str:
    """无来源归属的历史岗位继续可见；有归属时至少一个来源必须启用。"""
    return (
        f"(NOT EXISTS (SELECT 1 FROM job_run_items vr0 WHERE vr0.job_key={job_alias}.job_key) "
        f"OR EXISTS (SELECT 1 FROM job_run_items vr JOIN collect_runs cr "
        f"ON cr.id=vr.run_id WHERE vr.job_key={job_alias}.job_key AND cr.enabled=1))"
    )


def set_enabled(run_id: int, enabled: bool) -> dict:
    recover_legacy_ownership()
    conn = get_db()
    row = conn.execute("SELECT id FROM collect_runs WHERE id=?", (int(run_id),)).fetchone()
    if row is None:
        raise ValueError("采集记录不存在")
    conn.execute("UPDATE collect_runs SET enabled=? WHERE id=?",
                 (1 if enabled else 0, int(run_id)))
    conn.commit()
    return {"id": int(run_id), "enabled": bool(enabled)}


def list_runs(limit: int = 100) -> list[dict]:
    recover_legacy_ownership()
    conn = get_db()
    rows = conn.execute(
        "SELECT r.*,COUNT(DISTINCT m.job_key) item_count,"
        "COUNT(DISTINCT CASE WHEN length(trim(replace(replace(replace(replace("
        "COALESCE(d.jd,''),char(9),''),char(10),''),char(13),''),'　',''))) > 0 "
        "THEN m.job_key END) with_jd_count FROM collect_runs r "
        "LEFT JOIN job_run_items m ON m.run_id=r.id "
        "LEFT JOIN job_details d ON d.job_key=m.job_key GROUP BY r.id "
        "ORDER BY r.id DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for field in ("params", "stats"):
            try:
                item[field] = json.loads(item[field] or "{}")
            except (json.JSONDecodeError, TypeError):
                item[field] = {}
        item["enabled"] = bool(item.get("enabled", 1))
        item["paused"] = bool(item.get("paused", 0))
        item["owned_item_count"] = int(item.get("item_count") or 0)
        stats = item["stats"]
        item["result_count"] = item["owned_item_count"] or int(
            stats.get("total") or stats.get("touched") or 0)
        item["item_count"] = item["result_count"]
        item["plan_name"] = str(
            item["params"].get("name") or _legacy_config_name(conn, item))
        item["has_source_ownership"] = item["owned_item_count"] > 0
        item["completeness_known"] = bool(
            item["owned_item_count"] > 0 or item["result_count"] == 0)
        if item["completeness_known"]:
            item["with_jd_count"] = int(item.get("with_jd_count") or 0)
            item["list_only_count"] = max(
                0, item["owned_item_count"] - item["with_jd_count"])
        else:
            item["with_jd_count"] = None
            item["list_only_count"] = None
        item["can_export"] = item["owned_item_count"] > 0
        result.append(item)
    return result


def _delete_counts(conn, run_id: int, stats: dict) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) owned_count,"
        "SUM(CASE WHEN EXISTS(SELECT 1 FROM job_run_items other "
        "WHERE other.job_key=m.job_key AND other.run_id<>m.run_id) "
        "THEN 1 ELSE 0 END) shared_count "
        "FROM job_run_items m WHERE m.run_id=?", (int(run_id),)).fetchone()
    owned = int(row["owned_count"] or 0)
    shared = int(row["shared_count"] or 0)
    result_count = owned or _count_from_stats(stats)
    unattributed = result_count if owned == 0 else 0
    warning = ""
    if unattributed:
        warning = "旧采集记录缺少精确岗位归属，仅删除采集记录，不删除无法确认归属的岗位"
    return {
        "run_id": int(run_id), "result_count": result_count,
        "owned_job_count": owned, "exclusive_job_count": owned - shared,
        "shared_job_count": shared, "unattributed_job_count": unattributed,
        "exclusive_jobs": owned - shared, "shared_jobs": shared,
        "legacy_unattributed": unattributed,
        "warning": warning,
    }


def preview_delete(run_id: int) -> dict:
    """预览物理删除范围；共享岗位和无法确认归属的旧岗位不会删除。"""
    run_id = int(run_id)
    conn = get_db()
    row = conn.execute(
        "SELECT stats FROM collect_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("采集记录不存在")
    recover_legacy_ownership()
    row = conn.execute(
        "SELECT stats FROM collect_runs WHERE id=?", (run_id,)).fetchone()
    return _delete_counts(conn, run_id, _json_dict(row["stats"]))


def _assert_deletable(conn, row) -> None:
    status = str(row["status"] or "").strip().lower()
    if status in ("queued", "running", "paused") or bool(row["paused"]):
        raise ValueError("采集记录仍在运行或暂停中，请先取消并等待任务结束")
    if row["finished_at"] is None:
        raise ValueError("采集记录尚未结束，不能删除")

    # 补采任务通过 params 引用原 run，没有数据库外键，需要在这里主动保护。
    active = conn.execute(
        "SELECT id,params FROM collect_runs WHERE id<>? AND finished_at IS NULL",
        (int(row["id"]),)).fetchall()
    for candidate in active:
        source_run_id = _json_dict(candidate["params"]).get("source_run_id")
        try:
            referenced = int(source_run_id) == int(row["id"])
        except (TypeError, ValueError):
            referenced = False
        if referenced:
            raise ValueError("该采集记录正在被补采任务使用，请等待补采结束")


def delete_run(run_id: int, confirm_run_id) -> dict:
    """二次确认后，事务删除 run 及仅归属于它的岗位数据。"""
    run_id = int(run_id)
    try:
        if isinstance(confirm_run_id, bool):
            raise ValueError
        confirmed = int(confirm_run_id)
    except (TypeError, ValueError):
        confirmed = None
    if confirmed != run_id:
        raise ValueError("二次确认的采集记录 ID 不匹配")

    conn = get_db()
    if conn.execute("SELECT 1 FROM collect_runs WHERE id=?", (run_id,)).fetchone() is None:
        raise ValueError("采集记录不存在")
    recover_legacy_ownership()

    exclusive_sql = (
        "SELECT m.job_key FROM job_run_items m WHERE m.run_id=? "
        "AND NOT EXISTS(SELECT 1 FROM job_run_items other "
        "WHERE other.job_key=m.job_key AND other.run_id<>m.run_id)"
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id,status,paused,finished_at,stats FROM collect_runs WHERE id=?",
            (run_id,)).fetchone()
        if row is None:
            raise ValueError("采集记录不存在")
        _assert_deletable(conn, row)
        preview = _delete_counts(conn, run_id, _json_dict(row["stats"]))

        # 这些旧表没有岗位级 ON DELETE CASCADE，必须先显式清理或解除引用。
        for table in ("job_details", "greetings", "job_score_baselines", "interviews"):
            conn.execute(
                f"DELETE FROM {table} WHERE job_key IN ({exclusive_sql})", (run_id,))
        conn.execute(
            f"UPDATE conversations SET job_key=NULL WHERE job_key IN ({exclusive_sql})",
            (run_id,))
        deleted = conn.execute(
            f"DELETE FROM jobs WHERE job_key IN ({exclusive_sql})", (run_id,)).rowcount
        conn.execute("DELETE FROM collect_runs WHERE id=?", (run_id,))

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("删除后数据库外键检查失败，已回滚")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "ok": True, "run_id": run_id, "deleted_jobs": max(0, deleted),
        "preserved_shared_jobs": preview["shared_job_count"],
        "unattributed_jobs": preview["unattributed_job_count"],
        "warning": preview["warning"],
    }


def run_jobs(run_id: int) -> list[dict]:
    rows = get_db().execute(
        "SELECT j.*,d.jd FROM job_run_items m JOIN jobs j ON j.job_key=m.job_key "
        "LEFT JOIN job_details d ON d.job_key=j.job_key WHERE m.run_id=? "
        "ORDER BY j.last_seen_at DESC,j.job_key", (int(run_id),)).fetchall()
    return [dict(row) for row in rows]


def export_xlsx(run_id: int) -> BytesIO:
    jobs = run_jobs(run_id)
    if not jobs and get_db().execute(
            "SELECT 1 FROM collect_runs WHERE id=?", (int(run_id),)).fetchone() is None:
        raise ValueError("采集记录不存在")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "采集结果"
    headers = ["岗位", "公司", "薪资", "地点", "经验", "学历", "行业", "公司规模",
               "融资阶段", "HR活跃", "岗位链接", "JD", "最近更新"]
    sheet.append(headers)
    def safe(value):
        text = "" if value is None else str(value)
        return "'" + text if text.startswith(("=", "+", "-", "@")) else text
    for job in jobs:
        sheet.append([safe(value) for value in [
            job.get("title", ""), job.get("company", ""), job.get("salary", ""),
            job.get("location", ""), job.get("experience", ""), job.get("degree", ""),
            job.get("industry", ""), job.get("scale", ""), job.get("stage", ""),
            job.get("hr_active", ""), job.get("job_link", ""), job.get("jd", ""),
            job.get("last_seen_at", ""),
        ]])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in {"A": 28, "B": 24, "C": 16, "D": 18, "E": 12, "F": 10,
                          "G": 18, "H": 16, "I": 14, "J": 14, "K": 42,
                          "L": 80, "M": 22}.items():
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
