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
from urllib.parse import urlparse

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


def _load_websocket():
    """加载 CDP WebSocket 依赖；缺失时给出可执行的修复指引。"""
    try:
        import websocket
        return websocket
    except ImportError as error:
        raise RuntimeError(
            "缺少依赖 websocket-client，请在项目虚拟环境执行："
            "python -m pip install websocket-client") from error


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


def chrome_launch_command(conf: dict, headless: bool = False,
                          user_agent: str = "") -> list:
    """构造账号专用 Chrome 启动命令（纯函数，便于单测）。"""
    cmd = [config.CHROME_PATH,
           f"--remote-debugging-port={conf['cdp_port']}",
           f"--user-data-dir={conf['profile_dir']}",
           "--no-first-run", "--no-default-browser-check",
           "--remote-allow-origins=*"]
    if headless:
        # 真无头：不弹任何窗口。显式给定视口尺寸，避免 SPA 按无头默认
        # 800x600 小视口渲染异常；UA 标记由 launch() 两段式启动修正。
        cmd += ["--headless=new", "--window-size=1440,900"]
    if user_agent:
        cmd.append(f"--user-agent={user_agent}")
    return cmd


def _headless_instance_running(conf: dict) -> bool:
    """该账号 profile 的 Chrome 是否正以无头参数运行（按进程命令行判断）。"""
    marker = str(conf["profile_dir"])
    for line in _chrome_process_lines().splitlines():
        line = line.strip()
        if marker in line and "chrome" in line.lower() and "--headless" in line:
            return True
    return False


