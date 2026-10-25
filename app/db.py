import sqlite3
from contextlib import contextmanager

from .config import DB_PATH, DEFAULT_ALERT_THRESHOLD

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',  -- 昵称（以前是登录用的用户名）
    pw_hash TEXT NOT NULL DEFAULT '',   -- 以前的密码登录，已不用
    alert_threshold INTEGER NOT NULL DEFAULT {DEFAULT_ALERT_THRESHOLD},
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 邮箱验证码：每个邮箱只留最新一条，只存哈希
CREATE TABLE IF NOT EXISTS login_codes (
    email TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL
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
-- 行情与财务：按 (市场, 代码) 共享，每天更新
CREATE TABLE IF NOT EXISTS stock_prices (
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    change_pct REAL,
    PRIMARY KEY (market, ticker, date)
);
CREATE TABLE IF NOT EXISTS stock_data (
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    industry TEXT DEFAULT '',
    currency TEXT DEFAULT '',          -- 股价币种
    fin_currency TEXT DEFAULT '',      -- 财报币种（港股公司常用人民币报告）
    metrics_json TEXT DEFAULT '{{}}',  -- 最新价格、估值、分位、股息率、ROE 等
    annual_json TEXT DEFAULT '[]',     -- 近十年年度财务
    latest_json TEXT DEFAULT 'null',   -- 最新一期报告
    latest_report TEXT DEFAULT '',     -- 最新报告期，用于发现新财报
    us_prefix TEXT DEFAULT '',         -- 东方财富美股交易所前缀
    error TEXT DEFAULT '',
    updated_at TEXT,
    fin_updated_at TEXT,
    PRIMARY KEY (market, ticker)
);
CREATE TABLE IF NOT EXISTS fx_rates (
    currency TEXT NOT NULL,
    date TEXT NOT NULL,
    rate REAL NOT NULL,                -- 人民币 / 1 单位外币
    PRIMARY KEY (currency, date)
);
-- 暴跌、新财报等事件；暴跌原因分析与用户无关，多人共用
CREATE TABLE IF NOT EXISTS stock_events (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('drop', 'report')),
    event_date TEXT NOT NULL,
    title TEXT NOT NULL,
    data_json TEXT DEFAULT '{{}}',
    analysis_json TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (market, ticker, kind, event_date)
);
-- 新财报的十年价值分析：结合用户画像和投资逻辑，按持仓生成
CREATE TABLE IF NOT EXISTS report_analyses (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company_id, period)
);
-- 清仓记录：获利了结 / 止损出局，附这笔投资期间的总结
CREATE TABLE IF NOT EXISTS closed_trades (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (outcome IN ('获利了结', '止损出局')),
    shares REAL,
    cost_price REAL,
    sell_price REAL,
    currency TEXT DEFAULT '',
    opened_at TEXT DEFAULT '',
    closed_at TEXT NOT NULL,
    pnl REAL,                          -- 盈亏额（本币），只在填了份额时有
    pnl_pct REAL,
    note TEXT DEFAULT '',
    summary_json TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 暴跌 / 新财报事件的邮件提醒，每个用户每个事件只发一次
CREATE TABLE IF NOT EXISTS event_notices (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES stock_events(id) ON DELETE CASCADE,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, event_id)
);
-- 数据概览：每个用户每天一条
CREATE TABLE IF NOT EXISTS user_data_digests (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    digest_date TEXT NOT NULL,
    overview TEXT DEFAULT '',
    points_json TEXT DEFAULT '[]',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, digest_date)
);
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
    ("users", "login_email", "TEXT"),  # 验证过的登录邮箱（小写），唯一索引见 init_db
    ("report_items", "notified_at", "TEXT"),  # 预警邮件已发送时间
    # 结构化投资逻辑：thesis 为"为什么买"，另加关键假设、证伪条件、计划持有期
    ("companies", "assumptions", "TEXT NOT NULL DEFAULT ''"),
    ("companies", "wrong_if", "TEXT NOT NULL DEFAULT ''"),
    ("companies", "horizon", "TEXT NOT NULL DEFAULT ''"),
    ("report_items", "thesis_note", "TEXT NOT NULL DEFAULT ''"),  # 触及了哪条假设 / 证伪条件
    ("users", "judge_first_at", "TEXT"),  # 开启「先判断，再看 AI」的时间，之后入库的信息先隐藏 AI 判断
    # 股票池与仓位：holding 持仓中 / watching 观察中 / closed 投过的（清仓后停止跟踪）
    ("companies", "status", "TEXT NOT NULL DEFAULT 'holding'"),
    ("companies", "shares", "REAL"),
    ("companies", "cost_price", "REAL"),
    ("companies", "manual_weight", "REAL"),  # 不想填份额时，直接填仓位权重（%）
    ("companies", "opened_at", "TEXT NOT NULL DEFAULT ''"),
    ("companies", "lots_json", "TEXT NOT NULL DEFAULT '[]'"),  # 每笔买入 / 卖出；上面三列由它汇总
    # 投资画像
    ("users", "profile_tags_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("users", "profile_statement", "TEXT NOT NULL DEFAULT ''"),
    ("users", "profile_json", "TEXT NOT NULL DEFAULT ''"),
    ("users", "profile_at", "TEXT"),
    ("users", "data_prefs_json", "TEXT NOT NULL DEFAULT ''"),  # 数据页的指标列、关注点规则、仓位填写方式
    ("report_items", "profile_note", "TEXT NOT NULL DEFAULT ''"),  # 从用户的投资风格看这条信息
    ("report_items", "relevance", "REAL NOT NULL DEFAULT 0.5"),    # 与用户画像的相关度，用于排序
]


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    _relax_username()
    with get_conn() as conn:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_email ON users (login_email)")


OLD_USER_COLS = ("username TEXT UNIQUE NOT NULL", "pw_hash TEXT NOT NULL,")
NEW_USER_COLS = ("username TEXT NOT NULL DEFAULT ''", "pw_hash TEXT NOT NULL DEFAULT '',")


def _relax_username() -> None:
    """改成邮箱登录后，用户名只是昵称：去掉唯一、允许空。SQLite 改不了列约束，按官方步骤重建 users 表，
    列和数据原样保留（关掉外键检查，其他表对 users 的引用不受影响）。"""
    conn = _connect()
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()[0]
        if OLD_USER_COLS[0] not in sql:
            return
        for old, new in zip(OLD_USER_COLS, NEW_USER_COLS):
            sql = sql.replace(old, new)
        sql = sql.replace("CREATE TABLE users", "CREATE TABLE users_new", 1)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(f"BEGIN; {sql}; INSERT INTO users_new SELECT * FROM users; DROP TABLE users;"
                           " ALTER TABLE users_new RENAME TO users; COMMIT;")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"重建 users 表后外键检查失败：{bad[:5]}")
    finally:
        conn.close()


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
