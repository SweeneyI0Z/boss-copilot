"""模拟面试：面试官 agent（JD×简历出题 → 多轮问答点评 → 终版报告）。

流程：start(出题计划) → 逐题 answer(点评+追问一次) → finish(报告)。
transcript 全量随每次调用传给 LLM，保证多轮一致性。
"""
import json

from . import llm
from .db import get_db, now_iso

PLANNER_SYSTEM = """你是严格的技术面试官。根据岗位 JD 与候选人简历定制一场模拟面试。

出题原则：
1. 5-8 题，优先打击「简历与 JD 的 GAP」（L2 评委已标出），其次验证核心优势真伪
2. 题目要具体（如「RTOS 任务调度优先级反转怎么处理」），不要「请介绍一下你自己」
3. 每题带 probe（考察点）与 1 个追问方向
只输出 JSON：{"questions": [{"q": "题目", "probe": "考察点", "follow_up": "追问方向"}]}"""

TURN_SYSTEM = """你是严格的技术面试官，正在对候选人进行模拟面试。

根据完整对话记录与当前题目的候选人回答，输出：
- feedback: 对该回答的点评（指出对/错/遗漏，1-3 句，诚实不客套）
- action: "follow_up"（回答含糊或关键点缺失时追问一次，最多一次）或 "next"（进入下一题）
- content: 追问内容或下一题原文（action=next 且已是最后一题时输出「面试结束，请查看报告」）
只输出 JSON：{"feedback": "...", "action": "follow_up|next", "content": "..."}"""

REPORT_SYSTEM = """你是面试复盘教练。根据完整模拟面试记录输出报告。

只输出 JSON：
{"overall": "总体评价 2-3 句",
 "score": 0-100 的数字,
 "strengths": ["表现好的地方，对应题目"],
 "risks": ["暴露的风险点（知识错误/含糊/未答出），附纠正方向"],
 "prep": ["面试前必须补的清单，按优先级"],
 "closing_tips": ["临场建议"]"""


def _job_context(job_key: str) -> tuple:
    conn = get_db()
    row = conn.execute(
        "SELECT j.*, d.jd FROM jobs j LEFT JOIN job_details d ON d.job_key=j.job_key "
        "WHERE j.job_key=?", (job_key,)).fetchone()
    if row is None:
        raise llm.LLMError(f"岗位不存在: {job_key}")
    prof = conn.execute("SELECT resume_text FROM profile WHERE id=1").fetchone()
    resume = (prof["resume_text"] or "").strip()
    if len(resume) < 50:
        raise llm.LLMError("简历未填写：模拟面试以简历为对照基线")
    return row, resume


def start(job_key: str, client=None) -> dict:
    """生成出题计划并创建面试会话。"""
    row, resume = _job_context(job_key)
    l2 = json.loads(row["l2_detail"] or "{}")
    gaps = json.dumps(l2.get("gaps", []), ensure_ascii=False)
    user = (f"# 岗位\n{row['title']} | {row['company']} | {row['salary']}\n"
            f"JD：{(row['jd'] or '')[:2000]}\n\n"
            f"# L2 评委标出的 GAP\n{gaps}\n\n# 我的简历\n{resume[:2500]}")
    plan = llm.chat_json([{"role": "system", "content": PLANNER_SYSTEM},
                          {"role": "user", "content": user}], client=client)
    questions = plan.get("questions") or []
    questions = [q for q in questions if isinstance(q, dict) and q.get("q")]
    if not questions:
        raise llm.LLMError("出题计划为空")
    conn = get_db()
    transcript = [{"role": "bank", "questions": questions},
                  {"role": "interviewer", "content": questions[0]["q"],
                   "probe": questions[0].get("probe", ""),
                   "q_index": 0, "asked_at": now_iso()}]
    cur = conn.execute(
        "INSERT INTO interviews(job_key, status, transcript, created_at) VALUES(?,?,?,?)",
        (job_key, "active", json.dumps(transcript, ensure_ascii=False), now_iso()))
    conn.commit()
    return {"id": cur.lastrowid, "job_key": job_key, "title": row["title"],
            "total_questions": len(questions),
            "first_question": questions[0]["q"]}


