import sqlite3
from contextlib import contextmanager

from .config import DB_PATH, DEFAULT_ALERT_THRESHOLD

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    alert_threshold INTEGER NOT NULL DEFAULT {DEFAULT_ALERT_THRESHOLD},
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('A', 'HK', 'US')),
    thesis TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, market, ticker)
);
-- 原始新闻按 (市场, 代码) 共享，多个用户持有同一公司只抓取一次
CREATE TABLE IF NOT EXISTS raw_news (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT DEFAULT '',
    url TEXT DEFAULT '',
    source TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    uniq TEXT UNIQUE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_news_ticker ON raw_news (market, ticker, fetched_at);
-- 某公司已经分析过的原始新闻，避免重复分析
CREATE TABLE IF NOT EXISTS seen_news (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    raw_news_id INTEGER NOT NULL REFERENCES raw_news(id) ON DELETE CASCADE,
    PRIMARY KEY (company_id, raw_news_id)
);
CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    report_date TEXT NOT NULL,
    day_summary TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company_id, report_date)
);
CREATE TABLE IF NOT EXISTS report_items (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    direction TEXT DEFAULT '中性',
    score REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    horizon TEXT DEFAULT '',
    frameworks_json TEXT DEFAULT '{{}}',
    reasoning TEXT DEFAULT '',
    watch_points TEXT DEFAULT '',
    thesis_impact TEXT DEFAULT '',
    thesis_broken INTEGER DEFAULT 0,
    sources_json TEXT DEFAULT '[]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 每日概览：每个用户每天一条，当天每次检索完成后覆盖更新
CREATE TABLE IF NOT EXISTS user_digests (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    digest_date TEXT NOT NULL,
    overview TEXT DEFAULT '',
    companies_json TEXT DEFAULT '[]',
    reflections_json TEXT DEFAULT '[]',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, digest_date)
);
-- 模型调用记账（后台看成本）
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY,
    task TEXT NOT NULL,
    user_id INTEGER,
    prompt_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_at ON llm_usage (at);
-- 检索任务历史
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    user_id INTEGER,
    company_count INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    progress TEXT DEFAULT '',
    errors_json TEXT DEFAULT '[]',
    items_added INTEGER DEFAULT 0,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
-- 「先判断，再看 AI」：用户对每条信息自己的判断（每条信息只属于一个用户）
CREATE TABLE IF NOT EXISTS item_judgments (
    item_id INTEGER PRIMARY KEY REFERENCES report_items(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL CHECK (verdict IN ('利好', '利空', '中性')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 决策日志：买卖或不动的理由、情绪、把握，到期提醒复盘
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    emotion TEXT DEFAULT '',
    confidence INTEGER DEFAULT 3,
    price TEXT DEFAULT '',
    decided_on TEXT NOT NULL,
    review_on TEXT NOT NULL,
    review_note TEXT DEFAULT '',
    reviewed_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions (user_id, review_on);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 已有数据库上补加的列：(表, 列, 定义)
MIGRATIONS = [
    ("users", "role", "TEXT NOT NULL DEFAULT 'user'"),
    ("users", "banned", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_login_at", "TEXT"),
    ("users", "last_seen_at", "TEXT"),
    ("users", "email", "TEXT NOT NULL DEFAULT ''"),
    ("users", "email_alerts", "INTEGER NOT NULL DEFAULT 1"),
    ("report_items", "notified_at", "TEXT"),  # 预警邮件已发送时间
    # 结构化投资逻辑：thesis 为"为什么买"，另加关键假设、证伪条件、计划持有期
    ("companies", "assumptions", "TEXT NOT NULL DEFAULT ''"),
    ("companies", "wrong_if", "TEXT NOT NULL DEFAULT ''"),
    ("companies", "horizon", "TEXT NOT NULL DEFAULT ''"),
    ("report_items", "thesis_note", "TEXT NOT NULL DEFAULT ''"),  # 触及了哪条假设 / 证伪条件
    ("users", "judge_first_at", "TEXT"),  # 开启「先判断，再看 AI」的时间，之后入库的信息先隐藏 AI 判断
]


def init_db() -> None:
    from .config import ADMIN_USERNAMES

    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        if ADMIN_USERNAMES:
            conn.execute(f"UPDATE users SET role = 'admin' WHERE username IN ({','.join('?' * len(ADMIN_USERNAMES))})",
                         ADMIN_USERNAMES)


def alert_condition(threshold_sql: str = "u.alert_threshold") -> str:
    """预警判定放在查询时计算，这样用户调整阈值后历史信息也会重新判定。"""
    from .config import ALERT_MIN_CONFIDENCE

    return (
        f"((ABS(ri.score) >= {threshold_sql} AND ri.confidence >= {ALERT_MIN_CONFIDENCE})"
        f" OR ri.thesis_broken = 1)"
    )


def needs_judgment(user_alias: str = "u") -> str:
    """开启「先判断，再看 AI」之后入库、用户还没判断过的信息。"""
    return (f"({user_alias}.judge_first_at IS NOT NULL AND ri.created_at > {user_alias}.judge_first_at"
            " AND NOT EXISTS (SELECT 1 FROM item_judgments j WHERE j.item_id = ri.id))")


def thesis_text(c) -> str:
    """把结构化投资逻辑拼成给模型看的文本；什么都没写时返回空串。"""
    parts = [("为什么买", c["thesis"]), ("关键假设", c["assumptions"]),
             ("什么情况说明我错了", c["wrong_if"]), ("计划持有", c["horizon"])]
    return "\n".join(f"{k}：{v.strip()}" for k, v in parts if (v or "").strip())
