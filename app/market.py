"""行情与财务数据：日线价格、估值（PE/PB 及历史分位）、股息率、ROE、近十年年度财务、行业、汇率。

全部来自免费数据源，按 (市场, 代码) 共享，每天更新一次：
- A股：东方财富（日线、估值历史、财务指标、现金流量表、分红）；日线取不到时用新浪、腾讯
- 港股：东方财富（日线、财务指标、现金流量表、公司资料）、百度股市通（近十年 PE/PB）
- 美股：东方财富（日线、财务指标、现金流量表）、Finnhub（PE/PB/ROE 及季度历史、股息率、行业）
每个数据项单独容错：某个接口失败只影响对应字段，不影响其他字段。
"""
import json
import logging
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import DROP_ALERT_PCT, FINNHUB_API_KEY
from .db import get_conn

log = logging.getLogger(__name__)

CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD"}
# 收盘时间（当地时间）：盘中拿到的当天数据不算完整交易日，不参与暴跌判定
CLOSE = {"A": ("Asia/Shanghai", (15, 5)), "HK": ("Asia/Hong_Kong", (16, 15)), "US": ("America/New_York", (16, 5))}
INDEX = {"A": "沪深300", "HK": "恒生指数", "US": "标普500"}
PRICE_DAYS = 400       # 保存约一年半日线，用来算 52 周高点、200 日均线、年涨跌幅
PCT_YEARS = 10         # 估值分位的回看年数
DEFAULT_FX = {"CNY": 1.0, "HKD": 0.92, "USD": 7.15}


def _ak():
    import akshare as ak

    return ak


