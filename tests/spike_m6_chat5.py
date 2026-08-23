"""M6 spike v5：scraper 同款 attach 顺序（空白页→attach→焦点仿真→导航），
点开会话后直接读右侧面板 DOM 消息。"""
import json, time, urllib.request, websocket

PORT = 9222
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ver = json.loads(_opener.open(f"http://127.0.0.1:{PORT}/json/version").read())
ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=20)
_id = [0]

def call(method, params, sid=None):
    _id[0] += 1
    msg = {"id": _id[0], "method": method, "params": params}
    if sid:
        msg["sessionId"] = sid
    ws.send(json.dumps(msg))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id[0]:
            return r
    # 事件被丢弃（本 spike 不需要 Network）

tid = call("Target.createTarget", {"url": "about:blank", "background": True})["result"]["targetId"]
sid = call("Target.attachToTarget", {"targetId": tid, "flatten": True})["result"]["sessionId"]
call("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid=sid)
call("Page.navigate", {"url": "https://www.zhipin.com/web/geek/chat"}, sid=sid)
time.sleep(12)

r = call("Runtime.evaluate", {"expression": """
(() => { const els = document.querySelectorAll('.friend-content-warp');
  return JSON.stringify({n: els.length}); })()""", "returnByValue": True}, sid=sid)
print("会话数:", r["result"]["result"].get("value"))

def click_and_read(i):
    r = call("Runtime.evaluate", {"expression": f"""
    (() => {{ const els = document.querySelectorAll('.friend-content-warp');
      if (els.length <= {i}) return JSON.stringify({{ok:false}});
      const rc = els[{i}].getBoundingClientRect();
      return JSON.stringify({{ok:true, x:rc.x+rc.width/2, y:rc.y+rc.height/2,
        title: els[{i}].innerText.split('\\n').slice(0,3).join('|') }}); }})()""",
        "returnByValue": True}, sid=sid)
    info = json.loads(r["result"]["result"]["value"])
    if not info.get("ok"):
        return None
    for typ in ("mousePressed", "mouseReleased"):
        call("Input.dispatchMouseEvent", {"type": typ, "x": info["x"], "y": info["y"],
                                          "button": "left", "clickCount": 1}, sid=sid)
    time.sleep(4)
    r2 = call("Runtime.evaluate", {"expression": """
    (() => { const p = document.querySelector('.chat-conversation');
      return JSON.stringify({text: p ? p.innerText.slice(0, 600) : null}); })()""",
        "returnByValue": True}, sid=sid)
    out = json.loads(r2["result"]["result"]["value"])
    return {"title": info.get("title"), "panel": out["text"]}

for i in range(2):
    res = click_and_read(i)
    if res:
        print(f"\n== 会话[{i}]: {res['title']}")
        print((res["panel"] or "(空)")[:400].replace("\n", " | "))

call("Target.closeTarget", {"targetId": tid})
ws.close()
print("\ndone")
