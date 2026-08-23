"""boss-copilot FastAPI 入口：数据 API + 前端静态托管。"""
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from . import importer
from .boss import cdp
from .db import get_all_settings, get_db, init_db, now_iso, set_setting

app = FastAPI(title="boss-copilot")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.on_event("startup")
def _startup():
    init_db()


# ── 设置 / 档案 ──────────────────────────────────────────────────

@app.get("/api/settings")
def read_settings():
    return get_all_settings()


@app.put("/api/settings")
def write_settings(body: dict):
    for k, v in body.items():
        if k in config.DEFAULT_SETTINGS:
            set_setting(k, v)
    return {"ok": True}


class ProfileIn(BaseModel):
    resume_text: str
    expectations: dict = {}


@app.get("/api/profile")
def read_profile():
    row = get_db().execute("SELECT * FROM profile WHERE id=1").fetchone()
    return {"resume_text": row["resume_text"],
            "expectations": json.loads(row["expectations"] or "{}"),
            "updated_at": row["updated_at"]}


@app.put("/api/profile")
def write_profile(body: ProfileIn):
    get_db().execute(
        "UPDATE profile SET resume_text=?, expectations=?, updated_at=? WHERE id=1",
        (body.resume_text, json.dumps(body.expectations, ensure_ascii=False), now_iso()))
    get_db().commit()
    return {"ok": True}


# ── 导入 ────────────────────────────────────────────────────────

@app.post("/api/import/xlsx")
def import_xlsx(body: dict):
    path = body.get("path")
    if not path:
        raise HTTPException(400, "path required")
    p = Path(path).expanduser()
    if not p.exists():
        raise HTTPException(404, f"文件不存在: {p}")
    # Excel 正被打开时也能读：复制到临时文件再解析，避免锁冲突
    import shutil, tempfile
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        shutil.copy(p, tmp.name)
        return importer.import_xlsx(tmp.name)


@app.post("/api/import/json")
def import_json(body: dict):
    directory = body.get("dir") or str(Path.home() / ".boss-zhipin-scraper" / "job-result")
    return importer.import_scraper_json(directory)


# ── 岗位 ────────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(status: Optional[str] = None, q: str = "", source: str = "",
              sort: str = "composite", limit: int = 50, offset: int = 0):
    where, args = ["1=1"], []
    if status:
        where.append("status=?")
        args.append(status)
    if source:
        where.append("source=?")
        args.append(source)
    if q:
        where.append("(title LIKE ? OR company LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    cond = " AND ".join(where)

    order = {"composite": "composite DESC, composite_rough DESC, l1_score DESC",
             "l1": "composite_rough DESC, l1_score DESC",
             "match": "match_score DESC, match_rough DESC",
             "salary": "salary_max DESC",
             "recent": "last_seen_at DESC"}.get(sort, "composite DESC")
    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) c FROM jobs WHERE {cond}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT job_key, title, company, salary, salary_max, experience, degree, location,"
        f" industry, scale, stage, hr_active, source, status, l1_score, match_rough,"
        f" composite_rough, job_score, match_score, composite, priority"
        f" FROM jobs WHERE {cond} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    counts = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
    return {"total": total, "items": [dict(r) for r in rows],
            "status_counts": {r["status"]: r["c"] for r in counts}}


@app.get("/api/jobs/{job_key}")
def job_detail(job_key: str):
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
    if job is None:
        raise HTTPException(404, "job not found")
    detail = conn.execute(
        "SELECT jd, skill_tags, fetched_at FROM job_details WHERE job_key=?",
        (job_key,)).fetchone()
    out = dict(job)
    for f in ("l1_detail", "l2_detail"):
        try:
            out[f] = json.loads(out.get(f) or "{}")
        except (json.JSONDecodeError, TypeError):
            out[f] = {}
    out["jd"] = detail["jd"] if detail else ""
    out["skill_tags"] = detail["skill_tags"] if detail else ""
    return out


@app.get("/api/runs")
def runs():
    rows = get_db().execute(
        "SELECT * FROM collect_runs ORDER BY id DESC LIMIT 20").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d["params"] or "{}")
        d["stats"] = json.loads(d["stats"] or "{}")
        out.append(d)
    return out


# ── 双账号 ──────────────────────────────────────────────────────

@app.get("/api/accounts")
def accounts():
    return cdp.status()


@app.post("/api/accounts/{name}/launch")
def account_launch(name: str):
    if name not in config.ACCOUNTS:
        raise HTTPException(404, "unknown account")
    return cdp.launch(name)


@app.post("/api/accounts/{name}/login-page")
def account_login_page(name: str):
    if name not in config.ACCOUNTS:
        raise HTTPException(404, "unknown account")
    return cdp.open_login_page(name)


@app.get("/api/accounts/{name}/login-state")
def account_login_state(name: str):
    if name not in config.ACCOUNTS:
        raise HTTPException(404, "unknown account")
    return cdp.login_state(name)


@app.post("/api/accounts/{name}/stop")
def account_stop(name: str):
    if name not in config.ACCOUNTS:
        raise HTTPException(404, "unknown account")
    return cdp.stop(name)


# ── 前端 ────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
