"""招呼语：生成（LLM 优先 / 模板兜底）+ 队列状态机（draft→approved→sent/failed）。"""
import json

from . import llm
from .db import get_db, now_iso

SYSTEM_PROMPT = """你是求职沟通专家，为一个岗位写 BOSS直聘 打招呼开场白。

硬性要求：
1. ≤80 字，口语化，不用「尊敬的」类书信腔，不分段
2. 第一句点出与该岗位最相关的 1 个具体匹配点（技能/领域/项目），不空夸
3. 结尾一个轻量的行动引导（如「方便的话想简单聊下」），不要索取微信/电话
4. 不虚构简历里没有的经历；岗位信息与我的亮点由用户提供
只输出 JSON。"""

SCHEMA = """{"variants": ["招呼语1", "招呼语2", "招呼语3"]}"""

# 无 LLM 时的兜底模板（greeting_angle 来自 L2；再兜底用岗位名）
FALLBACK = ("您好，看到贵司在招「{title}」，我有对应的{angle}经验，"
            "方向很契合，方便的话想和您简单聊聊。")


def generate(job_key: str, client=None) -> dict:
    """为岗位生成 3 个招呼语变体并入队（status=draft）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT j.*, d.jd FROM jobs j LEFT JOIN job_details d ON d.job_key=j.job_key "
        "WHERE j.job_key=?", (job_key,)).fetchone()
    if row is None:
        raise llm.LLMError(f"岗位不存在: {job_key}")
    prof = conn.execute("SELECT resume_text FROM profile WHERE id=1").fetchone()
    resume = (prof["resume_text"] or "").strip()
    l2 = json.loads(row["l2_detail"] or "{}")
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
            variants = [v.strip() for v in out.get("variants", []) if v and v.strip()]
        except llm.LLMError:
            variants = None
    if not variants:
        source = "template"
        key = angle[:18] if angle else f"{row['title'][:12]}相关"
        variants = [FALLBACK.format(title=row["title"][:20], angle=key),
                    FALLBACK.format(title=row["title"][:20], angle=key).replace("简单聊聊", "进一步了解"),
                    FALLBACK.format(title=row["title"][:20], angle=key).replace("方便的话想", "期待能")]
    variants = variants[:3]

    exist = conn.execute("SELECT id FROM greetings WHERE job_key=? AND status IN "
                         "('draft','approved')", (job_key,)).fetchone()
    if exist:
        conn.execute("UPDATE greetings SET variants=?, chosen='', status='draft' WHERE id=?",
                     (json.dumps(variants, ensure_ascii=False), exist["id"]))
        gid = exist["id"]
    else:
        cur = conn.execute(
            "INSERT INTO greetings(job_key, variants, status, created_at) VALUES(?,?,?,?)",
            (job_key, json.dumps(variants, ensure_ascii=False), "draft", now_iso()))
        gid = cur.lastrowid
    conn.commit()
    return {"id": gid, "job_key": job_key, "variants": variants, "source": source}


def list_queue(status: str = "") -> list:
    conn = get_db()
    where, args = ("WHERE g.status=?", [status]) if status else ("", [])
    rows = conn.execute(
        f"SELECT g.*, j.title, j.company, j.salary, j.priority, j.status jstatus "
        f"FROM greetings g JOIN jobs j ON j.job_key=g.job_key {where} "
        "ORDER BY j.composite DESC, g.id DESC", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["variants"] = json.loads(d["variants"] or "[]")
        out.append(d)
    return out


def approve(greeting_id: int, chosen_index: int) -> dict:
    """选定变体并标记 approved（等待发送）。"""
    conn = get_db()
    g = conn.execute("SELECT * FROM greetings WHERE id=?", (greeting_id,)).fetchone()
    if g is None:
        raise llm.LLMError("greeting not found")
    if g["status"] not in ("draft", "approved"):
        raise llm.LLMError(f"状态 {g['status']} 不可编辑")
    variants = json.loads(g["variants"] or "[]")
    if not (0 <= chosen_index < len(variants)):
        raise llm.LLMError("chosen_index 越界")
    conn.execute("UPDATE greetings SET chosen=?, status='approved' WHERE id=?",
                 (variants[chosen_index], greeting_id))
    conn.commit()
    return {"ok": True}


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
        "SELECT g.id, g.job_key, g.chosen, j.title, j.company, j.job_link "
        "FROM greetings g JOIN jobs j ON j.job_key=g.job_key "
        "WHERE g.status='approved' AND j.status='active' ORDER BY j.composite DESC").fetchall()
    return [dict(r) for r in rows]