def _num(v) -> float | None:
    """把接口里的数字、"12.3%"、"862.28亿" 之类统一成 float；空值返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "False", "nan", "None"):
        return None
    mult = 1.0
    for suffix, m in (("%", 1.0), ("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if s.endswith(suffix):
            s, mult = s[: -len(suffix)], m
            break
    try:
        return float(s) * mult
    except ValueError:
        return None


def _day(v) -> str:
    return str(v)[:10]


def percentile(history: list[float], current: float | None) -> float | None:
    """当前值在历史中的分位（0~100）。只统计正值：亏损时的负 PE 没有可比性。"""
    vals = [v for v in history if v and v > 0]
    if current is None or current <= 0 or len(vals) < 20:
        return None
    return round(sum(1 for v in vals if v <= current) / len(vals) * 100, 1)


def _a_exchange(ticker: str) -> str:
    return "SH" if ticker.startswith(("5", "6", "9")) and not ticker.startswith("92") else \
        "BJ" if ticker.startswith(("4", "8", "92")) else "SZ"


def is_fund(market: str, ticker: str) -> bool:
    """场内 ETF / LOF（沪市 5 开头、深市 15/16/18 开头）：只有价格，没有估值和财报。"""
    return market == "A" and ticker.startswith(("5", "15", "16", "18"))


def _us_em(ticker: str) -> str:
    return ticker.replace(".", "_")


# ---------- 汇率 ----------

def refresh_fx() -> dict[str, float]:
    """中行折算价（人民币 / 1 单位外币），每天存一条；取不到时用最近一次的值。"""
    ak = _ak()
    start = (date.today() - timedelta(days=14)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    for ccy, name in (("USD", "美元"), ("HKD", "港币")):
        try:
            df = ak.currency_boc_sina(symbol=name, start_date=start, end_date=end)
            rows = [(r["日期"], _num(r["中行折算价"])) for r in df.to_dict("records") if _num(r["中行折算价"])]
            with get_conn() as conn:
                conn.executemany("INSERT OR REPLACE INTO fx_rates (currency, date, rate) VALUES (?, ?, ?)",
                                 [(ccy, _day(d), v / 100) for d, v in rows])
        except Exception as e:
            log.warning("fx %s failed: %s", ccy, e)
    return fx_rates()


def fx_rates() -> dict[str, float]:
    out = dict(DEFAULT_FX)
    with get_conn() as conn:
        for r in conn.execute("SELECT currency, rate FROM fx_rates f WHERE date ="
                              " (SELECT MAX(date) FROM fx_rates WHERE currency = f.currency)"):
            out[r["currency"]] = r["rate"]
    return out


# ---------- 日线 ----------

def _fetch_prices(market: str, ticker: str, stored_prefix: str) -> tuple[list[tuple], str]:
    """返回 ([(日期, 收盘, 涨跌幅%)], 美股交易所前缀)。前复权：最新价是真实价格，历史价格可比。
    先用东方财富；它的日线接口常常断开云服务器的连接，取不到时换新浪、腾讯。"""
    try:
        rows, prefix = _prices_em(market, ticker, stored_prefix)
        if rows:
            return rows, prefix
        err = None
    except Exception as e:
        err, prefix = e, stored_prefix
    for fn in _PRICE_BACKUPS[market]:
        try:
            if rows := _with_change(fn(ticker)):
                return rows, prefix
        except Exception as e:
            err = err or e
    if err:
        raise err
    return [], prefix


def _prices_em(market: str, ticker: str, stored_prefix: str) -> tuple[list[tuple], str]:
    ak = _ak()
    kw = dict(period="daily", start_date=_price_start(), end_date="20500101", adjust="qfq")
    prefix = stored_prefix
    if is_fund(market, ticker):
        df = ak.fund_etf_hist_em(symbol=ticker, **kw)
    elif market == "A":
        df = ak.stock_zh_a_hist(symbol=ticker, **kw)
    elif market == "HK":
        df = ak.stock_hk_hist(symbol=ticker, **kw)
    else:
        df = None
        # 东方财富的美股代码要带交易所前缀：105 纳斯达克、106 纽交所、107 美交所
        for p in ([stored_prefix] if stored_prefix else []) + ["105", "106", "107"]:
            try:
                df = ak.stock_us_hist(symbol=f"{p}.{_us_em(ticker)}", **kw)
            except Exception:
                df = None
            if df is not None and len(df):
                prefix = p
                break
        if df is None:
            return [], prefix
    rows = [(_day(r["日期"]), _num(r["收盘"]), _num(r["涨跌幅"])) for r in df.to_dict("records")]
    return [r for r in rows if r[1]], prefix


def _price_start() -> str:
    return (date.today() - timedelta(days=PRICE_DAYS)).strftime("%Y%m%d")


def _with_change(df) -> list[tuple]:
    """备用数据源只有收盘价：截取近 PRICE_DAYS 天，自己算涨跌幅。"""
    start = str(date.today() - timedelta(days=PRICE_DAYS))
    pts = [(_day(r["date"]), _num(r["close"])) for r in df.to_dict("records")]
    pts = [p for p in pts if p[1]]
    rows = []
    for i, (d, close) in enumerate(pts):
        if d >= start:
            rows.append((d, close, round((close / pts[i - 1][1] - 1) * 100, 2) if i else None))
    return rows


def _a_symbol(ticker: str) -> str:
    return _a_exchange(ticker).lower() + ticker


def _a_backup_sina(ticker: str):
    ak = _ak()
    if is_fund("A", ticker):  # 新浪的 ETF 日线不复权，分红除息当天会有小幅跳空
        return ak.fund_etf_hist_sina(symbol=_a_symbol(ticker))
    return ak.stock_zh_a_daily(symbol=_a_symbol(ticker), start_date=_price_start(), end_date="20500101", adjust="qfq")


def _a_backup_tx(ticker: str):
    return _ak().stock_zh_a_hist_tx(symbol=_a_symbol(ticker), start_date=_price_start(), end_date="20500101",
                                    adjust="qfq")


_PRICE_BACKUPS = {
    "A": [_a_backup_tx, _a_backup_sina],
    "HK": [lambda t: _ak().stock_hk_daily(symbol=t, adjust="qfq")],
    "US": [lambda t: _ak().stock_us_daily(symbol=t, adjust="qfq")],
}


def _completed(market: str, rows: list[tuple]) -> list[tuple]:
    """去掉盘中的当天数据。"""
    tz, close = CLOSE[market]
    now = datetime.now(ZoneInfo(tz))
    if rows and rows[-1][0] == now.strftime("%Y-%m-%d") and (now.hour, now.minute) < close:
        return rows[:-1]
    return rows


def _price_metrics(rows: list[tuple]) -> dict:
    if not rows:
        return {}
    closes = [r[1] for r in rows]
    last = closes[-1]

    def chg(n):
        return round((last / closes[-n - 1] - 1) * 100, 2) if len(closes) > n else None

    year = closes[-250:]
    ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
    return {
        "price": last, "price_date": rows[-1][0], "change_pct": rows[-1][2],
        "chg_20d": chg(20), "chg_60d": chg(60), "chg_250d": chg(250),
        "high_52w": max(year), "dist_high_52w": round((last / max(year) - 1) * 100, 2),
        "ma200_gap": round((last / ma200 - 1) * 100, 2) if ma200 else None,
    }


# ---------- 各市场的估值与财务 ----------

def _cutoff() -> str:
    return str(date.today() - timedelta(days=365 * PCT_YEARS))


def _a_data(ticker: str, need_fin: bool, price: float | None) -> dict:
    ak = _ak()
    out: dict = {"metrics": {}, "errors": []}
    m = out["metrics"]
    try:
        df = ak.stock_value_em(symbol=ticker)
        recs = [r for r in df.to_dict("records") if _day(r["数据日期"]) >= _cutoff()]
        cur = recs[-1]
        m.update(pe=_num(cur["PE(TTM)"]), pb=_num(cur["市净率"]), market_cap=_num(cur["总市值"]))
        m["pe_pct"] = percentile([_num(r["PE(TTM)"]) for r in recs], m["pe"])
        m["pb_pct"] = percentile([_num(r["市净率"]) for r in recs], m["pb"])
        m["pct_years"] = round((date.today() - date.fromisoformat(_day(recs[0]["数据日期"]))).days / 365, 1)
    except Exception as e:
        out["errors"].append(f"估值：{e}")
    try:
        # 股息率（TTM）：除权除息日在近一年内的现金分红合计 / 当前股价
        df = ak.stock_fhps_detail_em(symbol=ticker)
        year_ago = str(date.today() - timedelta(days=365))
        per10 = sum(_num(r["现金分红-现金分红比例"]) or 0 for r in df.to_dict("records")
                    if r.get("除权除息日") is not None and str(r["除权除息日"]) not in ("NaT", "nan")
                    and year_ago <= _day(r["除权除息日"]) <= str(date.today()))
        if price:
            m["dividend_yield"] = round(per10 / 10 / price * 100, 2)
    except Exception as e:
        out["errors"].append(f"分红：{e}")
    try:
        # 「按报告期」是累计值（中报 = 上半年合计）；「按单季度」是单季值，不能当年度数据用
        df = ak.stock_financial_analysis_indicator_em(symbol=f"{ticker}.{_a_exchange(ticker)}", indicator="按报告期")
        recs = sorted(df.to_dict("records"), key=lambda r: str(r["REPORT_DATE"]))
        to_row = lambda r: {  # noqa: E731
            "date": _day(r["REPORT_DATE"]), "revenue": _num(r["TOTALOPERATEREVE"]),
            "net_profit": _num(r["PARENTNETPROFIT"]), "rev_yoy": _num(r["TOTALOPERATEREVETZ"]),
            "np_yoy": _num(r["PARENTNETPROFITTZ"]), "roe": _num(r["ROEJQ"]),
            "gross_margin": _num(r["XSMLL"]), "net_margin": _num(r["XSJLL"]), "debt_ratio": _num(r["ZCFZL"]),
            "eps": _num(r["EPSJB"]), "bps": _num(r["BPS"])}
        out["latest"] = to_row(recs[-1])
        if need_fin:
            out["annual"] = [to_row(r) for r in recs if _day(r["REPORT_DATE"]).endswith("12-31")][-PCT_YEARS:]
    except Exception as e:
        out["errors"].append(f"财务指标：{e}")
    if need_fin and out.get("annual"):
        by_year = {r["date"][:4]: r for r in out["annual"]}
        try:
            df = ak.stock_cash_flow_sheet_by_yearly_em(symbol=f"{_a_exchange(ticker)}{ticker}")
            for r in df.to_dict("records"):
                row = by_year.get(_day(r["REPORT_DATE"])[:4])
                if row:
                    row["ocf"] = _num(r.get("NETCASH_OPERATE"))
                    row["capex"] = _num(r.get("CONSTRUCT_LONG_ASSET"))
        except Exception as e:
            out["errors"].append(f"现金流量表：{e}")
    if need_fin:
        try:
            info = ak.stock_individual_info_em(symbol=ticker)
            out["industry"] = str(dict(zip(info["item"], info["value"])).get("行业") or "")
        except Exception as e:
            out["errors"].append(f"行业：{e}")
    return out


def _baidu_history(market: str, ticker: str, indicator: str) -> list[float]:
    ak = _ak()
    fn = ak.stock_hk_valuation_baidu if market == "HK" else ak.stock_zh_valuation_baidu
    df = fn(symbol=ticker, indicator=indicator, period="近十年")
    return [_num(v) for v in df["value"]]


def _hk_data(ticker: str, need_fin: bool, price: float | None) -> dict:
    ak = _ak()
    out: dict = {"metrics": {}, "errors": []}
    m = out["metrics"]
    try:
        cur = ak.stock_hk_financial_indicator_em(symbol=ticker).to_dict("records")[0]
        m.update(pe=_num(cur.get("市盈率")), pb=_num(cur.get("市净率")), dividend_yield=_num(cur.get("股息率TTM(%)")),
                 market_cap=_num(cur.get("总市值(港元)")))
    except Exception as e:
        out["errors"].append(f"估值：{e}")
    for key, ind in (("pe", "市盈率(TTM)"), ("pb", "市净率")):
        try:
            hist = _baidu_history("HK", ticker, ind)
            m[f"{key}_pct"] = percentile(hist, m.get(key))
            m["pct_years"] = PCT_YEARS
        except Exception as e:
            out["errors"].append(f"{ind}历史：{e}")
    to_row = lambda r: {  # noqa: E731
        "date": _day(r["REPORT_DATE"]), "revenue": _num(r.get("OPERATE_INCOME")),
        "net_profit": _num(r.get("HOLDER_PROFIT")), "rev_yoy": _num(r.get("OPERATE_INCOME_YOY")),
        "np_yoy": _num(r.get("HOLDER_PROFIT_YOY")), "roe": _num(r.get("ROE_AVG")),
        "gross_margin": _num(r.get("GROSS_PROFIT_RATIO")), "net_margin": _num(r.get("NET_PROFIT_RATIO")),
        "debt_ratio": _num(r.get("DEBT_ASSET_RATIO")), "eps": _num(r.get("BASIC_EPS")), "bps": _num(r.get("BPS"))}
    try:
        recs = sorted(ak.stock_financial_hk_analysis_indicator_em(symbol=ticker, indicator="报告期").to_dict("records"),
                      key=lambda r: str(r["REPORT_DATE"]))
        out["latest"] = to_row(recs[-1])
        out["fin_currency"] = str(recs[-1].get("CURRENCY") or "")
    except Exception as e:
        out["errors"].append(f"财务指标：{e}")
    if need_fin:
        try:
            recs = sorted(ak.stock_financial_hk_analysis_indicator_em(symbol=ticker, indicator="年度").to_dict("records"),
                          key=lambda r: str(r["REPORT_DATE"]))
            out["annual"] = [to_row(r) for r in recs][-PCT_YEARS:]
            by_year = {r["date"][:4]: r for r in out["annual"]}
            df = ak.stock_financial_hk_report_em(stock=ticker, symbol="现金流量表", indicator="年度")
            for r in df.to_dict("records"):
                row = by_year.get(_day(r["REPORT_DATE"])[:4])
                if row and r["STD_ITEM_NAME"] == "经营业务现金净额":
                    row["ocf"] = _num(r["AMOUNT"])
                elif row and r["STD_ITEM_NAME"] == "购建固定资产":
                    row["capex"] = abs(_num(r["AMOUNT"]) or 0)
        except Exception as e:
            out["errors"].append(f"年度财务：{e}")
        try:
            out["industry"] = str(ak.stock_hk_company_profile_em(symbol=ticker).to_dict("records")[0].get("所属行业") or "")
        except Exception as e:
            out["errors"].append(f"行业：{e}")
    return out


def _finnhub(path: str, **params) -> dict:
    for attempt in range(2):  # 偶尔会断连，重试一次
        try:
            resp = httpx.get(f"https://finnhub.io/api/v1/{path}", params={**params, "token": FINNHUB_API_KEY}, timeout=30)
            resp.raise_for_status()
            return resp.json() or {}
        except httpx.HTTPError:
            if attempt:
                raise
    return {}


def _us_data(ticker: str, need_fin: bool, price: float | None) -> dict:
    ak = _ak()
    out: dict = {"metrics": {}, "errors": []}
    m = out["metrics"]
    if FINNHUB_API_KEY:
        try:
            d = _finnhub("stock/metric", symbol=ticker, metric="all")
            fm, q = d.get("metric") or {}, (d.get("series") or {}).get("quarterly") or {}
            m.update(pe=fm.get("peTTM"), pb=fm.get("pb") or fm.get("pbQuarterly"),
                     dividend_yield=fm.get("currentDividendYieldTTM") or fm.get("dividendYieldIndicatedAnnual"),
                     market_cap=(fm.get("marketCapitalization") or 0) * 1e6 or None)
            hist = lambda k: [p["v"] for p in q.get(k) or [] if p.get("period", "") >= _cutoff()]  # noqa: E731
            m["pe_pct"] = percentile(hist("peTTM") or hist("pe"), m["pe"])
            m["pb_pct"] = percentile(hist("pb"), m["pb"])
            m["pct_years"] = PCT_YEARS
            out["roe_ttm"] = fm.get("roeTTM")
            out["roe_annual"] = {p["period"][:4]: p["v"] * 100 for p in (d.get("series") or {}).get("annual", {}).get("roe") or []}
        except Exception as e:
            out["errors"].append(f"Finnhub 指标：{e}")
        if need_fin:
            try:
                out["industry"] = _finnhub("stock/profile2", symbol=ticker).get("finnhubIndustry") or ""
            except Exception as e:
                out["errors"].append(f"行业：{e}")
    to_row = lambda r: {  # noqa: E731
        "date": _day(r["REPORT_DATE"]), "revenue": _num(r.get("OPERATE_INCOME")),
        "net_profit": _num(r.get("PARENT_HOLDER_NETPROFIT")), "rev_yoy": _num(r.get("OPERATE_INCOME_YOY")),
        "np_yoy": _num(r.get("PARENT_HOLDER_NETPROFIT_YOY")), "roe": _num(r.get("ROE_AVG")),
        "gross_margin": _num(r.get("GROSS_PROFIT_RATIO")), "net_margin": _num(r.get("NET_PROFIT_RATIO")),
        "debt_ratio": _num(r.get("DEBT_ASSET_RATIO")), "eps": _num(r.get("BASIC_EPS"))}
    try:
        recs = sorted(ak.stock_financial_us_analysis_indicator_em(symbol=_us_em(ticker), indicator="累计季报")
                      .to_dict("records"), key=lambda r: str(r["REPORT_DATE"]))
        out["latest"] = to_row(recs[-1])
        out["fin_currency"] = str(recs[-1].get("CURRENCY_ABBR") or "")
    except Exception as e:
        out["errors"].append(f"财务指标：{e}")
    if need_fin:
        try:
            recs = sorted(ak.stock_financial_us_analysis_indicator_em(symbol=_us_em(ticker), indicator="年报")
                          .to_dict("records"), key=lambda r: str(r["REPORT_DATE"]))
            out["annual"] = [to_row(r) for r in recs][-PCT_YEARS:]
            by_year = {r["date"][:4]: r for r in out["annual"]}
            df = ak.stock_financial_us_report_em(stock=_us_em(ticker), symbol="现金流量表", indicator="年报")
            for r in df.to_dict("records"):
                row = by_year.get(_day(r["REPORT_DATE"])[:4])
                if row and r["ITEM_NAME"] == "经营活动产生的现金流量净额":
                    row["ocf"] = _num(r["AMOUNT"])
                elif row and r["ITEM_NAME"] == "购买固定资产":
                    row["capex"] = abs(_num(r["AMOUNT"]) or 0)
        except Exception as e:
            out["errors"].append(f"年度财务：{e}")
    # 金融类公司（如伯克希尔）东方财富没有 ROE，用 Finnhub 补
    for r in out.get("annual") or []:
        if r["roe"] is None:
            r["roe"] = out.get("roe_annual", {}).get(r["date"][:4])
    if out.get("latest") and out["latest"]["roe"] is None:
        out["latest"]["roe"] = out.get("roe_ttm")
    return out


FETCHERS = {"A": _a_data, "HK": _hk_data, "US": _us_data}


# ---------- 汇总 ----------

def _derive(m: dict, annual: list[dict], latest: dict | None, fx: dict, price_ccy: str, fin_ccy: str) -> None:
    """由年度财务和最新报告补充派生指标。百分比字段都以 % 为单位。"""
    for r in annual:
        if r.get("ocf") is not None:
            r["fcf"] = r["ocf"] - (r.get("capex") or 0)
    fy = annual[-1] if annual else {}
    if latest:
        m["report_period"] = latest["date"]
        m["roe"] = latest.get("roe")
        m["rev_yoy"], m["np_yoy"] = latest.get("rev_yoy"), latest.get("np_yoy")
        m["gross_margin"], m["net_margin"] = latest.get("gross_margin"), latest.get("net_margin")
        if latest.get("debt_ratio") is not None:
            m["debt_ratio"] = latest["debt_ratio"]
    m["roe_fy"] = fy.get("roe")
    m.setdefault("debt_ratio", fy.get("debt_ratio"))
    if fy.get("ocf") is not None and fy.get("net_profit"):
        m["ocf_np"] = round(fy["ocf"] / fy["net_profit"], 2)
    m["fcf"] = fy.get("fcf")
    # 净利润或自由现金流 ≤ 0 时比值没有意义，留空
    if fy.get("fcf") is not None and fy.get("net_profit") and fy["net_profit"] > 0:
        m["fcf_np"] = round(fy["fcf"] / fy["net_profit"], 2)
    if fy.get("fcf") and fy["fcf"] > 0 and m.get("market_cap"):
        fin_rate = fx.get(fin_ccy or price_ccy, 1.0)
        m["p_fcf"] = round(m["market_cap"] * fx.get(price_ccy, 1.0) / (fy["fcf"] * fin_rate), 2)
    # PEG 用近 3 年净利润复合增速：单期同比波动太大
    profits = [r.get("net_profit") for r in annual[-4:]]
    if len(profits) == 4 and all(p and p > 0 for p in profits):
        m["np_cagr3"] = round(((profits[-1] / profits[0]) ** (1 / 3) - 1) * 100, 2)
        if m.get("pe") and m["pe"] > 0 and m["np_cagr3"] > 0:
            m["peg"] = round(m["pe"] / m["np_cagr3"], 2)
    for k, v in list(m.items()):
        if isinstance(v, float):
            m[k] = round(v, 4)


def refresh_stock(market: str, ticker: str, fx: dict | None = None) -> list[dict]:
    """更新一只股票的价格、估值和财务，返回本次新出现的事件（暴跌 / 新财报）。"""
    fx = fx or fx_rates()
    with get_conn() as conn:
        old = conn.execute("SELECT * FROM stock_data WHERE market = ? AND ticker = ?", (market, ticker)).fetchone()
    old = dict(old) if old else {}
    errors: list[str] = []

    try:
        rows, prefix = _fetch_prices(market, ticker, old.get("us_prefix") or "")
    except Exception as e:
        rows, prefix = [], old.get("us_prefix") or ""
        errors.append(f"日线：{e}")
    rows = _completed(market, rows)
    if rows:
        with get_conn() as conn:
            conn.execute("DELETE FROM stock_prices WHERE market = ? AND ticker = ?", (market, ticker))
            conn.executemany("INSERT INTO stock_prices (market, ticker, date, close, change_pct) VALUES (?, ?, ?, ?, ?)",
                             [(market, ticker, *r) for r in rows])
    elif not old:
        errors.append("没有取到日线")
    else:  # 这次没取到：用上次存的日线，至少保留股价和依赖股价的指标
        with get_conn() as conn:
            rows = [tuple(r) for r in conn.execute("SELECT date, close, change_pct FROM stock_prices"
                                                   " WHERE market = ? AND ticker = ? ORDER BY date", (market, ticker))]

    metrics = _price_metrics(rows)
    # 年度财务和行业变化慢：只在第一次、出了新报告或者超过一周时重新拉取
    old_annual = json.loads(old.get("annual_json") or "[]")
    stale = (not old.get("fin_updated_at") or old["fin_updated_at"] < str(date.today() - timedelta(days=7))
             or not old.get("industry"))
    if is_fund(market, ticker):
        data = {"metrics": {}, "errors": [], "industry": "ETF / 基金"}
    else:
        data = FETCHERS[market](ticker, need_fin=stale or not old_annual, price=metrics.get("price"))
    latest = data.get("latest")
    new_report = bool(latest and old.get("latest_report") and latest["date"] > old["latest_report"])
    if new_report and "annual" not in data:  # 出了新报告：重新拉一次年度财务
        data = FETCHERS[market](ticker, need_fin=True, price=metrics.get("price"))
        latest = data.get("latest") or latest
    errors += data["errors"]
    annual = data.get("annual") or old_annual
    metrics.update({k: v for k, v in data["metrics"].items() if v is not None})
    price_ccy, fin_ccy = CURRENCY[market], data.get("fin_currency") or old.get("fin_currency") or CURRENCY[market]
    fin_ccy = {"RMB": "CNY", "人民币": "CNY", "港元": "HKD", "美元": "USD"}.get(fin_ccy, fin_ccy)
    _derive(metrics, annual, latest, fx, price_ccy, fin_ccy)

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO stock_data (market, ticker, industry, currency, fin_currency, metrics_json, annual_json,
                   latest_json, latest_report, us_prefix, error, updated_at, fin_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
               ON CONFLICT (market, ticker) DO UPDATE SET industry = excluded.industry, currency = excluded.currency,
                   fin_currency = excluded.fin_currency, metrics_json = excluded.metrics_json,
                   annual_json = excluded.annual_json, latest_json = excluded.latest_json,
                   latest_report = excluded.latest_report, us_prefix = excluded.us_prefix, error = excluded.error,
                   updated_at = CURRENT_TIMESTAMP, fin_updated_at = excluded.fin_updated_at""",
            (market, ticker, data.get("industry") or old.get("industry") or "", price_ccy, fin_ccy,
             json.dumps(metrics, ensure_ascii=False), json.dumps(annual, ensure_ascii=False),
             json.dumps(latest or json.loads(old.get("latest_json") or "null"), ensure_ascii=False),
             (latest or {}).get("date") or old.get("latest_report") or "", prefix, "；".join(errors)[:1000],
             str(date.today()) if "annual" in data else old.get("fin_updated_at")))

    events = []
    if rows and rows[-1][2] is not None and rows[-1][2] <= -DROP_ALERT_PCT:
        d, close, pct = rows[-1]
        events.append(_add_event(market, ticker, "drop", d, f"单日下跌 {abs(pct):.1f}%",
                                 {"change_pct": pct, "close": close, "index": INDEX[market],
                                  "index_change": index_change(market, d)}))
    if new_report:
        events.append(_add_event(market, ticker, "report", latest["date"], f"发布 {period_label(latest['date'])}",
                                 {"period": latest["date"], "latest": latest}))
    return [e for e in events if e]


