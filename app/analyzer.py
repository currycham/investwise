"""检索流水线：抓取（按公司去重）→ 过滤 + 多框架打分（按用户持仓，结合其投资逻辑）→ 按信息发布日期入库。

所有任务（每日定时、手动检索、新增持仓回溯一周）都进同一个队列，由一个后台线程依次执行，互不冲突。
"""
import hashlib
import json
import logging
import queue
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import costs, digest, llm, notify, sources
from .config import BACKFILL_DAYS, INACTIVE_DAYS, NEWS_LOOKBACK_HOURS, TIMEZONE
from .db import get_conn
from .prompts import ANALYSIS_SYSTEM

log = logging.getLogger(__name__)

_queue: queue.Queue = queue.Queue()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_pending: list[dict] = []   # 排队中的任务（用于前端显示），与 _queue 同步
status = {"running": False, "label": "", "user_id": None, "started_at": None, "finished_at": None,
          "progress": "", "errors": []}

MAX_CANDIDATES = 30
MAX_CANDIDATES_BACKFILL = 60


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


# ---------- 任务队列 ----------

def enqueue(user_id: int | None = None, company_ids: list[int] | None = None,
            lookback_hours: int = NEWS_LOOKBACK_HOURS, label: str = "每日检索") -> None:
    job = {"user_id": user_id, "company_ids": company_ids, "lookback_hours": lookback_hours, "label": label}
    with _worker_lock:
        _pending.append(job)
        _queue.put(job)
        _ensure_worker()


def enqueue_backfill(user_id: int, company_ids: list[int]) -> None:
    """新加入/更换的持仓：先回溯最近一周，不用等到第二天的定时检索。"""
    if company_ids:
        enqueue(user_id, company_ids, BACKFILL_DAYS * 24, f"新持仓回溯{BACKFILL_DAYS}天")


def pending_jobs() -> list[dict]:
    with _worker_lock:
        return [dict(j) for j in _pending]


def pending_for(user_id: int) -> int:
    with _worker_lock:
        return sum(1 for j in _pending if j["user_id"] in (None, user_id))


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_work_loop, name="analyzer", daemon=True)
        _worker.start()


def _work_loop() -> None:
    while True:
        job = _queue.get()
        with _worker_lock:
            _pending.remove(job)
        try:
            run(**job)
        except Exception:
            log.exception("job failed: %s", job)


# ---------- 执行 ----------

def run(user_id: int | None = None, company_ids: list[int] | None = None,
        lookback_hours: int = NEWS_LOOKBACK_HOURS, label: str = "每日检索") -> None:
    """同步执行一次检索。user_id 为空时处理所有用户；company_ids 限定只处理这些公司。"""
    status.update(running=True, label=label, user_id=user_id, progress="开始", errors=[],
                  started_at=now_local().isoformat(timespec="seconds"), finished_at=None)
    job_id = None
    try:
        sql, args = "SELECT c.* FROM companies c JOIN users u ON u.id = c.user_id WHERE u.banned = 0", []
        if user_id:
            sql, args = sql + " AND c.user_id = ?", args + [user_id]
        elif not company_ids:
            # 全站检索跳过长期不访问的用户；他们回来时会自动回溯
            sql += " AND COALESCE(u.last_seen_at, u.last_login_at, u.created_at) >= datetime('now', ?)"
            args.append(f"-{INACTIVE_DAYS} day")
        if company_ids:
            sql += f" AND c.id IN ({','.join('?' * len(company_ids))})"
            args += company_ids
        with get_conn() as conn:
            companies = [dict(r) for r in conn.execute(sql, args)]
            job_id = conn.execute("INSERT INTO jobs (label, user_id, company_count) VALUES (?, ?, ?)",
                                  (label, user_id, len(companies))).lastrowid

        targets = {(c["market"], c["ticker"]): c["name"] for c in companies}
        for i, ((market, ticker), name) in enumerate(targets.items(), 1):
            if _over_budget():
                return
            status["progress"] = f"抓取 {i}/{len(targets)}"
            try:
                _store_news(market, ticker, sources.fetch_news(market, ticker, name, lookback_hours))
            except Exception as e:
                log.exception("fetch failed")
                status["errors"].append(f"{name} 抓取失败：{e}")

        for i, c in enumerate(companies, 1):
            if _over_budget():
                return
            status["progress"] = f"分析 {i}/{len(companies)}"
            try:
                analyze_company(c, lookback_hours)
            except llm.BudgetError:
                _over_budget()
                return
            except Exception as e:
                log.exception("analyze failed")
                status["errors"].append(f"{c['name']} 分析失败：{e}")

        status["progress"] = "生成今日概览"
        for uid in {c["user_id"] for c in companies}:
            try:
                digest.generate(uid)
            except Exception as e:
                log.exception("digest failed")
                status["errors"].append(f"今日概览生成失败：{e}")
            try:
                notify.send_alerts(uid)
            except Exception as e:
                log.exception("alert email failed")
                status["errors"].append(f"预警邮件发送失败：{e}")
        status["progress"] = "完成"
    except Exception as e:
        status["errors"].append(f"任务中断：{e}")
        raise
    finally:
        status.update(running=False, finished_at=now_local().isoformat(timespec="seconds"))
        if job_id:
            _finish_job(job_id)