def _wait_cdp_gone(port: int, timeout: float = 10.0) -> bool:
    """等待 CDP 端口释放；旧实例完全退出后才能重启同端口实例。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(port):
            return True
        time.sleep(0.3)
    return False


# 无头实例 UA 修正的进程内缓存：修正过一次后，同次后端运行内
# 再次无头启动直接带上 --user-agent，省去两段式探测的重启开销。
_headless_user_agent_cache: str = ""


def _read_headless_user_agent(port: int) -> str:
    """读取无头实例实测 UA 并去掉 HeadlessChrome 标记；无标记返回空串。

    无头 UA 里的 HeadlessChrome/xxx 是风控最直接的识别特征；去掉后
    与有头 Chrome 的 reduced UA 完全一致（主版本.0.0.0）。
    """
    data = _http_get_json(f"http://127.0.0.1:{port}/json/version") or {}
    ua = str(data.get("User-Agent") or "")
    if "HeadlessChrome" in ua:
        return ua.replace("HeadlessChrome", "Chrome")
    return ""


def launch(account: str, wait_sec: float = 15, headless: bool = False) -> dict:
    """启动指定账号的专用 Chrome（已运行则直接返回）。

    headless=True 供自动采集使用：无窗口运行，UA 自动去掉 HeadlessChrome
    标记。headless=False 遇到运行中的无头实例时，按 profile 精准关闭后以
    有头模式重启——用户要窗口时，即便采集仍在进行也必须弹窗；进行中的
    采集任务会因 CDP 断开而失败，属预期取舍（断连报错不命中风控分类，
    不会触发当日熔断）。
    """
    conf = config.ACCOUNTS[account]
    port = conf["cdp_port"]
    if is_running(port):
        if headless:
            # 采集只复用运行中实例（无论有头无头），绝不重启打扰用户
            return {"ok": True, "already_running": True, "port": port,
                    "headless": _headless_instance_running(conf)}
        if not _headless_instance_running(conf):
            return {"ok": True, "already_running": True, "port": port,
                    "headless": False}
        stop(account)
        if not _wait_cdp_gone(port):
            return {"ok": False, "error": f"CDP {port} 旧实例关闭超时",
                    "port": port}

    conf["profile_dir"].mkdir(parents=True, exist_ok=True)

    def start(headless_flags: bool, user_agent: str = "") -> None:
        cmd = chrome_launch_command(conf, headless=headless_flags,
                                    user_agent=user_agent)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_ready() -> bool:
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if is_running(port):
                return True
            time.sleep(0.5)
        return False

    def started() -> dict:
        return {"ok": True, "already_running": False, "port": port,
                "headless": headless}

    if not headless:
        start(False)
        if not wait_ready():
            return {"ok": False, "error": f"CDP {port} 未就绪", "port": port}
        return started()

    # 无头两段式：先按默认参数启动，读实测 UA；带 HeadlessChrome 标记则
    # 关闭后带 --user-agent 重启（Chrome 152 实测仍带该标记）。
    global _headless_user_agent_cache
    user_agent = _headless_user_agent_cache
    start(True, user_agent)
    if not wait_ready():
        return {"ok": False, "error": f"CDP {port} 未就绪", "port": port}
    if not user_agent:
        user_agent = _read_headless_user_agent(port)
        if not user_agent:
            # 实测 UA 无标记（读取失败或浏览器已修复），不重启，下次再试
            return started()
        _headless_user_agent_cache = user_agent
        stop(account)
        if not _wait_cdp_gone(port):
            return {"ok": False, "error": f"CDP {port} UA 修正重启超时",
                    "port": port}
        start(True, user_agent)
        if not wait_ready():
            return {"ok": False, "error": f"CDP {port} 未就绪", "port": port}
    return started()


def _chrome_process_lines() -> str:
    """列出本机 chrome 进程行（pid\\t命令行），供按 profile 目录精准过滤。

    Windows 必须先强制 PowerShell 输出 UTF-8：profile 目录含中文用户名时，
    默认 GBK 输出会导致 marker 匹配失败，stop() 静默关不掉任何进程。
    """
    if os.name == "nt":
        command = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                   "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                   "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    else:
        r = subprocess.run(["ps", "-axo", "pid=,command="],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    return r.stdout


def stop(account: str) -> dict:
    """按 user-data-dir 精准关闭该账号的 Chrome，不碰其他 Chrome。"""
    conf = config.ACCOUNTS[account]
    marker = str(conf["profile_dir"])
    killed = 0
    for line in _chrome_process_lines().splitlines():
        line = line.strip()
        if marker not in line or "chrome" not in line.lower():
            continue
        pid_text = line.split(None, 1)[0]
        try:
            # Windows 必须带 /F 强杀：不带时只发 WM_CLOSE，后台/无窗口进程会幸存，
            # 端口继续被占用，后续启动与登录态检测全部落空。
            kill_cmd = ["taskkill", "/PID", pid_text, "/T", "/F"] if os.name == "nt" \
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
        probe_error = ""
        if running:
            try:
                live = login_state(name)
                if live.get("logged_in") is not None:
                    _save_login_state(name, live)
            except Exception as error:
                # 单账号登录态探测失败（如缺依赖）只记录原因，不拖垮整个账号页
                probe_error = str(error)[:200]
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
            "login_error": probe_error,
        }
    return out


# ── 登录引导（静止页面模式）────────────────────────────────────────

def _ws_eval(port: int, target_id: str, js: str):
    """在指定 session 上执行 JS 并取值（websocket-client 延迟导入）。"""
    websocket = _load_websocket()
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


def _navigate_login_page(account: str) -> dict:
    """在已运行的实例上导航到登录页（有头/无头两种模式复用）。

    导航前先对该标签开启焦点仿真：BOSS 是 SPA，无前台窗口（无头实例或
    后台标签）时不渲染，登录面板出不来，登录态 DOM 探测会全部落空。
    """
    conf = config.ACCOUNTS[account]
    try:
        websocket = _load_websocket()
        targets = _http_get_json(f"http://127.0.0.1:{conf['cdp_port']}/json") or []
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            return {"ok": False, "error": "无可用标签页"}
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
        try:
            ws.send(json.dumps({"id": 1, "method": "Emulation.setFocusEmulationEnabled",
                                "params": {"enabled": True}}))
            ws.recv()
            ws.send(json.dumps({"id": 2, "method": "Page.navigate",
                                "params": {"url": LOGIN_URL}}))
            ws.recv()
        finally:
            ws.close()
    except Exception as error:  # 依赖缺失或 CDP 连接失败都要可读地回给前端
        return {"ok": False, "error": f"打开登录页失败：{error}"[:200]}
    return {"ok": True, "port": conf["cdp_port"]}


def open_login_page(account: str) -> dict:
    """在有头窗口打开 BOSS 登录页；无头实例会被重启为有头，保证用户能操作。"""
    launched = launch(account)
    if not launched.get("ok"):
        return launched
    return _navigate_login_page(account)


def open_boss_job_page(job_link: str) -> dict:
    """用沟通号 Chrome 在前台打开一个合法的 BOSS 岗位链接。"""
    parsed = urlparse((job_link or "").strip())
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("http", "https") or
            not (host == "zhipin.com" or host.endswith(".zhipin.com"))):
        return {"ok": False, "error": "岗位缺少有效的 BOSS 原始链接"}

    account = account_for("communication")
    conf = config.ACCOUNTS[account]
    launched = launch(account)
    if not launched.get("ok"):
        return {"ok": False, "error": "沟通号 Chrome 启动失败"}

    browser = None
    try:
        import websocket
        version = _http_get_json(
            f"http://127.0.0.1:{conf['cdp_port']}/json/version") or {}
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("CDP 未提供浏览器连接")
        browser = websocket.create_connection(ws_url, timeout=10)

        def call(message_id: int, method: str, params: dict) -> dict:
            browser.send(json.dumps({"id": message_id, "method": method,
                                     "params": params}))
            while True:
                response = json.loads(browser.recv())
                if response.get("id") != message_id:
                    continue
                if "error" in response:
                    raise RuntimeError(response["error"].get("message", method))
                return response.get("result", {})

        # 新建标签而非复用登录页，避免打断用户正在进行的 BOSS 操作。
        target_id = call(1, "Target.createTarget", {"url": job_link})["targetId"]
        call(2, "Target.activateTarget", {"targetId": target_id})
        return {"ok": True, "account": account, "port": conf["cdp_port"]}
    except Exception as error:
        return {"ok": False, "error": f"沟通号 Chrome 打开岗位失败：{error}"[:200]}
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


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
    websocket = _load_websocket()
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
    """检测登录态；Chrome 未运行时以无头临时启动、导航登录页，完成后再停止。

    检测是只读动作，临时实例走无头，避免为一次探测弹出打扰窗口；
    需要人工登录时用户点「打开登录页」，仍按有头弹窗。
    """
    conf = config.ACCOUNTS[account]
    was_running = is_running(conf["cdp_port"])
    started_here = False
    result = None
    try:
        if not was_running:
            launched = launch(account, headless=True)
            if not launched.get("ok"):
                result = {"account": account, "running": False, "logged_in": None,
                          "hint": launched.get("error", "Chrome 启动失败")}
            else:
                started_here = not launched.get("already_running", False)
                opened = _navigate_login_page(account)
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
