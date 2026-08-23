"""发送器：沟通号 Chrome 上以真人节奏发送已批准的招呼语。

护栏（不可跳过）：
- 每日上限（settings.send_daily_limit）与硬顶（send_daily_hard_cap）
- 相邻发送随机间隔（send_gap_min/max_sec）
- 同公司 30 天内去重（sent_log）
- 命中「120/150位BOSS」限制文案或验证码信号 → 当天熔断（send_halted_day）

发送路径：job_detail 页点击「立即沟通」→ 在聊天输入框输入 → 发送（UI 级操作，
最接近真人；不做 API 直调）。
"""
import json
import random
import re
import time
from datetime import date, datetime

from .boss import cdp
from .db import get_db, get_setting, now_iso, set_setting

COMPANY_DEDUP_DAYS = 30

FIND_CHAT_BTN_JS = """
(() => {
  const btns = [...document.querySelectorAll('a,button,div[role=button]')];
  for (const b of btns) {
    const t = (b.innerText || '').trim();
    if (t === '立即沟通' || t === '继续沟通') {
      const r = b.getBoundingClientRect();
      return JSON.stringify({found: true, text: t,
        x: r.x + r.width / 2, y: r.y + r.height / 2});
    }
  }
  return JSON.stringify({found: false});
})()
"""

FIND_INPUT_JS = """
(() => {
  const el = document.querySelector(
    '.chat-input, textarea[placeholder*=说], [contenteditable=true], ' +
    'div[contenteditable], .input-area textarea, #chat-input');
  if (!el) return JSON.stringify({found: false});
  const r = el.getBoundingClientRect();
  return JSON.stringify({found: true, tag: el.tagName,
    editable: el.isContentEditable, x: r.x + r.width / 2, y: r.y + r.height / 2});
})()
"""

LIMIT_PATTERNS = re.compile(r"已与\d+位BOSS沟通|操作过于频繁|安全校验|滑块|验证")


def halted_today() -> bool:
    return get_setting("send_halted_day", "") == date.today().isoformat()


def halt(reason: str) -> None:
    set_setting("send_halted_day", date.today().isoformat())
    set_setting("send_halt_reason", reason[:200])


def sent_today() -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) c FROM sent_log WHERE day=? AND ok=1",
        (date.today().isoformat(),)).fetchone()
    return row["c"]


def company_recently_sent(company: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM sent_log WHERE company=? AND ok=1 AND "
        "created_at >= datetime('now', ?) LIMIT 1",
        (company, f'-{COMPANY_DEDUP_DAYS} days')).fetchone()
    return row is not None


def _page_text(cdp_cli, sid) -> str:
    r = cdp_cli.send("Runtime.evaluate",
                     {"expression": "document.body.innerText.slice(0, 3000)",
                      "returnByValue": True}, sid)
    return r.get("result", {}).get("result", {}).get("value") or ""


