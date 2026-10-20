"""提升投资认知的功能：投资逻辑体检、先判断再看 AI、决策日志与到期复盘。"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import llm
from .auth import current_user
from .config import TIMEZONE
from .db import get_conn, thesis_text
from .prompts import DECISION_REVIEW_SYSTEM, THESIS_REVIEW_SYSTEM

router = APIRouter(prefix="/api")
MARKET = {"A": "A股", "HK": "港股", "US": "美股"}
ACTIONS = ["买入", "加仓", "减仓", "卖出", "继续持有", "观望"]
VERDICTS = ["利好", "利空", "中性"]


def _today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _llm_json(messages: list[dict], task: str, user_id: int, **kw) -> dict:
    try:
        return llm.chat_json(messages, task=task, user_id=user_id, **kw)
    except llm.BudgetError as e:
        raise HTTPException(429, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))


def _own(conn, table: str, row_id: int, user_id: int) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND user_id = ?", (row_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "不存在")
    return dict(row)


# ---------- 投资逻辑体检 ----------

class ThesisIn(BaseModel):
    name: str = Field(default="", max_length=64)
    ticker: str = Field(default="", max_length=16)
    thesis: str = Field(default="", max_length=2000)
    assumptions: str = Field(default="", max_length=2000)
    wrong_if: str = Field(default="", max_length=2000)
    horizon: str = Field(default="", max_length=32)


@router.post("/thesis/review")
def review_thesis(body: ThesisIn, user: dict = Depends(current_user)):
    text = thesis_text(body.model_dump())
    if len(text) < 10:
        raise HTTPException(400, "先写几句再来体检")
    result = _llm_json(
        [{"role": "system", "content": THESIS_REVIEW_SYSTEM},
         {"role": "user", "content": f"公司：{body.name}（{body.ticker}）\n\n{text}"}],
        task="thesis_review", user_id=user["id"], temperature=0.3, max_tokens=1200)
    issues = [i for i in result.get("issues") or [] if isinstance(i, dict) and i.get("question")]
    return {"overall": str(result.get("overall") or ""), "issues": issues[:4]}


# ---------- 先判断，再看 AI ----------

class JudgeIn(BaseModel):
    verdict: str = Field(pattern="^(利好|利空|中性)$")


@router.post("/items/{item_id}/judge")
def judge(item_id: int, body: JudgeIn, user: dict = Depends(current_user)):
    with get_conn() as conn:
        owner = conn.execute(
            "SELECT c.user_id FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id"
            " JOIN companies c ON c.id = dr.company_id WHERE ri.id = ?", (item_id,)).fetchone()
        if not owner or owner["user_id"] != user["id"]:
            raise HTTPException(404, "不存在")
        # 判断只记第一次：揭晓 AI 结论之后再改，就失去了对照的意义
        conn.execute("INSERT OR IGNORE INTO item_judgments (item_id, user_id, verdict) VALUES (?, ?, ?)",
                     (item_id, user["id"], body.verdict))
    return {"ok": True}


@router.get("/thinking/judgments")
def judgment_stats(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT j.verdict, j.created_at AS judged_at, ri.id, ri.title, ri.direction, ri.score, ri.confidence,
                      ri.reasoning, dr.report_date, c.name AS company_name
               FROM item_judgments j JOIN report_items ri ON ri.id = j.item_id
               JOIN daily_reports dr ON dr.id = ri.report_id JOIN companies c ON c.id = dr.company_id
               WHERE j.user_id = ? ORDER BY j.created_at DESC""", (user["id"],))]
    agree = [r for r in rows if r["verdict"] == r["direction"]]
    # 方向完全相反（你看利好、AI 看利空或反之）最值得回看
    opposite = [r for r in rows if {r["verdict"], r["direction"]} == {"利好", "利空"}]
    matrix = {v: {d: 0 for d in VERDICTS} for v in VERDICTS}
    for r in rows:
        if r["direction"] in VERDICTS:
            matrix[r["verdict"]][r["direction"]] += 1
    return {"total": len(rows), "agree": len(agree), "opposite": len(opposite), "matrix": matrix,
            "disagreements": [r for r in rows if r["verdict"] != r["direction"]][:20]}


# ---------- 决策日志 ----------

class DecisionIn(BaseModel):
    action: str = Field(pattern=f"^({'|'.join(ACTIONS)})$")
    reason: str = Field(min_length=2, max_length=2000)
    emotion: str = Field(default="", max_length=32)
    confidence: int = Field(default=3, ge=1, le=5)
    price: str = Field(default="", max_length=32)
    review_days: int = Field(default=90, ge=7, le=730)


def _decision_out(r) -> dict:
    d = dict(r)
    d["due"] = not d["reviewed_at"] and d["review_on"] <= str(_today())
    return d


