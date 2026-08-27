"""岗位数据分析。

默认分布覆盖全部当前可用岗位，避免历史导入或收藏同步的数据因缺少来源关系而
无法分析；关键词、城市、日期和趋势仍只读取可靠的 job_collection_hits。

维度筛选（月薪区间/经验/学历/行业/规模/猎头）会同时作用于可靠命中查询、
默认总览查询和趋势查询；候选刻面选项遵循互斥语义：统计某一维度的候选项时
排除该维度自身的筛选条件，但保留其余全部条件。
"""
from collections import defaultdict

from .collection_runs import visible_jobs_clause
from .db import get_db


# 可多选交叉筛选的岗位属性维度（列名与 jobs 表一致）。
DIMENSION_FIELDS = ("experience", "degree", "industry", "scale")


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _multi(value) -> list[str]:
    """多维筛选值归一：字符串按逗号拆分，序列逐项保留，丢弃空项。"""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        items = [str(value).strip()]
    return [item for item in items if item]


def _number(value):
    """把查询参数解析为数值，非法输入返回 None（忽略该条件）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _where(filters: dict, alias: str = "h", current_only: bool = None) -> tuple[list[str], list]:
    if current_only is None:
        current_only = not (filters.get("date_from") or filters.get("date_to"))
    clauses = [f"{alias}.is_active=1"] if current_only else []
    args = []
    for field in ("keyword", "city_code", "city"):
        values = _as_list(filters.get(field))
        if values:
            clauses.append(f"{alias}.{field} IN ({','.join('?' for _ in values)})")
            args.extend(values)
    if filters.get("date_from"):
        clauses.append(f"substr({alias}.last_seen_at,1,10)>=?")
        args.append(str(filters["date_from"])[:10])
    if filters.get("date_to"):
        clauses.append(f"substr({alias}.last_seen_at,1,10)<=?")
        args.append(str(filters["date_to"])[:10])
    return clauses, args


def _dim_groups(filters: dict) -> dict[str, tuple[list[str], list]]:
    """维度筛选条件按维度分组（SQL 片断引用 jobs 别名 j），便于互斥组合。"""
    groups: dict[str, tuple[list[str], list]] = {}
    salary_clauses, salary_args = [], []
    midpoint = "((j.salary_min+j.salary_max)/2)"
    for key, bound in (("salary_min", ">="), ("salary_max", "<=")):
        value = _number(filters.get(key))
        if value is not None:
            salary_clauses.append(f"{midpoint}{bound}?")
            salary_args.append(value)
    groups["salary"] = (salary_clauses, salary_args)
    for field in DIMENSION_FIELDS:
        values = _multi(filters.get(field))
        clauses, args = [], []
        if values:
            clauses.append(f"j.{field} IN ({','.join('?' for _ in values)})")
            args.extend(values)
        groups[field] = (clauses, args)
    headhunter = str(filters.get("headhunter") or "").strip()
    hh_clauses, hh_args = [], []
    if headhunter in ("0", "1"):
        hh_clauses.append(
            "COALESCE(j.headhunter_override,j.is_headhunter,0)=?")
        hh_args.append(int(headhunter))
    groups["headhunter"] = (hh_clauses, hh_args)
    return groups


def _score_join(resume_id=None) -> tuple[str, list]:
    conn = get_db()
    if resume_id is None:
        resume = conn.execute(
            "SELECT id,revision FROM resumes WHERE is_default=1 AND archived_at IS NULL "
            "ORDER BY id LIMIT 1").fetchone()
    else:
        resume = conn.execute(
            "SELECT id,revision FROM resumes WHERE id=? AND archived_at IS NULL",
            (int(resume_id),)).fetchone()
    if resume is None:
        return "", []
    return (
        "LEFT JOIN job_resume_scores s ON s.job_key=j.job_key "
        "AND s.resume_id=? AND s.resume_revision=?",
        [resume["id"], resume["revision"]],
    )


def _distribution(values, empty_label="未标注") -> list[dict]:
    counts = defaultdict(int)
    for value in values:
        label = str(value or "").strip() or empty_label
        counts[label] += 1
    return [{"label": label, "count": count}
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _numeric_buckets(values, buckets) -> list[dict]:
    counts = [0] * len(buckets)
    unknown = 0
    for raw in values:
        if raw is None:
            unknown += 1
            continue
        value = float(raw)
        matched = False
        for index, (_, lower, upper) in enumerate(buckets):
            if value >= lower and (upper is None or value < upper):
                counts[index] += 1
                matched = True
                break
        if not matched:
            unknown += 1
    result = [{"label": bucket[0], "count": counts[index]}
              for index, bucket in enumerate(buckets)]
    if unknown:
        result.append({"label": "未标注", "count": unknown})
    return result


def _group_distinct(rows: list[dict], field: str, empty_label="未标注") -> list[dict]:
    grouped = defaultdict(set)
    for row in rows:
        grouped[str(row.get(field) or "").strip() or empty_label].add(row["job_key"])
    return [{"label": label, "count": len(keys)}
            for label, keys in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))]


def _quantile(sorted_values, ratio: float):
    """线性插值分位数；空样本返回 None。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(float(sorted_values[0]), 1)
    position = (len(sorted_values) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return round(sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight, 1)


def aggregate(filters: dict = None, resume_id=None) -> dict:
    """返回分析页全部聚合；支持关键词/城市/日期与多维交叉筛选。"""
    filters = dict(filters or {})
    if resume_id is None:
        resume_id = filters.get("resume_id")
    source_filters_active = any(
        _as_list(filters.get(field)) for field in ("keyword", "city_code", "city")
    ) or bool(filters.get("date_from") or filters.get("date_to"))
    base_clauses, base_args = _where(filters)
    score_join, score_args = _score_join(resume_id)
    score_select = (
        "COALESCE(s.job_score,s.l1_score) job_score,"
        "COALESCE(s.match_score,s.match_rough) match_score,"
        "COALESCE(s.composite,s.composite_rough) composite,s.priority priority"
        if score_join else
        "j.job_score,j.match_score,j.composite,j.priority"
    )
    dim_groups = _dim_groups(filters)

    def dim_conditions(exclude_dims=frozenset()) -> tuple[list[str], list]:
        clauses, args = [], []
        for name, group in dim_groups.items():
            if name in exclude_dims:
                continue
            clauses.extend(group[0])
            args.extend(group[1])
        return clauses, args

    # 岗位属性列统一从 jobs 表取（别名 j），供分布/榜单/漏斗复用。
    # 岗位属性列统一从 jobs 表取（别名 j），供分布/榜单/漏斗复用。
    # 占位符顺序：score_join 在先，随后是维度附加条件。
    reliable_base = (
        f"SELECT h.job_key,h.keyword,h.province,h.city,h.city_code,h.last_seen_at,"
        "j.company,j.skills,j.salary_min,j.salary_max,j.salary_months,j.experience,"
        "j.degree,j.industry,j.scale,j.status,"
        "COALESCE(j.headhunter_override,j.is_headhunter,0) headhunter,"
        f"{score_select} FROM job_collection_hits h "
        "JOIN collect_runs cr ON cr.id=h.run_id AND cr.enabled=1 "
        "JOIN jobs j ON j.job_key=h.job_key "
        f"{score_join} WHERE {' AND '.join(base_clauses)} AND j.status<>'excluded'"
    )

    def fetch_reliable(exclude_dims=frozenset()) -> list[dict]:
        extra_clauses, extra_args = dim_conditions(exclude_dims)
        sql = reliable_base
        if extra_clauses:
            sql += " AND " + " AND ".join(extra_clauses)
        # 占位符顺序与 SQL 文本一致：评分 join 在先，随后来源筛选与维度筛选。
        return [dict(row) for row in get_db().execute(
            sql, [*score_args, *base_args, *extra_args]).fetchall()]

    # 命中级行不去重：来源覆盖、by_keyword、by_city 都依赖多命中关系。
    reliable_rows = fetch_reliable()

    def fetch_current(exclude_dims=frozenset()) -> list[dict]:
        current_base = (
            "SELECT j.job_key,'' keyword,'' province,j.location city,'' city_code,"
            "j.last_seen_at,j.company,j.skills,j.salary_min,j.salary_max,"
            "j.salary_months,j.experience,j.degree,j.industry,j.scale,j.status,"
            "COALESCE(j.headhunter_override,j.is_headhunter,0) headhunter,"
            f"{score_select} FROM jobs j {score_join} "
            "WHERE j.status NOT IN ('excluded','delisted') AND "
            f"{visible_jobs_clause('j')}"
        )
        extra_clauses, extra_args = dim_conditions(exclude_dims)
        if extra_clauses:
            current_base += " AND " + " AND ".join(extra_clauses)
        return [dict(row) for row in get_db().execute(
            current_base, [*score_args, *extra_args]).fetchall()]

    def load_unique(exclude_dims=frozenset()) -> list[dict]:
        """按当前 scope 装载去重后的岗位行（每岗位一行）。"""
        rows = (fetch_reliable(exclude_dims) if source_filters_active
                else fetch_current(exclude_dims))
        jobs = {}
        for row in rows:
            jobs.setdefault(row["job_key"], row)
        return list(jobs.values())

    unique = load_unique()
    reliable_job_keys = {row["job_key"] for row in reliable_rows}
    reliable_jobs = sum(row["job_key"] in reliable_job_keys for row in unique)
    unattributed_jobs = len(unique) - reliable_jobs

    def monthly_of(row) -> float | None:
        if row["salary_min"] is None or row["salary_max"] is None:
            return None
        midpoint = (float(row["salary_min"]) + float(row["salary_max"])) / 2
        return round(midpoint, 1)

    monthly = []
    annual = []
    for row in unique:
        midpoint = monthly_of(row)
        monthly.append(midpoint)
        annual.append(None if midpoint is None else
                      round(midpoint * float(row["salary_months"] or 12), 1))
    known_monthly = sorted(value for value in monthly if value is not None)
    known_annual = [value for value in annual if value is not None]
    headhunters = sum(bool(row["headhunter"]) for row in unique)

    # 趋势只看可靠命中时间线，但同样服从维度筛选，保持全图联动。
    trend_extra = dim_conditions()
    trend_clauses, trend_args = _where(dict(filters), "h", current_only=False)
    trend_sql = (
        "SELECT substr(h.last_seen_at,1,10) day,COUNT(DISTINCT h.job_key) count "
        "FROM job_collection_hits h "
        "JOIN collect_runs cr ON cr.id=h.run_id AND cr.enabled=1 "
        "JOIN jobs j ON j.job_key=h.job_key WHERE "
        + (" AND ".join([*trend_clauses, *trend_extra[0]]) + " AND "
           if trend_clauses or trend_extra[0] else "")
        + "j.status<>'excluded' GROUP BY day ORDER BY day")
    trend = [dict(row) for row in get_db().execute(
        trend_sql, [*trend_args, *trend_extra[1]]).fetchall()]

    salary_monthly = _numeric_buckets(monthly, [
        ("10K以下", 0, 10), ("10-20K", 10, 20), ("20-30K", 20, 30),
        ("30-50K", 30, 50), ("50K以上", 50, None),
    ])
    salary_annual = _numeric_buckets(annual, [
        ("20万以下", 0, 200), ("20-30万", 200, 300), ("30-50万", 300, 500),
        ("50-80万", 500, 800), ("80万以上", 800, None),
    ])
    distributions = {
        "salary": salary_monthly, "annual_salary": salary_annual,
        "experience": _distribution(row["experience"] for row in unique),
        "degree": _distribution(row["degree"] for row in unique),
        "industry": _distribution(row["industry"] for row in unique),
        "scale": _distribution(row["scale"] for row in unique),
        "job_score": _numeric_buckets([row["job_score"] for row in unique], [
            ("60分以下", 0, 60), ("60-70分", 60, 70), ("70-80分", 70, 80),
            ("80-90分", 80, 90), ("90分以上", 90, None),
        ]),
        "priority": _distribution(row["priority"] for row in unique),
    }
    keywords = sorted({row["keyword"] for row in reliable_rows if row["keyword"]})
    city_values = sorted(
        {row["city_code"]: {"city": row["city"], "city_code": row["city_code"]}
         for row in reliable_rows if row["city_code"]}.values(),
        key=lambda item: item["city"])
    salary_mins = [float(row["salary_min"]) for row in unique
                   if row["salary_min"] is not None]
    salary_maxes = [float(row["salary_max"]) for row in unique
                    if row["salary_max"] is not None]
    headhunter_rate = round(headhunters / len(unique) * 100, 1) if unique else 0

    # 候选刻面：排除自身维度后统计其余条件下各取值的岗位数（互斥筛选语义）。
    options = {}
    for field in DIMENSION_FIELDS:
        facet_rows = load_unique(frozenset({field}))
        facet_counts: dict[str, int] = defaultdict(int)
        for row in facet_rows:
            value = str(row[field] or "").strip()
            if value:
                facet_counts[value] += 1
        selected = set(_multi(filters.get(field)))
        options[field] = [{"label": label, "count": count,
                           "selected": label in selected}
                          for label, count in sorted(
                              facet_counts.items(), key=lambda item: (-item[1], item[0]))]

    def category_avg_salary(field: str) -> list[dict]:
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in unique:
            midpoint = monthly_of(row)
            if midpoint is None:
                continue
            grouped[str(row[field] or "").strip() or "未标注"].append(midpoint)
        return [{"label": label, "value": round(sum(values) / len(values), 1),
                 "count": len(values)}
                for label, values in sorted(
                    grouped.items(), key=lambda item: (-len(item[1]), item[0]))]

    cross = {
        "experience_avg_salary": category_avg_salary("experience"),
        "degree_avg_salary": category_avg_salary("degree"),
        "industry_avg_salary": category_avg_salary("industry"),
    }

    top_companies_rows: dict[str, set] = defaultdict(set)
    for row in unique:
        top_companies_rows[str(row.get("company") or "").strip() or "未知公司"].add(
            row["job_key"])
    top_companies = [{"label": company, "value": len(keys)}
                     for company, keys in sorted(
                         top_companies_rows.items(),
                         key=lambda item: (-len(item[1]), item[0]))][:10]
    skill_counts: dict[str, int] = defaultdict(int)
    for row in unique:
        skills = str(row.get("skills") or "")
        for skill in {part.strip().lower() for part in skills.split("|") if part.strip()}:
            skill_counts[skill] += 1
    top_skills = [{"label": skill, "value": count} for skill, count in sorted(
        skill_counts.items(), key=lambda item: (-item[1], item[0]))][:10]

    funnel = _funnel_report(filters, resume_id, unique)

    summary = {
        "jobs": len(unique), "active_relations": len(reliable_rows),
        "reliable_jobs": reliable_jobs, "unattributed_jobs": unattributed_jobs,
        "source_coverage_rate": round(reliable_jobs / len(unique) * 100, 1)
        if unique else 0,
        "scope": "reliable_filtered" if source_filters_active else "all_current",
        "headhunter_jobs": headhunters, "headhunter_rate": headhunter_rate,
        "headhunter_ratio": headhunter_rate,
        "monthly_salary_avg_k": round(sum(known_monthly) / len(known_monthly), 1)
        if known_monthly else None,
        "annual_salary_avg_k": round(sum(known_annual) / len(known_annual), 1)
        if known_annual else None,
        "median_monthly_salary_k": _quantile(known_monthly, 0.5),
        "p25_monthly_salary_k": _quantile(known_monthly, 0.25),
        "p75_monthly_salary_k": _quantile(known_monthly, 0.75),
        "avg_salary_min": round(sum(salary_mins) / len(salary_mins), 1)
        if salary_mins else None,
        "avg_salary_max": round(sum(salary_maxes) / len(salary_maxes), 1)
        if salary_maxes else None,
        "keywords": len(keywords), "cities": len(city_values),
    }
    return {
        "summary": summary,
        "distributions": distributions,
        "meta": {"keywords": keywords,
                 "cities": [{"name": item["city"], "code": item["city_code"]}
                            for item in city_values],
                 "options": options},
        "trends": [{"label": item["day"], "count": item["count"]} for item in trend],
        # 同时保留语义更明确的字段，便于 API 使用方按需读取。
        "salary_monthly": salary_monthly, "salary_annual": salary_annual,
        "experience": distributions["experience"], "degree": distributions["degree"],
        "industry": distributions["industry"], "scale": distributions["scale"],
        "job_score": distributions["job_score"], "priority": distributions["priority"],
        "by_keyword": _group_distinct(reliable_rows, "keyword", "公司定向"),
        "by_city": _group_distinct(reliable_rows, "city"), "trend": trend,
        "cross": cross, "funnel": funnel,
        "top_companies": top_companies, "top_skills": top_skills,
        "filters": {"keywords": keywords, "cities": city_values},
    }


FUNNEL_STAGES = (
    ("pool", "入池岗位", None),
    ("greeted", "已打招呼", "greeted"),
    ("replied", "HR已回复", None),
    ("applied", "已投递", "applied"),
    ("interviewed", "面试", "interviewed"),
    ("offered", "Offer", "offered"),
)


def _funnel_report(filters: dict, resume_id, unique: list[dict]) -> dict:
    """基于当前筛选范围的投递转化漏斗（工作流阶段 + HR 回复事实）。"""
    scoped_keys = {row["job_key"] for row in unique}
    counts = {"pool": len(scoped_keys), "greeted": 0, "replied": 0,
              "applied": 0, "interviewed": 0, "offered": 0}
    stages_payload = []
    if scoped_keys:
        from . import workflow  # 局部导入避免与其他模块形成环
        states = workflow.states_for_jobs(scoped_keys, resume_id)
        replied_keys = _inbound_replied_keys(scoped_keys)
        for row in states.values():
            for stage in ("greeted", "applied", "interviewed", "offered"):
                if row[stage]:
                    counts[stage] += 1
        counts["replied"] = len(scoped_keys & replied_keys)
    previous = None
    for key, label, _ in FUNNEL_STAGES:
        count = counts[key]
        stages_payload.append({
            "key": key, "label": label, "count": count,
            "pool_rate": round(count / counts["pool"] * 100, 1)
            if counts["pool"] else 0,
            "step_rate": round(count / previous * 100, 1)
            if previous else None,
        })
        previous = count
    return {"stages": stages_payload}


def _inbound_replied_keys(scoped_keys: set[str]) -> set[str]:
    """存在 HR 来信消息的岗位集合（会话表关联岗位）。"""
    result = set()
    keys = list(scoped_keys)
    chunk_size = 400
    for start in range(0, len(keys), chunk_size):
        chunk = keys[start:start + chunk_size]
        marks = ",".join("?" for _ in chunk)
        rows = get_db().execute(
            "SELECT DISTINCT c.job_key FROM conversations c "
            f"JOIN messages m ON m.conversation_id=c.id AND m.direction='in' "
            f"WHERE c.job_key IN ({marks})", chunk).fetchall()
        result.update(row["job_key"] for row in rows)
    return result


analytics_report = aggregate
