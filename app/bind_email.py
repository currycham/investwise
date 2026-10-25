"""给以前用用户名登录的老账号绑定登录邮箱：python -m app.bind_email <用户ID> <邮箱>

绑定后用这个邮箱收验证码登录，进的就是原来的账号。提醒邮箱没填过的，一并设成这个邮箱。
"""
import sys

from .auth import normalize_email
from .db import get_conn, init_db


def main() -> None:
    if len(sys.argv) != 3 or not sys.argv[1].isdigit():
        sys.exit(__doc__.strip().splitlines()[0])
    user_id = int(sys.argv[1])
    try:
        email = normalize_email(sys.argv[2])
    except Exception:
        sys.exit("邮箱格式不对")
    init_db()
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            sys.exit(f"没有 {user_id} 号用户")
        other = conn.execute("SELECT id FROM users WHERE login_email = ? AND id != ?", (email, user_id)).fetchone()
        if other:
            sys.exit(f"这个邮箱已经绑在 {other['id']} 号用户上")
        conn.execute("UPDATE users SET login_email = ?, email = CASE WHEN email = '' THEN ? ELSE email END WHERE id = ?",
                     (email, email, user_id))
    print(f"已绑定：{user_id} 号用户「{user['username']}」，之前的登录邮箱 {user['login_email'] or '无'}")


if __name__ == "__main__":
    main()
