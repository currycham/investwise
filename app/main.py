import json
import logging
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import admin, analyzer, digest, llm, notify, parser, portfolio, profile, thinking
from .auth import current_user, hash_pw, login_session, user_from_session, verify_pw
from .config import ADMIN_USERNAMES, BASE_DIR, DAILY_RUN_TIME, INVITE_CODE, TIMEZONE
from .db import alert_condition, get_conn, init_db, needs_judgment, thesis_text
from .prompts import SOCRATIC_SYSTEM

logging.basicConfig(level=logging.INFO)
scheduler = BackgroundScheduler(timezone=TIMEZONE)
EMAIL_RE = r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$"


@asynccontextmanager
async def lifespan(_app):
    init_db()
    hour, minute = DAILY_RUN_TIME.split(":")
    scheduler.add_job(analyzer.enqueue, "cron", hour=int(hour), minute=int(minute), id="daily", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="InvestWise", lifespan=lifespan)
app.include_router(admin.router)
app.include_router(thinking.router)
app.include_router(profile.router)
app.include_router(portfolio.router)


# ---------- 认证 ----------

class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    invite_code: str = ""


@app.get("/api/public-config")
def public_config():
    return {"invite_required": bool(INVITE_CODE)}


@app.post("/api/register")
def register(body: Credentials, response: Response):
    if INVITE_CODE and body.invite_code.strip() != INVITE_CODE:
        raise HTTPException(403, "邀请码错误")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone():
            raise HTTPException(409, "用户名已存在")
        role = "admin" if body.username in ADMIN_USERNAMES else "user"
        cur = conn.execute("INSERT INTO users (username, pw_hash, role) VALUES (?, ?, ?)",
                           (body.username, hash_pw(body.password), role))
    login_session(response, cur.lastrowid)
    return {"ok": True}


