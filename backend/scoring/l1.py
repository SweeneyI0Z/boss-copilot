"""L1 机器初筛：纯电算评分（无 LLM），规则出自《岗位筛选评分规则.md》。

岗位粗分(100) = A行业 + B1薪资(15) + B2结构(5) + C强度中性(9) + D晋升中性(5)
              + E1融资×规模(12) + E2技能成长估算 + E3业务前沿估算
匹配粗分(100) = 词典命中率基准分，受 Gate 上限约束，叠加电算加减分
综合粗分 = 匹配×0.6 + 岗位×0.4；P 级：≥75 P0 / 65-74.9 P1 / 55-64.9 P2 / <55 P3

设计原则（规则原文）：
- 缺失 ≠ 扣分：无文本证据的维度给中性分
- L1 只用电算信号；语义判断（业务实质、E2 精评）留给 L2
"""
import json
import re

from .. import config
from ..db import get_db, now_iso

# ── 期望基线（可被 profile.expectations 覆盖）────────────────────
DEFAULT_EXPECT = {"salary_min": 25, "salary_max": 30, "city": "深圳"}

# ── A 行业前景（L1 电算：行业字段映射 + 头部品牌加成，语义修正留 L2）──
_INDUSTRY_ANCHORS = [
    # 顺序即优先级：低分档的关键词要先于其超集词判断（「电子商务」含「电子」）
    (18, ("人工智能", "机器人", "智能驾驶", "自动驾驶")),
    (15, ("半导体", "芯片", "医疗器械")),
    (7, ("电子商务", "批发", "零售", "外包", "定制开发", "贸易", "广告", "传媒")),
    (11, ("电子", "硬件开发", "计算机软件", "物联网", "智能硬件",
          "消费电子", "自动化", "新能源", "通讯", "通信")),
]
_TOP_BRANDS = ("华为", "大疆", "绿联", "Anker", "影石", "Insta360", "OPPO", "vivo",
               "小米", "比亚迪", "宁德时代", "迈瑞", "华大")


def industry_score(industry: str, company: str = "") -> float:
    text = industry or ""
    for score, kws in _INDUSTRY_ANCHORS:
        if any(k in text for k in kws):
            base = float(score)
            # 头部品牌上探一档（绿联/Anker 这类「智能硬件头部」落在 15）
            if score >= 11 and any(b in (company or "") for b in _TOP_BRANDS):
                base = min(base + 3, 17)
            return base
    return 9.0   # 未标注/其他：中性


# ── B1 薪资水平（15，按区间中值 vs 期望上限）──────────────────────
def salary_score(salary_min, salary_max, expect_max=30) -> float:
    if salary_min is None or salary_max is None:
        return 2.0   # 面议/解析失败
    mid = (salary_min + salary_max) / 2
    for bound, score in ((40, 15), (35, 13), (30, 11), (25, 9), (20, 6), (15, 4)):
        if mid >= bound:
            return float(score)
    return 2.0


# ── B2 薪资结构（5）─────────────────────────────────────────────
def structure_score(salary_text: str, months) -> float:
    text = salary_text or ""
    if "面议" in text or "上不封顶" in text:
        return 2.0
    if months is None:
        return 3.0   # 未标注中性
    if months >= 16:
        return 5.0
    if months >= 14:
        return 4.5
    if months >= 13:
        return 4.0
    return 3.0


# ── E1 公司成长性（12，融资 × 规模矩阵）──────────────────────────
def e1_score(stage: str, scale: str) -> float:
    stage, scale = stage or "", scale or ""
    big = ("10000" in scale) or ("万人" in scale)
    mid_ = ("1000-9999" in scale) or ("千-万" in scale)
    small = ("100-499" in scale) or ("100-999" in scale)
    tiny20 = "20-99" in scale
    tiny0 = "0-20" in scale
    if "上市" in stage:
        return 12.0 if big else (11.0 if mid_ else 10.0)
    if any(k in stage for k in ("D轮", "E轮", "Pre-IPO")):
        return 10.0
    if "不需要融资" in stage:
        if big or mid_:
            return 10.0
        return 8.0 if small else (7.0 if tiny20 else (5.0 if tiny0 else 8.0))
    if "C轮" in stage or "B轮" in stage:
        return 10.0 if (big or mid_ or small) else 8.5
    if "A轮" in stage:
        return 8.0 if not (tiny20 or tiny0) else 6.5
    if "天使" in stage or "未融资" in stage:
        return 5.0 if (tiny20 or tiny0) else 6.0
    return 6.0   # 未标注中性


