"""数据页：股票池（持仓中 / 观察中 / 投过的）、仓位权重、关注点、暴跌监控、新财报分析、清仓总结、数据概览。

行情和财务由 market.py 按股票共享更新；这里按用户组合计算仓位、套用用户自选的关注点规则，
并在每日流水线里生成暴跌原因分析（多人共用）、新财报的十年价值分析（按持仓，结合画像）和数据概览。
"""
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import llm, market, profile, sources
from .auth import current_user
from .catalog import CONCERNS, catalog_out
from .config import TIMEZONE
from .db import get_conn, thesis_text
from .prompts import CLOSE_SUMMARY_SYSTEM, DATA_DIGEST_SYSTEM, DROP_SYSTEM, REPORT_SYSTEM

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
MARKET = {"A": "A股", "HK": "港股", "US": "美股"}


def _today() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def _llm_json(messages: list[dict], task: str, user_id: int | None, **kw) -> dict:
    try:
        return llm.chat_json(messages, task=task, user_id=user_id, **kw)
    except llm.BudgetError as e:
        raise HTTPException(429, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))


def _own_company(conn, company_id: int, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM companies WHERE id = ? AND user_id = ?", (company_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "公司不存在")
    return dict(row)


def _yi(v) -> str:
    return "—" if v is None else f"{v / 1e8:,.1f}亿"


def _pct(v) -> str:
    return "—" if v is None else f"{v:.1f}%"


# ---------- 关注点 ----------

def _news_signals(conn, company_ids: list[int], moat_threshold: float) -> dict[int, dict]:
    """近期资讯里的护城河利空（价值框架评分很低）和投资逻辑被削弱的次数。"""
    out: dict[int, dict] = defaultdict(lambda: {"moat": [], "weakened": []})
    if not company_ids:
        return out
    rows = conn.execute(
        f"""SELECT dr.company_id, dr.report_date, ri.title, ri.frameworks_json, ri.thesis_impact, ri.thesis_broken
            FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id
            WHERE dr.company_id IN ({','.join('?' * len(company_ids))}) AND dr.report_date >= date('now', '-90 day')""",
        company_ids).fetchall()
    month_ago = str(date.today() - timedelta(days=30))
    for r in rows:
        fw = (json.loads(r["frameworks_json"] or "{}").get("价值投资") or {})
        if isinstance(fw.get("score"), (int, float)) and fw["score"] <= -moat_threshold:
            out[r["company_id"]]["moat"].append(f"{r['report_date']} {r['title']}")
        if r["report_date"] >= month_ago and (r["thesis_impact"] == "削弱" or r["thesis_broken"]):
            out[r["company_id"]]["weakened"].append(f"{r['report_date']} {r['title']}")
    return out


def evaluate_concerns(stock: dict | None, rules: dict, signals: dict) -> list[dict]:
    """按用户开启的规则判定关注点。返回 [{key, label, kind, detail}]。"""
    if not stock:
        return []
    m, annual = stock["metrics"], stock["annual"]
    out = []

    def hit(key: str, detail: str):
        label, kind = CONCERNS[key][0], CONCERNS[key][1]
        out.append({"key": key, "label": label, "kind": kind, "detail": detail})

    def on(key: str) -> float | None:
        r = rules.get(key) or {}
        return float(r.get("threshold") or 0) if r.get("on") else None

    def ago(field: str, years: int = 3):
        vals = [r.get(field) for r in annual]
        return (vals[-1], vals[-1 - years]) if len(vals) > years and vals[-1] is not None and vals[-1 - years] is not None else None

    if (t := on("overvalued")) is not None and m.get("pe_pct") is not None and m["pe_pct"] >= t:
        hit("overvalued", f"PE {m['pe']:.1f} 倍，处在历史 {m['pe_pct']:.0f}% 分位")
    if (t := on("pb_high")) is not None and m.get("pb_pct") is not None and m["pb_pct"] >= t:
        hit("pb_high", f"PB {m['pb']:.2f} 倍，处在历史 {m['pb_pct']:.0f}% 分位")
    if (t := on("undervalued")) is not None and m.get("pe_pct") is not None and m["pe_pct"] <= t:
        hit("undervalued", f"PE {m['pe']:.1f} 倍，处在历史 {m['pe_pct']:.0f}% 分位——是机会还是价值陷阱？")
    if (t := on("roe_low")) is not None and m.get("roe_fy") is not None and m["roe_fy"] < t:
        hit("roe_low", f"最近年度 ROE {m['roe_fy']:.1f}%（门槛 {t:g}%）")
    if (t := on("roe_decline")) is not None:
        n = max(1, int(t))
        roes = [r.get("roe") for r in annual][-(n + 1):]
        if len(roes) == n + 1 and None not in roes and all(b < a for a, b in zip(roes, roes[1:])):
            hit("roe_decline", f"ROE 连续 {n} 年下降：" + " → ".join(f"{v:.1f}%" for v in roes))
    if on("fcf_negative") is not None and annual and annual[-1].get("fcf") is not None and annual[-1]["fcf"] < 0:
        hit("fcf_negative", f"{annual[-1]['date'][:4]} 年自由现金流 {_yi(annual[-1]['fcf'])}")
    if (t := on("cash_quality")) is not None and m.get("ocf_np") is not None and m["ocf_np"] < t:
        hit("cash_quality", f"经营现金流 / 净利润 = {m['ocf_np']:.2f}")
    if (t := on("profit_decline")) is not None and m.get("np_yoy") is not None and m["np_yoy"] <= -t:
        hit("profit_decline", f"{market.period_label(m.get('report_period', ''))}净利润同比 {m['np_yoy']:.1f}%")
    if (t := on("revenue_decline")) is not None and m.get("rev_yoy") is not None and m["rev_yoy"] < -t:
        hit("revenue_decline", f"{market.period_label(m.get('report_period', ''))}营收同比 {m['rev_yoy']:.1f}%")
    if (t := on("margin_squeeze")) is not None and (g := ago("gross_margin")) and g[1] - g[0] >= t:
        hit("margin_squeeze", f"毛利率从 3 年前的 {g[1]:.1f}% 降到 {g[0]:.1f}%")
    if (t := on("debt_rising")) is not None and (d := ago("debt_ratio")) and d[0] - d[1] >= t:
        hit("debt_rising", f"资产负债率从 3 年前的 {d[1]:.1f}% 升到 {d[0]:.1f}%")
    if on("moat_news") is not None and signals.get("moat"):
        hit("moat_news", f"近 90 天 {len(signals['moat'])} 条：{signals['moat'][0]}")
    if on("thesis_weakened") is not None and signals.get("weakened"):
        hit("thesis_weakened", f"近 30 天 {len(signals['weakened'])} 条：{signals['weakened'][0]}")
    if (t := on("drawdown")) is not None and m.get("dist_high_52w") is not None and m["dist_high_52w"] <= -t:
        hit("drawdown", f"距 52 周高点 {m['dist_high_52w']:.1f}%")
    if on("below_ma200") is not None and m.get("ma200_gap") is not None and m["ma200_gap"] < 0:
        hit("below_ma200", f"股价低于 200 日均线 {abs(m['ma200_gap']):.1f}%")
    if (t := on("dividend_high")) is not None and m.get("dividend_yield") is not None and m["dividend_yield"] >= t:
        hit("dividend_high", f"股息率 {m['dividend_yield']:.2f}%")
    return out


# ---------- 组合 ----------

def portfolio(conn, user: dict) -> dict:
    """计算用户全部公司的行情、仓位和关注点。"""
    p = profile.prefs(user)
    fx = market.fx_rates()
    companies = [dict(r) for r in conn.execute("SELECT * FROM companies WHERE user_id = ? ORDER BY created_at",
                                               (user["id"],))]
    stocks = market.stock_rows({(c["market"], c["ticker"]) for c in companies})
    signals = _news_signals(conn, [c["id"] for c in companies], p["concerns"]["moat_news"]["threshold"])
    trades = defaultdict(list)
    for t in conn.execute("SELECT * FROM closed_trades WHERE user_id = ? ORDER BY closed_at DESC, id DESC",
                          (user["id"],)):
        trades[t["company_id"]].append(dict(t))

    rows = []
    for c in companies:
        s = stocks.get((c["market"], c["ticker"]))
        m = dict(s["metrics"]) if s else {}
        ccy = market.CURRENCY[c["market"]]
        row = {
            "id": c["id"], "name": c["name"], "ticker": c["ticker"], "market": c["market"], "status": c["status"],
            "industry": (s or {}).get("industry", ""), "currency": ccy, "metrics": m,
            "shares": c["shares"], "cost_price": c["cost_price"], "manual_weight": c["manual_weight"],
            "opened_at": c["opened_at"], "lots": _lots(c), "data_error": (s or {}).get("error", "") if s else "等待首次更新",
            "data_updated_at": (s or {}).get("updated_at"),
            "concerns": evaluate_concerns(s, p["concerns"], signals.get(c["id"], {})) if c["status"] != "closed" else [],
            "trades": trades.get(c["id"], []),
        }
        price = m.get("price")
        if c["status"] == "holding":
            if c["shares"] and price:
                row["market_value"] = c["shares"] * price * fx.get(ccy, 1)
            if c["shares"] and c["cost_price"]:
                row["cost_value"] = c["shares"] * c["cost_price"] * fx.get(ccy, 1)
            if c["cost_price"] and price:
                row["pnl_pct"] = round((price / c["cost_price"] - 1) * 100, 2)
            if row.get("market_value") is not None and row.get("cost_value") is not None:
                row["pnl"] = row["market_value"] - row["cost_value"]
        rows.append(row)
    _weights([r for r in rows if r["status"] == "holding"], p["position_mode"])
    for r in rows:  # 把仓位相关的值也放进 metrics，前端统一按指标 key 取值
        for k in ("weight", "cost_weight", "market_value", "pnl_pct"):
            if r.get(k) is not None:
                r["metrics"][k] = r[k]
        r["metrics"]["industry"] = r["industry"]
    return {"rows": rows, "fx": fx, "prefs": p, "summary": _summary(rows, p["position_mode"])}


def _lots(c: dict) -> list[dict]:
    """逐笔记录。老数据只有汇总的一条；在「只填权重」模式下改过买入价时，逐笔记录和汇总对不上，也退回汇总。"""
    lots = json.loads(c.get("lots_json") or "[]")
    if lots and summarize_lots(lots)[:2] == (c["shares"], c["cost_price"]):
        return lots
    if c["shares"]:
        return [{"side": "buy", "shares": c["shares"], "price": c["cost_price"], "date": c["opened_at"] or ""}]
    return []


def _weights(holding: list[dict], mode: str) -> None:
    """份额模式：按人民币市值和成本算权重。权重模式：用户填的是买入时的权重，按价格涨跌估算现在的权重。"""
    if mode == "shares":
        total_mv = sum(r.get("market_value") or 0 for r in holding)
        total_cost = sum(r.get("cost_value") or 0 for r in holding)
        for r in holding:
            if total_mv and r.get("market_value") is not None:
                r["weight"] = round(r["market_value"] / total_mv * 100, 2)
            if total_cost and r.get("cost_value") is not None:
                r["cost_weight"] = round(r["cost_value"] / total_cost * 100, 2)
        return
    for r in holding:
        r.pop("market_value", None)
        r.pop("cost_value", None)
        r.pop("pnl", None)
        if r["manual_weight"]:
            r["cost_weight"] = r["manual_weight"]
    drifted = {r["id"]: r["manual_weight"] * (1 + r["pnl_pct"] / 100)
               for r in holding if r["manual_weight"] and r.get("pnl_pct") is not None}
    total = sum(drifted.values())
    with_weight = [r for r in holding if r["manual_weight"]]
    for r in with_weight:
        if total and len(drifted) == len(with_weight):  # 都填了买入价才能估算
            r["weight"] = round(drifted[r["id"]] / total * sum(x["manual_weight"] for x in with_weight), 2)
        else:
            r["weight"] = r["manual_weight"]


def _summary(rows: list[dict], mode: str) -> dict:
    holding = [r for r in rows if r["status"] == "holding"]
    weighted = [r for r in holding if r.get("weight")]
    total_w = sum(r["weight"] for r in weighted)

    def dist(key):
        acc = defaultdict(float)
        for r in weighted:
            acc[key(r) or "未知"] += r["weight"] / total_w * 100
        return sorted(([k, round(v, 1)] for k, v in acc.items()), key=lambda x: -x[1])

    def wavg(metric, harmonic=False):
        """加权平均；PE 用调和平均（等价于组合整体的盈利收益率），亏损的公司不计入。"""
        pairs = [(r["weight"], r["metrics"].get(metric)) for r in weighted if r["metrics"].get(metric) is not None]
        if harmonic:
            pairs = [(w, v) for w, v in pairs if v > 0]
        tw = sum(w for w, _ in pairs)
        if not pairs or not tw:
            return None
        return round(tw / sum(w / v for w, v in pairs), 2) if harmonic else round(sum(w * v for w, v in pairs) / tw, 2)

    closed = [t for r in rows for t in r["trades"][:1] if r["status"] == "closed"]
    out = {
        "counts": {s: sum(1 for r in rows if r["status"] == s) for s in ("holding", "watching", "closed")},
        "position_mode": mode,
        "weighted_count": len(weighted),
        "markets": dist(lambda r: MARKET[r["market"]]) if total_w else [],
        "industries": dist(lambda r: r["industry"])[:8] if total_w else [],
        "pe": wavg("pe", True), "pe_pct": wavg("pe_pct"), "pb_pct": wavg("pb_pct"),
        "dividend_yield": wavg("dividend_yield"), "roe_fy": wavg("roe_fy"),
        "top_weight": max((r["weight"] for r in weighted), default=None),
        "risks": sum(1 for r in rows for c in r["concerns"] if c["kind"] == "risk"),
        "chances": sum(1 for r in rows for c in r["concerns"] if c["kind"] == "chance"),
        "closed_trades": len(closed),
        "wins": sum(1 for t in closed if t["outcome"] == "获利了结"),
    }
    if mode == "shares":
        mv = [r["market_value"] for r in holding if r.get("market_value") is not None]
        out["total_value"] = round(sum(mv), 2) if mv else None
        pnl = [r for r in holding if r.get("pnl") is not None]
        if pnl:
            out["total_pnl"] = round(sum(r["pnl"] for r in pnl), 2)
            cost = sum(r["cost_value"] for r in pnl)
            out["total_pnl_pct"] = round(out["total_pnl"] / cost * 100, 2) if cost else None
    return out


def _events(conn, user_id: int, days: int = 30) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, c.id AS company_id, c.name FROM stock_events e
           JOIN companies c ON c.market = e.market AND c.ticker = e.ticker
           WHERE c.user_id = ? AND c.status != 'closed' AND e.event_date >= date('now', ?)
           ORDER BY e.event_date DESC, e.id DESC""", (user_id, f"-{days} day")).fetchall()
    return [_event_out(r) for r in rows]


def _event_out(r) -> dict:
    d = dict(r)
    d["data"] = json.loads(d.pop("data_json") or "{}")
    d["analysis"] = json.loads(d.pop("analysis_json") or "null") if d.get("analysis_json") else None
    return d


# ---------- 接口 ----------

@router.get("/data")
def data_page(user: dict = Depends(current_user)):
    with get_conn() as conn:
        out = portfolio(conn, user)
        out["events"] = _events(conn, user["id"])
        dg = conn.execute("SELECT * FROM user_data_digests WHERE user_id = ? ORDER BY digest_date DESC LIMIT 1",
                          (user["id"],)).fetchone()
    out["catalog"] = catalog_out()
    out["digest"] = {"date": dg["digest_date"], "overview": dg["overview"], "points": json.loads(dg["points_json"])} \
        if dg else None
    out["has_profile"] = bool(user["profile_json"])
    missing = [r["id"] for r in out["rows"] if r["status"] != "closed" and not r["data_updated_at"]]
    out["updating"] = bool(missing)
    if missing:
        from . import analyzer

        if not analyzer.pending_for(user["id"]) and not analyzer.status["running"]:
            analyzer.enqueue_market(user["id"], missing)
    return out


@router.get("/companies/{company_id}/data")
def company_data(company_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        c = _own_company(conn, company_id, user["id"])
        stock = market.stock_rows({(c["market"], c["ticker"])}).get((c["market"], c["ticker"]))
        prices = [[r["date"], r["close"]] for r in conn.execute(
            "SELECT date, close FROM stock_prices WHERE market = ? AND ticker = ? ORDER BY date DESC LIMIT 250",
            (c["market"], c["ticker"]))][::-1]
        events = [_event_out(r) for r in conn.execute(
            "SELECT * FROM stock_events WHERE market = ? AND ticker = ? ORDER BY event_date DESC LIMIT 20",
            (c["market"], c["ticker"]))]
        analyses = [{"period": r["period"], "created_at": r["created_at"], **json.loads(r["content_json"])}
                    for r in conn.execute("SELECT * FROM report_analyses WHERE company_id = ? ORDER BY period DESC",
                                          (company_id,))]
    return {"annual": (stock or {}).get("annual", []), "latest": (stock or {}).get("latest"),
            "fin_currency": (stock or {}).get("fin_currency", ""), "prices": prices, "events": events,
            "analyses": analyses, "thesis": thesis_text(c)}


class LotIn(BaseModel):
    side: str = Field(default="buy", pattern="^(buy|sell)$")
    shares: float = Field(gt=0)
    price: float | None = Field(default=None, ge=0)
    date: str = Field(default="", max_length=10)


class PositionIn(BaseModel):
    shares: float | None = Field(default=None, ge=0)
    cost_price: float | None = Field(default=None, ge=0)
    manual_weight: float | None = Field(default=None, ge=0, le=100)
    opened_at: str = Field(default="", max_length=10)
    lots: list[LotIn] | None = Field(default=None, max_length=200)  # 按份额填时的逐笔记录


def summarize_lots(lots: list[dict]) -> tuple[float | None, float | None, str]:
    """逐笔记录 → (持有份额, 平均买入价, 首次买入日期)。按日期顺序用移动加权平均：
    买入摊进成本，卖出只减份额不改成本；卖光后再买重新算。没填价格的买入不参与成本。"""
    shares = cost_shares = cost = 0.0
    opened = ""
    for lot in sorted(lots, key=lambda x: x.get("date") or "9999"):
        n, price = lot["shares"], lot.get("price")
        if lot.get("side") == "sell":
            if n > shares + 1e-9:
                raise HTTPException(400, f"{lot.get('date') or '未填日期'} 卖出 {n:g} 股，超过当时持有的 {shares:g} 股")
            ratio = (shares - n) / shares
            shares, cost_shares, cost = shares - n, cost_shares * ratio, cost * ratio
            continue
        if shares <= 1e-9:
            cost_shares = cost = 0.0
            opened = lot.get("date") or ""
        shares += n
        if price:
            cost_shares += n
            cost += n * price
        opened = opened or lot.get("date") or ""
    if shares <= 1e-9:
        return None, None, ""
    return round(shares, 6), round(cost / cost_shares, 4) if cost_shares else None, opened


@router.put("/companies/{company_id}/position")
def update_position(company_id: int, body: PositionIn, user: dict = Depends(current_user)):
    with get_conn() as conn:
        _own_company(conn, company_id, user["id"])
        if body.lots is not None:
            lots = [{**lot.model_dump(), "date": lot.date.strip()} for lot in body.lots]
            shares, cost_price, opened_at = summarize_lots(lots)
            conn.execute("UPDATE companies SET shares = ?, cost_price = ?, opened_at = ?, lots_json = ? WHERE id = ?",
                         (shares, cost_price, opened_at, json.dumps(lots, ensure_ascii=False), company_id))
        else:
            conn.execute("UPDATE companies SET shares = ?, cost_price = ?, manual_weight = ?, opened_at = ? WHERE id = ?",
                         (body.shares or None, body.cost_price or None, body.manual_weight or None,
                          body.opened_at.strip(), company_id))
    return {"ok": True}


class StatusIn(BaseModel):
    status: str = Field(pattern="^(holding|watching)$")


@router.put("/companies/{company_id}/status")
def update_status(company_id: int, body: StatusIn, user: dict = Depends(current_user)):
    """持仓中 ↔ 观察中；投过的公司重新买入或重新关注时恢复跟踪。"""
    from . import analyzer

    with get_conn() as conn:
        c = _own_company(conn, company_id, user["id"])
        conn.execute("UPDATE companies SET status = ? WHERE id = ?", (body.status, company_id))
    if c["status"] == "closed":
        analyzer.enqueue_backfill(user["id"], [company_id])
    return {"ok": True}


class CloseIn(BaseModel):
    outcome: str = Field(pattern="^(获利了结|止损出局)$")
    sell_price: float | None = Field(default=None, gt=0)
    closed_at: str = Field(default="", max_length=10)
    cost_price: float | None = Field(default=None, gt=0)  # 之前没填仓位时，清仓时补填
    opened_at: str = Field(default="", max_length=10)
    note: str = Field(default="", max_length=2000)


@router.post("/companies/{company_id}/close")
def close_position(company_id: int, body: CloseIn, user: dict = Depends(current_user)):
    """清仓：记一笔交易，公司移到「投过的」，停止跟踪。"""
    with get_conn() as conn:
        c = _own_company(conn, company_id, user["id"])
        if c["status"] != "holding":
            raise HTTPException(400, "只有持仓中的公司可以清仓")
        c["cost_price"] = body.cost_price or c["cost_price"]
        c["opened_at"] = body.opened_at.strip() or c["opened_at"]
        pnl = pnl_pct = None
        if body.sell_price and c["cost_price"]:
            pnl_pct = round((body.sell_price / c["cost_price"] - 1) * 100, 2)
            if c["shares"]:
                pnl = round((body.sell_price - c["cost_price"]) * c["shares"], 2)
        cur = conn.execute(
            "INSERT INTO closed_trades (user_id, company_id, outcome, shares, cost_price, sell_price, currency,"
            " opened_at, closed_at, pnl, pnl_pct, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], company_id, body.outcome, c["shares"], c["cost_price"], body.sell_price,
             market.CURRENCY[c["market"]], c["opened_at"] or c["created_at"][:10], body.closed_at or _today(),
             pnl, pnl_pct, body.note.strip()))
        conn.execute("UPDATE companies SET status = 'closed', shares = NULL, cost_price = NULL, manual_weight = NULL,"
                     " opened_at = '', lots_json = '[]' WHERE id = ?", (company_id,))
    return {"trade_id": cur.lastrowid, "pnl": pnl, "pnl_pct": pnl_pct}


@router.post("/trades/{trade_id}/summary")
def trade_summary(trade_id: int, user: dict = Depends(current_user)):
    """AI 写这笔投资期间的总结。"""
    with get_conn() as conn:
        t = conn.execute("SELECT * FROM closed_trades WHERE id = ? AND user_id = ?", (trade_id, user["id"])).fetchone()
        if not t:
            raise HTTPException(404, "不存在")
        t = dict(t)
        c = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (t["company_id"],)).fetchone())
        start, end = t["opened_at"] or "0000", t["closed_at"]
        news = conn.execute(
            "SELECT dr.report_date, ri.title, ri.direction, ri.score, ri.thesis_impact FROM report_items ri"
            " JOIN daily_reports dr ON dr.id = ri.report_id WHERE dr.company_id = ? AND dr.report_date BETWEEN ? AND ?"
            " ORDER BY ABS(ri.score) DESC LIMIT 12", (c["id"], start, end)).fetchall()
        decisions = conn.execute(
            "SELECT decided_on, action, reason, emotion, confidence, review_note FROM decisions"
            " WHERE company_id = ? ORDER BY decided_on", (c["id"],)).fetchall()
        events = conn.execute(
            "SELECT event_date, title, analysis_json FROM stock_events WHERE market = ? AND ticker = ?"
            " AND event_date BETWEEN ? AND ? ORDER BY event_date", (c["market"], c["ticker"], start, end)).fetchall()
        reports = conn.execute("SELECT period, content_json FROM report_analyses WHERE company_id = ? ORDER BY period",
                               (c["id"],)).fetchall()
    ccy = t["currency"]
    msg = "\n\n".join([
        f"公司：{c['name']}（{MARKET[c['market']]} {c['ticker']}）",
        f"## 这笔投资\n结果：{t['outcome']}\n持有：{t['opened_at'] or '未记录'} 至 {t['closed_at']}\n"
        f"买入价：{t['cost_price'] or '未记录'} {ccy}　卖出价：{t['sell_price'] or '未记录'} {ccy}\n"
        f"收益率：{_pct(t['pnl_pct'])}" + (f"　盈亏额：{t['pnl']:,.0f} {ccy}" if t["pnl"] is not None else "")
        + f"\n清仓时写的：{t['note'] or '（没写）'}",
        f"## 买入时的投资逻辑\n{thesis_text(c) or '（没写）'}",
        "## 决策记录\n" + ("\n".join(f"- {d['decided_on']} {d['action']}（把握 {d['confidence']}/5，{d['emotion'] or '情绪未记录'}）："
                                   f"{d['reason'][:200]}" + (f"；复盘：{d['review_note'][:150]}" if d["review_note"] else "")
                                   for d in decisions) or "（没有）"),
        "## 持有期间的重要信息\n" + ("\n".join(f"- {n['report_date']} [{n['direction']} {n['score']:+.0f}] {n['title']}"
                                       f"（对投资逻辑：{n['thesis_impact'] or '—'}）" for n in news) or "（没有检索到）"),
        "## 暴跌与财报事件\n" + ("\n".join(
            [f"- {e['event_date']} {e['title']}：{json.loads(e['analysis_json'] or '{}').get('summary', '')}" for e in events]
            + [f"- {r['period']} 财报：{json.loads(r['content_json']).get('headline', '')}" for r in reports]) or "（没有）"),
    ])
    result = _llm_json([{"role": "system", "content": CLOSE_SUMMARY_SYSTEM}, {"role": "user", "content": msg}],
                       task="close_summary", user_id=user["id"], temperature=0.5, max_tokens=1500)
    summary = {k: result.get(k) for k in ("title", "story", "thesis_review", "decision_quality", "next_time")}
    summary["lessons"] = [str(x) for x in result.get("lessons") or []][:4]
    with get_conn() as conn:
        conn.execute("UPDATE closed_trades SET summary_json = ? WHERE id = ?",
                     (json.dumps(summary, ensure_ascii=False), trade_id))
    return summary


@router.post("/companies/{company_id}/report-analysis")
def report_analysis(company_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        c = _own_company(conn, company_id, user["id"])
    try:
        return generate_report_analysis(c, force=True)
    except llm.BudgetError as e:
        raise HTTPException(429, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))


@router.post("/events/{event_id}/analyze")
def event_analyze(event_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        e = conn.execute(
            "SELECT e.* FROM stock_events e JOIN companies c ON c.market = e.market AND c.ticker = e.ticker"
            " WHERE e.id = ? AND c.user_id = ?", (event_id, user["id"])).fetchone()
        if not e or e["kind"] != "drop":
            raise HTTPException(404, "不存在")
        name = conn.execute("SELECT name FROM companies WHERE market = ? AND ticker = ? AND user_id = ?",
                            (e["market"], e["ticker"], user["id"])).fetchone()["name"]
    try:
        return analyze_drop(dict(e), name)
    except llm.BudgetError as ex:
        raise HTTPException(429, str(ex))
    except llm.LLMError as ex:
        raise HTTPException(502, str(ex))


@router.post("/data/digest")
def data_digest_now(user: dict = Depends(current_user)):
    try:
        generate_data_digest(user["id"])
    except llm.BudgetError as e:
        raise HTTPException(429, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


# ---------- 每日流水线 ----------

def refresh_market(companies: list[dict], status: dict) -> None:
    """更新这些公司的行情与财务；对新出现的暴跌做原因分析，对新财报按持仓生成十年价值分析。"""
    targets = {(c["market"], c["ticker"]): c["name"] for c in companies}
    if not targets:
        return
    try:
        fx = market.refresh_fx()
    except Exception as e:
        log.exception("fx failed")
        status["errors"].append(f"汇率更新失败：{e}")
        fx = market.fx_rates()
    events = []
    for i, (market_, ticker) in enumerate(targets, 1):
        status["progress"] = f"更新行情与财务 {i}/{len(targets)}"
        try:
            events += market.refresh_stock(market_, ticker, fx)
        except Exception as e:
            log.exception("market refresh failed")
            status["errors"].append(f"{targets[(market_, ticker)]} 行情更新失败：{e}")
    for e in events:
        try:
            if e["kind"] == "drop":
                status["progress"] = f"分析暴跌原因：{targets[(e['market'], e['ticker'])]}"
                analyze_drop(e, targets[(e["market"], e["ticker"])])
            else:
                status["progress"] = f"分析新财报：{targets[(e['market'], e['ticker'])]}"
                for c in companies:
                    if (c["market"], c["ticker"]) == (e["market"], e["ticker"]):
                        generate_report_analysis(c)
        except llm.BudgetError:
            raise
        except Exception as ex:
            log.exception("event analysis failed")
            status["errors"].append(f"{targets[(e['market'], e['ticker'])]} {e['title']} 分析失败：{ex}")


def analyze_drop(event: dict, name: str) -> dict:
    """暴跌原因分析，结果存在事件上，多个用户共用。"""
    data = json.loads(event["data_json"]) if isinstance(event.get("data_json"), str) else event.get("data") or {}
    with get_conn() as conn:
        news = [dict(r) for r in conn.execute(
            "SELECT title, content, url, source, published_at FROM raw_news WHERE market = ? AND ticker = ?"
            " AND fetched_at >= datetime('now', '-4 day') ORDER BY published_at DESC LIMIT 15",
            (event["market"], event["ticker"]))]
    if len(news) < 5:  # 自家新闻太少时，专门搜一次下跌原因
        q = f"{name} {event['ticker']} 股价 下跌 原因"
        news += [n for n in sources.bocha_search(q, count=8, freshness="oneWeek") if n["title"]]
    lines = "\n\n".join(f"[{i}] {n['title']}\n来源：{n['source']}　时间：{n['published_at'] or '未知'}\n{n['content'][:300]}"
                        for i, n in enumerate(news, 1)) or "（没有找到相关新闻）"
    idx = data.get("index_change")
    msg = (f"公司：{name}（{MARKET[event['market']]} {event['ticker']}）\n交易日：{event['event_date']}\n"
           f"跌幅：{data.get('change_pct')}%　收盘价：{data.get('close')}\n"
           f"当天{data.get('index', '大盘')}：{'未知' if idx is None else f'{idx:+.2f}%'}\n\n最近的新闻：\n{lines}")
    out = llm.chat_json([{"role": "system", "content": DROP_SYSTEM}, {"role": "user", "content": msg}],
                        task="drop", temperature=0.2, max_tokens=1200)
    used = set()
    reasons = []
    for r in out.get("reasons") or []:
        if not isinstance(r, dict):
            continue
        ids = [i for i in r.get("source_ids") or [] if isinstance(i, int) and 1 <= i <= len(news)]
        used.update(ids)
        reasons.append({"text": str(r.get("text") or ""), "source_ids": ids})
    analysis = {"summary": str(out.get("summary") or ""), "category": str(out.get("category") or "原因不明"),
                "reasons": reasons, "long_term": str(out.get("long_term") or ""),
                "check": [str(x) for x in out.get("check") or []][:3],
                "sources": {i: {"title": news[i - 1]["title"], "url": news[i - 1]["url"], "source": news[i - 1]["source"]}
                            for i in sorted(used)}}
    with get_conn() as conn:
        conn.execute("UPDATE stock_events SET analysis_json = ? WHERE id = ?",
                     (json.dumps(analysis, ensure_ascii=False), event["id"]))
    return analysis


def _annual_table(annual: list[dict]) -> str:
    head = "年度 | 营收 | 营收增速 | 净利润 | 净利增速 | ROE | 毛利率 | 净利率 | 负债率 | 经营现金流 | 资本开支 | 自由现金流"
    rows = [f"{r['date'][:4]} | {_yi(r.get('revenue'))} | {_pct(r.get('rev_yoy'))} | {_yi(r.get('net_profit'))} | "
            f"{_pct(r.get('np_yoy'))} | {_pct(r.get('roe'))} | {_pct(r.get('gross_margin'))} | {_pct(r.get('net_margin'))} | "
            f"{_pct(r.get('debt_ratio'))} | {_yi(r.get('ocf'))} | {_yi(r.get('capex'))} | {_yi(r.get('fcf'))}"
            for r in annual]
    return "\n".join([head, *rows])


def generate_report_analysis(company: dict, force: bool = False) -> dict:
    """新财报的十年价值分析（按持仓生成，结合用户画像和投资逻辑）。"""
    stock = market.stock_rows({(company["market"], company["ticker"])}).get((company["market"], company["ticker"]))
    if not stock or not stock["annual"] or not stock["latest"]:
        raise HTTPException(400, "还没有这家公司的财务数据，等下一次检索更新后再试")
    period = stock["latest"]["date"]
    with get_conn() as conn:
        if not force and conn.execute("SELECT 1 FROM report_analyses WHERE company_id = ? AND period = ?",
                                      (company["id"], period)).fetchone():
            return {}
        user = dict(conn.execute("SELECT * FROM users WHERE id = ?", (company["user_id"],)).fetchone())
    m, lt = stock["metrics"], stock["latest"]
    msg = "\n\n".join([
        f"公司：{company['name']}（{MARKET[company['market']]} {company['ticker']}，{stock['industry'] or '行业未知'}）"
        f"，财报币种：{stock['fin_currency'] or '未知'}",
        f"## 最新一期：{market.period_label(period)}\n营收 {_yi(lt.get('revenue'))}（同比 {_pct(lt.get('rev_yoy'))}），"
        f"净利润 {_yi(lt.get('net_profit'))}（同比 {_pct(lt.get('np_yoy'))}），ROE {_pct(lt.get('roe'))}（累计值，未年化），"
        f"毛利率 {_pct(lt.get('gross_margin'))}，净利率 {_pct(lt.get('net_margin'))}，负债率 {_pct(lt.get('debt_ratio'))}",
        f"## 近十年年度财务\n{_annual_table(stock['annual'])}",
        f"## 当前估值\nPE（TTM）{m.get('pe') or '—'}，历史分位 {_pct(m.get('pe_pct'))}；PB {m.get('pb') or '—'}，"
        f"历史分位 {_pct(m.get('pb_pct'))}；股息率 {_pct(m.get('dividend_yield'))}；市值 / 自由现金流 {m.get('p_fcf') or '—'}；"
        f"自由现金流 / 净利润 {m.get('fcf_np') or '—'}",
        f"## 用户的投资画像\n{profile.brief(user) or '未提供'}",
        f"## 用户的投资逻辑\n{thesis_text(company) or '未提供'}",
    ])
    out = llm.chat_json([{"role": "system", "content": REPORT_SYSTEM}, {"role": "user", "content": msg}],
                        task="report", user_id=company["user_id"], temperature=0.3, max_tokens=2000)
    content = {"headline": str(out.get("headline") or ""), "verdict": str(out.get("verdict") or ""),
               "sections": [s for s in out.get("sections") or [] if isinstance(s, dict) and s.get("comment")],
               "thesis_check": str(out.get("thesis_check") or ""),
               "watch": [str(x) for x in out.get("watch") or []][:4],
               "questions": [str(x) for x in out.get("questions") or []][:3]}
    with get_conn() as conn:
        conn.execute("INSERT INTO report_analyses (company_id, period, content_json) VALUES (?, ?, ?)"
                     " ON CONFLICT (company_id, period) DO UPDATE SET content_json = excluded.content_json,"
                     " created_at = CURRENT_TIMESTAMP", (company["id"], period, json.dumps(content, ensure_ascii=False)))
    return {"period": period, **content}


def generate_data_digest(user_id: int) -> None:
    with get_conn() as conn:
        user = dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        data = portfolio(conn, user)
        events = _events(conn, user_id, days=7)
    rows = [r for r in data["rows"] if r["status"] != "closed" and r["metrics"].get("price")]
    if not rows:
        return
    s = data["summary"]
    fmt = lambda v, suffix="": "—" if v is None else f"{v:.1f}{suffix}"  # noqa: E731
    lines = [f"- {r['name']}（{MARKET[r['market']]}，{r['industry'] or '—'}，{'持仓' if r['status'] == 'holding' else '观察'}"
             f"{'，权重 ' + fmt(r.get('weight'), '%') if r.get('weight') else ''}）："
             f"PE {fmt(r['metrics'].get('pe'))}（分位 {fmt(r['metrics'].get('pe_pct'), '%')}），"
             f"股息率 {fmt(r['metrics'].get('dividend_yield'), '%')}，年度 ROE {fmt(r['metrics'].get('roe_fy'), '%')}，"
             f"一年涨跌 {fmt(r['metrics'].get('chg_250d'), '%')}；关注点：{'；'.join(c['label'] + '（' + c['detail'] + '）' for c in r['concerns']) or '无'}"
             for r in sorted(rows, key=lambda r: -(r.get("weight") or 0))]
    msg = "\n\n".join([
        f"## 用户的投资画像\n{profile.brief(user) or '未提供'}",
        f"## 组合汇总\n持仓 {s['counts']['holding']} 家，观察 {s['counts']['watching']} 家；"
        f"市场分布：{'、'.join(f'{k} {v}%' for k, v in s['markets']) or '未填仓位'}；"
        f"行业分布：{'、'.join(f'{k} {v}%' for k, v in s['industries']) or '未填仓位'}；"
        f"最大单一持仓 {fmt(s['top_weight'], '%')}；加权 PE {fmt(s['pe'])}，加权 PE 分位 {fmt(s['pe_pct'], '%')}，"
        f"加权股息率 {fmt(s['dividend_yield'], '%')}，加权年度 ROE {fmt(s['roe_fy'], '%')}",
        "## 各公司\n" + "\n".join(lines),
        "## 最近 7 天的事件\n" + ("\n".join(f"- {e['event_date']} {e['name']} {e['title']}"
                                        + (f"：{e['analysis']['summary']}" if e["analysis"] else "") for e in events) or "无"),
    ])
    out = llm.chat_json([{"role": "system", "content": DATA_DIGEST_SYSTEM}, {"role": "user", "content": msg}],
                        task="data_digest", user_id=user_id, temperature=0.4, max_tokens=1000)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_data_digests (user_id, digest_date, overview, points_json) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (user_id, digest_date) DO UPDATE SET overview = excluded.overview,"
            " points_json = excluded.points_json, updated_at = CURRENT_TIMESTAMP",
            (user_id, _today(), str(out.get("overview") or ""),
             json.dumps([str(x) for x in out.get("points") or []][:4], ensure_ascii=False)))
