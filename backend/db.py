"""SQLite 数据层：schema、连接、通用 DAO。

状态机与字段设计原则：
- 岗位常规生命周期不物理删除；仅采集中心用户二次确认删除采集记录时，可删除其独占岗位数据
- 评分字段同时存原始 JSON 明细与标量摘要，列表页不解析 JSON
"""
import json
import sqlite3
import threading
import weakref
from datetime import datetime, timezone

from . import config

_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _close_thread_conn(conn) -> None:
    """线程对象被回收时关闭其线程本地连接。

    线程本地连接在线程死亡后要等循环 GC 才释放，Windows 上期间文件句柄
    一直被占用；绑定到线程对象的终结器让关闭动作确定性发生。
    """
    try:
        conn.close()
    except sqlite3.Error:
        pass


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", "") != str(config.DB_PATH):
        close_db()
        conn = None
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 本项目约定连接严格线程本地、绝不跨线程并发使用；
        # check_same_thread=False 仅为允许线程死亡后由终结器跨线程关闭。
        conn = sqlite3.connect(config.DB_PATH, timeout=10,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.path = str(config.DB_PATH)
        weakref.finalize(threading.current_thread(), _close_thread_conn, conn)
    return conn


def close_db() -> None:
    """关闭当前线程的 SQLite 连接。

    Windows 不允许删除仍被打开的文件（POSIX 可以），测试与切库场景必须先
    关闭连接，否则临时数据目录里的 WAL 文件会让目录清理失败。
    """
    conn = getattr(_local, "conn", None)
    _local.conn = None
    _local.path = None
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS account_states (
  account TEXT PRIMARY KEY,
  logged_in INTEGER,                       -- 1/0/NULL（无法确认）
  hint TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  resume_text TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  expectations TEXT NOT NULL DEFAULT '{}'   -- 期望城市/薪资/方向 等 JSON
);

CREATE TABLE IF NOT EXISTS resumes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  resume_text TEXT NOT NULL DEFAULT '',
  expectations TEXT NOT NULL DEFAULT '{}',
  skill_profile TEXT NOT NULL DEFAULT '{}',
  boss_resume_label TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  archived_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT '',          -- queued/running/succeeded/partial/failed/cancelled
  data_source_at TEXT,                     -- 文件内数据时间；与运行完成时间分开
  risk_signal TEXT NOT NULL DEFAULT '',
  phase TEXT NOT NULL DEFAULT '',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,       -- 是否纳入岗位列表/总览/分析
  paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_run_items (
  run_id INTEGER NOT NULL REFERENCES collect_runs(id) ON DELETE CASCADE,
  job_key TEXT NOT NULL REFERENCES jobs(job_key) ON DELETE CASCADE,
  source TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, job_key)
);

CREATE TABLE IF NOT EXISTS collect_run_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES collect_runs(id) ON DELETE CASCADE,
  task_key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'search',
  keyword TEXT NOT NULL DEFAULT '',
  province TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL DEFAULT '',
  city_code TEXT NOT NULL DEFAULT '',
  filters TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  stats TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  list_file TEXT NOT NULL DEFAULT '',
  detail_file TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(run_id, task_key)
);

CREATE TABLE IF NOT EXISTS greetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL REFERENCES jobs(job_key),
  variants TEXT NOT NULL DEFAULT '[]',    -- 候选招呼语数组
  chosen TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',   -- draft/approved/sending/sent/failed/skipped
  sent_at TEXT, error TEXT,
  created_at TEXT NOT NULL,
  resume_id INTEGER REFERENCES resumes(id),
  resume_revision INTEGER,
  delivery_channel TEXT NOT NULL DEFAULT '', -- manual/auto
  delivery_status TEXT NOT NULL DEFAULT '', -- confirmed/needs_review/failed
  confirmed_at TEXT,
  updated_at TEXT
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
  created_at TEXT NOT NULL,
  resume_id INTEGER REFERENCES resumes(id),
  resume_revision INTEGER
);

CREATE TABLE IF NOT EXISTS job_resume_scores (
  job_key TEXT NOT NULL REFERENCES jobs(job_key) ON DELETE CASCADE,
  resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
  resume_revision INTEGER NOT NULL,
  l1_score REAL,
  l1_detail TEXT NOT NULL DEFAULT '{}',
  match_rough REAL,
  composite_rough REAL,
  l1_source TEXT NOT NULL DEFAULT '',
  job_score REAL,
  match_score REAL,
  composite REAL,
  priority TEXT NOT NULL DEFAULT '',
  l2_detail TEXT NOT NULL DEFAULT '{}',
  l2_source TEXT NOT NULL DEFAULT '',
  l2_stale INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(job_key, resume_id, resume_revision)
);

