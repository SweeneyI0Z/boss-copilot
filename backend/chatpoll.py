"""消息中心：账号A 聊天页轮询（DOM 快照）+ AI 草稿 + 人工点发。

策略（经 spike 验证）：
- 会话列表 = .friend-content-warp；点开后右侧 .chat-conversation 即完整消息历史
- 后台标签页必须开 Emulation.setFocusEmulationEnabled，否则 SPA 不渲染
- 每个会话存一份「快照」（按内容哈希去重），AI 草稿挂在最新快照上
- 发送 = 点击会话 → 输入框注入 → 回车（复用 sender 的输入与护栏经验）
"""
import hashlib
import json
import random
import time

from . import llm
from .boss import cdp
from .db import get_db, now_iso

ACCOUNT = "account_a"

LIST_PROBE = """
(() => { const els = document.querySelectorAll('.friend-content-warp');
  return JSON.stringify({n: els.length}); })()"""

CLICK_CONV = """
(() => { const els = document.querySelectorAll('.friend-content-warp');
  if (els.length <= %d) return JSON.stringify({ok: false});
  const rc = els[%d].getBoundingClientRect();
  const lines = els[%d].innerText.split('\\n').filter(Boolean);
  return JSON.stringify({ok: true, x: rc.x + rc.width / 2, y: rc.y + rc.height / 2,
    title: lines.slice(0, 4).join(' | ').slice(0, 120)}); })()"""

READ_PANEL = """
(() => { const p = document.querySelector('.chat-conversation');
  return JSON.stringify({text: p ? p.innerText : ''}); })()"""

DRAFT_SYSTEM = """你是求职沟通助手。根据我与招聘方的聊天记录，草拟一条回复。

要求：≤60字；自然、不卑不亢；不承诺简历里没有的能力；如对方要简历/微信/电话，
礼貌给出下一步（如「已投递附件简历，请查收」或「方便先在平台上聊几轮」）。
只输出 JSON：{"reply": "回复内容"}"""


class ChatSession:
    """账号A 聊天页的一个后台标签页会话（焦点仿真）。"""

    def __init__(self):
        import urllib.request
        import websocket
        cdp.launch(ACCOUNT)
        port = cdp.config.ACCOUNTS[ACCOUNT]["cdp_port"]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ver = json.loads(opener.open(f"http://127.0.0.1:{port}/json/version").read())
        self.ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=20)
        self._id = 0

    def call(self, method, params, sid=None):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == self._id:
                if "error" in r:
                    raise RuntimeError(f"{method}: {r['error'].get('message')}")
                return r

    def eval_json(self, js, sid):
        r = self.call("Runtime.evaluate", {"expression": js, "returnByValue": True},
                      sid=sid)
        try:
            return json.loads(r["result"]["result"]["value"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    def open_chat(self):
        tid = self.call("Target.createTarget",
                        {"url": "about:blank", "background": True})["result"]["targetId"]
        sid = self.call("Target.attachToTarget",
                        {"targetId": tid, "flatten": True})["result"]["sessionId"]
        self.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid=sid)
        self.call("Page.navigate", {"url": "https://www.zhipin.com/web/geek/chat"}, sid=sid)
        time.sleep(12)
        return tid, sid

    def close(self, tid):
        try:
            self.call("Target.closeTarget", {"targetId": tid})
        except (RuntimeError, OSError):
            pass
        self.ws.close()


def _conv_key(title: str) -> str:
    return hashlib.md5(title.encode()).hexdigest()[:16]


def poll(max_conversations: int = 8) -> dict:
    """轮询会话并落库。返回统计。"""
    login = cdp.login_state(ACCOUNT)
    if login.get("logged_in") is not True:
        return {"ok": False, "error": "账号A 未登录（到「双账号」页登录后再轮询）"}
    s = ChatSession()
    stats = {"conversations": 0, "new_snapshots": 0}
    tid = sid = None
    try:
        tid, sid = s.open_chat()
        n = s.eval_json(LIST_PROBE, sid).get("n", 0)
        conn = get_db()
        for i in range(min(n, max_conversations)):
            info = s.eval_json(CLICK_CONV % (i, i, i), sid)
            if not info.get("ok"):
                continue
            for typ in ("mousePressed", "mouseReleased"):
                s.call("Input.dispatchMouseEvent",
                       {"type": typ, "x": info["x"], "y": info["y"],
                        "button": "left", "clickCount": 1}, sid=sid)
            time.sleep(random.uniform(2.5, 4.5))
            panel = s.eval_json(READ_PANEL, sid).get("text", "")
            title = info.get("title", f"会话{i}")
            stats["conversations"] += 1
            key = _conv_key(title)
            snap_hash = hashlib.md5(panel.encode()).hexdigest()[:16]
            exists = conn.execute(
                "SELECT id FROM conversations WHERE boss_key=?", (key,)).fetchone()
            conv_id = exists["id"] if exists else None
            if conv_id is None:
                cur = conn.execute(
                    "INSERT INTO conversations(boss_key, boss_name, last_message_at) "
                    "VALUES(?,?,?)", (key, title[:60], now_iso()))
                conv_id = cur.lastrowid
            else:
                conn.execute("UPDATE conversations SET last_message_at=? WHERE id=?",
                             (now_iso(), conv_id))
            seen = conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? AND msg_key=?",
                (conv_id, snap_hash)).fetchone()
            if seen is None:
                conn.execute(
                    "INSERT INTO messages(conversation_id, direction, content, msg_key,"
                    " created_at) VALUES(?,?,?,?,?)",
                    (conv_id, "snapshot", panel[:4000], snap_hash, now_iso()))
                stats["new_snapshots"] += 1
            conn.commit()
    finally:
        if tid:
            s.close(tid)
    return {"ok": True, **stats}