# ── E2/E3 电算估算（L2 会精评覆盖）───────────────────────────────
def e2_estimate(skills_jd: str) -> float:
    text = skills_jd or ""
    if re.search(r"Agent|LLM|大模型|RAG|ROS|运动控制", text, re.I):
        return 10.0
    if re.search(r"RTOS|Linux|多协议|音视频|图形", text, re.I):
        return 8.5
    if re.search(r"STM32|MCU|单片机|物联网|IoT", text, re.I):
        return 7.0
    if re.search(r"测试|维护", text):
        return 5.0
    return 6.0


def e3_estimate(industry: str, skills_jd: str) -> float:
    text = (industry or "") + (skills_jd or "")
    if re.search(r"Agent|LLM|大模型|机器人|人工智能|医疗器械", text, re.I):
        return 5.0
    if re.search(r"智能硬件|IoT|物联网", text, re.I):
        return 3.5
    return 2.0


# ── Gate 硬门槛 ──────────────────────────────────────────────────
_G4_PURE_ALGO = re.compile(r"模型训练|深度学习|算法工程师|机器学习|CV|NLP|推荐算法|AIGC|数字人|短视频|小游戏|内容制作|工具运营")

def gates(job: dict, jd_text: str = "") -> tuple[float, list]:
    """返回 (匹配度上限, 触发说明列表)；多项触发取最低。"""
    caps, hits = [], []
    degree = job.get("degree") or ""
    if "硕士" in degree and "博士" not in degree:
        caps.append(70)
        hits.append("G1-要求硕士")
    exp = job.get("experience") or ""
    if "5-10年" in exp:
        caps.append(85); hits.append("G2-要求5-10年")
    elif "10年以上" in exp:
        caps.append(60); hits.append("G2-要求10年以上")
    elif exp in ("在校/应届", "应届生"):
        caps.append(75); hits.append("G2-应届专属岗")
    smax = job.get("salary_max")
    if smax is not None:
        if smax < 20:
            caps.append(65); hits.append("G3-薪资上限<20K")
        elif smax < 25:
            caps.append(80); hits.append("G3-薪资上限<25K")
    blob = " ".join([job.get("title") or "", job.get("skills") or "", jd_text[:400]])
    if _G4_PURE_ALGO.search(blob):
        caps.append(50); hits.append("G4-纯算法/非技术岗")
    return (min(caps) if caps else 100.0), hits


# ── 匹配粗分（词典命中率 + 电算加减分）────────────────────────────
_SUBTRACT = [
    (re.compile(r"Linux驱动|内核开发"), -8, "要求Linux驱动/内核"),
    (re.compile(r"模型训练|深度学习|调优|部署经验"), -10, "要求模型训练"),
    (re.compile(r"Java|Go后端|golang"), -10, "主体技能冲突(Java/Go)"),
    (re.compile(r"高并发|大规模后端"), -5, "要求大规模后端"),
]
_ADD = [
    (re.compile(r"医疗器械|医疗产品"), 5, "医疗器械公司"),
]

