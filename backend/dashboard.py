"""总览看板聚合：只读数据库缓存，不触发 Chrome 或采集任务。"""
from datetime import date, datetime, timedelta, timezone

from .db import get_db, get_setting


def _simple_period_counts(table: str, condition: str, time_field: str,
                          distinct: str = "*") -> dict:
    """生成总数/今日/近七天，condition 只使用内部常量。"""
    expr = "COUNT(*)" if distinct == "*" else f"COUNT(DISTINCT {distinct})"
    conn = get_db()
    total = conn.execute(
        f"SELECT {expr} c FROM {table} WHERE {condition}").fetchone()["c"]
    today = conn.execute(
        f"SELECT {expr} c FROM {table} WHERE {condition} "
        f"AND datetime({time_field}) >= datetime('now','start of day')").fetchone()["c"]
    seven = conn.execute(
        f"SELECT {expr} c FROM {table} WHERE {condition} "
        f"AND datetime({time_field}) >= datetime('now','-7 days')").fetchone()["c"]
    return {"total": total, "today": today, "last_7_days": seven}


def _greeting_counts() -> dict:
    row = get_db().execute("""
        WITH events AS (
          SELECT job_key, COALESCE(confirmed_at, sent_at, created_at) event_at
          FROM greetings
          WHERE delivery_status='confirmed' OR status='sent'
          UNION ALL
          SELECT job_key, created_at FROM sent_log WHERE ok=1
        ), confirmed AS (
          SELECT job_key, MAX(event_at) event_at FROM events GROUP BY job_key
        )
        SELECT COUNT(*) total,
          SUM(CASE WHEN datetime(event_at) >= datetime('now','start of day') THEN 1 ELSE 0 END) today,
          SUM(CASE WHEN datetime(event_at) >= datetime('now','-7 days') THEN 1 ELSE 0 END) last_7_days
        FROM confirmed
    """).fetchone()
    return {key: int(row[key] or 0) for key in ("total", "today", "last_7_days")}


def _parse_time(value: str):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _latest_time(*values):
    parsed = [(value, _parse_time(value)) for value in values if value]
    parsed = [(value, moment) for value, moment in parsed if moment]
    return max(parsed, key=lambda item: item[1])[0] if parsed else None


def _account_status() -> dict:
    conn = get_db()
    dual = bool(get_setting("dual_account_enabled", True))
    rows = {row["account"]: dict(row) for row in conn.execute(
        "SELECT account, logged_in, hint, checked_at FROM account_states")}
    required = ["collect", "account_a"] if dual else ["account_a"]
    accounts = []
    all_logged_in = True
    for account in required:
        saved = rows.get(account)
        logged_in = None if saved is None or saved["logged_in"] is None else bool(saved["logged_in"])
        all_logged_in = all_logged_in and logged_in is True
        accounts.append({
            "account": account,
            "logged_in": logged_in,
            "hint": saved["hint"] if saved else "尚未检测登录态",
            "checked_at": saved["checked_at"] if saved else None,
            "color": "green" if logged_in is True else "yellow",
        })
    return {
        "mode": "dual" if dual else "single",
        "label": "双账号" if dual else "单账号",
        "all_logged_in": all_logged_in,
        "color": "green" if all_logged_in else "yellow",
        "accounts": accounts,
    }


def _collection_status(collector_state: dict = None) -> dict:
    conn = get_db()
    online = conn.execute(
        "SELECT MAX(COALESCE(finished_at, started_at)) at FROM collect_runs "
        "WHERE kind NOT IN ('xlsx_import','json_import') "
        "AND (status IN ('succeeded','partial') OR (status='' AND finished_at IS NOT NULL))"
    ).fetchone()["at"]
    file_row = conn.execute(
        "SELECT data_source_at, finished_at FROM collect_runs "
        "WHERE kind IN ('xlsx_import','json_import') AND finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    file_at = _latest_time(file_row["data_source_at"], file_row["finished_at"]) if file_row else None
    latest = _latest_time(online, file_at)
    moment = _parse_time(latest)
    stale = True
    if moment:
        now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
        stale = now - moment > timedelta(days=3)
    unfinished = conn.execute(
        "SELECT id, kind, phase, started_at FROM collect_runs "
        "WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    running = bool((collector_state or {}).get("running", unfinished is not None))
    return {
        "running": running,
        "current": (collector_state or {}).get("current", unfinished["kind"] if unfinished else ""),
        "online_collected_at": online,
        "file_data_at": file_at,
        "latest_data_at": latest,
        "collected": latest is not None,
        "stale": stale,
        "color": "green" if latest and not stale else "yellow",
        "hint": ("正在采集" if running else
                 "数据已超过 3 天，请更新" if latest and stale else
                 "尚未采集数据" if not latest else "数据处于有效期内"),
    }


def _risk_status() -> dict:
    conn = get_db()
    halted = get_setting("send_halted_day", "") == date.today().isoformat()
    send_reason = get_setting("send_halt_reason", "") if halted else ""
    collect = conn.execute(
        "SELECT risk_signal FROM collect_runs "
        "WHERE kind NOT IN ('xlsx_import','json_import') ORDER BY id DESC LIMIT 1").fetchone()
    collect_reason = collect["risk_signal"] if collect else ""
    active = bool(halted or collect_reason)
    reasons = [reason for reason in (send_reason, collect_reason) if reason]
    return {"active": active, "color": "red" if active else "green",
            "reasons": reasons, "send_halted_today": halted}


def get_dashboard(collector_state: dict = None) -> dict:
    """返回首页完整快照；collector_state 可由接口传入内存运行态。"""
    conn = get_db()
    jobs = _simple_period_counts("jobs", "1=1", "first_seen_at")
    jobs["active"] = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='active'").fetchone()["c"]
    favorites = _simple_period_counts(
        "jobs", "favorite_at IS NOT NULL", "favorite_at")
    applications = _simple_period_counts(
        "applications", "status IN ('platform_confirmed','manual_confirmed')",
        "confirmed_at", "job_key")
    for status in ("platform_confirmed", "manual_confirmed"):
        applications[status] = conn.execute(
            "SELECT COUNT(DISTINCT job_key) c FROM applications WHERE status=?",
            (status,)).fetchone()["c"]
    greetings = _greeting_counts()
    accounts = _account_status()
    collection = _collection_status(collector_state)
    risk = _risk_status()
    overall_color = "red" if risk["active"] else (
        "green" if accounts["all_logged_in"] and not collection["stale"] else "yellow")
    return {
        "metrics": {
            "jobs": jobs,
            "favorites": favorites,
            "applications": applications,
            "greetings": greetings,
        },
        "system": {
            "color": overall_color,
            "accounts": accounts,
            "collection": collection,
            "risk": risk,
        },
        "quick_links": [
            {"label": "岗位列表", "route": "#/jobs"},
            {"label": "收藏作战台", "route": "#/cards"},
            {"label": "采集中心", "route": "#/collect"},
            {"label": "简历档案", "route": "#/profile"},
            {"label": "数据分析", "route": "#/analytics"},
        ],
    }


dashboard_snapshot = get_dashboard
