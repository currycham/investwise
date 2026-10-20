"""预警邮件：每次检索结束后，把本次新出现的预警信息发到用户邮箱（每条只发一次）。"""
import json
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

from .config import SITE_URL, SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USER
from .db import alert_condition, get_conn

log = logging.getLogger(__name__)
MARKET = {"A": "A股", "HK": "港股", "US": "美股"}


def enabled() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def send(to: str, subject: str, html: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("InvestWise", SMTP_FROM or SMTP_USER))
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    # 腾讯云等云服务器封了 25 端口，默认走 465（SSL）；587 走 STARTTLS
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


def send_alerts(user_id: int) -> int:
    """发送最近 2 天新入库、还没发过的预警，返回条数。

    用入库时间而不是任务开始时间：任务因预算用完中途停止时，已分析出的预警会在下一次任务里补发。"""
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not enabled() or not user or user["banned"] or not user["email"] or not user["email_alerts"]:
            return 0
        rows = conn.execute(
            f"""SELECT ri.*, dr.report_date, c.name AS company_name, c.ticker, c.market
                FROM report_items ri
                JOIN daily_reports dr ON dr.id = ri.report_id
                JOIN companies c ON c.id = dr.company_id
                JOIN users u ON u.id = c.user_id
                WHERE u.id = ? AND ri.created_at >= datetime('now', '-2 day') AND ri.notified_at IS NULL AND {alert_condition()}
                ORDER BY ri.thesis_broken DESC, ABS(ri.score) DESC""",
            (user_id,)).fetchall()
    if not rows:
        return 0
    names = "、".join(dict.fromkeys(r["company_name"] for r in rows))
    subject = f"InvestWise 预警：{names}（{len(rows)} 条）"
    hide = user["judge_first_at"] is not None  # 先判断，再看 AI：邮件里也不透露 AI 的结论
    send(user["email"], subject, _html(rows, hide), _text(rows, hide))
    with get_conn() as conn:
        conn.executemany("UPDATE report_items SET notified_at = CURRENT_TIMESTAMP WHERE id = ?",
                         [(r["id"],) for r in rows])
    return len(rows)


def send_test(to: str) -> None:
    send(to, "InvestWise 测试邮件",
         f"<p>邮件预警已配置成功。之后检索到达到阈值的信息时，会发到这个邮箱。</p><p><a href='{SITE_URL}'>{SITE_URL}</a></p>",
         f"邮件预警已配置成功。之后检索到达到阈值的信息时，会发到这个邮箱。\n{SITE_URL}")


def _label(r, hide: bool = False) -> str:
    if hide:
        return "待你判断"
    if r["thesis_broken"]:
        return "逻辑被证伪"
    return f"{r['direction']} {r['score']:+.0f}"


def _html(rows, hide: bool = False) -> str:
    parts = []
    for r in rows:
        bull = r["score"] > 0 and not r["thesis_broken"]
        color = "#3c5bd6" if hide else "#c62828" if bull else "#2e7d32"  # 红涨绿跌
        srcs = json.loads(r["sources_json"] or "[]")
        links = "　".join(
            f"<a href='{escape(s['url'])}'>{escape(s['source'] or s['title'][:20])}</a>" if s.get("url")
            else escape(s.get("source") or "") for s in srcs)
        impact = f"<p style='margin:4px 0;color:#555'>对你的投资逻辑：{escape(r['thesis_impact'])}</p>" \
            if r["thesis_impact"] and not hide else ""
        parts.append(f"""
<div style="border-left:4px solid {color};padding:8px 12px;margin:12px 0;background:#fafafa">
  <div style="color:#888;font-size:13px">{escape(r['company_name'])} · {MARKET[r['market']]} {escape(r['ticker'])} · {r['report_date']}</div>
  <div style="font-size:16px;margin:4px 0"><b style="color:{color}">[{escape(_label(r, hide))}]</b> <b>{escape(r['title'])}</b></div>
  <p style="margin:4px 0">{escape(r['summary'])}</p>
  {impact}
  <div style="font-size:13px;color:#888">来源：{links or '—'}</div>
</div>""")
    return f"""<div style="font-family:-apple-system,'PingFang SC',sans-serif;max-width:640px;line-height:1.6">
{''.join(parts)}
{"<p style='color:#555'>你开启了「先判断，再看 AI」：先在网站上给出你的判断，再看 AI 的分析。</p>" if hide else ""}
<p style="margin-top:20px"><a href="{SITE_URL}">打开 InvestWise 查看完整分析 →</a></p>
<p style="color:#aaa;font-size:12px">AI 初步判断，仅供参考，不构成投资建议。可在网站「设置」里关闭邮件预警。</p>
</div>"""


def _text(rows, hide: bool = False) -> str:
    lines = [f"[{_label(r, hide)}] {r['company_name']}（{r['ticker']}）{r['report_date']}\n{r['title']}\n{r['summary']}\n"
             for r in rows]
    return "\n".join(lines) + f"\n查看完整分析：{SITE_URL}\nAI 初步判断，仅供参考，不构成投资建议。"
