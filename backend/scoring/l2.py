"""L2 LLM 精评：逐条读 JD 全文，按 T1/T2/T3 模板打分 + 应聘建议。

分工原则：LLM 只做语义判断（维度分/加减分/优势GAP/建议），
所有算术（岗位分求和、匹配度合成、综合分、P级）由代码计算，保证确定性。
"""
import json

from .. import llm
from ..db import get_db, now_iso

SYSTEM_PROMPT = """你是资深求职评委，按《岗位筛选评分规则》对单个岗位做 L2 精评。

## 岗位评分维度（合计 100）
- A 行业前景(20)：18-20 AI/机器人/智驾核心产品；14-17 半导体/医疗器械/智能硬件头部品牌；10-13 电子/计算机软件/IoT/新能源；6-9 电商/传统行业/外包；2-5 挂AI名无技术沉淀
- B1 薪资水平(15)：按区间中值对比期望上限 {expect_max}K——≥40→15, 35-40→13, 30-35→11, 25-30→9, 20-25→6, 15-20→4, 其他→2
- B2 薪资结构(5)：≥16薪→5, 14-15薪→4.5, 13薪→4, 12薪/未标→3, 面议→2
- B3 福利文本(5)：JD 逐类计1分（社保/激励/假期/生活/长期，五类封顶）；缺失=2.5
- C 工作强度(15)：明确双休+弹性→13-15；明确双休→12；无信号→9；加班有补偿→7；加班无说明→6；大小周→4；996暗示→2
- D 晋升机制(10)：明确通道/双通道→8-10；培训/导师→6-7；无提及→5
- E1 公司成长(12)：{e1_hint}
- E2 技能成长(12)：Agent/LLM应用/ROS/复杂协议栈→11-12；RTOS/Linux进阶→9-10；常规STM32产品→7-8；单一模块/纯测试→4-6
- E3 业务前沿(6)：AI落地/机器人/创新医疗→5-6；常规智能硬件→3-4；传统→1-2

## 匹配度 S 维度（按岗位类型选模板，合计 100）
T1 嵌入式岗: S1嵌入式核心技能40 S2产品领域对口20 S3差异化加分15 S4学历专业10 S5工程成熟度15
T2 AI应用岗: S1 AI工程技能35 S2软件工程25 S3背景溢价15（JD涉智能硬件/IoT=15,纯互联网=4-8） S4转型证据15 S5门槛适配10
T3 交叉岗:   S1嵌入式基础35 S2 AI应用能力35 S3交叉场景价值30（S3按JD交叉贴合度20-30，其余维度并入S1/S2评）

诚实校准：简历没有生产级 RAG/LLM 上线经验时，S1(T2) 通常 18-26，不要客套给高分。

## 加减分（匹配度，数组列出）
减分例：要求Linux驱动-8 / 模型训练-10 / 主体Java/Go-10 / 大规模后端-5
加分例：医疗器械公司+5 / 技能标签命中≥8 +5 / 「经验不限」AI岗+3 / AI提效文化+3

## 输出 JSON（只输出 JSON）
{{
 "type": "T1|T2|T3",
 "dims": {{"A":0,"B1":0,"B2":0,"B3":0,"C":0,"D":0,"E1":0,"E2":0,"E3":0}},
 "s": {{"S1":0,"S2":0,"S3":0,"S4":0,"S5":0}},
 "adjust": [{{"delta":5,"reason":"医疗器械公司"}}],
 "gate_note": "若触发硬门槛写明（如要求硕士）",
 "strengths": ["对照 JD 的突出优势，2-4 条，具体到简历事实"],
 "gaps": [{{"gap":"硬缺口","action":"面试前怎么补/话术怎么绕"}}],
 "resume_advice": "投这个岗位时简历要突出/补齐哪些关键词与项目侧重（1-2句）",
 "greeting_angle": "打招呼时最值得提的 1 个匹配点（一句话）",
 "summary": "总评一句话",
 "advice": "投递建议（强投/正常投/谨慎/不投 + 原因）"
}}"""

E1_HINT = "已上市:万人12/千人11/其他10；D轮+→10；不需要融资:千人+10/百人8/几十人7/0-20人5；B/C轮8-10；A轮6-8；天使/未融资3-6"


def _resume_digest() -> str:
    row = get_db().execute("SELECT resume_text FROM profile WHERE id=1").fetchone()
    text = (row["resume_text"] or "").strip()
    if len(text) < 50:
        raise llm.LLMError("简历未填写或过短：请先在「简历档案」粘贴简历（L2 精评以简历为基线）")
    return text[:3000]


