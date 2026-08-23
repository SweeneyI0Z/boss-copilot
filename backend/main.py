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


# ── 评分 ────────────────────────────────────────────────────────

@app.post("/api/score/l1")
def run_l1(body: dict = None):
    force = bool((body or {}).get("force"))
    return scoring_l1.run_l1(force=force)


@app.post("/api/score/l2")
def run_l2(body: dict = None):
    from .scoring import l2 as scoring_l2
    from . import llm as llm_mod
    body = body or {}
    if not llm_mod.configured():
        raise HTTPException(400, "LLM 未配置：请在「设置」页填写 BYOK 信息")
    try:
        return scoring_l2.run_l2(limit=int(body.get("limit", 10)))
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


# ── AI 采集策略 ─────────────────────────────────────────────────

@app.get("/api/strategy")
def get_strategy():
    return strategy.get_plan()


@app.post("/api/strategy/generate")
def gen_strategy():
    from . import llm as llm_mod
    if not llm_mod.configured():
        raise HTTPException(400, "LLM 未配置：请在「设置」页填写 BYOK 信息")
    prof = get_db().execute("SELECT resume_text, expectations FROM profile WHERE id=1").fetchone()
    try:
        plan = strategy.generate_plan(prof["resume_text"],
                                      json.loads(prof["expectations"] or "{}"))
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))
    strategy.save_plan(plan)
    return plan


@app.put("/api/strategy")
def save_strategy(body: dict):
    strategy.save_plan(body)
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
    result = scoring_l1.run_l1(force=True, keep_imported=True)
    return {**result, "report": sync_mod.rescore_report()}


# ── 招呼语 ──────────────────────────────────────────────────────

@app.post("/api/greeting/generate")
def greeting_generate(body: dict):
    from . import greeting, llm as llm_mod
    try:
        return greeting.generate(body.get("job_key", ""))
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
        return greeting.approve(gid, int(body.get("index", 0)))
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.post("/api/greetings/{gid}/skip")
def greeting_skip(gid: int):
    from . import greeting
    return greeting.skip(gid)


@app.post("/api/greeting/send-batch")
def greeting_send_batch():
    """后台线程发送 approved 批次（账号A，全护栏）。"""
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


# ── 消息中心 ────────────────────────────────────────────────────

@app.post("/api/chat/poll")
def chat_poll(body: dict = None):
    from . import chatpoll, llm as llm_mod
    try:
        return chatpoll.poll(int((body or {}).get("max", 8)))
    except (llm_mod.LLMError, RuntimeError, OSError) as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/chat/conversations")
def chat_conversations():
    from . import chatpoll
    return chatpoll.conversations_with_drafts()


@app.post("/api/chat/draft")
def chat_draft(body: dict):
    from . import chatpoll, llm as llm_mod
    try:
        return chatpoll.generate_draft(int(body.get("conversation_id", 0)))
    except llm_mod.LLMError as e:
        raise HTTPException(400, str(e))


@app.post("/api/chat/send")
def chat_send(body: dict):
    from . import chatpoll
    return chatpoll.approve_and_send(int(body.get("conversation_id", 0)),
                                     body.get("reply", ""))


# ── 模拟面试 ────────────────────────────────────────────────────

@app.post("/api/interview/start")
def interview_start(body: dict):
    from . import interview, llm as llm_mod
    if not llm_mod.configured():
        raise HTTPException(400, "LLM 未配置：请在「设置」页填写 BYOK 信息")
    try:
        return interview.start(body.get("job_key", ""))
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
