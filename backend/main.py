"""boss-copilot FastAPI 入口：数据 API + 前端静态托管。"""
import json
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from . import importer
from . import strategy
from .boss import cdp
from .db import get_all_settings, get_db, get_setting, init_db, now_iso, set_setting
from .scoring import l1 as scoring_l1

app = FastAPI(title="boss-copilot")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.on_event("startup")
def _startup():
    config.migrate_legacy_data()
    init_db()
    # 上次进程退出时遗留的运行中任务不可能继续，明确标为中断。
    get_db().execute(
        "UPDATE collect_runs SET status='interrupted',finished_at=?,phase='finished',paused=0 "
        "WHERE finished_at IS NULL AND status IN ('running','paused')", (now_iso(),))
    get_db().commit()


@app.on_event("shutdown")
def _shutdown():
    global _ai_scheduler
    if _ai_scheduler is not None:
        _ai_scheduler.close(wait=True, timeout=2)
        _ai_scheduler = None


# ── 设置 / 档案 ──────────────────────────────────────────────────

@app.get("/api/settings")
def read_settings():
    return get_all_settings()


@app.put("/api/settings")
def write_settings(body: dict):
    normalized = dict(body)
    if "send_daily_hard_cap" in normalized:
        normalized["send_daily_hard_cap"] = min(110, max(1, int(
            normalized["send_daily_hard_cap"])))
    hard_cap = int(normalized.get(
        "send_daily_hard_cap", get_setting("send_daily_hard_cap", 110)))
    if "send_daily_limit" in normalized:
        normalized["send_daily_limit"] = min(hard_cap, max(1, int(
            normalized["send_daily_limit"])))
    elif "send_daily_hard_cap" in normalized:
        normalized["send_daily_limit"] = min(
            hard_cap, max(1, int(get_setting("send_daily_limit", 40))))
    if "send_gap_min_sec" in normalized:
        normalized["send_gap_min_sec"] = min(90, max(30, int(
            normalized["send_gap_min_sec"])))
    if "send_gap_max_sec" in normalized:
        normalized["send_gap_max_sec"] = min(90, max(30, int(
            normalized["send_gap_max_sec"])))
    gap_min = int(normalized.get("send_gap_min_sec", get_setting("send_gap_min_sec", 30)))
    gap_max = int(normalized.get("send_gap_max_sec", get_setting("send_gap_max_sec", 90)))
    if gap_min > gap_max:
        normalized["send_gap_max_sec"] = gap_min
    for k, v in normalized.items():
        if k in config.DEFAULT_SETTINGS:
            if k == "dual_account_enabled" and not isinstance(v, bool):
                raise HTTPException(400, "dual_account_enabled 必须是布尔值")
            set_setting(k, v)
    return {"ok": True}


class LLMTestIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@app.post("/api/llm/test")
def test_llm_connection(body: LLMTestIn):
    from . import llm
    try:
        return llm.test_connection(body.base_url, body.api_key, body.model)
    except llm.LLMError as e:
        raise HTTPException(400, str(e))


class ProfileIn(BaseModel):
    resume_text: str
    expectations: Optional[dict] = None


@app.get("/api/profile")
def read_profile():
    from . import resumes
    return resumes.get_default_resume()


@app.put("/api/profile")
def write_profile(body: ProfileIn):
    from . import resumes
    current = resumes.get_default_resume()
    updated = resumes.save_revision(
        current["id"], body.resume_text, expectations=body.expectations)
    rescore = _rescore_resume(updated) if updated["revision"] != current["revision"] else None
    return {"ok": True, "resume": updated, "rescore": rescore}


_ai_scheduler = None
_ai_scheduler_lock = threading.Lock()
_score_flights = {}
_score_flights_lock = threading.Lock()


def _stream_progress(task: dict):
    """把模型增量转换为节流后的任务事件，避免每个 token 都广播整份快照。"""
    parts = []
    emitted = 0
    length = 0

    def on_delta(delta: str):
        nonlocal emitted, length
        parts.append(delta)
        length += len(delta)
        if length - emitted < 240:
            return
        emitted = length
        scheduler = _get_ai_scheduler()
        scheduler.update_task(
            task["id"], progress=min(0.9, 0.15 + length / 5000),
            message=f"模型正在流式生成 · {length} 字",
            partial_output="".join(parts)[-800:])

    return on_delta


