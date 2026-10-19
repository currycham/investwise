import hashlib
import secrets

from fastapi import Cookie, Depends, HTTPException, Response

from .config import INACTIVE_DAYS
from .db import get_conn


def hash_pw(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def verify_pw(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_pw(password, salt), stored)


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
    if user["role"] != "admin":
        raise HTTPException(404, "Not Found")  # 不暴露后台的存在
    return user


def login_session(response: Response, user_id: int) -> None:
    _touch({"id": user_id})  # 登录也算访问；要在更新 last_login_at 之前判断是否长期未访问
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
        conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
