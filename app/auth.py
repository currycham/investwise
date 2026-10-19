import hashlib
import secrets

from fastapi import Cookie, Depends, HTTPException, Response

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
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(404, "Not Found")  # 不暴露后台的存在
    return user


def login_session(response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
        conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
