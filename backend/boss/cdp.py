"""双账号 CDP 管理：采集号(9222) 与 账号A(9223) 的启动/健康/登录引导。

原则（沿 boss-zhipin-scraper 惯例）：
- 只按隔离 user-data-dir 精准启停，绝不触碰用户主 Chrome
- 登录引导用「静止页面」模式：打开 zhipin.com 后不再自动导航，避免打扰登录
"""
import json
import subprocess
import time
import urllib.request

from .. import config

CHROME = config.CHROME_PATH

# 本机 CDP 请求绝不走系统代理（用户常挂 Clash 类代理，会把 127.0.0.1 劫持成 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def launch(account: str, wait_sec: float = 15) -> dict:
    """启动指定账号的专用 Chrome（已运行则直接返回）。"""
    conf = config.ACCOUNTS[account]
    if is_running(conf["cdp_port"]):
        return {"ok": True, "already_running": True, "port": conf["cdp_port"]}
    conf["profile_dir"].mkdir(parents=True, exist_ok=True)
    cmd = [CHROME,
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
    """按 user-data-dir 精准关闭该账号的 Chrome（ps 匹配命令行，不碰其他进程）。"""
    conf = config.ACCOUNTS[account]
    marker = str(conf["profile_dir"])
    r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    killed = 0
    for line in r.stdout.splitlines():
        line = line.strip()
        if marker not in line or "Google Chrome" not in line:
            continue
        pid_text = line.split(None, 1)[0]
        try:
            subprocess.run(["kill", pid_text], check=False)
            killed += 1
        except (OSError, ValueError):
            continue
    return {"ok": True, "stopped": killed}


def status() -> dict:
    out = {}
    for name, conf in config.ACCOUNTS.items():
        running = is_running(conf["cdp_port"])
        out[name] = {
            "label": conf["label"], "port": conf["cdp_port"],
            "profile": str(conf["profile_dir"]),
            "running": running,
            "browser": browser_version(conf["cdp_port"]) if running else "",
        }
    return out


# ── 登录引导（静止页面模式）────────────────────────────────────────

def _ws_eval(port: int, sid: str, js: str):
    """在指定 session 上执行 JS 并取值（websocket-client 延迟导入）。"""
    import websocket
    targets = _http_get_json(f"http://127.0.0.1:{port}/json") or []
    page = next((t for t in targets if t.get("type") == "page"), None)
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
    """在前台标签页打开 zhipin.com（不自动刷新，用户手动登录）。"""
    conf = config.ACCOUNTS[account]
    launch(account)
    import websocket
    targets = _http_get_json(f"http://127.0.0.1:{conf['cdp_port']}/json") or []
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        return {"ok": False, "error": "无可用标签页"}
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.navigate",
                            "params": {"url": "https://www.zhipin.com/"}}))
        ws.recv()
    finally:
        ws.close()
    return {"ok": True, "port": conf["cdp_port"]}


def login_state(account: str) -> dict:
    """轻量登录态探测：只看首页 DOM 特征（不调任何 BOSS API，账号A零足迹）。

    判据：已登录首页会出现用户头像/昵称区；未登录则顶部有「登录」按钮。
    """
    conf = config.ACCOUNTS[account]
    if not is_running(conf["cdp_port"]):
        return {"account": account, "running": False, "logged_in": None}
    import websocket
    targets = _http_get_json(f"http://127.0.0.1:{conf['cdp_port']}/json") or []
    page = next((t for t in targets if t.get("type") == "page"), None)
    if page and "zhipin.com" in (page.get("url") or ""):
        js = ("(function(){var u=document.querySelector('[class*=user] img,"
              "[class*=avatar],.geek-name');var l=document.querySelector("
              "'[class*=login],a[href*=login]');"
              "return JSON.stringify({user: !!u, loginBtn: !!l && (l.innerText||'').includes('登录')});})()")
        try:
            val = _ws_eval(conf["cdp_port"], page["id"], js)
            d = json.loads(val) if val else {}
            logged = bool(d.get("user")) and not d.get("loginBtn")
            return {"account": account, "running": True, "logged_in": logged,
                    "hint": "" if logged else "请在打开的窗口中登录"}
        except (OSError, ValueError, KeyError):
            pass
    return {"account": account, "running": True, "logged_in": None,
            "hint": "未检测到 zhipin.com 标签页，请先打开登录页"}