CREATE TABLE IF NOT EXISTS job_score_baselines (
  job_key TEXT PRIMARY KEY,
  l1_score REAL,
  l1_detail TEXT NOT NULL DEFAULT '{}',
  match_rough REAL,
  composite_rough REAL,
  job_score REAL,
  match_score REAL,
  composite REAL,
  priority TEXT NOT NULL DEFAULT '',
  l2_detail TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'imported',
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL REFERENCES jobs(job_key) ON DELETE CASCADE,
  resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'unknown',   -- unknown/platform_confirmed/manual_confirmed
  probe_evidence TEXT NOT NULL DEFAULT '',
  probe_error TEXT NOT NULL DEFAULT '',
  last_probed_at TEXT,
  confirmed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(job_key, resume_id)
);

CREATE TABLE IF NOT EXISTS job_workflow_states (
  job_key TEXT NOT NULL REFERENCES jobs(job_key) ON DELETE CASCADE,
  resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
  greeted_at TEXT,
  applied_at TEXT,
  interviewed_at TEXT,
  offered_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(job_key, resume_id)
);

CREATE TABLE IF NOT EXISTS job_collection_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES collect_run_tasks(id) ON DELETE CASCADE,
  run_id INTEGER NOT NULL REFERENCES collect_runs(id) ON DELETE CASCADE,
  job_key TEXT NOT NULL REFERENCES jobs(job_key) ON DELETE CASCADE,
  search_key TEXT NOT NULL,
  keyword TEXT NOT NULL DEFAULT '',
  province TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL DEFAULT '',
  city_code TEXT NOT NULL DEFAULT '',
  filters TEXT NOT NULL DEFAULT '{}',
  is_active INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(search_key, job_key, run_id)
);

CREATE TABLE IF NOT EXISTS job_favorite_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL REFERENCES jobs(job_key) ON DELETE CASCADE,
  account TEXT NOT NULL,                      -- collect / account_a（哪个账号的BOSS收藏）
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(job_key, account)
);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str,
                 columns: dict[str, str]) -> None:
    """给旧库补列；SQLite 不支持一次添加多列，因此逐列幂等执行。"""
    existing = _table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _json_source(raw: str, fallback: str = "") -> str:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return fallback
    return value.get("source", fallback) if isinstance(value, dict) else fallback


