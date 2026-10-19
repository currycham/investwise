import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analyzer, llm, parser
from .config import BASE_DIR, DAILY_RUN_TIME, INVITE_CODE, TIMEZONE
from .db import alert_condition, get_conn, init_db
from .prompts import SOCRATIC_SYSTEM

logging.basicConfig(level=logging.INFO)
scheduler = BackgroundScheduler(timezone=TIMEZONE)


@asynccontextmanager
async def lifespan(_app):
    init_db()
    hour, minute = DAILY_RUN_TIME.split(":")
    scheduler.add_job(analyzer.enqueue, "cron", hour=int(hour), minute=int(minute), id="daily", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="InvestWise", lifespan=lifespan)


# ---------- 认证 ----------

def _hash_pw(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def _verify_pw(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(_hash_pw(password, salt), stored)


def current_user(session: str | None = Cookie(default=None)) -> dict:
    if not session:
        raise HTTPException(401, "未登录")
    with get_conn() as conn:
        row = conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                           (session,)).fetchone()
    if not row:
        raise HTTPException(401, "未登录")
    return dict(row)


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    invite_code: str = ""


def _login(response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)


@app.post("/api/register")
def register(body: Credentials, response: Response):
    if body.invite_code != INVITE_CODE:
        raise HTTPException(403, "邀请码错误")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone():
            raise HTTPException(409, "用户名已存在")
        cur = conn.execute("INSERT INTO users (username, pw_hash) VALUES (?, ?)",
                           (body.username, _hash_pw(body.password)))
    _login(response, cur.lastrowid)
    return {"ok": True}


@app.post("/api/login")
def login(body: Credentials, response: Response):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not row or not _verify_pw(body.password, row["pw_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    _login(response, row["id"])
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(default=None)):
    if session:
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (session,))
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "alert_threshold": user["alert_threshold"]}


class Settings(BaseModel):
    alert_threshold: int = Field(ge=1, le=10)


@app.put("/api/settings")
def update_settings(body: Settings, user: dict = Depends(current_user)):
    with get_conn() as conn:
        conn.execute("UPDATE users SET alert_threshold = ? WHERE id = ?", (body.alert_threshold, user["id"]))
    return {"ok": True}


# ---------- 持仓 ----------

class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    ticker: str = Field(min_length=1, max_length=16)
    market: str | None = None
    thesis: str = Field(default="", max_length=2000)


