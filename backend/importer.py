"""数据导入：xlsx（人工整理版岗位库）+ scraper JSON（采集结果）。

统一 upsert 语义：同 job_key 再次导入时刷新可变字段（薪资/活跃度/时间戳），
已有评分不覆盖（评分永远由评分引擎或更新的导入写入）。
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .db import get_db, now_iso

_SALARY_RE = re.compile(
    r"^(?P<lo>\d+(?:\.\d+)?)\s*-\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<unit>[Kk元])"
    r"(?:·(?P<mo>\d+)薪)?(?P<per>.*)$")
_DAILY_RE = re.compile(r"^(?P<lo>\d+)\s*-\s*(?P<hi>\d+)元/天")


def parse_salary(text: str):
    """解析薪资串 → (min_k, max_k, months)。单位统一为 K；元/天按 21.75 天折算。

    >>> parse_salary("30-60K")
    (30.0, 60.0, 12.0)
    >>> parse_salary("25-40K·14薪")
    (25.0, 40.0, 14.0)
    """
    if not text:
        return None, None, None
    text = str(text).strip()
    m = _DAILY_RE.match(text)
    if m:
        lo, hi = int(m["lo"]) * 21.75 / 1000, int(m["hi"]) * 21.75 / 1000
        return round(lo, 1), round(hi, 1), 12.0
    m = _SALARY_RE.match(text)
    if not m:
        return None, None, None
    lo, hi = float(m["lo"]), float(m["hi"])
    if m["unit"] == "元":          # 20-30元/时 等少见格式：不折算，仅保序
        lo, hi = lo / 1000, hi / 1000
    return lo, hi, float(m["mo"]) if m["mo"] else 12.0


def job_key_from(link: str, title: str, company: str, salary: str) -> str:
    """岗位唯一键：job_detail 链接中的 encryptJobId 优先，否则内容哈希。"""
    m = re.search(r"/job_detail/([^./?#]+)\.html", link or "")
    if m:
        return m.group(1)
    raw = f"{company}|{title}|{salary}".strip()
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _num(v):
    """xlsx 数值列宽容转换（float/str/None）。"""
    if v is None or v == "":
        return None
    try:
        return round(float(str(v).replace("≈", "")), 1)
    except ValueError:
        return None


def upsert_job(job: dict) -> tuple[str, bool]:
    """插入或刷新岗位。excluded 岗位保持冻结，评分字段不动。"""
    conn = get_db()
    ts = now_iso()
    key = job["job_key"]
    fields = ["salary", "hr_active", "skills", "job_link", "company_link",
              "experience", "degree", "location", "industry", "scale", "stage",
              "salary_min", "salary_max", "salary_months", "hr_title",
              "is_headhunter", "headhunter_reason"]
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    fields = [field for field in fields if field in columns]
    cur = conn.execute(
        "SELECT job_key, status FROM jobs WHERE job_key=?", (key,)).fetchone()
    if cur is not None and cur["status"] == "excluded":
        return key, False
    if cur is None:
        insert_fields = [field for field in fields if job.get(field) is not None]
        cols = ["job_key", "title", "company", "first_seen_at", "last_seen_at",
                "source", "status", "jd_len"] + insert_fields
        vals = [key, job.get("title", ""), job.get("company", ""), ts, ts,
                job.get("source", "import"), job.get("status", "active"),
                job.get("jd_len", 0)] + [job.get(f) for f in insert_fields]
        conn.execute(
            f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
            vals)
        created = True
    else:
        sets = ", ".join(f"{f}=?" for f in fields if job.get(f) not in (None, ""))
        reactivate = ", status='active'" if cur["status"] == "delisted" else ""
        if sets:
            conn.execute(
                f"UPDATE jobs SET {sets}, last_seen_at=?{reactivate} WHERE job_key=?",
                [job[f] for f in fields if job.get(f) not in (None, "")] + [ts, key])
        elif reactivate:
            conn.execute(
                "UPDATE jobs SET status='active', last_seen_at=? WHERE job_key=?",
                (ts, key))
        created = False
    if job.get("jd"):
        conn.execute(
            "INSERT INTO job_details(job_key, jd, skill_tags, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(job_key) DO UPDATE SET jd=excluded.jd, "
            "skill_tags=excluded.skill_tags, fetched_at=excluded.fetched_at",
            (key, job["jd"], job.get("skill_tags", ""), ts))
        conn.execute("UPDATE jobs SET jd_len=? WHERE job_key=?",
                     (len(job["jd"]), key))
    if job.get("l1_score") is not None and job.get("l1_source") != "keep":
        conn.execute(
            "UPDATE jobs SET l1_score=?, l1_detail=?, match_rough=?, composite_rough=? "
            "WHERE job_key=? AND l1_score IS NULL",
            (job["l1_score"], json.dumps(job.get("l1_detail", {}), ensure_ascii=False),
             job.get("match_rough"), job.get("composite_rough"), key))
    if job.get("composite") is not None:
        conn.execute(
            "UPDATE jobs SET job_score=?, match_score=?, composite=?, priority=?, "
            "l2_detail=?, l2_source=? WHERE job_key=? AND composite IS NULL",
            (job.get("job_score"), job.get("match_score"), job["composite"],
             job.get("priority"), json.dumps(job.get("l2_detail", {}), ensure_ascii=False),
             job.get("l2_source", "imported"), key))
    conn.commit()
    return key, created


def _record_run(kind: str, params: dict, stats: dict, data_source_at: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO collect_runs(kind, params, stats, started_at, finished_at, status, "
        "data_source_at, phase) VALUES(?,?,?,?,?,'succeeded',?,'finished')",
        (kind, json.dumps(params, ensure_ascii=False),
         json.dumps(stats, ensure_ascii=False), now_iso(), now_iso(),
         data_source_at or None))
    conn.commit()


# ── xlsx 导入（人工整理版：总览 + JD详情 + L1/L2 历史评分）──────────────

def import_xlsx(path: str) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    overview = _sheet_rows(wb["岗位总览"])
    details = _sheet_rows(wb["JD详情"])
    l1 = _sheet_rows(wb["L1初筛"])
    l2 = _sheet_rows(wb["L2评级"])

    jd_by_seq = {}
    for r in details:
        seq = _num(r.get("序号"))
        if seq is not None:
            jd_by_seq[int(seq)] = str(r.get("JD全文") or "")
    l1_by_seq = {int(_num(r["序号"])): r for r in l1 if _num(r.get("序号")) is not None}
    l2_by_seq = {int(_num(r["序号"])): r for r in l2 if _num(r.get("序号")) is not None}

    created = updated = excluded_skipped = 0
    for r in overview:
        seq = _num(r.get("序号"))
        if seq is None:
            continue
        title = str(r.get("岗位名称") or "").strip()
        company = str(r.get("公司") or "").strip()
        if not title or not company:
            continue
        salary = str(r.get("薪资") or "").strip()
        link = str(r.get("岗位链接") or "").strip()
        lo, hi, mo = parse_salary(salary)
        seq = int(seq)
        l1r, l2r = l1_by_seq.get(seq), l2_by_seq.get(seq)

        job = {
            "job_key": job_key_from(link, title, company, salary),
            "title": title, "company": company, "salary": salary,
            "salary_min": lo, "salary_max": hi, "salary_months": mo,
            "experience": str(r.get("经验") or ""), "degree": str(r.get("学历") or ""),
            "location": str(r.get("区域") or ""), "industry": str(r.get("公司行业") or ""),
            "scale": str(r.get("公司规模") or ""), "stage": str(r.get("融资阶段") or ""),
            "hr_active": str(r.get("招聘者活跃") or ""),
            "jd_len": _num(r.get("JD字数")) or 0,
            "job_link": link, "source": "import",
            "jd": jd_by_seq.get(seq, ""),
        }
        if l1r is not None:
            job.update({
                "l1_score": _num(l1r.get("岗位粗分")),
                "match_rough": _num(l1r.get("匹配粗分")),
                "composite_rough": _num(l1r.get("综合粗分")),
                "l1_detail": {"source": "imported", "gate": str(l1r.get("Gate判定") or ""),
                              "skills_hit": str(l1r.get("技能命中(嵌/AI/硬/域)") or ""),
                              "l2_marked": str(l1r.get("L2") or "")},
            })
        if l2r is not None:
            job.update({
                "job_score": _num(l2r.get("岗位评分")),
                "match_score": _num(l2r.get("匹配度")),
                "composite": _num(l2r.get("综合分")),
                "priority": str(l2r.get("P级") or ""),
                "l2_source": "imported",
                "l2_detail": {
                    "type": str(l2r.get("类型") or ""), "summary": str(l2r.get("总评") or ""),
                    "advice": str(l2r.get("投递建议") or ""),
                    "dims": {k: _num(l2r.get(k))
                             for k in ("A行业", "B1薪资", "B2结构", "B3福利", "C强度", "D晋升",
                                       "E1成长", "E2技能", "E3前沿", "S1", "S2", "S3", "S4",
                                       "S5", "加减分") if _num(l2r.get(k)) is not None},
                },
            })
        existing = get_db().execute(
            "SELECT status FROM jobs WHERE job_key=?", (job["job_key"],)).fetchone()
        if existing is not None and existing["status"] == "excluded":
            excluded_skipped += 1
            continue
        job_key, is_new = upsert_job(job)
        imported_score = {
            field: job[field] for field in (
                "l1_score", "l1_detail", "match_rough", "composite_rough",
                "job_score", "match_score", "composite", "priority", "l2_detail")
            if job.get(field) is not None
        }
        if imported_score:
            # 基线只接受首次导入；后续引擎评分和重复导入都不能改写它。
            from . import resumes
            resumes.save_imported_baseline(job_key, **imported_score)
            if resumes.get_job_score(job_key) is None:
                current_score = dict(imported_score)
                if "l1_score" in imported_score:
                    current_score["l1_source"] = "imported"
                if "composite" in imported_score:
                    current_score["l2_source"] = "imported"
                resumes.save_job_score(job_key, **current_score)
        created += is_new
        updated += not is_new

    stats = {"total": created + updated, "created": created, "updated": updated,
             "excluded_skipped": excluded_skipped,
             "with_jd": len(jd_by_seq), "with_l2": len(l2_by_seq)}
    try:
        source_at = datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone().isoformat(
            timespec="seconds")
    except OSError:
        source_at = ""
    _record_run("xlsx_import", {"path": str(path)}, stats, source_at)
    return stats


def _sheet_rows(ws) -> list[dict]:
    """按表头行（含「序号」列的行）转 dict 列表；兼容标题行偏移。"""
    rows = list(ws.iter_rows(values_only=True))
    header_i, headers = None, None
    for i, r in enumerate(rows):
        cells = [str(c).strip() if c is not None else "" for c in r]
        if "序号" in cells and ("岗位名称" in cells or "JD全文" in cells):
            header_i, headers = i, cells
            break
    if header_i is None:
        return []
    out = []
    for r in rows[header_i + 1:]:
        row = {h: v for h, v in zip(headers, r) if h}
        if row.get("序号") is None:
            continue
        out.append(row)
    return out


# ── scraper JSON 导入 ──────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return default


def _detail_map(detail_files: list[Path]) -> dict:
    detail_by_key = {}
    for f in detail_files:
        arr = _load_json(f, [])
        if isinstance(arr, dict):
            arr = arr.get("details", [])
        for d in arr:
            key = job_key_from(d.get("job_link", "") or d.get("link", ""),
                               d.get("title", ""), d.get("company", ""),
                               d.get("salary", ""))
            detail_by_key[key] = d
    return detail_by_key


def _native_headhunter(job: dict):
    for key in ("is_headhunter", "headhunter", "is_hunter", "hunter_job"):
        if key in job and job[key] is not None:
            value = job[key]
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "是", "猎头")
            return bool(value)
    return None


def _headhunter_fields(company: str, hr_title: str, raw: dict) -> tuple[int, str]:
    native = _native_headhunter(raw)
    try:
        from .job_state import detect_headhunter
        result = detect_headhunter(company, hr_title, native)
        if isinstance(result, dict):
            return int(bool(result.get("is_headhunter"))), str(result.get("reason") or "")
        if isinstance(result, tuple):
            return int(bool(result[0])), str(result[1] if len(result) > 1 else "")
    except (ImportError, TypeError, ValueError):
        pass
    if native is not None:
        return int(native), "来源原生标记" if native else ""
    if "猎头" in hr_title:
        return 1, "招聘者职位包含猎头"
    if re.match(r"^某.+公司$", company):
        return 1, "公司名称为匿名公司"
    return 0, ""


def _job_from_scraper(raw: dict, source: str, detail: dict) -> dict:
    company = str(raw.get("boss_name") or raw.get("company") or "").strip()
    title = str(raw.get("title") or raw.get("job_name") or "").strip()
    salary = str(raw.get("salary") or "").strip()
    link = str(raw.get("job_link") or raw.get("link") or "").strip()
    lo, hi, mo = parse_salary(salary)
    tags = raw.get("tags", "")
    if isinstance(tags, list):
        tags = " | ".join(str(item) for item in tags)
    hr_title = str(raw.get("boss_title") or raw.get("boss_position")
                   or raw.get("hr_title") or "").strip()
    is_headhunter, headhunter_reason = _headhunter_fields(company, hr_title, raw)
    skill_tags = detail.get("skill_tags", []) or []
    if isinstance(skill_tags, list):
        skill_tags = " | ".join(str(item) for item in skill_tags)
    return {
        "job_key": job_key_from(link, title, company, salary),
        "title": title, "company": company, "salary": salary,
        "salary_min": lo, "salary_max": hi, "salary_months": mo,
        "experience": (str(tags) + " | ").split(" | ")[0] or "",
        "degree": _degree_from_tags(str(tags)),
        "location": raw.get("location", ""),
        "industry": raw.get("company_industry", ""),
        "scale": raw.get("company_scale", ""),
        "stage": raw.get("company_stage", ""),
        "hr_active": (detail.get("boss_active_status")
                      or raw.get("boss_active_status", "")),
        "hr_title": hr_title,
        "skills": raw.get("skills", ""), "job_link": link,
        "company_link": raw.get("company_link", ""), "source": source,
        "jd": detail.get("jd", ""), "skill_tags": skill_tags,
        "is_headhunter": is_headhunter,
        "headhunter_reason": headhunter_reason,
    }


def import_scraper_files(list_files, detail_files=None, record_run=True) -> dict:
    """只导入调用方明确指定的文件，供单次在线采集使用。"""
    list_paths = [Path(path).expanduser() for path in list_files]
    detail_paths = [Path(path).expanduser() for path in (detail_files or [])]
    detail_by_key = _detail_map(detail_paths)

    created = updated = skipped = invalid = 0
    touched = []
    source_times = []
    conn = get_db()
    for f in list_paths:
        data = _load_json(f, {})
        if isinstance(data, dict) and data.get("scraped_at"):
            source_times.append(str(data["scraped_at"]))
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        if not isinstance(jobs, list):
            continue
        source = "company" if "company" in f.name else "search"
        for raw in jobs:
            if not isinstance(raw, dict):
                invalid += 1
                continue
            probe = _job_from_scraper(raw, source, {})
            if not probe["title"] or not probe["company"]:
                invalid += 1
                continue
            existing = conn.execute(
                "SELECT status FROM jobs WHERE job_key=?", (probe["job_key"],)).fetchone()
            if existing is not None and existing["status"] == "excluded":
                skipped += 1
                continue
            job = _job_from_scraper(
                raw, source, detail_by_key.get(probe["job_key"], {}))
            key, is_new = upsert_job(job)
            created += is_new
            updated += not is_new
            touched.append(key)

    stats = {"total": created + updated, "created": created, "updated": updated,
             "excluded_skipped": skipped, "invalid": invalid,
             "with_jd": sum(1 for key in touched if key in detail_by_key),
             "files": [f.name for f in list_paths],
             "detail_files": [f.name for f in detail_paths],
             "job_keys": list(dict.fromkeys(touched))}
    if record_run:
        _record_run("json_import", {"files": [str(f) for f in list_paths],
                                     "detail_files": [str(f) for f in detail_paths]}, stats,
                    max(source_times) if source_times else "")
    return stats


def import_scraper_details(detail_file: str) -> dict:
    """补写一个详情文件；不存在或 excluded 的岗位不会被创建/更新。"""
    path = Path(detail_file).expanduser()
    details = _detail_map([path])
    conn = get_db()
    updated = skipped = missing = 0
    ts = now_iso()
    for key, detail in details.items():
        row = conn.execute(
            "SELECT status FROM jobs WHERE job_key=?", (key,)).fetchone()
        if row is None:
            missing += 1
            continue
        if row["status"] == "excluded":
            skipped += 1
            continue
        jd = str(detail.get("jd") or "")
        if not jd:
            continue
        tags = detail.get("skill_tags", []) or []
        if isinstance(tags, list):
            tags = " | ".join(str(tag) for tag in tags)
        conn.execute(
            "INSERT INTO job_details(job_key, jd, skill_tags, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(job_key) DO UPDATE SET jd=excluded.jd, "
            "skill_tags=excluded.skill_tags, fetched_at=excluded.fetched_at",
            (key, jd, str(tags), ts))
        conn.execute(
            "UPDATE jobs SET jd_len=?, hr_active=COALESCE(NULLIF(?,''),hr_active) "
            "WHERE job_key=?", (len(jd), detail.get("boss_active_status", ""), key))
        updated += 1
    conn.commit()
    return {"total": len(details), "updated": updated,
            "excluded_skipped": skipped, "job_missing": missing,
            "file": path.name}


def import_scraper_json(path: str, detail_path: str = None) -> dict:
    """兼容入口：文件路径精确导入；目录路径保留旧版批量导入语义。"""
    base = Path(path).expanduser()
    if base.is_file():
        details = [detail_path] if detail_path else []
        return import_scraper_files([base], details)
    list_files = sorted(list(base.glob("boss_jobs_*.json"))
                        + list(base.glob("boss_company_jobs_*.json")))
    detail_files = sorted(base.glob("boss_details_*.json"))
    return import_scraper_files(list_files, detail_files)


def _degree_from_tags(tags: str) -> str:
    for t in ("博士", "硕士", "本科", "大专", "高中", "初中", "学历不限"):
        if t in (tags or ""):
            return t
    return ""