def _migrate_legacy_data(conn: sqlite3.Connection) -> None:
    """把单档案和 jobs 内嵌评分迁到新模型；只补缺失记录，绝不回写历史值。"""
    default = conn.execute(
        "SELECT * FROM resumes WHERE is_default=1 AND archived_at IS NULL "
        "ORDER BY id LIMIT 1").fetchone()
    if default is None:
        first = conn.execute(
            "SELECT * FROM resumes WHERE archived_at IS NULL ORDER BY id LIMIT 1").fetchone()
        if first is None:
            legacy = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
            ts = (legacy["updated_at"] if legacy else None) or now_iso()
            cur = conn.execute(
                "INSERT INTO resumes(name, resume_text, expectations, skill_profile, "
                "boss_resume_label, revision, is_default, created_at, updated_at) "
                "VALUES(?,?,?,?,?,1,1,?,?)",
                ("默认简历", legacy["resume_text"] if legacy else "",
                 legacy["expectations"] if legacy else "{}", "{}", "", ts, ts))
            default_id = cur.lastrowid
        else:
            default_id = first["id"]
            conn.execute("UPDATE resumes SET is_default=1 WHERE id=?", (default_id,))
    else:
        default_id = default["id"]

    # 异常中断或旧试验库可能留下多个默认项，建唯一索引前先确定唯一默认档案。
    conn.execute(
        "UPDATE resumes SET is_default=CASE WHEN id=? THEN 1 ELSE 0 END "
        "WHERE archived_at IS NULL", (default_id,))

    # legacy profile 的迁移目标一经确定就保持不变；后续切换默认简历或提升修订号
    # 时，重启不能把 jobs 旧镜像灌入另一份简历的当前修订。
    marker = conn.execute(
        "SELECT value FROM settings WHERE key='legacy_profile_resume_id'").fetchone()
    try:
        legacy_resume_id = int(json.loads(marker["value"])) if marker else None
    except (TypeError, ValueError, json.JSONDecodeError):
        legacy_resume_id = None
    legacy_resume = conn.execute(
        "SELECT * FROM resumes WHERE id=?", (legacy_resume_id,)).fetchone() \
        if legacy_resume_id else None
    if legacy_resume is None:
        legacy_resume_id = default_id
        legacy_resume = conn.execute(
            "SELECT * FROM resumes WHERE id=?", (legacy_resume_id,)).fetchone()
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('legacy_profile_resume_id',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(legacy_resume_id),))
    revision = 1
    completed = conn.execute(
        "SELECT value FROM settings WHERE key='legacy_score_migration_completed'").fetchone()
    try:
        migration_done = bool(json.loads(completed["value"])) if completed else False
    except (TypeError, ValueError, json.JSONDecodeError):
        migration_done = False
    if not migration_done:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE l1_score IS NOT NULL OR job_score IS NOT NULL "
            "OR match_score IS NOT NULL OR composite IS NOT NULL").fetchall()
        for row in rows:
            l1_source = _json_source(row["l1_detail"], "legacy")
            l2_source = row["l2_source"] or "legacy"
            ts = row["last_seen_at"] or now_iso()
            conn.execute(
                "INSERT OR IGNORE INTO job_resume_scores("
                "job_key, resume_id, resume_revision, l1_score, l1_detail, match_rough, "
                "composite_rough, l1_source, job_score, match_score, composite, priority, "
                "l2_detail, l2_source, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["job_key"], legacy_resume_id, revision, row["l1_score"],
                 row["l1_detail"] or "{}", row["match_rough"], row["composite_rough"],
                 l1_source, row["job_score"], row["match_score"], row["composite"],
                 row["priority"] or "", row["l2_detail"] or "{}", l2_source, ts, ts))
            if l1_source == "imported" or l2_source == "imported":
                conn.execute(
                    "INSERT OR IGNORE INTO job_score_baselines("
                    "job_key, l1_score, l1_detail, match_rough, composite_rough, job_score, "
                    "match_score, composite, priority, l2_detail, imported_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (row["job_key"], row["l1_score"], row["l1_detail"] or "{}",
                     row["match_rough"], row["composite_rough"], row["job_score"],
                     row["match_score"], row["composite"], row["priority"] or "",
                     row["l2_detail"] or "{}", ts))
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('legacy_score_migration_completed','true') "
            "ON CONFLICT(key) DO UPDATE SET value='true'")

    conn.execute(
        "UPDATE greetings SET resume_id=?, resume_revision=? "
        "WHERE resume_id IS NULL", (legacy_resume_id, revision))
    conn.execute(
        "UPDATE greetings SET delivery_channel='auto' "
        "WHERE delivery_channel='' AND status IN "
        "('sending','sent','failed')")
    conn.execute(
        "UPDATE greetings SET delivery_status=CASE "
        "WHEN status='sent' THEN 'confirmed' WHEN status='failed' THEN 'failed' "
        "WHEN status='sending' THEN 'needs_review' ELSE delivery_status END "
        "WHERE delivery_status='' AND status IN ('sending','sent','failed')")
    conn.execute(
        "UPDATE greetings SET confirmed_at=COALESCE(sent_at, created_at) "
        "WHERE confirmed_at IS NULL AND delivery_status='confirmed'")
    conn.execute(
        "UPDATE greetings SET updated_at=COALESCE(sent_at, created_at) "
        "WHERE updated_at IS NULL")
    conn.execute(
        "UPDATE interviews SET resume_id=?, resume_revision=? WHERE resume_id IS NULL",
        (legacy_resume_id, revision))


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    # 轻量迁移列全部保留默认值，旧代码可继续直接 INSERT/UPDATE。
    _add_columns(conn, "jobs", {
        "origin_query": "TEXT DEFAULT ''",
        "favorite_at": "TEXT",
        "excluded_at": "TEXT",
        "status_before_excluded": "TEXT NOT NULL DEFAULT ''",
        "hr_title": "TEXT NOT NULL DEFAULT ''",
        "is_headhunter": "INTEGER NOT NULL DEFAULT 0",
        "headhunter_reason": "TEXT NOT NULL DEFAULT ''",
        "headhunter_override": "INTEGER",
    })
    _add_columns(conn, "greetings", {
        "resume_id": "INTEGER",
        "resume_revision": "INTEGER",
        "delivery_channel": "TEXT NOT NULL DEFAULT ''",
        "delivery_status": "TEXT NOT NULL DEFAULT ''",
        "confirmed_at": "TEXT",
        "updated_at": "TEXT",
    })
    _add_columns(conn, "collect_runs", {
        "status": "TEXT NOT NULL DEFAULT ''",
        "data_source_at": "TEXT",
        "risk_signal": "TEXT NOT NULL DEFAULT ''",
        "phase": "TEXT NOT NULL DEFAULT ''",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "paused": "INTEGER NOT NULL DEFAULT 0",
    })
    _add_columns(conn, "interviews", {
        "resume_id": "INTEGER",
        "resume_revision": "INTEGER",
    })
    _add_columns(conn, "job_workflow_states", {
        "offered_at": "TEXT",
    })
    # 默认设置与档案占位
    for k, v in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                     (k, json.dumps(v, ensure_ascii=False)))
    conn.execute("INSERT OR IGNORE INTO profile(id, resume_text, updated_at, expectations) "
                 "VALUES(1, '', ?, '{}')", (now_iso(),))
    _migrate_legacy_data(conn)
    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_resumes_one_default
          ON resumes(is_default) WHERE is_default=1 AND archived_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_jobs_favorite ON jobs(favorite_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_headhunter
          ON jobs(headhunter_override, is_headhunter);
        CREATE INDEX IF NOT EXISTS idx_job_resume_scores_current
          ON job_resume_scores(resume_id, resume_revision, composite DESC);
        CREATE INDEX IF NOT EXISTS idx_applications_status
          ON applications(status, confirmed_at);
        CREATE INDEX IF NOT EXISTS idx_collect_tasks_run_status
          ON collect_run_tasks(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_collection_hits_source_active
          ON job_collection_hits(search_key, is_active);
        CREATE INDEX IF NOT EXISTS idx_collection_hits_job_active
          ON job_collection_hits(job_key, is_active);
        CREATE INDEX IF NOT EXISTS idx_job_run_items_job
          ON job_run_items(job_key, run_id);
        CREATE INDEX IF NOT EXISTS idx_favorite_hits_job
          ON job_favorite_hits(job_key);
        CREATE INDEX IF NOT EXISTS idx_workflow_resume
          ON job_workflow_states(resume_id, updated_at);
        CREATE TRIGGER IF NOT EXISTS job_score_baselines_no_update
        BEFORE UPDATE ON job_score_baselines
        BEGIN
          SELECT RAISE(ABORT, '导入评分基线不可修改');
        END;
    """)
    conn.execute(
        "UPDATE collect_runs SET status=CASE "
        "WHEN finished_at IS NULL THEN 'running' "
        "WHEN stats LIKE '%\"error\"%' THEN 'failed' ELSE 'succeeded' END "
        "WHERE status='' OR status IS NULL")
    conn.execute(
        "INSERT OR IGNORE INTO job_run_items(run_id,job_key,source,created_at) "
        "SELECT DISTINCT run_id,job_key,'collection_hit',COALESCE(first_seen_at,?) "
        "FROM job_collection_hits", (now_iso(),))
    conn.execute(
        "INSERT OR IGNORE INTO job_run_items(run_id,job_key,source,created_at) "
        "SELECT (SELECT id FROM collect_runs WHERE kind IN ('xlsx_import','json_import') "
        "ORDER BY id DESC LIMIT 1),j.job_key,'legacy_import',? FROM jobs j "
        "WHERE j.source='import' AND EXISTS(SELECT 1 FROM collect_runs "
        "WHERE kind IN ('xlsx_import','json_import')) AND NOT EXISTS("
        "SELECT 1 FROM job_run_items m WHERE m.job_key=j.job_key)", (now_iso(),))
    conn.execute(
        "INSERT OR IGNORE INTO job_run_items(run_id,job_key,source,created_at) "
        "SELECT (SELECT id FROM collect_runs WHERE kind='favorite_sync' "
        "ORDER BY id DESC LIMIT 1),j.job_key,'legacy_favorite',? FROM jobs j "
        "WHERE EXISTS(SELECT 1 FROM collect_runs WHERE kind='favorite_sync') "
        "AND EXISTS(SELECT 1 FROM job_favorite_hits f WHERE f.job_key=j.job_key) "
        "AND NOT EXISTS(SELECT 1 FROM job_run_items m WHERE m.job_key=j.job_key)",
        (now_iso(),))
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