def conversations_with_drafts() -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT c.*, (SELECT content FROM messages m WHERE m.conversation_id=c.id "
        "ORDER BY m.id DESC LIMIT 1) latest_snapshot FROM conversations c "
        "ORDER BY c.last_message_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        drafts = conn.execute(
            "SELECT id, content, draft_reply, draft_status, created_at FROM messages "
            "WHERE conversation_id=? AND draft_reply != '' ORDER BY id DESC LIMIT 3",
            (r["id"],)).fetchall()
        d["drafts"] = [dict(x) for x in drafts]
        out.append(d)
    return out


def generate_draft(conversation_id: int, client=None) -> dict:
    """为会话最新快照生成 AI 回复草稿（不发送）。"""
    conn = get_db()
    snap = conn.execute(
        "SELECT id, content FROM messages WHERE conversation_id=? "
        "ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
    if snap is None:
        raise llm.LLMError("会话没有快照，请先轮询")
    prof = conn.execute("SELECT resume_text FROM profile WHERE id=1").fetchone()
    if not llm.configured():
        raise llm.LLMError("LLM 未配置")
    user = (f"# 招聘方与我最近的聊天（快照）\n{snap['content'][:2500]}\n\n"
            f"# 我的简历（节选）\n{(prof['resume_text'] or '')[:1500]}")
    out = llm.chat_json([{"role": "system", "content": DRAFT_SYSTEM},
                         {"role": "user", "content": user}], client=client)
    reply = (out.get("reply") or "").strip()
    if not reply:
        raise llm.LLMError("模型没有给出回复")
    conn.execute(
        "UPDATE messages SET draft_reply=?, draft_status='pending' WHERE id=?",
        (reply, snap["id"]))
    conn.commit()
    return {"reply": reply}


def _latest_msg_id(conversation_id: int) -> int:
    return get_db().execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
        (conversation_id,)).fetchone()["id"]


def approve_and_send(conversation_id: int, reply_text: str) -> dict:
    """人工点发：点开会话 → 输入框注入 → 回车。带基础间隔保护。"""
    login = cdp.login_state(ACCOUNT)
    if login.get("logged_in") is not True:
        return {"ok": False, "error": "账号A 未登录"}
    conn = get_db()
    conv = conn.execute("SELECT * FROM conversations WHERE id=?",
                        (conversation_id,)).fetchone()
    if conv is None:
        return {"ok": False, "error": "会话不存在"}
    from .sender import FIND_INPUT_JS, _type_and_send
    s = ChatSession()
    tid = sid = None
    try:
        tid, sid = s.open_chat()
        n = s.eval_json(LIST_PROBE, sid).get("n", 0)
        target = None
        for i in range(n):
            info = s.eval_json(CLICK_CONV % (i, i, i), sid)
            if info.get("ok") and _conv_key(info.get("title", "")) == conv["boss_key"]:
                target = info
                break
        if target is None:
            return {"ok": False, "error": "会话在页面列表中未找到（可能被折叠）"}
        for typ in ("mousePressed", "mouseReleased"):
            s.call("Input.dispatchMouseEvent",
                   {"type": typ, "x": target["x"], "y": target["y"],
                    "button": "left", "clickCount": 1}, sid=sid)
        time.sleep(3)
        box = s.eval_json(FIND_INPUT_JS, sid)
        if not box.get("found"):
            return {"ok": False, "error": "未找到输入框"}
        _type_and_send(s, sid, box, reply_text)
        time.sleep(2)
        conn.execute(
            "UPDATE messages SET draft_status='sent' WHERE conversation_id=? "
            "AND draft_reply=?", (conversation_id, reply_text))
        conn.execute(
            "INSERT INTO messages(conversation_id, direction, content, msg_key, "
            "created_at) VALUES(?,?,?,?,?)",
            (conversation_id, "out", reply_text,
             hashlib.md5(("out" + reply_text).encode()).hexdigest()[:16], now_iso()))
        conn.commit()
        return {"ok": True}
    finally:
        if tid:
            s.close(tid)
