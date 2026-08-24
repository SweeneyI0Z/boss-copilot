"""boss-copilot FastAPI 入口：数据 API + 前端静态托管。"""
import json
import threading
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
        "UPDATE collect_runs SET status='interrupted',finished_at=?,phase='finished' "
        "WHERE finished_at IS NULL AND status='running'", (now_iso(),))
    get_db().commit()


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


_resume_score_state = {"running": {}, "pending": {}, "results": {}}
_resume_score_lock = threading.Lock()


def _run_resume_l2(resume_id: int, revision: int, favorite_keys: list) -> None:
    from .scoring import l2 as scoring_l2
    try:
        result = scoring_l2.run_l2(
            limit=len(favorite_keys), only_missing=False,
            resume_id=resume_id, job_keys=favorite_keys, force=True)
    except Exception as e:
        result = {"error": str(e)[:300]}
    next_job = None
    key = str(resume_id)
    with _resume_score_lock:
        _resume_score_state["results"][key] = result
        next_job = _resume_score_state["pending"].pop(key, None)
        if next_job:
            _resume_score_state["running"][key] = next_job["revision"]
        else:
            _resume_score_state["running"].pop(key, None)
    if next_job:
        threading.Thread(
            target=_run_resume_l2,
            args=(resume_id, next_job["revision"], next_job["job_keys"]),
            daemon=True).start()


def _schedule_resume_l2(resume: dict, favorite_keys: list) -> str:
    key = str(resume["id"])
    payload = {"revision": resume["revision"], "job_keys": list(favorite_keys)}
    start_now = False
    with _resume_score_lock:
        if key in _resume_score_state["running"]:
            # 同一简历再次更新时只保留最新修订，当前任务结束后串行补跑。
            _resume_score_state["pending"][key] = payload
            return "queued"
        _resume_score_state["running"][key] = resume["revision"]
        start_now = True
    if start_now:
        threading.Thread(
            target=_run_resume_l2,
            args=(resume["id"], resume["revision"], favorite_keys), daemon=True).start()
    return "started"


def _rescore_resume(resume: dict) -> dict:
    """保存简历后同步重算 L1；收藏岗位 L2 在后台重评。"""
    from . import llm as llm_mod
    l1_result = scoring_l1.run_l1(force=True, resume_id=resume["id"])
    favorite_keys = [row["job_key"] for row in get_db().execute(
        "SELECT job_key FROM jobs WHERE favorite_at IS NOT NULL AND status='active'")]
    background = bool(favorite_keys and llm_mod.configured())
    schedule = _schedule_resume_l2(resume, favorite_keys) if background else "not_needed"
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
    with _resume_score_lock:
        return {key: dict(value) for key, value in _resume_score_state.items()}


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
                     "AND h.keyword LIKE ? AND h.is_active=1)")
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
