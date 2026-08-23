"""账号 CDP 管理：采集号(9222) 与沟通号(9223) 的启动/健康/登录引导。

原则（沿 boss-zhipin-scraper 惯例）：
- 只按隔离 user-data-dir 精准启停，绝不触碰用户主 Chrome
- 登录引导用「静止页面」模式：打开 zhipin.com 后不再自动导航，避免打扰登录
"""
import json
import os
import subprocess
import time
import urllib.request

from .. import config

# 本机 CDP 请求绝不走系统代理（用户常挂 Clash 类代理，会把 127.0.0.1 劫持成 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LOGIN_URL = "https://www.zhipin.com/web/user/"
LOGIN_STATE_JS = r"""
(() => {
  const visible = el => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
  const text = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const controls = [...document.querySelectorAll('a,button')].filter(visible);
  const loginControl = controls.some(el => {
    const label = text(el);
    const href = el.getAttribute('href') || '';
    return ['登录', '登录/注册', '注册/登录'].includes(label) ||
      (/\/web\/user\/?(?:\?|$)/.test(href) && /登录|注册/.test(label));
  });
  const loginPanel = [...document.querySelectorAll(
    '.sign-wrap,.login-register-content,[class*=login-register],input[placeholder*=手机号]')]
    .some(visible);
  const header = document.querySelector('#header,.boss-header,.site-header,header');
  const userControl = !!(header && header.querySelector(
    '.nav-figure,.user-nav,.geek-name,a[href*="/web/geek/resume"],a[href*="/web/geek/chat"]'));
  const authenticatedRoute = /^\/web\/geek\/(chat|recommend|resume|manage)(?:\/|$)/
    .test(location.pathname);
  return JSON.stringify({login: loginControl || loginPanel, user: userControl,
    authenticated: authenticatedRoute});
})()
"""


def _http_get_json(url: str, timeout=3):
    try:
        with _opener.open(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def is_running(port: int) -> bool:
    return _http_get_json(f"http://127.0.0.1:{port}/json/version") is not None


def browser_version(port: int) -> str:
    data = _http_get_json(f"http://127.0.0.1:{port}/json/version") or {}
    return data.get("Browser", "")


def dual_account_enabled() -> bool:
    """是否启用采集号与沟通号隔离；历史安装默认保持双账号行为。"""
    from ..db import get_setting
    return get_setting("dual_account_enabled", True) is not False


def account_for(purpose: str) -> str:
    """按用途选择账号；单账号模式统一使用沟通号。"""
    if purpose == "communication":
        return "account_a"
    if purpose == "collect":
        return "collect" if dual_account_enabled() else "account_a"
    raise ValueError(f"unknown account purpose: {purpose}")


def launch(account: str, wait_sec: float = 15) -> dict:
    """启动指定账号的专用 Chrome（已运行则直接返回）。"""
    conf = config.ACCOUNTS[account]
    if is_running(conf["cdp_port"]):
        return {"ok": True, "already_running": True, "port": conf["cdp_port"]}
    conf["profile_dir"].mkdir(parents=True, exist_ok=True)
    cmd = [config.CHROME_PATH,
           f"--remote-debugging-port={conf['cdp_port']}",
           f"--user-data-dir={conf['profile_dir']}",
           "--no-first-run", "--no-default-browser-check",
           "--remote-allow-origins=*"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if is_running(conf["cdp_port"]):
            return {"ok": True, "already_running": False, "port": conf["cdp_port"]}
        time.sleep(0.5)
    return {"ok": False, "error": f"CDP {conf['cdp_port']} 未就绪", "port": conf["cdp_port"]}


def stop(account: str) -> dict:
    """按 user-data-dir 精准关闭该账号的 Chrome，不碰其他 Chrome。"""
    conf = config.ACCOUNTS[account]
    marker = str(conf["profile_dir"])
    if os.name == "nt":
        command = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                   "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                           capture_output=True, text=True)
    else:
        r = subprocess.run(["ps", "-axo", "pid=,command="],
                           capture_output=True, text=True)
    killed = 0
    for line in r.stdout.splitlines():
        line = line.strip()
        if marker not in line or "chrome" not in line.lower():
            continue
        pid_text = line.split(None, 1)[0]
        try:
            kill_cmd = ["taskkill", "/PID", pid_text, "/T"] if os.name == "nt" \
                else ["kill", pid_text]
            subprocess.run(kill_cmd, check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            killed += 1
        except (OSError, ValueError):
            continue
    return {"ok": True, "stopped": killed}


def status() -> dict:
    collect_account = account_for("collect")
    communication_account = account_for("communication")
    out = {}
    for name, conf in config.ACCOUNTS.items():
        running = is_running(conf["cdp_port"])
        if running:
            live = login_state(name)
            if live.get("logged_in") is not None:
                _save_login_state(name, live)
        roles = []
        if name == collect_account:
            roles.append("采集")
        if name == communication_account:
            roles.append("沟通")
        out[name] = {
            "label": conf["label"], "port": conf["cdp_port"],
            "description": conf.get("description", ""),
            "roles": roles, "enabled": bool(roles),
            "running": running,
            "browser": browser_version(conf["cdp_port"]) if running else "",
            "login_state": saved_login_state(name),
        }
    return out


# ── 登录引导（静止页面模式）────────────────────────────────────────

def _ws_eval(port: int, target_id: str, js: str):
    """在指定 session 上执行 JS 并取值（websocket-client 延迟导入）。"""
    import websocket
    targets = _http_get_json(f"http://127.0.0.1:{port}/json") or []
    page = next((t for t in targets if t.get("id") == target_id), None)
    if not page:
        return None
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": js, "returnByValue": True}}))
        resp = json.loads(ws.recv())
        return resp.get("result", {}).get("result", {}).get("value")
    finally:
        ws.close()


def open_login_page(account: str) -> dict:
    """在前台标签页打开 BOSS 登录页。"""
    conf = config.ACCOUNTS[account]
    launched = launch(account)
    if not launched.get("ok"):
        return launched
    import websocket
    targets = _http_get_json(f"http://127.0.0.1:{conf['cdp_port']}/json") or []
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        return {"ok": False, "error": "无可用标签页"}
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.navigate",
                            "params": {"url": LOGIN_URL}}))
        ws.recv()
    finally:
        ws.close()
    return {"ok": True, "port": conf["cdp_port"]}