def _run_score_artifact(task: dict, cancelled) -> dict:
    """跨页面共用同一岗位评分调用；等待者复用结果，取消的所有者不会拖垮另一页。"""
    from . import llm as llm_mod, resumes
    from .scoring import l2 as scoring_l2

    resume_id = int(task["resume_id"])
    revision = int(task["payload"]["resume_revision"])
    current = resumes.get_resume(resume_id)
    if int(current["revision"]) != revision:
        raise llm_mod.LLMCancelledError("简历已更新，旧修订评分已取消")
    if cancelled():
        raise llm_mod.LLMCancelledError("评分任务已取消")

    scoring_l1.run_l1(
        force=False, resume_id=resume_id, job_keys=[task["job_key"]])
    claim = (task["job_key"], resume_id, revision)
    while True:
        with _score_flights_lock:
            flight = _score_flights.get(claim)
            owner = flight is None
            if owner:
                flight = {"event": threading.Event(), "result": None, "error": None}
                _score_flights[claim] = flight
        if owner:
            try:
                result = scoring_l2.score_job_llm(
                    task["job_key"], resume_id=resume_id,
                    force=bool(task["payload"].get("force", True)),
                    on_delta=_stream_progress(task), cancelled=cancelled)
                flight["result"] = result
                return result
            except Exception as error:
                flight["error"] = error
                raise
            finally:
                with _score_flights_lock:
                    _score_flights.pop(claim, None)
                flight["event"].set()

        while not flight["event"].wait(0.1):
            if cancelled():
                raise llm_mod.LLMCancelledError("评分任务已取消")
        if flight["result"] is not None:
            return flight["result"]
        if isinstance(flight["error"], llm_mod.LLMCancelledError) and not cancelled():
            continue
        raise flight["error"] or llm_mod.LLMError("评分任务未返回结果")


def _run_ai_task(task: dict, cancelled):
    from . import greeting, llm as llm_mod, resumes
    resume = resumes.get_resume(int(task["resume_id"]))
    revision = int(task["payload"].get("resume_revision") or 0)
    if resume.get("archived") or int(resume["revision"]) != revision:
        raise llm_mod.LLMCancelledError("简历已归档或更新，生成任务已取消")
    if task["kind"] in ("score", "analysis"):
        return _run_score_artifact(task, cancelled)
    if task["kind"] == "greeting":
        return greeting.generate(
            task["job_key"], resume_id=resume["id"],
            fallback_on_error=not llm_mod.configured(),
            on_delta=_stream_progress(task), cancelled=cancelled)
    raise ValueError("不支持的 AI 任务类型")


def _get_ai_scheduler():
    global _ai_scheduler
    if _ai_scheduler is None:
        with _ai_scheduler_lock:
            if _ai_scheduler is None:
                from .ai_tasks import AITaskScheduler
                _ai_scheduler = AITaskScheduler(_run_ai_task, max_concurrency=5)
    return _ai_scheduler


def _enqueue_ai(page: str, kind: str, job_keys: list, resume: dict,
                force: bool = True) -> dict:
    items = [{"job_key": key, "payload": {
        "resume_revision": int(resume["revision"]), "force": bool(force),
    }} for key in dict.fromkeys(str(key) for key in job_keys if key)]
    return _get_ai_scheduler().enqueue_many(
        page, kind, items, resume_id=int(resume["id"]))


def _rescore_resume(resume: dict) -> dict:
    """保存简历后同步重算规则分；收藏岗位精评进入工作台统一队列。"""
    from . import llm as llm_mod
    if _ai_scheduler is not None:
        for page in ("jobs", "workbench"):
            _ai_scheduler.cancel(page=page, resume_id=resume["id"])
    l1_result = scoring_l1.run_l1(force=True, resume_id=resume["id"])
    favorite_keys = [row["job_key"] for row in get_db().execute(
        "SELECT job_key FROM jobs WHERE favorite_at IS NOT NULL AND status='active'")]
    background = bool(favorite_keys and llm_mod.configured())
    if background:
        scheduler = _get_ai_scheduler()
        queued = _enqueue_ai("workbench", "analysis", favorite_keys, resume)
        schedule = "started" if queued["added"] else "deduplicated"
    else:
        schedule = "not_needed"
    return {"l1": l1_result, "favorite_l2_background": background,
            "favorite_jobs": len(favorite_keys), "l2_schedule": schedule}


# ── 多简历档案 ──────────────────────────────────────────────────

@app.get("/api/resumes")
def resumes_list(include_archived: bool = False):
    from . import resumes
    return resumes.list_resumes(include_archived=include_archived)


