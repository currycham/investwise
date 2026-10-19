"""每日流水线：抓取（按公司去重）→ 过滤 + 多框架打分（按用户持仓，结合其投资逻辑）→ 入库。"""
import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import llm, sources
from .config import NEWS_LOOKBACK_HOURS, TIMEZONE
from .db import get_conn
from .prompts import ANALYSIS_SYSTEM

log = logging.getLogger(__name__)

_lock = threading.Lock()
status = {"running": False, "started_at": None, "finished_at": None, "progress": "", "errors": []}

MAX_CANDIDATES = 30


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


def run(user_id: int | None = None) -> bool:
    """user_id 为空时处理所有用户。已有任务在跑时返回 False。"""
    if not _lock.acquire(blocking=False):
        return False
    status.update(running=True, started_at=now_local().isoformat(timespec="seconds"),
                  finished_at=None, progress="开始", errors=[])
    try:
        with get_conn() as conn:
            sql = "SELECT * FROM companies" + (" WHERE user_id = ?" if user_id else "")
            companies = [dict(r) for r in conn.execute(sql, (user_id,) if user_id else ())]

        targets = {(c["market"], c["ticker"]): c["name"] for c in companies}
        for i, ((market, ticker), name) in enumerate(targets.items(), 1):
            status["progress"] = f"抓取 {i}/{len(targets)}：{name}"
            try:
                _store_news(market, ticker, sources.fetch_news(market, ticker, name, NEWS_LOOKBACK_HOURS))
            except Exception as e:
                log.exception("fetch failed")
                status["errors"].append(f"{name} 抓取失败：{e}")

        for i, c in enumerate(companies, 1):
            status["progress"] = f"分析 {i}/{len(companies)}：{c['name']}"
            try:
                analyze_company(c)
            except Exception as e:
                log.exception("analyze failed")
                status["errors"].append(f"{c['name']} 分析失败：{e}")
        status["progress"] = "完成"
    finally:
        status.update(running=False, finished_at=now_local().isoformat(timespec="seconds"))
        _lock.release()
    return True


def _store_news(market: str, ticker: str, items: list[dict]) -> None:
    with get_conn() as conn:
        for it in items:
            key = it["url"] or it["title"]
            uniq = hashlib.sha1(f"{market}|{ticker}|{key}".encode()).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO raw_news (market, ticker, title, content, url, source, published_at, uniq)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (market, ticker, it["title"], it["content"], it["url"], it["source"], it["published_at"], uniq),
            )


def analyze_company(company: dict) -> None:
    since = (datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT rn.* FROM raw_news rn WHERE rn.market = ? AND rn.ticker = ? AND rn.fetched_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM seen_news s WHERE s.company_id = ? AND s.raw_news_id = rn.id)"
            " ORDER BY rn.published_at DESC LIMIT ?",
            (company["market"], company["ticker"], since, company["id"], MAX_CANDIDATES),
        )]
    if not rows:
        return

    candidates = "\n\n".join(
        f"[{i}] {r['title']}\n来源：{r['source']}　时间：{r['published_at'] or '未知'}\n{r['content'][:600]}"
        for i, r in enumerate(rows, 1)
    )
    market_name = {"A": "A股", "HK": "港股", "US": "美股"}[company["market"]]
    user_msg = (
        f"公司：{company['name']}（{market_name} {company['ticker']}）\n"
        f"今天日期：{now_local():%Y-%m-%d}\n"
        f"用户的投资逻辑：{company['thesis'].strip() or '未提供'}\n\n"
        f"候选信息：\n{candidates}"
    )
    result = llm.chat_json(
        [{"role": "system", "content": ANALYSIS_SYSTEM}, {"role": "user", "content": user_msg}],
        temperature=0.2, max_tokens=6000,
    )

    items = result.get("items") or []
    today = now_local().strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO seen_news (company_id, raw_news_id) VALUES (?, ?)",
                         [(company["id"], r["id"]) for r in rows])
        if not items:
            return
        conn.execute(
            "INSERT INTO daily_reports (company_id, report_date, day_summary) VALUES (?, ?, ?)"
            " ON CONFLICT (company_id, report_date) DO UPDATE SET"
            " day_summary = CASE WHEN excluded.day_summary != '' THEN excluded.day_summary ELSE day_summary END",
            (company["id"], today, result.get("day_summary") or ""),
        )
        report_id = conn.execute("SELECT id FROM daily_reports WHERE company_id = ? AND report_date = ?",
                                 (company["id"], today)).fetchone()["id"]
        for it in items:
            # 来源链接只取自真实抓取的数据，不采信模型生成的 URL
            srcs = [{"title": rows[i - 1]["title"], "url": rows[i - 1]["url"], "source": rows[i - 1]["source"],
                     "published_at": rows[i - 1]["published_at"]}
                    for i in it.get("source_ids") or [] if isinstance(i, int) and 1 <= i <= len(rows)]
            if not srcs:  # 没有可追溯来源的结论不展示
                continue
            conn.execute(
                "INSERT INTO report_items (report_id, title, summary, direction, score, confidence, horizon,"
                " frameworks_json, reasoning, watch_points, thesis_impact, thesis_broken, sources_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, it.get("title") or "", it.get("summary") or "", it.get("direction") or "中性",
                 _clamp(it.get("score"), -10, 10), _clamp(it.get("confidence"), 0, 1), it.get("horizon") or "",
                 json.dumps(it.get("frameworks") or {}, ensure_ascii=False), it.get("reasoning") or "",
                 json.dumps(it.get("watch_points") or [], ensure_ascii=False),
                 it.get("thesis_impact") or "", 1 if it.get("thesis_broken") else 0,
                 json.dumps(srcs, ensure_ascii=False)),
            )


def _clamp(v, lo, hi) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.0
