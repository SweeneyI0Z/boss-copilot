"""M5 只读 spike：验证 job_detail 页「立即沟通」按钮的检测选择器（不点击）。"""
import json, time, urllib.request, websocket
from backend.sender import FIND_CHAT_BTN_JS, FIND_INPUT_JS

item = json.loads(urllib.request.urlopen(
    "http://127.0.0.1:8787/api/jobs?limit=1&sort=composite").read())["items"][0]
full = json.loads(urllib.request.urlopen(
    f'http://127.0.0.1:8787/api/jobs/{item["job_key"]}').read())
link = {"title": full["title"], "company": full["company"], "job_link": full["job_link"]}
print("测试岗位:", link["title"], "|", link["company"], "|", link["job_link"])

targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
page = next(t for t in targets if t["type"] == "page")
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15)
_id = 0

def send(method, params):
    global _id
    _id += 1
    ws.send(json.dumps({"id": _id, "method": method, "params": params}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id:
            return r

def eval_js(js):
    return send("Runtime.evaluate", {"expression": js, "returnByValue": True}) \
        .get("result", {}).get("result", {}).get("value")

send("Page.navigate", {"url": link["job_link"]})
time.sleep(8)
btn = json.loads(eval_js(FIND_CHAT_BTN_JS) or "{}")
print("沟通按钮检测:", btn)
inp = json.loads(eval_js(FIND_INPUT_JS) or "{}")
print("聊天输入框检测(未点击,应为false):", inp)
ws.close()
