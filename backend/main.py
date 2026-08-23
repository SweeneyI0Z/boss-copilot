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
from . import strategy
from .boss import cdp
from .db import get_all_settings, get_db, init_db, now_iso, set_setting
from .scoring import l1 as scoring_l1

app = FastAPI(title="boss-copilot")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.on_event("startup")
def _startup():
    config.migrate_legacy_data()
    init_db()


# ── 设置 / 档案 ──────────────────────────────────────────────────

@app.get("/api/settings")
def read_settings():
    return get_all_settings()


@app.put("/api/settings")
def write_settings(body: dict):
    for k, v in body.items():
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


_resume_score_state = {"running": False, "resume_id": None, "result": None}


def _rescore_resume(resume: dict) -> dict:
    """保存简历后同步重算 L1；收藏岗位 L2 在后台重评。"""
    from . import llm as llm_mod
    l1_result = scoring_l1.run_l1(force=True, resume_id=resume["id"])
    favorite_keys = [row["job_key"] for row in get_db().execute(
        "SELECT job_key FROM jobs WHERE favorite_at IS NOT NULL AND status='active'")]
    background = bool(favorite_keys and llm_mod.configured())
    if background and not _resume_score_state["running"]:
        import threading

        def _run():
            from .scoring import l2 as scoring_l2
            try:
                _resume_score_state["result"] = scoring_l2.run_l2(
                    limit=len(favorite_keys), only_missing=False,
                    resume_id=resume["id"], job_keys=favorite_keys, force=True)
            except Exception as e:
                _resume_score_state["result"] = {"error": str(e)[:300]}
            finally:
                _resume_score_state["running"] = False

        _resume_score_state.update({"running": True, "resume_id": resume["id"],
                                    "result": None})
        threading.Thread(target=_run, daemon=True).start()
    return {"l1": l1_result, "favorite_l2_background": background,
            "favorite_jobs": len(favorite_keys)}


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
    return dict(_resume_score_state)


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
        shutil.copy(p, tmp.name)
        return importer.import_xlsx(tmp.name)


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
    from . import resumes
    try:
        resume = resumes.get_default_resume() if resume_id is None else \
            resumes.get_resume(resume_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    where, args = ["1=1"], []
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
        where.append("EXISTS (SELECT 1 FROM job_collection_hits h WHERE h.job_key=j.job_key "
                     "AND h.keyword LIKE ?)")
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
    score_exists = "s.job_key IS NOT NULL"
    rows = conn.execute(
        f"SELECT j.job_key, j.title, j.company, j.salary, j.salary_max, j.experience, "
        f"j.degree, j.location, j.industry, j.scale, j.stage, j.hr_active, j.source, "
        f"j.status, j.last_seen_at, j.job_link, j.favorite_at, j.is_headhunter, "
        f"j.headhunter_reason, j.headhunter_override, {hunter_expr} effective_headhunter, "
        f"CASE WHEN {score_exists} THEN s.l1_score ELSE j.l1_score END current_l1_score, "
        f"CASE WHEN {score_exists} THEN s.match_rough ELSE j.match_rough END current_match_rough, "
        f"CASE WHEN {score_exists} THEN s.composite_rough ELSE j.composite_rough END current_composite_rough, "
        f"CASE WHEN {score_exists} THEN s.job_score ELSE j.job_score END current_job_score, "
        f"CASE WHEN {score_exists} THEN s.match_score ELSE j.match_score END current_match_score, "
        f"CASE WHEN {score_exists} THEN s.composite ELSE j.composite END current_composite, "
        f"CASE WHEN {score_exists} THEN s.priority ELSE j.priority END current_priority "
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
    counts = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
    return {"total": total, "items": items,
            "status_counts": {r["status"]: r["c"] for r in counts},
            "resume_id": resume["id"], "resume_revision": resume["revision"]}


@app.get("/api/jobs/{job_key}")
def job_detail(job_key: str, resume_id: Optional[int] = None):
    from . import applications, resumes
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
    if current_score:
        for field in ("l1_score", "l1_detail", "match_rough", "composite_rough",
                      "job_score", "match_score", "composite", "priority",
                      "l2_detail", "l2_source", "l2_stale"):
            out[field] = current_score.get(field)
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


# ── 评分 ────────────────────────────────────────────────────────

@app.post("/api/score/l1")
def run_l1(body: dict = None):
    body = body or {}
    resume_id = body.get("resume_id")
    return scoring_l1.run_l1(
        force=bool(body.get("force")),
        resume_id=int(resume_id) if resume_id else None,
        job_keys=body.get("job_keys"))


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
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


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

@app.post("/api/collect/run")
def collect_run(body: dict):
    from . import collector
    kind = body.get("kind", "search")
    if kind == "search":
        if not body.get("keyword"):
            raise HTTPException(400, "keyword required")
        tasks = [{"type": "search", "keyword": body["keyword"],
                  "city": body.get("city", "深圳"), "pages": int(body.get("pages", 3))}]
    elif kind == "company":
        if not (body.get("url") or body.get("brand_id")):
            raise HTTPException(400, "url or brand_id required")
        tasks = [{"type": "company", **{k: v for k, v in body.items()
                                        if k in ("url", "brand_id", "name", "pages")}}]
    elif kind == "plan":
        tasks = collector.plan_tasks()
        if not tasks:
            raise HTTPException(400, "采集计划为空：先在上方生成/保存策略")
    else:
        raise HTTPException(400, "kind must be search/company/plan")
    return collector.start(kind, tasks, sync_mode=bool(body.get("sync")))


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


@app.post("/api/sync/refresh")
def sync_refresh():
    """按已保存计划重跑采集：diff 下架 + HR 活跃度剔除。"""
    from . import collector
    tasks = collector.plan_tasks()
    if not tasks:
        raise HTTPException(400, "采集计划为空：先生成/保存策略（或先跑一次按计划采集）")
    return collector.start("sync", tasks, sync_mode=True)


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
    try:
        return greeting.generate(body.get("job_key", ""),
                                 resume_id=body.get("resume_id"))
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
def greeting_send_batch():
    """后台线程发送 approved 批次（沟通号，全护栏）。"""
    from . import sender
    import threading

    global _send_state
    if getattr(_send_state, "running", False):
        return {"ok": False, "error": "已有发送批次在执行"}

    def _run():
        try:
            _send_state.result = sender.send_batch()
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
