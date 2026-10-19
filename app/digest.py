"""每日概览：汇总用户当天新分析出的信息，给出一句话总览、逐公司小结和组合层面的思考问题。

方向、分数、对投资逻辑的影响都由已有打分在代码里汇总，模型只负责写文字，避免数字前后不一致。
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import llm
from .config import TIMEZONE
from .db import alert_condition, get_conn
from .prompts import DIGEST_SYSTEM

log = logging.getLogger(__name__)

MARKET = {"A": "A股", "HK": "港股", "US": "美股"}


def _today_bounds() -> tuple[str, str]:
    """返回 (本地日期, 本地今天零点对应的 UTC 时间)，report_items.created_at 存的是 UTC。"""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    return now.strftime("%Y-%m-%d"), start_utc.strftime("%Y-%m-%d %H:%M:%S")


def generate(user_id: int) -> None:
    today, start_utc = _today_bounds()
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        companies = [dict(r) for r in conn.execute("SELECT * FROM companies WHERE user_id = ?", (user_id,))]
        items = [dict(r) for r in conn.execute(
            f"""SELECT ri.*, dr.report_date, dr.company_id, {alert_condition('?')} AS is_alert
                FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id
                JOIN companies c ON c.id = dr.company_id
                WHERE c.user_id = ? AND ri.created_at >= ?
                ORDER BY ABS(ri.score) DESC""",
            (user["alert_threshold"], user_id, start_utc))]
    if not items:
        return  # 今天没有新信息，接口会返回"今日无重要变化"

    by_company: dict[int, list[dict]] = {}
    for it in items:
        by_company.setdefault(it["company_id"], []).append(it)
    rows = [_company_row(c, by_company[c["id"]]) for c in companies if c["id"] in by_company]
    rows.sort(key=lambda r: (-r["alerts"], -abs(r["score"])))

    text = _write(user_id, companies, rows, by_company)
    for r in rows:
        r["summary"] = text["summaries"].get(str(r["company_id"])) or r["top_title"]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_digests (user_id, digest_date, overview, companies_json, reflections_json)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id, digest_date) DO UPDATE SET"
            " overview = excluded.overview, companies_json = excluded.companies_json,"
            " reflections_json = excluded.reflections_json, updated_at = CURRENT_TIMESTAMP",
            (user_id, today, text["overview"], json.dumps(rows, ensure_ascii=False),
             json.dumps(text["reflections"], ensure_ascii=False)),
        )


def _company_row(company: dict, items: list[dict]) -> dict:
    # 净倾向：按置信度加权求和，截断到 ±10；最强的那条决定"主要事件"
    net = max(-10.0, min(10.0, sum(i["score"] * i["confidence"] for i in items)))
    impacts = {i["thesis_impact"] for i in items}
    if any(i["thesis_broken"] for i in items):
        thesis = "被证伪"
    elif "削弱" in impacts and "加强" in impacts:
        thesis = "有加强也有削弱"
    elif "削弱" in impacts:
        thesis = "削弱"
    elif "加强" in impacts:
        thesis = "加强"
    else:
        thesis = ""
    bulls = sum(1 for i in items if i["direction"] == "利好")
    bears = sum(1 for i in items if i["direction"] == "利空")
    if bulls and bears and abs(net) < 2:
        stance = "多空交织"
    else:
        stance = "偏利好" if net >= 1 else "偏利空" if net <= -1 else "中性"
    return {
        "company_id": company["id"], "name": company["name"], "ticker": company["ticker"],
        "market": company["market"], "stance": stance, "score": round(net, 1),
        "count": len(items), "alerts": sum(1 for i in items if i["is_alert"]),
        "thesis_effect": thesis, "top_title": items[0]["title"],
        "dates": sorted({i["report_date"] for i in items}),
    }


def _write(user_id: int, companies: list[dict], rows: list[dict], by_company: dict[int, list[dict]]) -> dict:
    quiet = [c["name"] for c in companies if c["id"] not in by_company]
    blocks = []
    for r in rows:
        c = next(x for x in companies if x["id"] == r["company_id"])
        lines = "\n".join(
            f"  - {i['report_date']} [{i['direction']} {i['score']:+.0f}，置信度{i['confidence']:.1f}] {i['title']}：{i['summary']}"
            f"（对投资逻辑：{i['thesis_impact'] or '未提供'}）" for i in by_company[r["company_id"]][:6])
        blocks.append(
            f"### company_id={r['company_id']} {c['name']}（{MARKET[c['market']]} {c['ticker']}）"
            f" 净倾向 {r['stance']} {r['score']:+.1f}；对投资逻辑的影响：{r['thesis_effect'] or '无'}\n"
            f"用户的投资逻辑：{c['thesis'] or '未提供'}\n{lines}")
    user_msg = (f"用户共持有 {len(companies)} 家公司。\n"
                f"今天有新信息的公司：\n\n" + "\n\n".join(blocks) +
                f"\n\n今天没有重要信息的公司：{'、'.join(quiet) or '无'}")
    fallback = {"overview": f"今天 {len(companies)} 家公司中 {len(rows)} 家有新信息。",
                "summaries": {}, "reflections": []}
    try:
        out = llm.chat_json([{"role": "system", "content": DIGEST_SYSTEM}, {"role": "user", "content": user_msg}],
                            temperature=0.4, max_tokens=2500, task="digest", user_id=user_id)
    except Exception:
        log.exception("digest generation failed")
        return fallback
    summaries = out.get("summaries") if isinstance(out.get("summaries"), dict) else {}
    reflections = [str(x) for x in (out.get("reflections") or []) if x][:3]
    return {"overview": out.get("overview") or fallback["overview"],
            "summaries": {str(k): str(v) for k, v in summaries.items()}, "reflections": reflections}


def latest(user_id: int) -> dict:
    today, _ = _today_bounds()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_digests WHERE user_id = ? AND digest_date = ?",
                           (user_id, today)).fetchone()
        alive = {r["id"] for r in conn.execute("SELECT id FROM companies WHERE user_id = ?", (user_id,))}
        last = conn.execute(
            "SELECT MAX(dr.report_date) AS d FROM daily_reports dr JOIN companies c ON c.id = dr.company_id"
            " WHERE c.user_id = ?", (user_id,)).fetchone()["d"]
    if not row:
        return {"date": today, "empty": True, "last_info_date": last, "company_total": len(alive)}
    companies = [c for c in json.loads(row["companies_json"]) if c["company_id"] in alive]  # 去掉已删除的公司
    return {
        "date": today, "empty": not companies, "last_info_date": last, "company_total": len(alive),
        "overview": row["overview"], "companies": companies,
        "reflections": json.loads(row["reflections_json"]),
        "updated_at": _to_local(row["updated_at"]),
    }


def _to_local(utc_str: str) -> str:
    t = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    return t.astimezone(ZoneInfo(TIMEZONE)).strftime("%H:%M")
