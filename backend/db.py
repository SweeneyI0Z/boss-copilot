"""SQLite 数据层：schema、连接、通用 DAO。

状态机与字段设计原则：
- 岗位永不物理删除，靠 status 表达生命周期
- 评分字段同时存原始 JSON 明细与标量摘要，列表页不解析 JSON
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  resume_text TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  expectations TEXT NOT NULL DEFAULT '{}'   -- 期望城市/薪资/方向 等 JSON
);

CREATE TABLE IF NOT EXISTS jobs (
  job_key TEXT PRIMARY KEY,               -- encrypt_job_id 可得则用之，否则 md5(公司|岗位|薪资)
  encrypt_job_id TEXT,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  salary TEXT NOT NULL DEFAULT '',
  salary_min REAL, salary_max REAL,       -- 解析后的数值区间（K）
  salary_months REAL,                     -- 薪数（14薪/16薪），缺省 12
  experience TEXT DEFAULT '',
  degree TEXT DEFAULT '',
  location TEXT DEFAULT '',
  industry TEXT DEFAULT '',
  scale TEXT DEFAULT '',
  stage TEXT DEFAULT '',
  skills TEXT DEFAULT '',                 -- 技能标签（| 分隔）
  hr_active TEXT DEFAULT '',
  jd_len INTEGER DEFAULT 0,
  job_link TEXT DEFAULT '',
  company_link TEXT DEFAULT '',
  source TEXT NOT NULL DEFAULT 'import',  -- import/search/company
  status TEXT NOT NULL DEFAULT 'active',  -- active/delisted/hr_inactive/excluded
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  l1_score REAL, l1_detail TEXT,          -- 电算明细 JSON（含 gate/各维度）
  match_rough REAL,                       -- L1 匹配粗分
  composite_rough REAL,                   -- L1 综合粗分
  job_score REAL, match_score REAL, composite REAL, priority TEXT,  -- L2 终评
  l2_detail TEXT,                         -- L2 明细 JSON（B3/C/D/E2/E3/S 维度/加减分/建议）
  l2_source TEXT DEFAULT ''               -- imported / llm
);

CREATE TABLE IF NOT EXISTS job_details (
  job_key TEXT PRIMARY KEY REFERENCES jobs(job_key),
  jd TEXT NOT NULL DEFAULT '',
  skill_tags TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collect_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                     -- xlsx_import/json_import/search/company/sync
  params TEXT NOT NULL DEFAULT '{}',
  stats TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS greetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL REFERENCES jobs(job_key),
  variants TEXT NOT NULL DEFAULT '[]',    -- 候选招呼语数组
  chosen TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',   -- draft/approved/sending/sent/failed/skipped
  sent_at TEXT, error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,                      -- YYYY-MM-DD，配额统计用
  job_key TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  hr_key TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  boss_key TEXT NOT NULL UNIQUE,          -- encryptBossId 或会话标识
  boss_name TEXT NOT NULL DEFAULT '',
  job_key TEXT,
  last_message_at TEXT,
  unread INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  direction TEXT NOT NULL,                -- in/out
  content TEXT NOT NULL,
  msg_key TEXT UNIQUE,                    -- 去重键（外部消息id或内容hash）
  draft_reply TEXT DEFAULT '',
  draft_status TEXT DEFAULT '',           -- ''/pending/approved/sent
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',  -- active/finished
  transcript TEXT NOT NULL DEFAULT '[]',  -- [{role, content, feedback?}]
  report TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    # 默认设置与档案占位
    for k, v in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                     (k, json.dumps(v, ensure_ascii=False)))
    conn.execute("INSERT OR IGNORE INTO profile(id, resume_text, updated_at, expectations) "
                 "VALUES(1, '', ?, '{}')", (now_iso(),))
    conn.commit()


def get_setting(key: str, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(key: str, value) -> None:
    get_db().execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)))
    get_db().commit()


def get_all_settings() -> dict:
    return {r["key"]: json.loads(r["value"])
            for r in get_db().execute("SELECT key, value FROM settings")}