def _add_event(market: str, ticker: str, kind: str, event_date: str, title: str, data: dict) -> dict | None:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO stock_events (market, ticker, kind, event_date, title, data_json)"
            " VALUES (?, ?, ?, ?, ?, ?)", (market, ticker, kind, event_date, title, json.dumps(data, ensure_ascii=False)))
        if not cur.rowcount:
            return None
        return dict(conn.execute("SELECT * FROM stock_events WHERE id = ?", (cur.lastrowid,)).fetchone())


def period_label(d: str) -> str:
    """2026-06-30 → 2026 中报。"""
    suffix = {"03-31": "一季报", "06-30": "中报", "09-30": "三季报", "12-31": "年报"}.get(d[5:10], "财报")
    return f"{d[:4]} {suffix}"


_index_cache: dict[str, dict[str, float]] = {}


def index_change(market: str, day: str) -> float | None:
    """某个交易日对应大盘指数的涨跌幅（%），用来判断是个股问题还是整个市场在跌。"""
    try:
        if market not in _index_cache or day not in _index_cache[market]:
            ak = _ak()
            if market == "A":
                df = ak.stock_zh_index_daily_em(symbol="sh000300")
                closes = list(zip(df["date"].astype(str), df["close"]))
            elif market == "HK":
                df = ak.stock_hk_index_daily_em(symbol="HSI")
                closes = list(zip(df["date"].astype(str), df["latest"]))
            else:
                df = ak.index_us_stock_sina(symbol=".INX")
                closes = list(zip(df["date"].astype(str), df["close"]))
            closes = closes[-30:]
            _index_cache[market] = {d[:10]: round((c / closes[i - 1][1] - 1) * 100, 2)
                                    for i, (d, c) in enumerate(closes) if i}
        return _index_cache[market].get(day)
    except Exception as e:
        log.warning("index %s failed: %s", market, e)
        return None


def stock_rows(keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """批量读取已存的行情与财务。"""
    if not keys:
        return {}
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM stock_data").fetchall() if len(keys) > 200 else [
            r for k in keys for r in conn.execute("SELECT * FROM stock_data WHERE market = ? AND ticker = ?", k)]
    out = {}
    for r in rows:
        if (r["market"], r["ticker"]) in keys:
            d = dict(r)
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            d["annual"] = json.loads(d.pop("annual_json") or "[]")
            d["latest"] = json.loads(d.pop("latest_json") or "null")
            out[(r["market"], r["ticker"])] = d
    return out
