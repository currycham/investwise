"""新闻/公告数据源。每个函数返回 [{title, content, url, source, published_at}]，失败时返回空列表不抛异常。"""
import logging
from datetime import datetime, timedelta

import httpx

from .config import BOCHA_API_KEY, FINNHUB_API_KEY, SEC_USER_AGENT

log = logging.getLogger(__name__)

SEC_FORMS = {"8-K", "10-K", "10-Q", "6-K", "20-F", "S-1", "SC 13D", "DEF 14A"}
SEC_8K_ITEMS = {
    "1.01": "签订重大协议", "1.02": "终止重大协议", "1.03": "破产或接管", "1.05": "重大网络安全事件",
    "2.01": "完成收购或资产处置", "2.02": "发布业绩", "2.03": "新增重大债务", "2.05": "重组/裁员成本",
    "2.06": "重大资产减值", "3.01": "退市或不满足上市标准通知", "4.01": "更换审计师",
    "4.02": "此前财报不可再依赖（财务重述）", "5.01": "控制权变更", "5.02": "董事/高管变动",
    "5.07": "股东大会投票结果", "7.01": "监管披露（FD）", "8.01": "其他重大事项",
}
_sec_cik_cache: dict[str, str] = {}


def _item(title, content="", url="", source="", published_at="") -> dict:
    return {
        "title": (title or "").strip(),
        "content": (content or "").strip()[:2000],
        "url": url or "",
        "source": source or "",
        "published_at": str(published_at or ""),
    }


def akshare_stock_news(ticker: str) -> list[dict]:
    """东方财富个股新闻（A股）。"""
    try:
        import akshare as ak
    except ImportError:
        return []
    try:
        df = ak.stock_news_em(symbol=ticker)
    except Exception as e:
        log.warning("akshare stock_news_em(%s) failed: %s", ticker, e)
        return []
    return [
        _item(r.get("新闻标题"), r.get("新闻内容"), r.get("新闻链接"),
              r.get("文章来源") or "东方财富", r.get("发布时间"))
        for r in df.to_dict("records")
    ]


def bocha_search(query: str, count: int = 10) -> list[dict]:
    """博查 AI 搜索，覆盖中文财经媒体与公告转载。"""
    if not BOCHA_API_KEY:
        return []
    try:
        resp = httpx.post(
            "https://api.bochaai.com/v1/web-search",
            headers={"Authorization": f"Bearer {BOCHA_API_KEY}"},
            json={"query": query, "freshness": "oneDay", "summary": True, "count": count},
            timeout=30,
        )
        resp.raise_for_status()
        pages = (resp.json().get("data") or {}).get("webPages", {}).get("value", []) or []
    except Exception as e:
        log.warning("bocha search(%s) failed: %s", query, e)
        return []
    return [
        _item(p.get("name"), p.get("summary") or p.get("snippet"), p.get("url"),
              p.get("siteName") or "网络", p.get("datePublished") or p.get("dateLastCrawled"))
        for p in pages
    ]


def finnhub_company_news(symbol: str, since: datetime) -> list[dict]:
    if not FINNHUB_API_KEY:
        return []
    try:
        resp = httpx.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": since.strftime("%Y-%m-%d"),
                "to": datetime.now().strftime("%Y-%m-%d"),
                "token": FINNHUB_API_KEY,
            },
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json() or []
    except Exception as e:
        log.warning("finnhub news(%s) failed: %s", symbol, e)
        return []
    return [
        _item(r.get("headline"), r.get("summary"), r.get("url"), r.get("source") or "Finnhub",
              datetime.fromtimestamp(r["datetime"]).isoformat() if r.get("datetime") else "")
        for r in rows[:30]
    ]


def _sec_cik(symbol: str) -> str | None:
    if not _sec_cik_cache:
        try:
            resp = httpx.get("https://www.sec.gov/files/company_tickers.json",
                             headers={"User-Agent": SEC_USER_AGENT}, timeout=30)
            resp.raise_for_status()
            for row in resp.json().values():
                _sec_cik_cache[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
        except Exception as e:
            log.warning("SEC ticker map failed: %s", e)
            return None
    return _sec_cik_cache.get(symbol.upper().replace(".", "-"))


def sec_filings(symbol: str, since: datetime) -> list[dict]:
    """SEC EDGAR 官方披露（8-K 重大事项、10-Q/10-K 财报等）。"""
    cik = _sec_cik(symbol)
    if not cik:
        return []
    try:
        resp = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers={"User-Agent": SEC_USER_AGENT}, timeout=30)
        resp.raise_for_status()
        recent = resp.json()["filings"]["recent"]
    except Exception as e:
        log.warning("SEC filings(%s) failed: %s", symbol, e)
        return []
    out = []
    since_str = since.strftime("%Y-%m-%d")
    n = len(recent["form"])
    for form, date, acc, doc, items in zip(recent["form"], recent["filingDate"], recent["accessionNumber"],
                                           recent["primaryDocument"], recent.get("items") or [""] * n):
        if date < since_str:
            break
        if form not in SEC_FORMS:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
        events = "、".join(SEC_8K_ITEMS[i] for i in (items or "").split(",") if i.strip() in SEC_8K_ITEMS)
        title = f"[SEC {form}] {symbol}" + (f"：{events}" if events else "")
        content = f"{symbol} 于 {date} 向 SEC 提交 {form} 文件" + (f"，披露事项：{events}。" if events else "。")
        out.append(_item(title, content, url, "SEC EDGAR", date))
    return out


def fetch_news(market: str, ticker: str, name: str, lookback_hours: int) -> list[dict]:
    since = datetime.now() - timedelta(hours=lookback_hours)
    items: list[dict] = []
    if market == "A":
        items += akshare_stock_news(ticker)
        items += bocha_search(f"{name} {ticker} 公告 最新消息")
    elif market == "HK":
        items += bocha_search(f"{name} {ticker}.HK 港股 公告 最新消息")
    elif market == "US":
        items += finnhub_company_news(ticker, since)
        items += sec_filings(ticker, since)
        items += bocha_search(f"{name} {ticker} 美股 最新消息")
    return [i for i in items if i["title"] and _is_recent(i["published_at"], since)]


def _is_recent(published_at: str, since: datetime) -> bool:
    """时间无法解析时保留，交给模型判断是否旧闻。"""
    s = published_at.strip().replace("T", " ").replace("Z", "")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(s[:n], fmt) >= since
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date() >= since.date()
    except ValueError:
        return True
