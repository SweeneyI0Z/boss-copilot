"""M6 spike v3：聊天页左侧会话列表 DOM 探测。"""
import json, time, urllib.request, websocket

PORT = 9222
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ver = json.loads(_opener.open(f"http://127.0.0.1:{PORT}/json/version").read())
bws = ver["webSocketDebuggerUrl"]
ws = websocket.create_connection(bws, timeout=15)
_id = [0]

def bsend(method, params):
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id[0]:
            return r

tid = bsend("Target.createTarget",
            {"url": "https://www.zhipin.com/web/geek/chat", "background": True})["result"]["targetId"]
sid = bsend("Target.attachToTarget", {"targetId": tid, "flatten": True})["result"]["sessionId"]
bsend("Emulation.setFocusEmulationEnabled", {"enabled": True, "sessionId": sid})
time.sleep(12)
_id[0] += 1
ws.send(json.dumps({"id": _id[0], "method": "Runtime.evaluate", "sessionId": sid,
                    "params": {"expression": """
(() => {
  // 找包含会话的滚动容器：有多个相似兄弟结构的列表
  const out = {body_len: document.body.innerText.length,
               head: document.body.innerText.slice(0, 150)};
  // 常见聊天页 class
  for (const sel of ['.chat-conversation', '.conversation-list', '[class*=conversation]',
                     '[class*=session]', '[class*=friend]', '.chat-list', '[class*=list]']) {
    const els = document.querySelectorAll(sel);
    if (els.length) {
      out[sel] = {n: els.length,
        first_cls: els[0].className + '',
        first_text: (els[0].innerText || '').replace(/\\n/g, '|').slice(0, 80)};
    }
  }
  return JSON.stringify(out);
})()""", "returnByValue": True, "awaitPromise": False}}))
while True:
    r = json.loads(ws.recv())
    if r.get("id") == _id[0]:
        print(r.get("result", {}).get("result", {}).get("value"))
        break
time.sleep(3)
bsend("Target.closeTarget", {"targetId": tid})
ws.close()
