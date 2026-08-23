"""BOSS 原平台状态的按需只读探测。

只读取当前岗位页明确展示的“简历已发送”状态，不同步会话、不解析消息方向。
未出现确定证据时一律返回 unknown，由用户在作战卡人工确认。
"""
import json
import re
import time
import urllib.request

from .boss import cdp


_APPLICATION_PATTERNS = (
    re.compile(r"简历已发送"),
    re.compile(r"已发送(?:附件|在线)?简历"),
    re.compile(r"已投递(?:附件|在线)?简历"),
)
_RISK_PATTERN = re.compile(r"操作过于频繁|环境存在异常|安全校验|滑块|验证")


def classify_application_text(text: str) -> dict:
    """仅接受明确完成态，避免把“发送简历”按钮误判为已投递。"""
    text = text or ""
    if _RISK_PATTERN.search(text):
        return {"status": "unknown", "risk": True,
                "hint": "页面出现风控或验证信号，已停止探测"}
    for pattern in _APPLICATION_PATTERNS:
        match = pattern.search(text)
        if match:
            return {"status": "platform_confirmed", "risk": False,
                    "evidence": match.group(0)}
    return {"status": "unknown", "risk": False,
            "hint": "未发现明确的简历已发送标志，可在 BOSS 完成后人工确认"}


class _BrowserSession:
    def __init__(self, account: str):
        import websocket
        self.account = account
        port = cdp.config.ACCOUNTS[account]["cdp_port"]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        version = json.loads(opener.open(
            f"http://127.0.0.1:{port}/json/version", timeout=5).read())
        self.ws = websocket.create_connection(
            version["webSocketDebuggerUrl"], timeout=20)
        self._id = 0

    def call(self, method: str, params: dict, session_id: str = "") -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params}
        if session_id:
            message["sessionId"] = session_id
        self.ws.send(json.dumps(message))
        while True:
            response = json.loads(self.ws.recv())
            if response.get("id") == self._id:
                if "error" in response:
                    raise RuntimeError(response["error"].get("message", method))
                return response

    def close(self):
        self.ws.close()


def probe_job_page(job_link: str, wait_sec: int = 12) -> dict:
    """用沟通号按需打开单个岗位页并读取明确投递状态。"""
    if not job_link:
        return {"status": "unknown", "hint": "岗位缺少 BOSS 原始链接"}
    account = cdp.account_for("communication")
    launched = cdp.launch(account)
    if not launched.get("ok"):
        return {"status": "unknown", "hint": "沟通号 Chrome 启动失败"}
    login = cdp.login_state(account)
    if login.get("logged_in") is not True:
        return {"status": "unknown", "hint": "沟通号未登录"}

    browser = None
    target_id = session_id = ""
    try:
        browser = _BrowserSession(account)
        target_id = browser.call("Target.createTarget", {
            "url": "about:blank", "background": True,
        })["result"]["targetId"]
        session_id = browser.call("Target.attachToTarget", {
            "targetId": target_id, "flatten": True,
        })["result"]["sessionId"]
        browser.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, session_id)
        browser.call("Page.navigate", {"url": job_link}, session_id)
        deadline = time.time() + max(3, wait_sec)
        latest = ""
        while time.time() < deadline:
            time.sleep(1)
            result = browser.call("Runtime.evaluate", {
                "expression": "document.body ? document.body.innerText.slice(0,12000) : ''",
                "returnByValue": True,
            }, session_id)
            latest = result.get("result", {}).get("result", {}).get("value") or ""
            classified = classify_application_text(latest)
            if classified.get("status") == "platform_confirmed" or classified.get("risk"):
                return classified
        return classify_application_text(latest)
    except Exception as error:  # WebSocket 超时/断连也必须优雅降级为 unknown
        return {"status": "unknown", "hint": f"平台状态探测失败：{error}"[:200]}
    finally:
        if browser and target_id:
            try:
                browser.call("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
