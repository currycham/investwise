"""邮箱验证码登录。没有密码；新邮箱第一次登录时就是注册，设置了邀请码时要填邀请码。"""
import hashlib
import hmac
import logging
import re
import secrets

from fastapi import Cookie, Depends, HTTPException, Response

from . import notify
from .config import ADMIN_EMAILS, INACTIVE_DAYS, INVITE_CODE, SITE_URL
from .db import get_conn

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE_MINUTES = 10
CODE_TRIES = 5


def _hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}:{code}".encode()).hexdigest()


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不对")
    return email


def send_code(email: str, invite: str = "") -> None:
    email = normalize_email(email)
    with get_conn() as conn:
        user = conn.execute("SELECT banned FROM users WHERE login_email = ?", (email,)).fetchone()
        if user and user["banned"]:
            raise HTTPException(403, "账号已停用，请联系管理员")
        if not user and INVITE_CODE and invite.strip() != INVITE_CODE and email not in ADMIN_EMAILS:
            raise HTTPException(403, "新用户注册需要邀请码")
        # 同一邮箱 60 秒内只能发一次（留几秒余量，免得前端倒计时刚结束就被拒）
        if conn.execute("SELECT 1 FROM login_codes WHERE email = ? AND sent_at > datetime('now', '-55 seconds')",
                        (email,)).fetchone():
            raise HTTPException(429, "验证码刚发过，一分钟后再试")
        code = f"{secrets.randbelow(10**6):06d}"
        conn.execute("INSERT OR REPLACE INTO login_codes (email, code_hash, expires_at, tries, sent_at)"
                     " VALUES (?, ?, datetime('now', ?), 0, CURRENT_TIMESTAMP)",
                     (email, _hash(email, code), f"+{CODE_MINUTES} minutes"))
    text = f"你的 InvestWise 登录验证码是：{code}\n\n{CODE_MINUTES} 分钟内有效。如果不是你本人操作，忽略这封邮件即可。\n\n{SITE_URL}"
    if not notify.enabled():
        log.warning("SMTP 未配置，验证码只打印到日志：%s %s", email, code)
        return
    try:
        notify.send(email, f"InvestWise 登录验证码 {code}", f"<p>{text}</p>".replace("\n", "<br>"), text)
    except Exception as e:
        log.exception("send code failed")
        raise HTTPException(502, f"验证码发送失败：{e}")


def verify_code(email: str, code: str) -> int:
    """验证通过返回用户 ID；邮箱没注册过就新建账号。"""
    email = normalize_email(email)
    with get_conn() as conn:
        row = conn.execute("SELECT *, expires_at < CURRENT_TIMESTAMP AS expired FROM login_codes WHERE email = ?",
                           (email,)).fetchone()
        if not row or row["expired"]:
            raise HTTPException(400, "验证码已过期，请重新获取")
        if row["tries"] >= CODE_TRIES:
            raise HTTPException(429, "试错太多次，请重新获取验证码")
        wrong = not hmac.compare_digest(row["code_hash"], _hash(email, code.strip()))
        if wrong:  # 计数要先提交：在 with 里抛异常会回滚
            conn.execute("UPDATE login_codes SET tries = tries + 1 WHERE email = ?", (email,))
    if wrong:
        raise HTTPException(400, "验证码不对")
    with get_conn() as conn:
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
        user = conn.execute("SELECT id, banned FROM users WHERE login_email = ?", (email,)).fetchone()
        if user and user["banned"]:
            raise HTTPException(403, "账号已停用，请联系管理员")
        if user:
            return user["id"]
        # 提醒邮箱默认就是登录邮箱，设置里可以改
        return conn.execute("INSERT INTO users (login_email, email) VALUES (?, ?)", (email, email)).lastrowid


def is_admin(user: dict) -> bool:
    return user["role"] == "admin" or (user["login_email"] or "") in ADMIN_EMAILS


def display_name(user: dict) -> str:
    """页面上显示昵称；没有昵称就显示登录邮箱 @ 前面的部分。"""
    return user["username"] or (user["login_email"] or "").split("@")[0] or f"用户 {user['id']}"


def user_from_session(session: str | None) -> dict | None:
    if not session:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                           (session,)).fetchone()
    return dict(row) if row and not row["banned"] else None


def current_user(session: str | None = Cookie(default=None)) -> dict:
    user = user_from_session(session)
    if not user:
        raise HTTPException(401, "未登录")
    _touch(user)
    return user


def _touch(user: dict) -> None:
    """记录最近访问（10 分钟内只写一次）。长期没来的用户回来时，回溯其持仓最近一周的信息。"""
    with get_conn() as conn:
        was_inactive = conn.execute(
            "SELECT COALESCE(last_seen_at, last_login_at, created_at) < datetime('now', ?) FROM users WHERE id = ?",
            (f"-{INACTIVE_DAYS} day", user["id"])).fetchone()[0]
        # 条件更新是原子的：页面同时发出多个请求时，只有一个会更新成功并触发回溯
        updated = conn.execute(
            "UPDATE users SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?"
            " AND (last_seen_at IS NULL OR last_seen_at < datetime('now', '-10 minutes'))", (user["id"],)).rowcount
        if not updated:
            return
        company_ids = [r[0] for r in conn.execute("SELECT id FROM companies WHERE user_id = ?", (user["id"],))]
    if was_inactive and company_ids:
        from . import analyzer

        analyzer.enqueue_backfill(user["id"], company_ids)


def admin_user(user: dict = Depends(current_user)) -> dict:
    if not is_admin(user):
        raise HTTPException(404, "Not Found")  # 不暴露后台的存在
    return user


def login_session(response: Response, user_id: int) -> None:
    _touch({"id": user_id})  # 登录也算访问；要在更新 last_login_at 之前判断是否长期未访问
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
        conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