def build_messages(job: dict, jd: str, resume: str, l1_detail: dict, expect: dict) -> list:
    info = {k: job.get(k, "") for k in
            ("title", "company", "salary", "experience", "degree", "location",
             "industry", "scale", "stage", "skills", "hr_active")}
    l1_electric = {k: l1_detail.get(k) for k in ("A", "B1", "B2", "E1", "E2est", "cap")
                   if isinstance(l1_detail, dict)}
    system = SYSTEM_PROMPT.format(expect_max=expect.get("salary_max", 30), e1_hint=E1_HINT)
    user = (f"# 岗位信息\n{json.dumps(info, ensure_ascii=False)}\n\n"
            f"# JD 全文\n{(jd or '（无 JD，按标题与技能标签判断）')[:2500]}\n\n"
            f"# L1 电算参考（可直接采用电算值，有文本证据才改）\n"
            f"{json.dumps(l1_electric or {}, ensure_ascii=False)}\n\n"
            f"# 我的简历\n{resume}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def finalize(result: dict, cap: float) -> dict:
    """代码侧合成最终分：岗位分=Σdims；匹配度=min(cap, Σs+Σadjust)；综合=0.6匹配+0.4岗位。"""
    dims = {k: float(v) for k, v in (result.get("dims") or {}).items()
            if isinstance(v, (int, float))}
    s = {k: float(v) for k, v in (result.get("s") or {}).items()
         if isinstance(v, (int, float))}
    adjust = 0.0
    for a in result.get("adjust") or []:
        try:
            adjust += float(a.get("delta", 0))
        except (TypeError, ValueError):
            continue
    job_score = round(min(sum(dims.values(), 0.0), 100.0), 1)
    match_raw = sum(s.values(), 0.0) + adjust
    match_score = round(max(0.0, min(match_raw, cap)), 1)
    composite = round(match_score * 0.6 + job_score * 0.4, 1)
    priority = ("P0" if composite >= 75 else
                "P1" if composite >= 65 else
                "P2" if composite >= 55 else "P3")
    result.update({"job_score": job_score, "match_score": match_score,
                   "composite": composite, "priority": priority})
    return result


def score_job_llm(job_key: str, client=None) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT j.*, d.jd FROM jobs j LEFT JOIN job_details d ON d.job_key=j.job_key "
        "WHERE j.job_key=?", (job_key,)).fetchone()
    if row is None:
        raise llm.LLMError(f"岗位不存在: {job_key}")
    resume = _resume_digest()
    l1_detail = json.loads(row["l1_detail"] or "{}")
    expect = {"salary_max": 30}
    result = llm.chat_json(build_messages(dict(row), row["jd"] or "", resume,
                                          l1_detail, expect), client=client)
    result = finalize(result, cap=float(l1_detail.get("cap", 100)))
    result["engine"] = "l2-llm"
    cur = conn.execute(
        "UPDATE jobs SET job_score=?, match_score=?, composite=?, priority=?, "
        "l2_detail=?, l2_source='llm', last_seen_at=? WHERE job_key=? AND composite IS NULL",
        (result["job_score"], result["match_score"], result["composite"],
         result["priority"], json.dumps(result, ensure_ascii=False),
         now_iso(), job_key))
    if cur.rowcount == 0:
        raise llm.LLMError("该岗位已有 L2 评分，未覆盖（需 force 重评）")
    conn.commit()
    return result


def run_l2(limit: int = 10, only_missing: bool = True, client=None) -> dict:
    """对 L1 综合粗分 Top N 且无 L2 的岗位精评。"""
    conn = get_db()
    where = "composite IS NULL" if only_missing else "1=1"
    rows = conn.execute(
        f"SELECT job_key FROM jobs WHERE status='active' AND {where} "
        "AND composite_rough IS NOT NULL "
        "ORDER BY composite_rough DESC LIMIT ?", (limit,)).fetchall()
    done, failed = [], []
    for r in rows:
        try:
            res = score_job_llm(r["job_key"], client=client)
            done.append({"job_key": r["job_key"], "title": res.get("summary", "")[:40],
                         "composite": res["composite"], "priority": res["priority"]})
        except llm.LLMError as e:
            failed.append({"job_key": r["job_key"], "error": str(e)[:120]})
    return {"scored": len(done), "failed": failed, "items": done}
