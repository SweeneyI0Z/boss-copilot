"""招呼语：生成（LLM 优先 / 模板兜底）+ 队列状态机（draft→approved→sent/failed）。"""
import json

from . import llm
from .db import get_db, now_iso

SYSTEM_PROMPT = """你是求职沟通专家，为一个岗位写 BOSS直聘 打招呼开场白。

写作要求：
1. professional 为专业完整版，建议 150-220 字，接近用户给出的完整经历概述
2. concise 为精简版，建议 80-120 字，保留最强匹配点
3. technical 为技术聚焦版，建议 120-180 字，突出与 JD 直接相关的技术链路
4. 开头自然表达对具体岗位感兴趣，结尾礼貌邀请进一步沟通
5. 不虚构简历里没有的经历，不堆砌 JD 关键词，不使用夸张承诺
只输出 JSON。"""

SCHEMA = """{"professional":"专业完整版","concise":"精简版","technical":"技术聚焦版"}"""
VARIANT_LABELS = ["专业完整版", "精简版", "技术聚焦版"]

# 无 LLM 或缺少某个版本时使用不虚构经历的安全文案。
def _fallback_variants(title: str, angle: str) -> list[str]:
    title = title[:20]
    angle = angle[:24]
    raw = [
        (f"您好，我对贵司的「{title}」很感兴趣。从岗位描述看，工作重点与{angle}方向相关。"
         "我希望结合简历中的真实经历，进一步介绍自己在需求分析、方案设计、功能开发、"
         "联调测试和持续迭代中的具体职责，以及项目里的技术取舍、问题处理和最终交付结果。"
         "对于暂未覆盖的要求，我也会如实说明能力边界和学习计划。希望有机会了解团队现阶段"
        "的业务目标、协作方式和岗位最需要解决的问题，并进一步沟通，谢谢！"),
        (f"您好，我对贵司的「{title}」很感兴趣。从 JD 看，岗位重点涉及{angle}。"
         "希望有机会结合岗位需求，进一步介绍简历里真实记录的职责、技术方案和交付结果，也期待了解团队当前重点，谢谢！"),
        (f"您好，我关注到贵司正在招聘「{title}」。从 JD 看，岗位重点涉及{angle}。"
         "我希望重点沟通简历中与此相关的技术实践，包括实际负责的模块、方案选择、联调测试、"
         "问题定位和迭代交付；对尚未覆盖的能力要求也会如实说明。期待进一步了解具体业务场景、"
         "技术栈和团队当前最需要解决的问题，谢谢！"),
    ]
    return raw


def _resume_context(resume_id=None):
    conn = get_db()
    if resume_id is None:
        prof = conn.execute("SELECT resume_text FROM profile WHERE id=1").fetchone()
        # 兼容旧 profile 直接写入，但新记录仍绑定默认简历修订，不能绕过失效检查。
        from . import resumes
        default = resumes.get_default_resume()
        return ((prof["resume_text"] or default.get("resume_text") or "").strip(),
                int(default["id"]), int(default["revision"]))
    from . import resumes
    prof = resumes.get_resume(int(resume_id))
    if prof.get("archived"):
        raise llm.LLMError("已归档简历不能用于生成招呼语")
    return (prof.get("resume_text") or "").strip(), int(prof["id"]), int(prof["revision"])


def generate(job_key: str, client=None, resume_id: int = None,
             fallback_on_error: bool = True, on_delta=None, cancelled=None) -> dict:
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
            out = llm.chat_json(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": user}],
                client=client, on_delta=on_delta, cancelled=cancelled)
            if isinstance(out.get("variants"), list):
                candidates = out["variants"]
            else:
                candidates = [out.get("professional"), out.get("concise"),
                              out.get("technical")]
            variants = [v.strip() for v in candidates if isinstance(v, str) and v.strip()]
        except llm.LLMCancelledError:
            raise
        except llm.LLMError:
            if not fallback_on_error:
                raise
            variants = None
    fallback_variants = _fallback_variants(
        row["title"], angle or f"{row['title'][:12]}相关")
    if not variants:
        source = "template"
        variants = fallback_variants
    else:
        variants = [variants[index] if index < len(variants) and variants[index]
                    else fallback_variants[index] for index in range(3)]
    variants = variants[:3]

    if cancelled and cancelled():
        raise llm.LLMCancelledError("招呼语任务已取消，结果未保存")

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


def pending_batch(job_keys=None) -> list:
    """approved 且岗位仍 active 的批次（发送器消费）。"""
    conn = get_db()
    keys = list(dict.fromkeys(str(key) for key in (job_keys or []) if key))
    key_clause = ""
    args = []
    if keys:
        key_clause = f"AND g.job_key IN ({','.join('?' for _ in keys)}) "
        args.extend(keys)
    rows = conn.execute(
        "SELECT g.id, g.job_key, g.chosen, g.resume_id, g.resume_revision, "
        "j.title, j.company, j.job_link FROM greetings g "
        "JOIN jobs j ON j.job_key=g.job_key LEFT JOIN resumes r ON r.id=g.resume_id "
        "WHERE g.status='approved' AND j.status='active' "
        "AND g.resume_id IS NOT NULL AND g.resume_revision=r.revision "
        f"{key_clause}ORDER BY j.composite DESC", args).fetchall()
    return [dict(r) for r in rows]
