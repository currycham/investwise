"""后台管理接口：仅管理员可用（非管理员一律 404）。"""
import json
import secrets
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import analyzer, digest
from .auth import admin_user, hash_pw
from .config import ADMIN_USERNAMES, BACKFILL_DAYS, BOCHA_API_KEY, DEEPSEEK_API_KEY, TIMEZONE
from .db import get_conn

router = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_user)])

TASKS = {"analyze": "信息分析打分", "digest": "今日概览", "socratic": "提问助手", "parse": "持仓识别", "other": "其他"}


def _local(utc: str | None) -> str | None:
    if not utc:
        return None
    t = datetime.strptime(utc[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    return t.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")


def _utc_days_ago(days: int) -> str:
    """北京时间 N 天前零点，换算成 UTC 字符串（库里时间都存 UTC）。"""
    tz = ZoneInfo(TIMEZONE)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


# ---------- 概览 ----------

_balance_cache: dict = {"at": 0.0, "results": []}


def _balances() -> list[dict]:
    """查询 API 余额，缓存 10 分钟。"""
    if time.time() - _balance_cache["at"] < 600:
        return _balance_cache["results"]
    results = []
    if DEEPSEEK_API_KEY:
        results.append(_balance("DeepSeek", "分析打分、概览、提问助手", "https://platform.deepseek.com/top_up",
                                lambda: _deepseek_balance()))
    if BOCHA_API_KEY:
        results.append(_balance("博查搜索", "港股主数据源，A股/美股补充", "https://open.bochaai.com",
                                lambda: _bocha_balance()))
    _balance_cache.update(at=time.time(), results=results)
    return results


def _balance(name: str, use: str, console: str, fetch) -> dict:
    try:
        return {"name": name, "use": use, "console": console, "balance": round(fetch(), 2), "error": None}
    except Exception as e:
        return {"name": name, "use": use, "console": console, "balance": None, "error": str(e)[:200]}


def _deepseek_balance() -> float:
    r = httpx.get("https://api.deepseek.com/user/balance",
                  headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}, timeout=15)
    r.raise_for_status()
    infos = r.json().get("balance_infos") or []
    cny = next((b for b in infos if b.get("currency") == "CNY"), infos[0] if infos else {})
    return float(cny.get("total_balance") or 0)


def _bocha_balance() -> float:
    r = httpx.get("https://api.bochaai.com/v1/fund/remaining",
                  headers={"Authorization": f"Bearer {BOCHA_API_KEY}"}, timeout=15)
    r.raise_for_status()
    return float(r.json()["data"]["remaining"])


@router.get("/overview")
def overview():
    today, week = _utc_days_ago(0), _utc_days_ago(6)
    with get_conn() as conn:
        n = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        stats = [
            ["今日模型成本（估算）", f"¥{n('SELECT COALESCE(SUM(cost), 0) FROM llm_usage WHERE at >= ?', today):.2f}"],
            ["近 7 天成本（估算）", f"¥{n('SELECT COALESCE(SUM(cost), 0) FROM llm_usage WHERE at >= ?', week):.2f}"],
            ["用户总数", n("SELECT COUNT(*) FROM users")],
            ["近 7 天活跃用户", n("SELECT COUNT(*) FROM users WHERE last_login_at >= ?", week)],
            ["持仓条目 / 去重公司",
             f"{n('SELECT COUNT(*) FROM companies')} / {n('SELECT COUNT(DISTINCT market || ticker) FROM companies')}"],
            ["今日抓取新闻", n("SELECT COUNT(*) FROM raw_news WHERE fetched_at >= ?", today)],
            ["今日新增重要信息", n("SELECT COUNT(*) FROM report_items WHERE created_at >= ?", today)],
            ["今日提问轮数", n("SELECT COUNT(*) FROM chat_messages WHERE role = 'user' AND created_at >= ?", today)],
            ["近 7 天失败任务", n("SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND started_at >= ?", week)],
        ]
        by_task = [dict(r) for r in conn.execute(
            "SELECT task, COUNT(*) AS calls, SUM(prompt_tokens) AS input, SUM(completion_tokens) AS output,"
            " SUM(cost) AS cost FROM llm_usage WHERE at >= ? GROUP BY task ORDER BY cost DESC", (week,))]
        by_day = [dict(r) for r in conn.execute(
            "SELECT substr(datetime(at, '+8 hours'), 1, 10) AS day, COUNT(*) AS calls, SUM(cost) AS cost"
            " FROM llm_usage WHERE at >= ? GROUP BY day ORDER BY day DESC", (week,))]
    for t in by_task:
        t["name"] = TASKS.get(t["task"], t["task"])
    return {"stats": stats, "by_task": by_task, "by_day": by_day, "balances": _balances(),
            "queue": _queue_state()}


def _queue_state() -> dict:
    s = analyzer.status
    return {"running": s["running"], "label": s["label"], "progress": s["progress"],
            "pending": [j["label"] for j in analyzer.pending_jobs()]}


# ---------- 用户 ----------

@router.get("/users")
def users():
    month = _utc_days_ago(29)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT u.id, u.username, u.role, u.banned, u.alert_threshold, u.created_at, u.last_login_at,
                 (SELECT COUNT(*) FROM companies c WHERE c.user_id = u.id) AS companies,
                 (SELECT COUNT(*) FROM chat_messages m JOIN companies c ON c.id = m.company_id
                   WHERE c.user_id = u.id AND m.role = 'user') AS questions,
                 (SELECT COALESCE(SUM(cost), 0) FROM llm_usage x WHERE x.user_id = u.id AND x.at >= ?) AS cost_30d,
                 (SELECT COALESCE(SUM(cost), 0) FROM llm_usage x WHERE x.user_id = u.id) AS cost_total
               FROM users u ORDER BY u.id""", (month,))]
    for r in rows:
        r["created_at"], r["last_login_at"] = _local(r["created_at"]), _local(r["last_login_at"])
        r["pinned_admin"] = r["username"] in ADMIN_USERNAMES
    return rows


class RoleIn(BaseModel):
    role: str


class BanIn(BaseModel):
    banned: bool


def _target(conn, user_id: int, admin: dict) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    if row["id"] == admin["id"]:
        raise HTTPException(400, "不能修改自己的角色或状态")
    return dict(row)


@router.post("/users/{user_id}/role")
def set_role(user_id: int, body: RoleIn, admin: dict = Depends(admin_user)):
    with get_conn() as conn:
        u = _target(conn, user_id, admin)
        if body.role != "admin" and u["username"] in ADMIN_USERNAMES:
            raise HTTPException(400, "该用户在 .env 的 ADMIN_USERNAMES 里，需先从配置中移除")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", ("admin" if body.role == "admin" else "user", user_id))
    return {"ok": True}


@router.post("/users/{user_id}/ban")
def set_banned(user_id: int, body: BanIn, admin: dict = Depends(admin_user)):
    with get_conn() as conn:
        _target(conn, user_id, admin)
        conn.execute("UPDATE users SET banned = ? WHERE id = ?", (1 if body.banned else 0, user_id))
        if body.banned:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, admin: dict = Depends(admin_user)):
    """生成临时密码并让该用户所有登录失效；临时密码只在这次返回中出现。"""
    password = secrets.token_urlsafe(6)
    with get_conn() as conn:
        _target(conn, user_id, admin)
        conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_pw(password), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return {"password": password}


# ---------- 任务 ----------

@router.get("/jobs")
def jobs():
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT j.*, u.username FROM jobs j LEFT JOIN users u ON u.id = j.user_id ORDER BY j.id DESC LIMIT 100")]
    for r in rows:
        r["errors"] = json.loads(r.pop("errors_json") or "[]")
        if r["finished_at"]:
            secs = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
            r["seconds"] = int(secs)
        r["started_at"], r["finished_at"] = _local(r["started_at"]), _local(r["finished_at"])
    return {"queue": _queue_state(), "jobs": rows}


@router.post("/run-all")
def run_all():
    analyzer.enqueue(None, label="全站检索（管理员触发）")
    return {"ok": True}


@router.post("/digests")
def regenerate_digests():
    """按当天已有信息重新生成所有用户的今日概览（改了概览提示词后用）。"""
    def work():
        with get_conn() as conn:
            uids = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM companies")]
        for uid in uids:
            try:
                digest.generate(uid)
            except Exception:
                pass
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


# ---------- 持仓公司 ----------

@router.get("/companies")
def companies():
    week = _utc_days_ago(6)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT c.market, c.ticker, MIN(c.name) AS name, COUNT(*) AS holders,
                 (SELECT COUNT(*) FROM raw_news rn WHERE rn.market = c.market AND rn.ticker = c.ticker) AS news_total,
                 (SELECT COUNT(*) FROM raw_news rn WHERE rn.market = c.market AND rn.ticker = c.ticker
                   AND rn.fetched_at >= ?) AS news_7d,
                 (SELECT MAX(rn.fetched_at) FROM raw_news rn WHERE rn.market = c.market AND rn.ticker = c.ticker)
                   AS last_fetched,
                 (SELECT COUNT(*) FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id
                   JOIN companies c2 ON c2.id = dr.company_id
                   WHERE c2.market = c.market AND c2.ticker = c.ticker AND ri.created_at >= ?) AS items_7d
               FROM companies c GROUP BY c.market, c.ticker ORDER BY holders DESC, news_7d DESC""", (week, week))]
    for r in rows:
        r["last_fetched"] = _local(r["last_fetched"])
    return rows


class TickerIn(BaseModel):
    market: str
    ticker: str


@router.post("/companies/backfill")
def backfill_company(body: TickerIn):
    with get_conn() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM companies WHERE market = ? AND ticker = ?",
                                          (body.market, body.ticker))]
    if not ids:
        raise HTTPException(404, "没有用户持有这家公司")
    analyzer.enqueue(None, ids, BACKFILL_DAYS * 24, f"回溯 {body.ticker}（管理员触发）")
    return {"ok": True, "companies": len(ids)}