def match_rough(job: dict, jd_text: str, dictionary: dict, cap: float,
                expect: dict) -> tuple:
    text = " ".join([job.get("title") or "", job.get("skills") or "",
                     (jd_text or "")[:1500]])
    half = {str(word) for word in dictionary.get("half_weight", [])}
    hits = 0.0
    per_cat = {}
    for cat in ("embedded", "comm_iot", "hardware", "ai_soft", "domain_algo", "general"):
        words = dictionary.get(cat, [])
        cat_hits = 0.0
        for w in words:
            word = str(w)
            if word.lower() in text.lower():
                cat_hits += 0.5 if word in half else 1.0
        per_cat[cat] = round(cat_hits, 1)
        hits += cat_hits
    coverage = min(hits / 14.0, 1.0)          # 14 个加权命中即视为满覆盖（可调参数）
    score = 55 + 45 * coverage
    notes = []
    for pat, delta, why in _SUBTRACT:
        if pat.search(text):
            score += delta
            notes.append(f"{why}{delta}")
    for pat, delta, why in _ADD:
        if pat.search(text):
            score += delta
            notes.append(f"{why}+{delta}")
    if hits >= 8:
        score += 5
        notes.append("标签命中≥8 +5")
    if re.search(r"经验不限|1-3年", job.get("experience") or "") and \
       re.search(r"Agent|LLM|AI", text, re.I):
        score += 3
        notes.append("低门槛AI岗 +3")
    score = max(0.0, min(score, cap))
    return round(score, 1), {"coverage": round(coverage, 2), "hits": per_cat,
                             "cap": cap, "adjust": notes}


# ── 单岗评分 ────────────────────────────────────────────────────
def score_job(job: dict, jd_text: str, dictionary: dict, expect: dict) -> dict:
    A = industry_score(job.get("industry", ""), job.get("company", ""))
    B1 = salary_score(job.get("salary_min"), job.get("salary_max"),
                      expect.get("salary_max", 30))
    B2 = structure_score(job.get("salary", ""), job.get("salary_months"))
    E1 = e1_score(job.get("stage", ""), job.get("scale", ""))
    skills_blob = (job.get("skills") or "") + " " + (jd_text or "")[:800]
    E2, E3 = e2_estimate(skills_blob), e3_estimate(job.get("industry", ""), skills_blob)
    job_rough = round(A + B1 + B2 + 9 + 5 + E1 + E2 + E3, 1)

    cap, gate_hits = gates(job, jd_text)
    mr, match_detail = match_rough(job, jd_text, dictionary, cap, expect)
    composite = round(mr * 0.6 + job_rough * 0.4, 1)
    priority = ("P0" if composite >= 75 else
                "P1" if composite >= 65 else
                "P2" if composite >= 55 else "P3")
    return {
        "job_rough": job_rough, "match_rough": mr,
        "composite_rough": composite, "priority_rough": priority,
        "l1_detail": {"A": A, "B1": B1, "B2": B2, "E1": E1, "E2est": E2, "E3est": E3,
                      "C": 9, "D": 5, "gate": gate_hits, "cap": cap,
                      "match": match_detail, "engine": "l1"},
    }


# ── 全量执行（只填空值；force=True 时重算并覆盖 L1 自身）──────────
def _merge_resume_dictionary(skill_profile: dict) -> dict:
    """构造当前简历技能词典；空画像才沿用旧版单简历基线。"""
    legacy_dictionary = json.loads(
        get_db().execute("SELECT value FROM settings WHERE key='skill_dictionary'")
        .fetchone()[0]) if get_db().execute(
        "SELECT 1 FROM settings WHERE key='skill_dictionary'").fetchone() else \
        config.DEFAULT_SKILL_DICTIONARY
    profile = skill_profile if isinstance(skill_profile, dict) else {}
    if not profile:
        return {k: list(v) for k, v in legacy_dictionary.items()}
    merged = {category: [] for category in
              ("embedded", "comm_iot", "hardware", "ai_soft", "domain_algo",
               "general", "half_weight")}
    for category in ("embedded", "comm_iot", "hardware", "ai_soft", "domain_algo",
                     "half_weight"):
        values = profile.get(category, [])
        if isinstance(values, str):
            values = [x.strip() for x in re.split(r"[,，|、\n]", values) if x.strip()]
        if isinstance(values, list):
            merged[category] = list(dict.fromkeys(values))
    # 简单画像的 skills 属于通用命中，不能全部误归类为 AI 技能。
    generic = profile.get("skills", [])
    if isinstance(generic, str):
        generic = [x.strip() for x in re.split(r"[,，|、\n]", generic) if x.strip()]
    if isinstance(generic, list):
        merged["general"] = list(dict.fromkeys(generic))
    seen = set()
    for category in ("embedded", "comm_iot", "hardware", "ai_soft", "domain_algo",
                     "general"):
        unique = []
        for word in merged[category]:
            marker = str(word).lower()
            if marker not in seen:
                unique.append(word)
                seen.add(marker)
        merged[category] = unique
    return merged