def _over_budget() -> bool:
    """预算用完时停止任务；没分析的新闻不会标记为已读，明天的检索会接着处理。"""
    try:
        costs.check_budget()
        return False
    except costs.BudgetExceeded as e:
        status["errors"].append(str(e))
        status["progress"] = "预算用完，已暂停"
        return True


def _finish_job(job_id: int) -> None:
    with get_conn() as conn:
        started = conn.execute("SELECT started_at FROM jobs WHERE id = ?", (job_id,)).fetchone()["started_at"]
        added = conn.execute("SELECT COUNT(*) FROM report_items WHERE created_at >= ?", (started,)).fetchone()[0]
        conn.execute(
            "UPDATE jobs SET status = ?, progress = ?, errors_json = ?, items_added = ?,"
            " finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            ("done" if status["progress"] == "完成" else "failed", status["progress"],
             json.dumps(status["errors"], ensure_ascii=False), added, job_id))


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


def analyze_company(company: dict, lookback_hours: int = NEWS_LOOKBACK_HOURS) -> None:
    backfill = lookback_hours > NEWS_LOOKBACK_HOURS
    limit = MAX_CANDIDATES_BACKFILL if backfill else MAX_CANDIDATES
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT rn.* FROM raw_news rn WHERE rn.market = ? AND rn.ticker = ? AND rn.fetched_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM seen_news s WHERE s.company_id = ? AND s.raw_news_id = rn.id)"
            " ORDER BY rn.published_at DESC LIMIT ?",
            (company["market"], company["ticker"], since, company["id"], limit),
        )]
    if not rows:
        return

    today = now_local().date()
    start = (now_local() - timedelta(hours=lookback_hours)).date()
    candidates = "\n\n".join(
        f"[{i}] {r['title']}\n来源：{r['source']}　时间：{r['published_at'] or '未知'}\n"
        f"{r['content'][:400 if backfill else 600]}"
        for i, r in enumerate(rows, 1)
    )
    market_name = {"A": "A股", "HK": "港股", "US": "美股"}[company["market"]]
    user_msg = (
        f"公司：{company['name']}（{market_name} {company['ticker']}）\n"
        f"今天日期：{today}\n"
        f"信息时间范围：{start} 至 {today}\n"
        f"用户的投资逻辑：{company['thesis'].strip() or '未提供'}\n\n"
        f"候选信息：\n{candidates}"
    )
    result = llm.chat_json(
        [{"role": "system", "content": ANALYSIS_SYSTEM}, {"role": "user", "content": user_msg}],
        temperature=0.2, max_tokens=8000 if backfill else 6000, task="analyze", user_id=company["user_id"],
    )

    summaries = result.get("day_summaries") or {}
    if not isinstance(summaries, dict):
        summaries = {}
    with get_conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO seen_news (company_id, raw_news_id) VALUES (?, ?)",
                         [(company["id"], r["id"]) for r in rows])
        for it in result.get("items") or []:
            # 来源链接只取自真实抓取的数据，不采信模型生成的 URL
            picked = [rows[i - 1] for i in it.get("source_ids") or [] if isinstance(i, int) and 1 <= i <= len(rows)]
            if not picked:  # 没有可追溯来源的结论不展示
                continue
            report_date = _item_date(picked, start, today)
            report_id = _report_id(conn, company["id"], report_date, summaries.get(report_date) or "")
            srcs = [{"title": r["title"], "url": r["url"], "source": r["source"], "published_at": r["published_at"]}
                    for r in picked]
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


def _item_date(picked: list[dict], start, today) -> str:
    """事件归档到其来源中最新的发布日期；解析不出或超出范围时归到今天。"""
    dates = [t.date() for r in picked if (t := sources.parse_time(r["published_at"]))]
    dates = [d for d in dates if start <= d <= today]
    return str(max(dates) if dates else today)


def _report_id(conn, company_id: int, report_date: str, day_summary: str) -> int:
    conn.execute(
        "INSERT INTO daily_reports (company_id, report_date, day_summary) VALUES (?, ?, ?)"
        " ON CONFLICT (company_id, report_date) DO UPDATE SET"
        " day_summary = CASE WHEN excluded.day_summary != '' THEN excluded.day_summary ELSE day_summary END",
        (company_id, report_date, day_summary),
    )
    return conn.execute("SELECT id FROM daily_reports WHERE company_id = ? AND report_date = ?",
                        (company_id, report_date)).fetchone()["id"]


def _clamp(v, lo, hi) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.0