@app.post("/api/login")
def login(body: Credentials, response: Response):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not row or not verify_pw(body.password, row["pw_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    if row["banned"]:
        raise HTTPException(403, "账号已停用，请联系管理员")
    login_session(response, row["id"])
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
    return {"username": user["username"], "alert_threshold": user["alert_threshold"], "is_admin": user["role"] == "admin",
            "email": user["email"], "email_alerts": bool(user["email_alerts"]), "email_enabled": notify.enabled(),
            "judge_first": user["judge_first_at"] is not None}


class Settings(BaseModel):
    alert_threshold: int = Field(ge=1, le=10)
    email: str = Field(default="", max_length=200, pattern=EMAIL_RE)
    email_alerts: bool = True
    judge_first: bool = False


@app.put("/api/settings")
def update_settings(body: Settings, user: dict = Depends(current_user)):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET alert_threshold = ?, email = ?, email_alerts = ?,"
            " judge_first_at = CASE WHEN ? THEN COALESCE(judge_first_at, CURRENT_TIMESTAMP) END WHERE id = ?",
            (body.alert_threshold, body.email.strip(), int(body.email_alerts), int(body.judge_first), user["id"]))
    return {"ok": True}


class TestEmail(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=EMAIL_RE)


_last_test_email: dict[int, float] = {}


@app.post("/api/settings/test-email")
def test_email(body: TestEmail, user: dict = Depends(current_user)):
    if not notify.enabled():
        raise HTTPException(400, "站点还没配置发信邮箱（SMTP），请联系管理员")
    if time.time() - _last_test_email.get(user["id"], 0) < 60:
        raise HTTPException(429, "一分钟内只能发一封测试邮件")
    _last_test_email[user["id"]] = time.time()
    try:
        notify.send_test(body.email.strip())
    except Exception as e:
        raise HTTPException(502, f"发送失败：{e}")
    return {"ok": True}


# ---------- 持仓 ----------

class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    ticker: str = Field(min_length=1, max_length=16)
    market: str | None = None
    thesis: str = Field(default="", max_length=2000)       # 为什么买
    assumptions: str = Field(default="", max_length=2000)  # 关键假设
    wrong_if: str = Field(default="", max_length=2000)     # 什么情况说明我错了
    horizon: str = Field(default="", max_length=32)        # 计划持有
    status: str = Field(default="holding", pattern="^(holding|watching)$")  # 新加入的公司：持仓中 / 观察中

    def thesis_values(self) -> tuple[str, str, str, str]:
        return self.thesis.strip(), self.assumptions.strip(), self.wrong_if.strip(), self.horizon.strip()


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
                      AND {alert_condition()}) AS alerts_7d,
                  (SELECT COUNT(*) FROM report_items ri JOIN daily_reports dr ON dr.id = ri.report_id
                    JOIN users u ON u.id = c.user_id
                    WHERE dr.company_id = c.id AND {needs_judgment()}) AS pending_judgments,
                  (SELECT COUNT(*) FROM decisions d WHERE d.company_id = c.id) AS decisions
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
            "INSERT OR IGNORE INTO companies (user_id, name, ticker, market, thesis, assumptions, wrong_if, horizon,"
            " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], body.name.strip(), ticker, market, *body.thesis_values(), body.status),
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
            conn.execute("UPDATE companies SET name = ?, ticker = ?, market = ?, thesis = ?, assumptions = ?,"
                         " wrong_if = ?, horizon = ? WHERE id = ?",
                         (body.name.strip(), ticker, market, *body.thesis_values(), company_id))
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
    return parser.parse_companies(text, user["id"])


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
                "INSERT OR IGNORE INTO companies (user_id, name, ticker, market, thesis, assumptions, wrong_if, horizon,"
                " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], c.name.strip(), norm[1], norm[0], *c.thesis_values(), c.status),
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
    d["needs_judgment"] = bool(d.get("needs_judgment"))
    if d["needs_judgment"]:  # 先判断，再看 AI：用户判断之前不下发 AI 的结论
        d.update(direction="", score=0, confidence=0, horizon="", frameworks={}, reasoning="", watch_points=[],
                 thesis_impact="", thesis_note="", thesis_broken=0, profile_note="")
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
                f"SELECT ri.*, {alert_condition()} AS is_alert, {needs_judgment()} AS needs_judgment,"
                " j.verdict AS my_verdict FROM report_items ri JOIN users u ON u.id = ?"
                " LEFT JOIN item_judgments j ON j.item_id = ri.id"
                " WHERE ri.report_id = ? ORDER BY ABS(ri.score) * (0.5 + ri.relevance) DESC",  # 按画像相关度调整排序
                (user["id"], rep["id"])).fetchall()
            out.append({"date": rep["report_date"], "day_summary": rep["day_summary"],
                        "items": [_item_out(i) for i in items]})
    return out


@app.get("/api/alerts")
def alerts(days: int = 7, user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT ri.*, 1 AS is_alert, dr.report_date, c.id AS company_id, c.name AS company_name,
                       c.ticker, c.market, {needs_judgment()} AS needs_judgment,
                       (SELECT verdict FROM item_judgments j WHERE j.item_id = ri.id) AS my_verdict
                FROM report_items ri
                JOIN daily_reports dr ON dr.id = ri.report_id
                JOIN companies c ON c.id = dr.company_id
                JOIN users u ON u.id = c.user_id
                WHERE c.user_id = ? AND dr.report_date >= date('now', ?) AND {alert_condition()}
                ORDER BY dr.report_date DESC, ABS(ri.score) DESC""",
            (user["id"], f"-{days} day")).fetchall()
    return [_item_out(r) for r in rows]


@app.get("/api/digest")
def get_digest(user: dict = Depends(current_user)):
    return digest.latest(user["id"])


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
            f"## 用户写下的投资逻辑\n{thesis_text(company) or '（用户还没写，这本身就值得追问）'}\n\n"
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
        reply = llm.chat(messages, temperature=0.7, max_tokens=1200, task="socratic", user_id=user["id"])
    except llm.BudgetError as e:
        raise HTTPException(429, str(e))
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


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.png")


@app.get("/admin")
def admin_page(session: str | None = Cookie(default=None)):
    user = user_from_session(session)
    if not user or user["role"] != "admin":
        return PlainTextResponse("Not Found", status_code=404)
    return FileResponse(STATIC / "admin.html")
