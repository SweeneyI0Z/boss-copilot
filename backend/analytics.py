"""岗位数据分析。

默认分布覆盖全部当前可用岗位，避免历史导入或收藏同步的数据因缺少来源关系而
无法分析；关键词、城市、日期和趋势仍只读取可靠的 job_collection_hits。
"""
from collections import defaultdict

from .db import get_db


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


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


def aggregate(filters: dict = None, resume_id=None) -> dict:
    """返回分析页全部聚合；filters 支持关键词、城市和日期区间。"""
    filters = dict(filters or {})
    if resume_id is None:
        resume_id = filters.get("resume_id")
    source_filters_active = any(
        _as_list(filters.get(field)) for field in ("keyword", "city_code", "city")
    ) or bool(filters.get("date_from") or filters.get("date_to"))
    clauses, where_args = _where(filters)
    score_join, score_args = _score_join(resume_id)
    score_select = (
        "COALESCE(s.job_score,s.l1_score) job_score,"
        "COALESCE(s.match_score,s.match_rough) match_score,"
        "COALESCE(s.composite,s.composite_rough) composite,s.priority priority"
        if score_join else
        "j.job_score,j.match_score,j.composite,j.priority"
    )
    sql = (
        "SELECT h.job_key,h.keyword,h.province,h.city,h.city_code,h.last_seen_at,"
        "j.salary_min,j.salary_max,j.salary_months,j.experience,j.degree,j.industry,"
        "j.scale,j.status,COALESCE(j.headhunter_override,j.is_headhunter,0) headhunter,"
        f"{score_select} FROM job_collection_hits h JOIN jobs j ON j.job_key=h.job_key "
        f"{score_join} WHERE {' AND '.join(clauses)} AND j.status<>'excluded'"
    )
    reliable_rows = [dict(row) for row in get_db().execute(
        sql, [*score_args, *where_args]).fetchall()]

    if source_filters_active:
        rows = reliable_rows
    else:
        # 历史导入和 BOSS 收藏同步没有可靠关键词关系，但岗位属性本身仍可分析。
        # 默认总览纳入这些当前岗位；一旦使用来源筛选，则只返回可靠命中。
        current_sql = (
            "SELECT j.job_key,'' keyword,'' province,j.location city,'' city_code,"
            "j.last_seen_at,j.salary_min,j.salary_max,j.salary_months,j.experience,"
            "j.degree,j.industry,j.scale,j.status,"
            "COALESCE(j.headhunter_override,j.is_headhunter,0) headhunter,"
            f"{score_select} FROM jobs j {score_join} "
            "WHERE j.status NOT IN ('excluded','delisted')"
        )
        rows = [dict(row) for row in get_db().execute(
            current_sql, score_args).fetchall()]

    # 同一岗位可命中多个关键词；总体分布只计一次，来源维度再分别去重。
    jobs = {}
    for row in rows:
        jobs.setdefault(row["job_key"], row)
    unique = list(jobs.values())
    reliable_job_keys = {row["job_key"] for row in reliable_rows}
    reliable_jobs = sum(row["job_key"] in reliable_job_keys for row in unique)
    unattributed_jobs = len(unique) - reliable_jobs
    monthly = []
    annual = []
    for row in unique:
        if row["salary_min"] is None or row["salary_max"] is None:
            monthly.append(None)
            annual.append(None)
            continue
        midpoint = (float(row["salary_min"]) + float(row["salary_max"])) / 2
        monthly.append(round(midpoint, 1))
        annual.append(round(midpoint * float(row["salary_months"] or 12), 1))
    known_monthly = [value for value in monthly if value is not None]
    known_annual = [value for value in annual if value is not None]
    headhunters = sum(bool(row["headhunter"]) for row in unique)

    trend_filters = dict(filters)
    trend_clauses, trend_args = _where(trend_filters, "h", current_only=False)
    trend_sql = (
        "SELECT substr(h.last_seen_at,1,10) day,COUNT(DISTINCT h.job_key) count "
        "FROM job_collection_hits h JOIN jobs j ON j.job_key=h.job_key WHERE "
        + (" AND ".join(trend_clauses) + " AND " if trend_clauses else "")
        + "j.status<>'excluded' GROUP BY day ORDER BY day"
    )
    trend = [dict(row) for row in get_db().execute(trend_sql, trend_args).fetchall()]

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
                            for item in city_values]},
        "trends": [{"label": item["day"], "count": item["count"]} for item in trend],
        # 同时保留语义更明确的字段，便于 API 使用方按需读取。
        "salary_monthly": salary_monthly, "salary_annual": salary_annual,
        "experience": distributions["experience"], "degree": distributions["degree"],
        "industry": distributions["industry"], "scale": distributions["scale"],
        "job_score": distributions["job_score"], "priority": distributions["priority"],
        "by_keyword": _group_distinct(reliable_rows, "keyword", "公司定向"),
        "by_city": _group_distinct(reliable_rows, "city"), "trend": trend,
        "filters": {"keywords": keywords, "cities": city_values},
    }


analytics_report = aggregate
