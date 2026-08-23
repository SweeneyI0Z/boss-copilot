"""M6 spike v4（修）：正确的 session 路由 + 全程事件缓冲。"""
import json, time, urllib.request, websocket

PORT = 9222
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ver = json.loads(_opener.open(f"http://127.0.0.1:{PORT}/json/version").read())
bws = ver["webSocketDebuggerUrl"]
ws = websocket.create_connection(bws, timeout=20)
_id = [0]
ALL_EVENTS = []


def call(method, params, sid=None):
    """sessionId 为 CDP 顶层字段，不在 params 里。"""
    _id[0] += 1
    msg = {"id": _id[0], "method": method, "params": params}
    if sid:
        msg["sessionId"] = sid
    ws.send(json.dumps(msg))
    while True:
        r = json.loads(ws.recv())
        if "id" not in r:
            ALL_EVENTS.append(r)      # 事件先行入缓冲
            continue
        if r["id"] == _id[0]:
            if "error" in r:
                raise RuntimeError(f"{method}: {r['error']}")
            return r


def collect(sec):
    ws.settimeout(1)
    end = time.time() + sec
    while time.time() < end:
        try:
            ev = json.loads(ws.recv())
            if "id" not in ev:
                ALL_EVENTS.append(ev)
        except websocket.WebSocketTimeoutException:
            continue


tid = call("Target.createTarget",
           {"url": "https://www.zhipin.com/web/geek/chat", "background": True})["result"]["targetId"]
SID = call("Target.attachToTarget", {"targetId": tid, "flatten": True})["result"]["sessionId"]
call("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid=SID)
call("Network.enable", {}, sid=SID)
collect(12)
call("Runtime.enable", {}, sid=SID)
collect(2)


def urls_from(events, exclude=None):
    out, fin = {}, set()
    for ev in events:
        m, p = ev.get("method", ""), ev.get("params", {})
        if m == "Network.requestWillBeSent":
            out[p.get("requestId")] = p.get("request", {}).get("url", "")
        elif m == "Network.loadingFinished":
            fin.add(p.get("requestId"))
    if exclude:
        out = {k: v for k, v in out.items() if v not in exclude}
    return out, fin


first_urls, _ = urls_from(ALL_EVENTS)
print("== 首屏消息/会话相关 API ==")
for rid, u in first_urls.items():
    if any(k in u.lower() for k in ("msg", "friendlist", "geekfilter", "history")):
        print(" ", u[:140])

before = set(first_urls.values())
r = call("Runtime.evaluate", {"expression": """
(() => { const els = document.querySelectorAll('.friend-content-warp');
  if (els.length < 2) return JSON.stringify({ok:false, n: els.length});
  const rc = els[1].getBoundingClientRect();
  return JSON.stringify({ok:true, x:rc.x+rc.width/2, y:rc.y+rc.height/2,
    text: els[1].innerText.replace(/\\n/g,'|').slice(0,60)}); })()""",
    "returnByValue": True}, sid=SID)
info = json.loads(r["result"]["result"]["value"])
print("\n第2会话:", info)
if info.get("ok"):
    for typ in ("mousePressed", "mouseReleased"):
        call("Input.dispatchMouseEvent",
             {"type": typ, "x": info["x"], "y": info["y"], "button": "left",
              "clickCount": 1}, sid=SID)
    collect(8)
    # 只有点击后到达的事件会新增
    new_events = [e for e in ALL_EVENTS if e not in ()]
    reqs, fin = urls_from(new_events, exclude=before)
    print("\n== 点击后新增 API ==")
    for rid, u in reqs.items():
        if rid in fin and "/wapi/" in u:
            print(" ", u[:150])
            rr = call("Network.getResponseBody", {"requestId": rid}, sid=SID)
            try:
                data = json.loads(rr.get("result", {}).get("body", ""))
                zp = data.get("zpData")
                print("   code:", data.get("code"),
                      "| keys:", list(zp.keys())[:10] if isinstance(zp, dict) else zp)
                for k in ("messages", "result", "list", "msgList", "bossPageMsgList"):
                    v = zp.get(k) if isinstance(zp, dict) else None
                    if isinstance(v, list) and v:
                        print(f"   {k}[0]:", json.dumps(v[0], ensure_ascii=False)[:220])
            except (json.JSONDecodeError, TypeError) as e:
                print("   body解析失败:", e)

call("Target.closeTarget", {"targetId": tid})
ws.close()
print("\ndone")
