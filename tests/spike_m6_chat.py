"""M6 只读 spike：BOSS 聊天页(/web/geek/chat)的 wapi 端点探测（采集号 9222）。"""
import json, time, urllib.request, websocket

PORT = 9222
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过系统代理
ver = json.loads(_opener.open(f"http://127.0.0.1:{PORT}/json/version").read())
bws = ver["webSocketDebuggerUrl"]

# 建一个新后台标签页（复用 scraper 的方式：Target.createTarget + attach）
ws = websocket.create_connection(bws, timeout=15)
_id = [0]

def send(method, params):
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id[0]:
            return r

r = send("Target.createTarget", {"url": "about:blank", "background": True})
tid = r["result"]["targetId"]
r = send("Target.attachToTarget", {"targetId": tid, "flatten": True})
sid = r["result"]["sessionId"]

def to_session(resp):
    return {"id": resp["id"], "result": resp.get("result", {}),
            "method": resp.get("method"), "params": resp.get("params", {})}

# session 级命令需带 sessionId 字段
def ssend(method, params):
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params,
                        "sessionId": sid}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id[0]:
            return r

events = []
ssend("Network.enable", {})
ssend("Page.navigate", {"url": "https://www.zhipin.com/web/geek/chat"})
t0 = time.time()
while time.time() - t0 < 15:
    ws.settimeout(2)
    try:
        ev = json.loads(ws.recv())
        events.append(ev)
    except websocket.WebSocketTimeoutException:
        pass

requests_map, finished = {}, set()
for ev in events:
    m, p = ev.get("method", ""), ev.get("params", {})
    if m == "Network.requestWillBeSent":
        requests_map[p.get("requestId")] = p.get("request", {}).get("url", "")
    elif m == "Network.loadingFinished":
        finished.add(p.get("requestId"))

print("== 聊天页 wapi 请求 ==")
hits = []
for rid, u in requests_map.items():
    if "/wapi/" in u and rid in finished:
        print(" ", u.split("?")[0])
        hits.append((rid, u))

# 取前几个响应体看结构
print("\n== 响应结构（前4个含 friend/chat/message 关键字的）==")
count = 0
for rid, u in hits:
    if not any(k in u.lower() for k in ("friend", "chat", "message", "geeklist")):
        continue
    r = ssend("Network.getResponseBody", {"requestId": rid})
    body = r.get("result", {}).get("body", "")
    try:
        data = json.loads(body)
    except Exception:
        continue
    zp = data.get("zpData")
    print(f"\n[code={data.get('code')}] {u[:110]}")
    if isinstance(zp, dict):
        print("  zpData keys:", list(zp.keys())[:12])
        for k in ("result", "friendList", "messages", "list"):
            v = zp.get(k)
            if isinstance(v, list) and v:
                print(f"  {k}[0] keys:", list(v[0].keys())[:15] if isinstance(v[0], dict) else v[0])
    count += 1
    if count >= 4:
        break

send("Target.closeTarget", {"targetId": tid})
ws.close()
print("\ndone")