@app.post("/api/resumes")
def resumes_create(body: dict):
    from . import resumes
    try:
        created = resumes.create_resume(
            body.get("name", ""), body.get("resume_text", ""),
            body.get("expectations"), body.get("skill_profile"),
            body.get("boss_resume_label", ""), bool(body.get("make_default")))
        return {**created, "rescore": _rescore_resume(created)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/resumes/{resume_id}")
def resumes_get(resume_id: int):
    from . import resumes
    try:
        return resumes.get_resume(resume_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.put("/api/resumes/{resume_id}")
def resumes_update(resume_id: int, body: dict):
    from . import resumes
    allowed = {k: v for k, v in body.items() if k in {
        "name", "resume_text", "expectations", "skill_profile", "boss_resume_label"}}
    try:
        before = resumes.get_resume(resume_id)
        updated = resumes.update_resume(resume_id, **allowed)
        if updated["revision"] != before["revision"]:
            updated["rescore"] = _rescore_resume(updated)
        return updated
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/resumes/{resume_id}")
@app.post("/api/resumes/{resume_id}/archive")
def resumes_archive(resume_id: int):
    from . import resumes
    try:
        return resumes.archive_resume(resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/resumes/{resume_id}/restore")
def resumes_restore(resume_id: int, body: dict = None):
    from . import resumes
    try:
        return resumes.restore_resume(resume_id, bool((body or {}).get("make_default")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/resumes/{resume_id}/default")
def resumes_default(resume_id: int):
    from . import resumes
    try:
        return resumes.set_default(resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/resumes/{resume_id}/rescore")
def resumes_rescore(resume_id: int):
    from . import resumes
    try:
        return _rescore_resume(resumes.get_resume(resume_id))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/resume-score/status")
def resumes_rescore_status():
    return _get_ai_scheduler().snapshot("workbench")


# ── 总览与岗位用户状态 ──────────────────────────────────────────

@app.get("/api/dashboard")
def dashboard_read():
    from . import collector, dashboard
    return dashboard.get_dashboard(collector.status())


@app.put("/api/jobs/{job_key}/favorite")
@app.post("/api/jobs/{job_key}/favorite")
def job_favorite(job_key: str, body: dict):
    from . import job_state
    try:
        return job_state.favorite_job(job_key, bool(body.get("favorite", True)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/jobs/{job_key}/exclude")
def job_exclude(job_key: str):
    from . import job_state
    try:
        return job_state.exclude_job(job_key)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/jobs/{job_key}/restore")
def job_restore(job_key: str):
    from . import job_state
    try:
        return job_state.restore_job(job_key)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs-excluded")
def jobs_excluded(limit: int = 100, offset: int = 0):
    from . import job_state
    return job_state.list_excluded(limit, offset)


@app.put("/api/jobs/{job_key}/headhunter")
def job_headhunter(job_key: str, body: dict):
    from . import job_state
    try:
        return job_state.set_headhunter_override(job_key, body.get("value"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs/{job_key}/workflow")
def job_workflow_get(job_key: str, resume_id: Optional[int] = None):
    from . import workflow
    try:
        return workflow.get_state(job_key, resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/jobs/{job_key}/workflow")
def job_workflow_update(job_key: str, body: dict):
    from . import workflow
    try:
        return workflow.set_stage(
            job_key, str(body.get("stage", "")), bool(body.get("enabled", True)),
            body.get("resume_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/applications")
def applications_list(status: str = "", resume_id: Optional[int] = None,
                      limit: int = 100, offset: int = 0):
    from . import applications
    try:
        return applications.list_applications(status, resume_id, limit, offset)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/applications/{job_key}")
def application_get(job_key: str, resume_id: Optional[int] = None):
    from . import applications
    try:
        return applications.get_application(job_key, resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/applications/{job_key}/confirm")
def application_confirm(job_key: str, body: dict = None):
    from . import applications
    try:
        return applications.confirm_application(job_key, (body or {}).get("resume_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/applications/{job_key}/probe")
def application_probe(job_key: str, body: dict = None):
    from . import applications, platform_status
    row = get_db().execute(
        "SELECT job_link FROM jobs WHERE job_key=?", (job_key,)).fetchone()
    if row is None:
        raise HTTPException(404, "岗位不存在")
    result = platform_status.probe_job_page(row["job_link"])
    if result.get("risk"):
        from . import sender
        sender.halt(result.get("hint", "平台状态探测出现风控信号"))
    try:
        saved = applications.record_probe_result(
            job_key, (body or {}).get("resume_id"),
            platform_confirmed=result.get("status") == "platform_confirmed",
            evidence=result.get("evidence", ""), error=result.get("hint", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    saved["probe"] = result
    return saved


@app.put("/api/applications/{job_key}")
def application_update(job_key: str, body: dict):
    from . import applications
    try:
        return applications.set_application_status(
            job_key, body.get("resume_id"), body.get("status", "unknown"))
    except ValueError as e:
        raise HTTPException(400, str(e))


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
        temp_path = Path(tmp.name)
    shutil.copy2(p, temp_path)
    try:
        return importer.import_xlsx(str(temp_path))
    finally:
        temp_path.unlink(missing_ok=True)


@app.post("/api/import/json")
def import_json(body: dict):
    directory = body.get("dir") or str(config.COLLECT_RESULT_DIR)
    return importer.import_scraper_json(directory)


# ── 岗位 ────────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(status: Optional[str] = "active", q: str = "", source: str = "",
              keyword: str = "", favorite: str = "all", headhunter: str = "all",
              resume_id: Optional[int] = None, sort: str = "composite",
              limit: int = 50, offset: int = 0):
    from . import collection_runs, resumes, workflow
    try:
        resume = resumes.get_default_resume() if resume_id is None else \
            resumes.get_resume(resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    where, args = ["1=1", collection_runs.visible_jobs_clause("j")], []
    if status in (None, "", "active"):
        where.append("j.status='active'")
    elif status == "archived":
        where.append("j.status IN ('delisted','hr_inactive')")
    elif status != "all":
        if status not in ("delisted", "hr_inactive", "excluded"):
            raise HTTPException(400, "status 无效")
        where.append("j.status=?")
        args.append(status)
    if source:
        where.append("j.source=?")
        args.append(source)
    if q:
        where.append("(j.title LIKE ? OR j.company LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if keyword:
        where.append("EXISTS (SELECT 1 FROM job_collection_hits h "
                     "JOIN collect_runs hr ON hr.id=h.run_id AND hr.enabled=1 "
                     "WHERE h.job_key=j.job_key AND h.keyword LIKE ? AND h.is_active=1)")
        args.append(f"%{keyword}%")
    if favorite == "only":
        where.append("j.favorite_at IS NOT NULL")
    elif favorite == "exclude":
        where.append("j.favorite_at IS NULL")
    elif favorite != "all":
        raise HTTPException(400, "favorite 必须是 all/only/exclude")
    hunter_expr = "COALESCE(j.headhunter_override,j.is_headhunter)"
    if headhunter == "only":
        where.append(f"{hunter_expr}=1")
    elif headhunter == "exclude":
        where.append(f"{hunter_expr}=0")
    elif headhunter != "all":
        raise HTTPException(400, "headhunter 必须是 all/only/exclude")
    cond = " AND ".join(where)

    order = {"composite": "current_composite DESC, current_composite_rough DESC",
             "l1": "current_composite_rough DESC, current_l1_score DESC",
             "job": "current_job_score DESC, current_l1_score DESC",
             "match": "current_match_score DESC, current_match_rough DESC",
             "salary": "j.salary_max DESC",
             "recent": "j.last_seen_at DESC"}.get(sort, "current_composite DESC")
    conn = get_db()
    total = conn.execute(
        f"SELECT COUNT(*) c FROM jobs j WHERE {cond}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT j.job_key, j.title, j.company, j.salary, j.salary_max, j.experience, "
        f"j.degree, j.location, j.industry, j.scale, j.stage, j.hr_active, j.source, "
        f"j.status, j.last_seen_at, j.job_link, j.favorite_at, j.is_headhunter, "
        f"j.headhunter_reason, j.headhunter_override, {hunter_expr} effective_headhunter, "
        f"(SELECT group_concat(DISTINCT f.account) FROM job_favorite_hits f "
        f"WHERE f.job_key=j.job_key) favorite_accounts, "
        f"s.l1_score current_l1_score, s.match_rough current_match_rough, "
        f"s.composite_rough current_composite_rough, s.job_score current_job_score, "
        f"s.match_score current_match_score, s.composite current_composite, "
        f"s.priority current_priority "
        f"FROM jobs j LEFT JOIN job_resume_scores s ON s.job_key=j.job_key "
        f"AND s.resume_id=? AND s.resume_revision=? WHERE {cond} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        [resume["id"], resume["revision"], *args,
         max(1, min(int(limit), 500)), max(0, int(offset))]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for source_name, target_name in (
                ("current_l1_score", "l1_score"),
                ("current_match_rough", "match_rough"),
                ("current_composite_rough", "composite_rough"),
                ("current_job_score", "job_score"),
                ("current_match_score", "match_score"),
                ("current_composite", "composite"),
                ("current_priority", "priority")):
            item[target_name] = item.pop(source_name)
        item["is_favorite"] = bool(item.get("favorite_at"))
        item["effective_headhunter"] = bool(item.get("effective_headhunter"))
        items.append(item)
    keys = [item["job_key"] for item in items]
    states = workflow.states_for_jobs(keys, resume["id"])
    details = {}
    if keys:
        marks = ",".join("?" for _ in keys)
        details = {row["job_key"]: str(row["jd"] or "") for row in conn.execute(
            f"SELECT job_key,jd FROM job_details WHERE job_key IN ({marks})", keys)}
    welfare_words = ("五险一金", "年终奖", "带薪年假", "补充医疗", "餐补", "房补",
                     "住房补贴", "交通补贴", "股票期权", "定期体检", "节日福利",
                     "员工旅游", "弹性工作")
    for item in items:
        jd = details.get(item["job_key"], "")
        text = f"{item.get('title', '')}\n{jd}"
        item["has_weekend"] = "双休" in text
        item["has_benefits"] = any(word in text for word in welfare_words)
        item["workflow"] = states.get(item["job_key"], {})
        item.update(states.get(item["job_key"], {}))
    counts = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs j WHERE "
        f"{collection_runs.visible_jobs_clause('j')} GROUP BY status").fetchall()
    return {"total": total, "items": items,
            "status_counts": {r["status"]: r["c"] for r in counts},
            "resume_id": resume["id"], "resume_revision": resume["revision"]}


@app.get("/api/jobs/{job_key}")
def job_detail(job_key: str, resume_id: Optional[int] = None):
    from . import applications, resumes, workflow
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
    if job is None:
        raise HTTPException(404, "job not found")
    detail = conn.execute(
        "SELECT jd, skill_tags, fetched_at FROM job_details WHERE job_key=?",
        (job_key,)).fetchone()
    try:
        resume = resumes.get_default_resume() if resume_id is None else \
            resumes.get_resume(resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    current_score = resumes.get_job_score(job_key, resume["id"], resume["revision"])
    baseline = resumes.get_job_baseline(job_key)
    out = dict(job)
    for f in ("l1_detail", "l2_detail"):
        try:
            out[f] = json.loads(out.get(f) or "{}")
        except (json.JSONDecodeError, TypeError):
            out[f] = {}
    out["jd"] = detail["jd"] if detail else ""
    out["skill_tags"] = detail["skill_tags"] if detail else ""
    out["fetched_at"] = detail["fetched_at"] if detail else None
    for field in ("l1_score", "match_rough", "composite_rough", "job_score",
                  "match_score", "composite", "priority", "l2_source"):
        out[field] = current_score.get(field) if current_score else None
    out["l1_detail"] = current_score.get("l1_detail", {}) if current_score else {}
    out["l2_detail"] = current_score.get("l2_detail", {}) if current_score else {}
    out["l2_stale"] = bool(current_score.get("l2_stale")) if current_score else False
    out["current_score"] = current_score
    out["baseline"] = baseline
    out["imported_baseline"] = baseline
    out["resume"] = {"id": resume["id"], "name": resume["name"],
                     "revision": resume["revision"]}
    out["favorite"] = bool(out.get("favorite_at"))
    out["is_favorite"] = out["favorite"]
    out["effective_headhunter"] = bool(
        out.get("is_headhunter") if out.get("headhunter_override") is None
        else out.get("headhunter_override"))
    out["headhunter_manual"] = (None if out.get("headhunter_override") is None
                                else bool(out.get("headhunter_override")))
    out["application"] = applications.get_application(job_key, resume["id"])
    out["workflow"] = workflow.get_state(job_key, resume["id"])
    out.update({key: out["workflow"][key]
                for key in ("greeted", "applied", "interviewed", "offered")})
    text = f"{out.get('title', '')}\n{out.get('jd', '')}"
    out["has_weekend"] = "双休" in text
    out["has_benefits"] = any(word in text for word in (
        "五险一金", "年终奖", "带薪年假", "补充医疗", "餐补", "房补", "住房补贴",
        "交通补贴", "股票期权", "定期体检", "节日福利", "员工旅游", "弹性工作"))
    return out


@app.post("/api/jobs/{job_key}/open-boss")
def job_open_boss(job_key: str):
    """在沟通号 Chrome 中打开岗位原始页面，供人工继续操作。"""
    row = get_db().execute(
        "SELECT job_link FROM jobs WHERE job_key=?", (job_key,)).fetchone()
    if row is None:
        raise HTTPException(404, "岗位不存在")
    result = cdp.open_boss_job_page(row["job_link"])
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "沟通号 Chrome 打开岗位失败"))
    return result


# ── BOSS 收藏（感兴趣）同步 ──────────────────────────────────────

@app.post("/api/favorites/sync")
def favorites_sync_start(body: dict = None):
    from . import favorites
    result = favorites.start_sync((body or {}).get("max_pages"))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "收藏同步启动失败"))
    return result


@app.get("/api/favorites/sync/status")
def favorites_sync_status():
    from . import favorites
    return favorites.status()


@app.post("/api/favorites/sync/cancel")
def favorites_sync_cancel():
    from . import favorites
    return favorites.cancel()


@app.post("/api/favorites/sync/retry-details")
def favorites_sync_retry_details(body: dict = None):
    from . import favorites
    result = favorites.retry_details((body or {}).get("source_run_id"))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "没有可补齐的收藏 JD"))
    return result


@app.get("/api/runs")
def runs(limit: int = 100):
    from . import collection_runs, collector
    items = collection_runs.list_runs(limit)
    current = collector.status()
    for item in items:
        if item["id"] != current.get("run_id"):
            continue
        item["runtime"] = {
            "running": bool(current.get("running")),
            "paused": bool(current.get("paused")),
            "phase": current.get("phase", ""),
            "current": current.get("current", ""),
            "progress": current.get("progress") or {},
        }
    return items


@app.put("/api/runs/{run_id}/enabled")
def run_enabled(run_id: int, body: dict):
    from . import collection_runs
    try:
        return collection_runs.set_enabled(run_id, bool(body.get("enabled", True)))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/runs/{run_id}/delete-preview")
def run_delete_preview(run_id: int):
    from . import collection_runs
    try:
        return collection_runs.preview_delete(run_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/runs/{run_id}")
def run_delete(run_id: int, body: dict):
    from . import collection_runs
    try:
        return collection_runs.delete_run(run_id, body.get("confirm_run_id"))
    except ValueError as e:
        message = str(e)
        if "不存在" in message:
            status = 404
        elif "运行" in message or "暂停" in message or "补采" in message or "尚未结束" in message:
            status = 409
        else:
            status = 400
        raise HTTPException(status, message)


@app.get("/api/runs/{run_id}/export")
def run_export(run_id: int):
    from . import collection_runs
    try:
        output = collection_runs.export_xlsx(run_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="collect-run-{run_id}.xlsx"'},
    )


# ── 评分 ────────────────────────────────────────────────────────

@app.post("/api/score/l1")
def run_l1(body: dict = None):
    body = body or {}
    resume_id = body.get("resume_id")
    try:
        return scoring_l1.run_l1(
            force=bool(body.get("force")),
            resume_id=int(resume_id) if resume_id else None,
            job_keys=body.get("job_keys"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/score/l2")
def run_l2(body: dict = None):
    from .scoring import l2 as scoring_l2
    from . import llm as llm_mod
    body = body or {}
    if not llm_mod.configured():
        raise HTTPException(400, "LLM 未配置：请在「设置」页填写 BYOK 信息")
    try:
        resume_id = body.get("resume_id")
        return scoring_l2.run_l2(
            limit=int(body.get("limit", 10)),
            only_missing=not bool(body.get("force")),
            resume_id=int(resume_id) if resume_id else None,
            job_keys=body.get("job_keys"), force=bool(body.get("force")))
    except (llm_mod.LLMError, ValueError) as e:
        raise HTTPException(400, str(e))


# ── 页面级 AI 任务 ──────────────────────────────────────────────

_AI_PAGE_KINDS = {"jobs": {"score"}, "workbench": {"analysis", "greeting"}}


def _validate_ai_page_kind(page: str, kind: str = None) -> None:
    if page not in _AI_PAGE_KINDS:
        raise HTTPException(404, "AI 任务页面不存在")
    if kind is not None and kind not in _AI_PAGE_KINDS[page]:
        raise HTTPException(400, "该页面不支持此生成任务")


@app.post("/api/ai/tasks/{page}", status_code=202)
def ai_tasks_enqueue(page: str, body: dict):
    from . import llm as llm_mod, resumes
    kind = str(body.get("kind") or "")
    # 岗位评分与匹配度来自同一次精评，统一成一个 artifact，避免重复调用。
    if page == "jobs" and kind in ("job_score", "match_score"):
        kind = "score"
    _validate_ai_page_kind(page, kind)
    raw_keys = body.get("job_keys") or []
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    job_keys = list(dict.fromkeys(str(key) for key in raw_keys if key))
    if not job_keys:
        raise HTTPException(400, "请至少选择一个岗位")
    if len(job_keys) > 500:
        raise HTTPException(400, "单次最多加入 500 个岗位")
    try:
        resume = (resumes.get_resume(int(body["resume_id"]))
                  if body.get("resume_id") else resumes.get_default_resume())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    if resume.get("archived"):
        raise HTTPException(400, "已归档简历不能创建生成任务")
    if kind in ("score", "analysis") and not llm_mod.configured():
        raise HTTPException(400, "LLM 未配置：请在「设置」页填写 BYOK 信息")

    rows = get_db().execute(
        "SELECT job_key FROM jobs WHERE status='active' AND job_key IN (%s)" %
        ",".join("?" for _ in job_keys), job_keys).fetchall()
    existing = {row["job_key"] for row in rows}
    missing = [key for key in job_keys if key not in existing]
    accepted = [key for key in job_keys if key in existing]
    if not accepted:
        raise HTTPException(400, "所选岗位不存在或已不在当前列表")
    result = _enqueue_ai(
        page, kind, accepted, resume, force=bool(body.get("force", True)))
    return {**result, "page": page, "kind": kind, "missing": missing,
            "resume_id": resume["id"], "resume_revision": resume["revision"],
            "snapshot": _get_ai_scheduler().snapshot(page)}


@app.get("/api/ai/tasks/{page}")
def ai_tasks_status(page: str):
    _validate_ai_page_kind(page)
    return _get_ai_scheduler().snapshot(page)


@app.post("/api/ai/tasks/{page}/cancel")
def ai_tasks_cancel(page: str, body: dict = None):
    _validate_ai_page_kind(page)
    body = body or {}
    kind = body.get("kind")
    if page == "jobs" and kind in ("job_score", "match_score"):
        kind = "score"
    if kind:
        _validate_ai_page_kind(page, str(kind))
    try:
        count = _get_ai_scheduler().cancel(
            page=page, job_key=body.get("job_key"),
            resume_id=int(body["resume_id"]) if body.get("resume_id") else None,
            kind=str(kind) if kind else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "cancelled": count,
            "snapshot": _get_ai_scheduler().snapshot(page)}


@app.get("/api/ai/tasks/{page}/stream")
def ai_tasks_stream(page: str, since: Optional[int] = None):
    _validate_ai_page_kind(page)
    return StreamingResponse(
        _get_ai_scheduler().event_stream(page, since_version=since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── AI 采集策略 ─────────────────────────────────────────────────

@app.get("/api/strategy")
def get_strategy(resume_id: Optional[int] = None):
    return strategy.get_plan(resume_id)


@app.post("/api/strategy/generate")
def gen_strategy(body: dict = None):
    from . import llm as llm_mod
    from . import resumes
    if not llm_mod.configured():
        raise HTTPException(400, "LLM 未配置：请在「设置」页填写 BYOK 信息")
    resume_id = (body or {}).get("resume_id")
    try:
        prof = resumes.get_default_resume() if not resume_id else resumes.get_resume(resume_id)
        if prof.get("archived"):
            raise ValueError("已归档简历不能生成采集策略")
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        plan = strategy.generate_plan(prof["resume_text"], prof["expectations"])
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))
    strategy.save_plan(plan, prof["id"])
    return plan


@app.put("/api/strategy")
def save_strategy(body: dict):
    resume_id = body.pop("resume_id", None)
    strategy.save_plan(body, resume_id)
    return {"ok": True}


# ── 在线采集与同步 ──────────────────────────────────────────────

def _default_resume_id(resume_id=None) -> int:
    if resume_id:
        return int(resume_id)
    from . import resumes
    return int(resumes.get_default_resume()["id"])


@app.get("/api/collect/config")
def collect_config_get(resume_id: Optional[int] = None):
    from . import cities, collector
    rid = _default_resume_id(resume_id)
    return {"config": collector.get_collect_config(rid),
            "city_options": cities.city_groups(), "resume_id": rid,
            "max_combinations": collector.MAX_SEARCH_COMBINATIONS}


@app.put("/api/collect/config")
def collect_config_save(body: dict, resume_id: Optional[int] = None):
    from . import cities, collector
    rid = _default_resume_id(resume_id or body.get("resume_id"))
    try:
        config_data = collector.save_collect_config(body, rid)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"config": config_data, "city_options": cities.city_groups(),
            "resume_id": rid, "max_combinations": collector.MAX_SEARCH_COMBINATIONS}


@app.get("/api/collect/options")
def collect_options():
    from . import cities, collector
    return {"city_options": cities.city_groups(),
            "filter_values": collector.FILTER_VALUE_MAPS,
            "max_combinations": collector.MAX_SEARCH_COMBINATIONS}

@app.post("/api/collect/run")
def collect_run(body: dict):
    from . import collector
    kind = body.get("kind", "search")
    resume_id = _default_resume_id(body.get("resume_id"))
    if kind == "config":
        result = collector.start_config(
            body.get("config") or body, resume_id, sync_mode=bool(body.get("sync")))
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "采集配置无效"))
        return result
    if kind == "search":
        if not body.get("keyword"):
            raise HTTPException(400, "keyword required")
        tasks = [{"type": "search", "keyword": body["keyword"],
                  "city": body.get("city", "深圳"), "pages": int(body.get("pages", 3)),
                  "filters": body.get("filters", {}), "resume_id": resume_id}]
    elif kind == "company":
        if not (body.get("url") or body.get("brand_id")):
            raise HTTPException(400, "url or brand_id required")
        tasks = [{"type": "company", "resume_id": resume_id,
                  **{k: v for k, v in body.items()
                     if k in ("url", "brand_id", "name", "pages")}}]
    elif kind == "plan":
        tasks = collector.plan_tasks(resume_id)
        if not tasks:
            raise HTTPException(400, "采集计划为空：先在上方生成/保存策略")
    else:
        raise HTTPException(400, "kind must be config/search/company/plan")
    result = collector.start(kind, tasks, sync_mode=bool(body.get("sync")),
                             fetch_details=bool(body.get("fetch_details", True)),
                             resume_id=resume_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "采集启动失败"))
    return result


@app.get("/api/collect/status")
def collect_status():
    from . import collector
    st = collector.status()
    st["recent"] = runs()
    return st


@app.post("/api/collect/cancel")
def collect_cancel():
    from . import collector
    return collector.cancel()


@app.post("/api/collect/pause")
def collect_pause():
    from . import collector
    result = collector.pause()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "采集暂停失败"))
    return result


@app.post("/api/collect/resume")
def collect_resume():
    from . import collector
    result = collector.resume()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "采集继续失败"))
    return result


@app.post("/api/collect/retry-details")
def collect_retry_details(body: dict = None):
    from . import collector
    result = collector.retry_missing((body or {}).get("source_run_id"))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "没有可重试的详情"))
    return result


@app.post("/api/sync/refresh")
def sync_refresh(body: dict = None):
    """按已保存计划重跑采集：diff 下架 + HR 活跃度剔除。"""
    from . import collector
    resume_id = _default_resume_id((body or {}).get("resume_id"))
    tasks = collector.plan_tasks(resume_id)
    if not tasks:
        raise HTTPException(400, "采集计划为空：先生成/保存策略（或先跑一次按计划采集）")
    return collector.start("sync", tasks, sync_mode=True, fetch_details=True,
                           resume_id=resume_id)


@app.get("/api/analytics")
def analytics_read(keyword: str = "", city_code: str = "", date_from: str = "",
                   date_to: str = "", resume_id: Optional[int] = None):
    from . import analytics
    filters = {key: value for key, value in {
        "keyword": keyword, "city_code": city_code,
        "date_from": date_from, "date_to": date_to,
    }.items() if value}
    return analytics.aggregate(filters, _default_resume_id(resume_id))


@app.post("/api/sync/rescore")
def sync_rescore():
    """简历/词典变更后：L1 重算（保留 xlsx 导入基线）+ P 级变化报告。"""
    from . import sync as sync_mod
    from . import resumes
    resume = resumes.get_default_resume()
    result = scoring_l1.run_l1(force=True, resume_id=resume["id"])
    return {**result, "report": sync_mod.rescore_report()}


# ── 招呼语 ──────────────────────────────────────────────────────

@app.post("/api/greeting/generate")
def greeting_generate(body: dict):
    from . import greeting, llm as llm_mod
    from . import resumes
    try:
        resume_id = body.get("resume_id") or resumes.get_default_resume()["id"]
        return greeting.generate(body.get("job_key", ""),
                                 resume_id=resume_id)
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.get("/api/greetings")
def greetings_list(status: str = ""):
    from . import greeting
    return greeting.list_queue(status or None) if status else greeting.list_queue()


@app.post("/api/greetings/{gid}/approve")
def greeting_approve(gid: int, body: dict):
    from . import greeting, llm as llm_mod
    try:
        return greeting.approve(gid, int(body.get("index", 0)),
                                body.get("chosen_text", ""))
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.put("/api/greetings/{gid}")
def greeting_update(gid: int, body: dict):
    return greeting_approve(gid, body)


@app.post("/api/greetings/{gid}/confirm-manual")
def greeting_confirm_manual(gid: int, body: dict = None):
    from . import greeting, llm as llm_mod
    try:
        return greeting.confirm_manual(gid, (body or {}).get("chosen_text", ""))
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.post("/api/greetings/{gid}/skip")
def greeting_skip(gid: int):
    from . import greeting
    return greeting.skip(gid)


@app.post("/api/greeting/send-batch")
def greeting_send_batch(body: dict = None):
    """后台线程发送 approved 批次（沟通号，全护栏）。"""
    from . import sender
    import threading

    global _send_state
    if getattr(_send_state, "running", False):
        return {"ok": False, "error": "已有发送批次在执行"}

    def _run():
        try:
            _send_state.result = sender.send_batch((body or {}).get("job_keys"))
        except Exception as e:  # 线程兜底：任何异常都可见
            _send_state.result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        _send_state.running = False

    _send_state.running = True
    _send_state.result = None
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "note": "发送已在后台执行，间隔 30-90s/条；结果见发送状态"}


@app.get("/api/greeting/send-status")
def greeting_send_status():
    from . import sender
    from .db import get_setting
    st = getattr(_send_state, "result", None)
    return {"sending": getattr(_send_state, "running", False),
            "last_result": st,
            "halted_today": sender.halted_today(),
            "halt_reason": get_setting("send_halt_reason", ""),
            "sent_today": sender.sent_today()}


_send_state = type("S", (), {"running": False, "result": None})()


# ── 模拟面试 ────────────────────────────────────────────────────

@app.post("/api/interview/start")
def interview_start(body: dict):
    from . import interview, llm as llm_mod
    if not llm_mod.configured():
        raise HTTPException(400, "LLM 未配置：请在「设置」页填写 BYOK 信息")
    try:
        from . import resumes
        resume_id = body.get("resume_id") or resumes.get_default_resume()["id"]
        return interview.start(body.get("job_key", ""), resume_id=resume_id)
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.post("/api/interview/{sid}/answer")
def interview_answer(sid: int, body: dict):
    from . import interview, llm as llm_mod
    try:
        return interview.answer(sid, body.get("text", ""))
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.post("/api/interview/{sid}/finish")
def interview_finish(sid: int):
    from . import interview, llm as llm_mod
    try:
        return interview.finish(sid)
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.get("/api/interview/{sid}")
def interview_get(sid: int):
    from . import interview
    return interview.get(sid)


@app.get("/api/interviews")
def interview_list():
    from . import interview
    return interview.list_sessions()


# ── 账号管理 ────────────────────────────────────────────────────

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
    return cdp.check_login_state(name)


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
