"""招呼语：生成（LLM 优先 / 模板兜底）+ 队列状态机（draft→approved→sent/failed）。"""
import json

from . import llm
from .db import get_db, now_iso

SYSTEM_PROMPT = """你是求职沟通专家，为一个岗位写 BOSS直聘 打招呼开场白。

硬性要求：
1. professional 为 150-220 字的专业完整版，接近用户给出的完整经历概述
2. concise 为 80-120 字的精简版，保留最强匹配点
3. technical 为 120-180 字的技术聚焦版，突出与 JD 直接相关的技术链路
4. 开头自然表达对具体岗位感兴趣，结尾礼貌邀请进一步沟通
5. 不虚构简历里没有的经历，不堆砌 JD 关键词，不使用夸张承诺
只输出 JSON。"""

SCHEMA = """{"professional":"专业完整版","concise":"精简版","technical":"技术聚焦版"}"""
VARIANT_LABELS = ["专业完整版", "精简版", "技术聚焦版"]

# 无 LLM 时的兜底模板（greeting_angle 来自 L2；再兜底用岗位名）
FALLBACK = ("您好，我对贵司的「{title}」很感兴趣。我具备与岗位相关的{angle}经验，"
            "希望有机会结合具体业务进一步沟通，谢谢！")


def _resume_context(resume_id=None):
    conn = get_db()
    if resume_id is None:
        prof = conn.execute("SELECT resume_text FROM profile WHERE id=1").fetchone()
        return (prof["resume_text"] or "").strip(), None, None
    from . import resumes
    prof = resumes.get_resume(int(resume_id))
    return (prof.get("resume_text") or "").strip(), int(prof["id"]), int(prof["revision"])