def _own_company(conn, company_id: int, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM companies WHERE id = ? AND user_id = ?", (company_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "公司不存在")
    return dict(row)


def _normalized(body: CompanyIn) -> tuple[str, str]:
    norm = parser.normalize(body.ticker, body.market or None)
    if not norm:
        raise HTTPException(400, f"无法识别代码 {body.ticker}（A股6位数字 / 港股5位数字 / 美股字母代码）")
    return norm


@app.get("/api/companies")
def list_companies(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT c.*,
                  (SELECT MAX(dr.report_date) FROM daily_reports dr WHERE dr.company_id = c.id) AS last_report,
                  (SELECT COUNT(*) FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id
                    JOIN users u ON u.id = c.user_id
                    WHERE dr.company_id = c.id AND dr.report_date >= date('now', '-7 day')
                      AND {alert_condition()}) AS alerts_7d
                FROM companies c WHERE c.user_id = ?
                ORDER BY alerts_7d DESC, last_report DESC, c.created_at""",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/companies")
def add_company(body: CompanyIn, user: dict = Depends(current_user)):
    market, ticker = _normalized(body)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO companies (user_id, name, ticker, market, thesis) VALUES (?, ?, ?, ?, ?)",
            (user["id"], body.name.strip(), ticker, market, body.thesis.strip()),
        )
        if not cur.rowcount:
            raise HTTPException(409, "已在列表中")
    analyzer.enqueue_backfill(user["id"], [cur.lastrowid])
    return {"id": cur.lastrowid}


@app.put("/api/companies/{company_id}")
def update_company(company_id: int, body: CompanyIn, user: dict = Depends(current_user)):
    market, ticker = _normalized(body)
    with get_conn() as conn:
        old = _own_company(conn, company_id, user["id"])
        replaced = (old["market"], old["ticker"]) != (market, ticker)
        try:
            conn.execute("UPDATE companies SET name = ?, ticker = ?, market = ?, thesis = ? WHERE id = ?",
                         (body.name.strip(), ticker, market, body.thesis.strip(), company_id))
        except Exception:
            raise HTTPException(409, "该公司已在列表中")
        if replaced:  # 换成了另一家公司，清空旧公司的历史
            conn.execute("DELETE FROM daily_reports WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM seen_news WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM chat_messages WHERE company_id = ?", (company_id,))
    if replaced:
        analyzer.enqueue_backfill(user["id"], [company_id])
    return {"ok": True}


@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        _own_company(conn, company_id, user["id"])
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return {"ok": True}


@app.post("/api/import/parse")
async def import_parse(text: str = Form(default=""), file: UploadFile | None = File(default=None),
                       user: dict = Depends(current_user)):
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(400, "文件不能超过 5MB")
        try:
            text = parser.file_to_text(file.filename, data) + "\n" + text
        except Exception as e:
            raise HTTPException(400, f"文件解析失败：{e}")
    return parser.parse_companies(text)


class ImportConfirm(BaseModel):
    companies: list[CompanyIn]


@app.post("/api/import/confirm")
def import_confirm(body: ImportConfirm, user: dict = Depends(current_user)):
    added: list[int] = []
    with get_conn() as conn:
        for c in body.companies:
            norm = parser.normalize(c.ticker, c.market or None)
            if not norm:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO companies (user_id, name, ticker, market, thesis) VALUES (?, ?, ?, ?, ?)",
                (user["id"], c.name.strip(), norm[1], norm[0], c.thesis.strip()),
            )
            if cur.rowcount:
                added.append(cur.lastrowid)
    analyzer.enqueue_backfill(user["id"], added)
    return {"added": len(added)}


# ---------- 信息时间线与预警 ----------

def _item_out(r) -> dict:
    d = dict(r)
    d["frameworks"] = json.loads(d.pop("frameworks_json") or "{}")
    d["sources"] = json.loads(d.pop("sources_json") or "[]")
    d["watch_points"] = json.loads(d["watch_points"] or "[]")
    d["is_alert"] = bool(d.get("is_alert"))
    return d


@app.get("/api/companies/{company_id}/timeline")
def timeline(company_id: int, days: int = 90, user: dict = Depends(current_user)):
    with get_conn() as conn:
        _own_company(conn, company_id, user["id"])
        reports = conn.execute(
            "SELECT * FROM daily_reports WHERE company_id = ? AND report_date >= date('now', ?)"
            " ORDER BY report_date DESC", (company_id, f"-{days} day")).fetchall()
        out = []
        for rep in reports:
            items = conn.execute(
                f"SELECT ri.*, {alert_condition('?')} AS is_alert FROM report_items ri"
                " WHERE ri.report_id = ? ORDER BY ABS(ri.score) DESC",
                (user["alert_threshold"], rep["id"])).fetchall()
            out.append({"date": rep["report_date"], "day_summary": rep["day_summary"],
                        "items": [_item_out(i) for i in items]})
    return out


@app.get("/api/alerts")
def alerts(days: int = 7, user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT ri.*, 1 AS is_alert, dr.report_date, c.id AS company_id, c.name AS company_name,
                       c.ticker, c.market
                FROM report_items ri
                JOIN daily_reports dr ON dr.id = ri.report_id
                JOIN companies c ON c.id = dr.company_id
                JOIN users u ON u.id = c.user_id
                WHERE c.user_id = ? AND dr.report_date >= date('now', ?) AND {alert_condition()}
                ORDER BY dr.report_date DESC, ABS(ri.score) DESC""",
            (user["id"], f"-{days} day")).fetchall()
    return [_item_out(r) for r in rows]


@app.post("/api/run")
def trigger_run(user: dict = Depends(current_user)):
    if analyzer.pending_for(user["id"]) or _own_running(user["id"]):
        raise HTTPException(409, "已有检索任务在排队或运行")
    analyzer.enqueue(user["id"], label="手动检索")
    return {"ok": True}


def _own_running(user_id: int) -> bool:
    s = analyzer.status
    return s["running"] and s["user_id"] in (None, user_id)


@app.get("/api/run/status")
def run_status(user: dict = Depends(current_user)):
    s = analyzer.status
    mine = s["user_id"] in (None, user["id"])  # 不向其他用户透露你的任务细节
    return {
        "running": _own_running(user["id"]),
        "busy": s["running"],
        "pending": analyzer.pending_for(user["id"]),
        "label": s["label"] if mine else "",
        "progress": s["progress"] if mine else "",
        "finished_at": s["finished_at"] if mine else None,
        "errors": s["errors"] if mine else [],
    }


# ---------- 苏格拉底式提问助手 ----------

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def _chat_context(conn, company: dict) -> str:
    items = conn.execute(
        "SELECT dr.report_date, ri.title, ri.summary, ri.direction, ri.score, ri.thesis_impact"
        " FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id"
        " WHERE dr.company_id = ? AND dr.report_date >= date('now', '-30 day')"
        " ORDER BY dr.report_date DESC LIMIT 15", (company["id"],)).fetchall()
    news = "\n".join(f"- {i['report_date']} [{i['direction']} {i['score']:+.0f}] {i['title']}：{i['summary']}"
                     for i in items) or "（近30天无重要信息）"
    market = {"A": "A股", "HK": "港股", "US": "美股"}[company["market"]]
    return (f"## 讨论对象\n{company['name']}（{market} {company['ticker']}）\n\n"
            f"## 用户写下的投资逻辑\n{company['thesis'] or '（用户还没写，这本身就值得追问）'}\n\n"
            f"## 最近信息\n{news}")


@app.get("/api/companies/{company_id}/chat")
def chat_history(company_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        _own_company(conn, company_id, user["id"])
        rows = conn.execute("SELECT role, content, created_at FROM chat_messages WHERE company_id = ? ORDER BY id",
                            (company_id,)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/companies/{company_id}/chat")
def chat(company_id: int, body: ChatIn, user: dict = Depends(current_user)):
    with get_conn() as conn:
        company = _own_company(conn, company_id, user["id"])
        context = _chat_context(conn, company)
        history = [dict(r) for r in conn.execute(
            "SELECT role, content FROM chat_messages WHERE company_id = ? ORDER BY id DESC LIMIT 30",
            (company_id,))][::-1]
    messages = [{"role": "system", "content": SOCRATIC_SYSTEM + "\n\n" + context}, *history,
                {"role": "user", "content": body.message}]
    try:
        reply = llm.chat(messages, temperature=0.7, max_tokens=1200)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    with get_conn() as conn:
        conn.executemany("INSERT INTO chat_messages (company_id, role, content) VALUES (?, ?, ?)",
                         [(company_id, "user", body.message), (company_id, "assistant", reply)])
    return {"reply": reply}


@app.delete("/api/companies/{company_id}/chat")
def chat_reset(company_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        _own_company(conn, company_id, user["id"])
        conn.execute("DELETE FROM chat_messages WHERE company_id = ?", (company_id,))
    return {"ok": True}


# ---------- 前端 ----------

STATIC = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