def _eval_json(cdp_cli, sid, js):
    r = cdp_cli.send("Runtime.evaluate",
                     {"expression": js, "returnByValue": True}, sid)
    try:
        return json.loads(r.get("result", {}).get("result", {}).get("value") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _click(cdp_cli, sid, x, y):
    for typ in ("mousePressed", "mouseReleased"):
        cdp_cli.send("Input.dispatchMouseEvent",
                     {"type": typ, "x": x, "y": y, "button": "left",
                      "clickCount": 1}, sid)


def _type_and_send(cdp_cli, sid, info, text):
    """点击输入框 → 聚焦 → 写入文本 → 回车发送。"""
    _click(cdp_cli, sid, info["x"], info["y"])
    time.sleep(random.uniform(0.8, 1.6))
    if info.get("editable"):
        escaped = text.replace("\\", "\\\\").replace("'", "\\'")
        cdp_cli.send("Runtime.evaluate", {"expression":
            f"(function(){{const s=document.activeElement;"
            f"document.execCommand('insertText', false, '{escaped}');}})()"}, sid)
    else:
        for ch in text:
            cdp_cli.send("Input.dispatchKeyEvent",
                         {"type": "keyDown", "text": ch}, sid)
            time.sleep(random.uniform(0.02, 0.08))
            cdp_cli.send("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch}, sid)
    time.sleep(random.uniform(0.5, 1.2))
    cdp_cli.send("Input.dispatchKeyEvent",
                 {"type": "keyDown", "key": "Enter", "code": "Enter",
                  "windowsVirtualKeyCode": 13}, sid)
    time.sleep(0.15)
    cdp_cli.send("Input.dispatchKeyEvent",
                 {"type": "keyUp", "key": "Enter", "code": "Enter",
                  "windowsVirtualKeyCode": 13}, sid)


def send_one(cdp_cli, sid, job_link: str, text: str) -> dict:
    """发送单条。返回 {ok, halt_reason?}。任何限制信号立即向上抛熔断。"""
    cdp_cli.send("Page.navigate", {"url": job_link}, sid)
    deadline = time.time() + 20
    btn = {}
    while time.time() < deadline:
        time.sleep(1.5)
        page = _page_text(cdp_cli, sid)
        if LIMIT_PATTERNS.search(page):
            return {"ok": False, "halt_reason": "页面出现限制/验证信号"}
        btn = _eval_json(cdp_cli, sid, FIND_CHAT_BTN_JS)
        if btn.get("found"):
            break
    if not btn.get("found"):
        return {"ok": False, "error": "未找到「立即沟通」按钮（可能已沟通/已下架）"}
    _click(cdp_cli, sid, btn["x"], btn["y"])
    # 等聊天输入框（页内面板或跳转聊天页）
    deadline = time.time() + 20
    info = {}
    while time.time() < deadline:
        time.sleep(1.5)
        page = _page_text(cdp_cli, sid)
        if LIMIT_PATTERNS.search(page):
            return {"ok": False, "halt_reason": "沟通弹窗出现限制文案"}
        info = _eval_json(cdp_cli, sid, FIND_INPUT_JS)
        if info.get("found"):
            break
    if not info.get("found"):
        return {"ok": False, "error": "未找到聊天输入框"}
    _type_and_send(cdp_cli, sid, info, text)
    time.sleep(2.0)
    page = _page_text(cdp_cli, sid)
    if LIMIT_PATTERNS.search(page):
        return {"ok": False, "halt_reason": "发送后出现限制文案"}
    after = _eval_json(cdp_cli, sid, FIND_INPUT_JS)
    cleared = after.get("found") is True   # 输入框仍可用即视为流程完成
    return {"ok": bool(cleared)}


def send_batch() -> dict:
    """消费 approved 队列，全程护栏。返回执行报告。"""
    from . import greeting
    if halted_today():
        return {"ok": False, "halted": True,
                "reason": get_setting("send_halt_reason", "今天已熔断")}
    limit = int(get_setting("send_daily_limit", 40))
    hard = int(get_setting("send_daily_hard_cap", 110))
    already = sent_today()
    if already >= hard:
        return {"ok": False, "halted": True, "reason": f"已达硬顶 {hard}"}
    account = cdp.account_for("communication")
    st = cdp.launch(account)
    if not st.get("ok"):
        return {"ok": False, "error": "沟通号 Chrome 启动失败"}
    login = cdp.login_state(account)
    if login.get("logged_in") is not True:
        return {"ok": False, "error": "沟通号未登录：请到「账号管理」页打开登录页完成登录"}

    batch = greeting.pending_batch()
    sent, skipped, failed = 0, [], []
    import websocket

    class _Cdp:
        """薄封装：按需建一个前台标签页 session。"""
        def __init__(self):
            import urllib.request
            port = cdp.config.ACCOUNTS[account]["cdp_port"]
            targets = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json").read())
            page = next((t for t in targets if t["type"] == "page"), None)
            self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15)
            self.sid = 1

        def send(self, method, params, sid=None):
            self.sid += 1
            self.ws.send(json.dumps({"id": self.sid, "method": method,
                                     "params": params}))
            while True:
                resp = json.loads(self.ws.recv())
                if resp.get("id") == self.sid:
                    if "error" in resp:
                        raise RuntimeError(f"CDP {method}: {resp['error'].get('message')}")
                    return resp

        def close(self):
            self.ws.close()

    cli = _Cdp()
    try:
        for item in batch:
            if sent_today() >= limit:
                skipped.append({**item, "why": "达到每日上限"})
                break
            if halted_today():
                skipped.append({**item, "why": "触发熔断"})
                break
            if company_recently_sent(item["company"]):
                skipped.append({**item, "why": "同公司30天内已发"})
                continue
            try:
                result = send_one(cli, None, item["job_link"], item["chosen"])
            except (RuntimeError, OSError, websocket.WebSocketException) as e:
                result = {"ok": False, "error": str(e)[:120]}
            conn = get_db()
            if result.get("ok"):
                conn.execute(
                    "UPDATE greetings SET status='sent', sent_at=? WHERE id=?",
                    (now_iso(), item["id"]))
                conn.execute(
                    "INSERT INTO sent_log(day, job_key, company, ok, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (date.today().isoformat(), item["job_key"], item["company"],
                     1, now_iso()))
                conn.commit()
                sent += 1
            else:
                if result.get("halt_reason"):
                    halt(result["halt_reason"])
                    skipped.append({**item, "why": f"熔断: {result['halt_reason']}"})
                    break
                conn.execute(
                    "UPDATE greetings SET status='failed', error=? WHERE id=?",
                    ((result.get("error") or "unknown")[:200], item["id"]))
                conn.commit()
                failed.append({**item, "error": result.get("error")})
            gap = random.uniform(int(get_setting("send_gap_min_sec", 30)),
                                 int(get_setting("send_gap_max_sec", 90)))
            time.sleep(gap)
    finally:
        cli.close()
    return {"ok": True, "sent": sent, "skipped": skipped, "failed": failed,
            "halted": halted_today()}
