"""采集历史的来源归属、启停和导出。"""
import json
from io import BytesIO

from openpyxl import Workbook

from .db import get_db, now_iso


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
    conn = get_db()
    row = conn.execute("SELECT id FROM collect_runs WHERE id=?", (int(run_id),)).fetchone()
    if row is None:
        raise ValueError("采集记录不存在")
    conn.execute("UPDATE collect_runs SET enabled=? WHERE id=?",
                 (1 if enabled else 0, int(run_id)))
    conn.commit()
    return {"id": int(run_id), "enabled": bool(enabled)}


def list_runs(limit: int = 100) -> list[dict]:
    rows = get_db().execute(
        "SELECT r.*,COUNT(DISTINCT m.job_key) item_count FROM collect_runs r "
        "LEFT JOIN job_run_items m ON m.run_id=r.id GROUP BY r.id "
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
        item["has_source_ownership"] = item["owned_item_count"] > 0
        item["can_export"] = item["owned_item_count"] > 0
        result.append(item)
    return result


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