def _resume_context(resume_id: int) -> tuple:
    from .. import resumes
    resume = resumes.get_resume(resume_id)
    if resume.get("archived"):
        raise ValueError("已归档简历不能用于新评分")
    expect = {**DEFAULT_EXPECT, **(resume.get("expectations") or {})}
    dictionary = _merge_resume_dictionary(resume.get("skill_profile") or {})
    return resume, expect, dictionary


def run_l1(force: bool = False, keep_imported: bool = False,
           resume_id: int = None, job_keys: list = None) -> dict:
    """执行 L1。

    未传 resume_id 时保留旧版写 jobs 的行为；显式传入时把结果写入
    岗位×简历×修订表，导入基线只读且永不被引擎覆盖。
    """
    conn = get_db()
    profile_mode = resume_id is not None
    if profile_mode:
        resume, expect, dictionary = _resume_context(int(resume_id))
        revision = int(resume["revision"])
    else:
        dictionary = _merge_resume_dictionary({})
        prof = conn.execute("SELECT expectations FROM profile WHERE id=1").fetchone()
        expect = {**DEFAULT_EXPECT, **(json.loads(prof["expectations"] or "{}"))}
        revision = None

    where, args = ["j.status != 'excluded'"], []
    if job_keys:
        where.append("j.job_key IN (%s)" % ",".join("?" * len(job_keys)))
        args.extend(job_keys)
    rows = conn.execute(
        "SELECT j.*, d.jd FROM jobs j LEFT JOIN job_details d ON d.job_key=j.job_key "
        "WHERE " + " AND ".join(where), args).fetchall()
    scored = skipped = 0
    for r in rows:
        is_imported = "imported" in (r["l1_detail"] or "")
        if profile_mode:
            existing = conn.execute(
                "SELECT l1_score,composite FROM job_resume_scores WHERE job_key=? AND resume_id=? "
                "AND resume_revision=?", (r["job_key"], resume_id, revision)).fetchone()
            if not force and existing and existing["l1_score"] is not None:
                skipped += 1
                continue
        else:
            if keep_imported and is_imported:
                skipped += 1
                continue
            if not force and r["l1_score"] is not None:
                skipped += 1
                continue
        result = score_job(dict(r), r["jd"] or "", dictionary, expect)
        detail_json = json.dumps(result["l1_detail"], ensure_ascii=False)
        if profile_mode:
            from .. import resumes
            score_fields = {
                "l1_score": result["job_rough"], "l1_detail": result["l1_detail"],
                "match_rough": result["match_rough"],
                "composite_rough": result["composite_rough"], "l1_source": "engine",
            }
            if not existing or existing["composite"] is None:
                score_fields["priority"] = result["priority_rough"]
            resumes.save_job_score(
                r["job_key"], resume_id=resume_id, resume_revision=revision,
                **score_fields)
        else:
            conn.execute(
                "UPDATE jobs SET l1_score=?, l1_detail=?, match_rough=?, composite_rough=? "
                "WHERE job_key=?",
                (result["job_rough"], detail_json, result["match_rough"],
                 result["composite_rough"], r["job_key"]))
        scored += 1
    conn.commit()
    return {"scored": scored, "skipped": skipped, "total": len(rows),
            "resume_id": resume_id, "resume_revision": revision}