def generate(job_key: str, client=None, resume_id: int = None) -> dict:
    """为岗位生成 3 个招呼语变体并入队（status=draft）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT j.*, d.jd FROM jobs j LEFT JOIN job_details d ON d.job_key=j.job_key "
        "WHERE j.job_key=?", (job_key,)).fetchone()
    if row is None:
        raise llm.LLMError(f"岗位不存在: {job_key}")
    resume, resume_id, resume_revision = _resume_context(resume_id)
    l2 = json.loads(row["l2_detail"] or "{}")
    if resume_id is not None:
        from . import resumes
        score = resumes.get_job_score(job_key, resume_id, resume_revision) or {}
        l2 = score.get("l2_detail") or l2
    angle = l2.get("greeting_angle") or ""

    variants = None
    source = "llm"
    if llm.configured() and len(resume) >= 50:
        user = (f"# 岗位\n{row['title']} | {row['company']} | {row['salary']} | "
                f"{row['experience']} {row['degree']}\nJD 摘要：{(row['jd'] or '')[:800]}\n\n"
                f"# L2 评委给出的匹配切入点\n{angle or '（无，请从 JD 与简历自行提炼）'}\n\n"
                f"# 我的简历（节选）\n{resume[:2000]}\n\n按此结构输出：\n{SCHEMA}")
        try:
            out = llm.chat_json([{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": user}], client=client)
            if isinstance(out.get("variants"), list):
                candidates = out["variants"]
            else:
                candidates = [out.get("professional"), out.get("concise"),
                              out.get("technical")]
            variants = [v.strip() for v in candidates if isinstance(v, str) and v.strip()]
        except llm.LLMError:
            variants = None
    if not variants:
        source = "template"
        key = angle[:18] if angle else f"{row['title'][:12]}相关"
        base = FALLBACK.format(title=row["title"][:20], angle=key)
        variants = [base, base.replace("希望有机会结合具体业务进一步沟通", "方便的话想进一步了解岗位需求"),
                    base.replace("具备与岗位相关的", "在项目中积累了与岗位相关的")]
    variants = variants[:3]
    while len(variants) < 3:
        variants.append(variants[-1] if variants else FALLBACK.format(
            title=row["title"][:20], angle=row["title"][:12]))

    if resume_id is None:
        exist = conn.execute("SELECT id FROM greetings WHERE job_key=? AND resume_id IS NULL "
                             "AND status IN ('draft','approved')", (job_key,)).fetchone()
    else:
        exist = conn.execute("SELECT id FROM greetings WHERE job_key=? AND resume_id=? "
                             "AND status IN ('draft','approved')", (job_key, resume_id)).fetchone()
    if exist:
        conn.execute("UPDATE greetings SET variants=?, chosen='', status='draft', "
                     "resume_revision=?, delivery_channel='', delivery_status='', updated_at=? "
                     "WHERE id=?", (json.dumps(variants, ensure_ascii=False),
                                    resume_revision, now_iso(), exist["id"]))
        gid = exist["id"]
    else:
        cur = conn.execute(
            "INSERT INTO greetings(job_key, variants, status, created_at, updated_at, "
            "resume_id, resume_revision, delivery_channel, delivery_status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (job_key, json.dumps(variants, ensure_ascii=False), "draft", now_iso(),
             now_iso(), resume_id, resume_revision, "", ""))
        gid = cur.lastrowid
    conn.commit()
    return {"id": gid, "job_key": job_key, "variants": variants,
            "variant_labels": VARIANT_LABELS, "source": source,
            "resume_id": resume_id, "resume_revision": resume_revision}


def list_queue(status: str = "") -> list:
    conn = get_db()
    where, args = ("WHERE g.status=?", [status]) if status else ("", [])
    rows = conn.execute(
        f"SELECT g.*, j.title, j.company, j.salary, j.priority, j.status jstatus, "
        f"r.name resume_name, r.revision current_resume_revision "
        f"FROM greetings g JOIN jobs j ON j.job_key=g.job_key "
        f"LEFT JOIN resumes r ON r.id=g.resume_id {where} "
        "ORDER BY j.composite DESC, g.id DESC", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["variants"] = json.loads(d["variants"] or "[]")
        d["variant_labels"] = VARIANT_LABELS
        d["stale"] = bool(d.get("resume_id") and
                          d.get("resume_revision") != d.get("current_resume_revision"))
        out.append(d)
    return out


def approve(greeting_id: int, chosen_index: int = 0, chosen_text: str = "") -> dict:
    """选定变体并标记 approved（等待发送）。"""
    conn = get_db()
    g = conn.execute("SELECT * FROM greetings WHERE id=?", (greeting_id,)).fetchone()
    if g is None:
        raise llm.LLMError("greeting not found")
    if g["status"] not in ("draft", "approved"):
        raise llm.LLMError(f"状态 {g['status']} 不可编辑")
    variants = json.loads(g["variants"] or "[]")
    if chosen_text and chosen_text.strip():
        chosen = chosen_text.strip()
    else:
        if not (0 <= chosen_index < len(variants)):
            raise llm.LLMError("chosen_index 越界")
        chosen = variants[chosen_index]
    if len(chosen) > 500:
        raise llm.LLMError("招呼语不能超过 500 字")
    conn.execute("UPDATE greetings SET chosen=?, status='approved', delivery_status='', "
                 "updated_at=? WHERE id=?", (chosen, now_iso(), greeting_id))
    conn.commit()
    return {"ok": True}


def confirm_manual(greeting_id: int, chosen_text: str = "") -> dict:
    """用户在 BOSS 原平台发送后人工确认；重复确认保持幂等。"""
    conn = get_db()
    row = conn.execute(
        "SELECT g.*, j.company FROM greetings g JOIN jobs j ON j.job_key=g.job_key "
        "WHERE g.id=?", (greeting_id,)).fetchone()
    if row is None:
        raise llm.LLMError("greeting not found")
    if row["delivery_status"] == "confirmed":
        return {"ok": True, "already_confirmed": True}
    variants = json.loads(row["variants"] or "[]")
    chosen = (chosen_text or row["chosen"] or (variants[0] if variants else "")).strip()
    if not chosen:
        raise llm.LLMError("请先选择或编辑招呼语")
    ts = now_iso()
    conn.execute(
        "UPDATE greetings SET chosen=?, status='sent', sent_at=?, confirmed_at=?, "
        "delivery_channel='manual', delivery_status='confirmed', updated_at=? WHERE id=?",
        (chosen, ts, ts, ts, greeting_id))
    conn.execute(
        "INSERT INTO sent_log(day, job_key, company, ok, created_at) "
        "VALUES(date('now','localtime'),?,?,1,?)",
        (row["job_key"], row["company"], ts))
    conn.commit()
    return {"ok": True, "confirmed_at": ts}


def skip(greeting_id: int) -> dict:
    conn = get_db()
    conn.execute("UPDATE greetings SET status='skipped' WHERE id=? AND status IN "
                 "('draft','approved')", (greeting_id,))
    conn.commit()
    return {"ok": True}


def pending_batch() -> list:
    """approved 且岗位仍 active 的批次（发送器消费）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT g.id, g.job_key, g.chosen, g.resume_id, g.resume_revision, "
        "j.title, j.company, j.job_link FROM greetings g "
        "JOIN jobs j ON j.job_key=g.job_key LEFT JOIN resumes r ON r.id=g.resume_id "
        "WHERE g.status='approved' AND j.status='active' "
        "AND (g.resume_id IS NULL OR g.resume_revision=r.revision) "
        "ORDER BY j.composite DESC").fetchall()
    return [dict(r) for r in rows]