def _load(session_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM interviews WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise llm.LLMError("面试会话不存在")
    return row, json.loads(row["transcript"] or "[]")


def _questions_of(transcript: list) -> list:
    return [t for t in transcript if t.get("role") == "interviewer"]


def answer(session_id: int, text: str, client=None) -> dict:
    """候选人作答 → 面试官点评 + 追问/下一题（题库取自 transcript 首条 bank）。"""
    row, transcript = _load(session_id)
    if row["status"] != "active":
        raise llm.LLMError("面试已结束")
    if not text or not text.strip():
        raise llm.LLMError("回答为空")
    transcript.append({"role": "candidate", "content": text.strip(),
                       "answered_at": now_iso()})
    conn = get_db()
    job, resume = _job_context(row["job_key"])
    asked = _questions_of(transcript)
    history = "\n".join(
        f"[{'面试官' if t['role'] == 'interviewer' else '我'}] {t['content']}"
        for t in transcript[-12:] if t.get("role") in ("interviewer", "candidate")
        and t.get("content"))
    user = (f"# 岗位\n{job['title']} | {job['company']}\n\n# 对话记录\n{history}\n\n"
            f"# 提示\n已问 {len(asked)} 题；上一条是我的最新回答。")
    out = llm.chat_json([{"role": "system", "content": TURN_SYSTEM},
                         {"role": "user", "content": user}], client=client)
    action = out.get("action", "next")
    content = (out.get("content") or "").strip()
    if not content:
        action, content = "next", "（面试官未给出内容，直接下一题）"
    if action == "next" and "面试结束" not in content:
        nxt = _next_question(transcript)
        content = nxt if nxt else "面试结束，请点击「生成报告」查看复盘。"
    transcript.append({"role": "interviewer", "content": content,
                       "action": action, "asked_at": now_iso()})
    conn.execute("UPDATE interviews SET transcript=? WHERE id=?",
                 (json.dumps(transcript, ensure_ascii=False), session_id))
    conn.commit()
    return {"feedback": out.get("feedback", ""), "action": action, "content": content}


def _next_question(transcript: list):
    """从 bank 中取第一个还没被问过的题。"""
    bank = next((t.get("questions", []) for t in transcript
                 if t.get("role") == "bank"), [])
    asked = {t.get("content") for t in _questions_of(transcript)}
    for q in bank:
        if q.get("q") and q["q"] not in asked:
            return q["q"]
    return None


def finish(session_id: int, client=None) -> dict:
    """生成终版报告并结束会话。"""
    row, transcript = _load(session_id)
    if not _questions_of(transcript):
        raise llm.LLMError("还没有任何问答")
    history = "\n".join(
        f"[{'面试官' if t['role'] == 'interviewer' else '我'}] {t['content']}"
        for t in transcript if t.get("role") in ("interviewer", "candidate")
        and t.get("content"))
    job, resume = _job_context(row["job_key"])
    user = (f"# 岗位\n{job['title']} | {job['company']} | {job['salary']}\n\n"
            f"# 我的简历（节选）\n{resume[:1500]}\n\n# 面试记录\n{history}")
    report = llm.chat_json([{"role": "system", "content": REPORT_SYSTEM},
                            {"role": "user", "content": user}], client=client)
    conn = get_db()
    conn.execute("UPDATE interviews SET status='finished', report=? WHERE id=?",
                 (json.dumps(report, ensure_ascii=False), session_id))
    conn.commit()
    return report


def get(session_id: int) -> dict:
    row, transcript = _load(session_id)
    return {"id": row["id"], "job_key": row["job_key"], "status": row["status"],
            "transcript": transcript,
            "report": json.loads(row["report"] or "{}")}


def list_sessions() -> list:
    rows = get_db().execute(
        "SELECT i.id, i.job_key, i.status, i.created_at, j.title, j.company "
        "FROM interviews i JOIN jobs j ON j.job_key=i.job_key "
        "ORDER BY i.id DESC LIMIT 20").fetchall()
    return [dict(r) for r in rows]