@router.get("/decisions")
def list_decisions(company_id: int | None = None, user: dict = Depends(current_user)):
    sql = ("SELECT d.*, c.name AS company_name, c.ticker, c.market FROM decisions d"
           " JOIN companies c ON c.id = d.company_id WHERE d.user_id = ?")
    args: list = [user["id"]]
    if company_id:
        sql, args = sql + " AND d.company_id = ?", args + [company_id]
    with get_conn() as conn:
        return [_decision_out(r) for r in conn.execute(sql + " ORDER BY d.decided_on DESC, d.id DESC", args)]


@router.post("/companies/{company_id}/decisions")
def add_decision(company_id: int, body: DecisionIn, user: dict = Depends(current_user)):
    today = _today()
    with get_conn() as conn:
        _own(conn, "companies", company_id, user["id"])
        cur = conn.execute(
            "INSERT INTO decisions (user_id, company_id, action, reason, emotion, confidence, price,"
            " decided_on, review_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], company_id, body.action, body.reason.strip(), body.emotion.strip(), body.confidence,
             body.price.strip(), str(today), str(today + timedelta(days=body.review_days))))
    return {"id": cur.lastrowid}


@router.delete("/decisions/{decision_id}")
def delete_decision(decision_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        _own(conn, "decisions", decision_id, user["id"])
        conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
    return {"ok": True}


def _since(conn, d: dict) -> dict:
    """决策之后这家公司发生了什么。复盘时完整展示，不受「先判断」模式影响。"""
    items = [dict(r) for r in conn.execute(
        "SELECT dr.report_date, ri.title, ri.summary, ri.direction, ri.score, ri.confidence, ri.thesis_impact,"
        " ri.thesis_broken FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id"
        " WHERE dr.company_id = ? AND dr.report_date >= ? ORDER BY dr.report_date",
        (d["company_id"], d["decided_on"]))]
    net = sum(i["score"] * i["confidence"] for i in items)
    top = sorted(items, key=lambda i: -abs(i["score"]))[:8]
    return {"count": len(items), "net": round(max(-10, min(10, net)), 1),
            "weakened": sum(1 for i in items if i["thesis_impact"] == "削弱" or i["thesis_broken"]),
            "strengthened": sum(1 for i in items if i["thesis_impact"] == "加强"),
            "top": sorted(top, key=lambda i: i["report_date"])}


@router.get("/decisions/due")
def due_decisions(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT d.*, c.name AS company_name, c.ticker, c.market FROM decisions d"
            " JOIN companies c ON c.id = d.company_id"
            " WHERE d.user_id = ? AND d.reviewed_at IS NULL AND d.review_on <= ? ORDER BY d.review_on",
            (user["id"], str(_today())))]
        for r in rows:
            r["since"] = _since(conn, r)
    return rows


@router.post("/decisions/{decision_id}/questions")
def review_questions(decision_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        d = _own(conn, "decisions", decision_id, user["id"])
        c = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (d["company_id"],)).fetchone())
        since = _since(conn, d)
    news = "\n".join(f"- {i['report_date']} [{i['direction']} {i['score']:+.0f}] {i['title']}：{i['summary']}"
                     for i in since["top"]) or "（这段时间没有重要信息）"
    msg = (f"公司：{c['name']}（{MARKET[c['market']]} {c['ticker']}）\n\n"
           f"## 当时的决策（{d['decided_on']}）\n动作：{d['action']}\n理由：{d['reason']}\n"
           f"情绪：{d['emotion'] or '未记录'}\n把握程度：{d['confidence']}/5\n价格：{d['price'] or '未记录'}\n\n"
           f"## 投资逻辑\n{thesis_text(c) or '未写'}\n\n"
           f"## 之后的重要信息（共 {since['count']} 条，净倾向 {since['net']:+.1f}）\n{news}")
    result = _llm_json([{"role": "system", "content": DECISION_REVIEW_SYSTEM}, {"role": "user", "content": msg}],
                       task="review", user_id=user["id"], temperature=0.5, max_tokens=800)
    return {"questions": [str(q) for q in result.get("questions") or []][:3]}


class ReviewIn(BaseModel):
    note: str = Field(default="", max_length=4000)
    snooze_days: int = Field(default=0, ge=0, le=365)


@router.put("/decisions/{decision_id}/review")
def save_review(decision_id: int, body: ReviewIn, user: dict = Depends(current_user)):
    with get_conn() as conn:
        _own(conn, "decisions", decision_id, user["id"])
        if body.snooze_days:
            conn.execute("UPDATE decisions SET review_on = ? WHERE id = ?",
                         (str(_today() + timedelta(days=body.snooze_days)), decision_id))
        else:
            if len(body.note.strip()) < 2:
                raise HTTPException(400, "写几句复盘再保存")
            conn.execute("UPDATE decisions SET review_note = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (body.note.strip(), decision_id))
    return {"ok": True}