def _classify_login_dom(account: str, data: dict) -> dict:
    """登录入口优先，只有明确用户菜单才判为已登录。"""
    if data.get("login"):
        return {"account": account, "running": True, "logged_in": False,
                "hint": "未登录"}
    if data.get("user") or data.get("authenticated"):
        return {"account": account, "running": True, "logged_in": True,
                "hint": ""}
    return {"account": account, "running": True, "logged_in": None,
            "hint": "页面尚未加载完成或无法确认登录态"}


def login_state(account: str) -> dict:
    """轻量登录态探测：只看页面 DOM 特征，不读取 Cookie 或调用 BOSS API。

    判据：出现登录入口一律未登录；无登录入口且出现明确用户菜单才算已登录。
    """
    conf = config.ACCOUNTS[account]
    if not is_running(conf["cdp_port"]):
        return {"account": account, "running": False, "logged_in": None,
                "hint": "Chrome 未启动，请先点击「启动」或「打开登录页」"}
    import websocket
    targets = _http_get_json(f"http://127.0.0.1:{conf['cdp_port']}/json") or []
    pages = [t for t in targets
             if t.get("type") == "page" and "zhipin.com" in (t.get("url") or "")]
    states = []
    for page in pages:
        try:
            val = _ws_eval(conf["cdp_port"], page["id"], LOGIN_STATE_JS)
            d = json.loads(val) if val else {}
            state = _classify_login_dom(account, d)
            if state.get("logged_in") is True:
                return state
            states.append(state)
        except (OSError, ValueError, KeyError, websocket.WebSocketException):
            continue
    logged_out = next((state for state in states
                       if state.get("logged_in") is False), None)
    if logged_out:
        return logged_out
    return {"account": account, "running": True, "logged_in": None,
            "hint": "未检测到 zhipin.com 标签页，请先打开登录页"}


def saved_login_state(account: str):
    """读取最近一次人工检测结果。"""
    from ..db import get_db
    row = get_db().execute(
        "SELECT logged_in, hint, checked_at FROM account_states WHERE account=?",
        (account,)).fetchone()
    if row is None:
        return None
    logged_in = None if row["logged_in"] is None else bool(row["logged_in"])
    return {"logged_in": logged_in, "hint": row["hint"],
            "checked_at": row["checked_at"]}


def _save_login_state(account: str, result: dict) -> dict:
    """持久化检测结果，供页面刷新后继续展示。"""
    from ..db import get_db, now_iso
    checked_at = now_iso()
    logged_in = result.get("logged_in")
    stored = None if logged_in is None else int(bool(logged_in))
    conn = get_db()
    conn.execute(
        "INSERT INTO account_states(account, logged_in, hint, checked_at) VALUES(?,?,?,?) "
        "ON CONFLICT(account) DO UPDATE SET logged_in=excluded.logged_in, "
        "hint=excluded.hint, checked_at=excluded.checked_at",
        (account, stored, result.get("hint", "")[:200], checked_at))
    conn.commit()
    result["checked_at"] = checked_at
    return result


def check_login_state(account: str, wait_sec: float = 10,
                      interval: float = 0.5) -> dict:
    """检测登录态；Chrome 未运行时临时启动、打开登录页，完成后再停止。"""
    conf = config.ACCOUNTS[account]
    was_running = is_running(conf["cdp_port"])
    started_here = False
    result = None
    try:
        if not was_running:
            launched = launch(account)
            if not launched.get("ok"):
                result = {"account": account, "running": False, "logged_in": None,
                          "hint": launched.get("error", "Chrome 启动失败")}
            else:
                started_here = not launched.get("already_running", False)
                opened = open_login_page(account)
                if not opened.get("ok"):
                    result = {"account": account, "running": True,
                              "logged_in": None,
                              "hint": opened.get("error", "登录页打开失败")}

        if result is None:
            deadline = time.time() + max(0, wait_sec)
            while True:
                result = login_state(account)
                logged_in = result.get("logged_in")
                # 临时打开登录页时，已登录 profile 可能先渲染登录页再重定向。
                if logged_in is True or time.time() >= deadline:
                    break
                if logged_in is False and not started_here:
                    break
                time.sleep(interval)
    except Exception as e:  # 检测边界兜底：失败结果仍需落库，且临时 Chrome 仍需停止
        result = {"account": account, "running": was_running or started_here,
                  "logged_in": None, "hint": f"登录态检测失败：{e}"[:200]}
    finally:
        if started_here:
            try:
                stop(account)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass

    result = result or {"account": account, "running": was_running,
                        "logged_in": None, "hint": "登录态检测失败"}
    result["auto_started"] = started_here
    if started_here:
        result["running"] = False
    return _save_login_state(account, result)
