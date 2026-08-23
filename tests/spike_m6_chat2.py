"""M6 spike v2：点击第一个会话，捕获消息历史 API（采集号，只读浏览）。"""
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

def ssend(method, params, sid):
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params,
                        "sessionId": sid}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id[0]:
            return r

tid = bsend("Target.createTarget",
            {"url": "https://www.zhipin.com/web/geek/chat", "background": True})["result"]["targetId"]
sid = bsend("Target.attachToTarget", {"targetId": tid, "flatten": True})["result"]["sessionId"]
ssend("Network.enable", {}, sid)
time.sleep(12)

before = set()
def drain(sec):
    ws.settimeout(sec)
    end = time.time() + sec
    evs = []
    while time.time() < end:
        try:
            evs.append(json.loads(ws.recv()))
        except websocket.WebSocketTimeoutException:
            break
    return evs

evs = drain(3)
for ev in evs:
    if ev.get("method") == "Network.requestWillBeSent":
        before.add(ev["params"].get("request", {}).get("url", ""))

# 点击第一个会话项（聊天列表第一项）
r = ssend("Runtime.evaluate", {"expression": """
(() => {
  const items = document.querySelectorAll('.item-friend, [class*=friend-item], li[class*=chat-item], .chat-list li');
  if (!items.length) return JSON.stringify({clicked: false, n: 0});
  const it = items[0];
  const rc = it.getBoundingClientRect();
  return JSON.stringify({clicked: true, n: items.length,
    x: rc.x + rc.width/2, y: rc.y + rc.height/2,
    text: it.innerText.slice(0, 50)});
})()""", "returnByValue": True}, sid)
info = json.loads(r["result"]["result"]["value"])
print("会话项:", info)
if info.get("clicked"):
    ssend("Input.dispatchMouseEvent", {"type": "mousePressed", "x": info["x"],
                                       "y": info["y"], "button": "left", "clickCount": 1}, sid)
    ssend("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": info["x"],
                                       "y": info["y"], "button": "left", "clickCount": 1}, sid)
    time.sleep(6)
    evs2 = drain(3)
    reqs, fin = {}, set()
    for ev in evs2:
        m, p = ev.get("method", ""), ev.get("params", {})
        if m == "Network.requestWillBeSent":
            u = p.get("request", {}).get("url", "")
            if u not in before:
                reqs[p.get("requestId")] = u
        elif m == "Network.loadingFinished":
            fin.add(p.get("requestId"))
    print("\n== 点击后新增请求 ==")
    for rid, u in reqs.items():
        if rid in fin and "/wapi/" in u:
            print(" ", u[:130])
            if "msg" in u.lower() or "chat" in u.lower():
                rr = ssend("Network.getResponseBody", {"requestId": rid}, sid)
                try:
                    data = json.loads(rr.get("result", {}).get("body", ""))
                    zp = data.get("zpData")
                    print("   code:", data.get("code"), "| zpData keys:",
                          list(zp.keys())[:12] if isinstance(zp, dict) else zp)
                    for k in ("messages", "result", "list"):
                        v = zp.get(k) if isinstance(zp, dict) else None
                        if isinstance(v, list) and v:
                            print(f"   {k}[0]:", json.dumps(v[0], ensure_ascii=False)[:200])
                except Exception as e:
                    print("   解析失败:", e)

bsend("Target.closeTarget", {"targetId": tid})
ws.close()
print("\ndone")
