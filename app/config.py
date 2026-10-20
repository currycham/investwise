import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

BOCHA_API_KEY = os.getenv("BOCHA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "InvestWise admin@example.com")

# 注册邀请码；留空表示开放注册（内测期间）
INVITE_CODE = os.getenv("INVITE_CODE", "").strip()
# 这些用户名注册或启动时自动成为管理员（逗号分隔）；其他管理员可在后台设置
ADMIN_USERNAMES = [u.strip() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip()]

# DeepSeek 单价（元 / 百万 tokens），只用于后台成本估算，以控制台账单为准
DEEPSEEK_PRICE_INPUT = float(os.getenv("DEEPSEEK_PRICE_INPUT", "2"))
DEEPSEEK_PRICE_CACHED = float(os.getenv("DEEPSEEK_PRICE_CACHED", "0.2"))
DEEPSEEK_PRICE_OUTPUT = float(os.getenv("DEEPSEEK_PRICE_OUTPUT", "3"))
BOCHA_PRICE_PER_CALL = float(os.getenv("BOCHA_PRICE_PER_CALL", "0.036"))  # 元/次

# ---------- 成本控制 ----------
# 博查用法：fallback = 港股总是用，A股/美股只在免费数据源当天没抓到信息时补搜；always = 每家公司都搜
BOCHA_MODE = os.getenv("BOCHA_MODE", "fallback")
# 超过这么多天没访问网站的用户，暂停每日检索；再次访问时自动回溯
INACTIVE_DAYS = int(os.getenv("INACTIVE_DAYS", "14"))
# 全站每日成本上限（元，含模型和博查，北京时间零点重置）；用完后当天暂停检索、概览和提问
DAILY_BUDGET_YUAN = float(os.getenv("DAILY_BUDGET_YUAN", "10"))
DAILY_RUN_TIME = os.getenv("DAILY_RUN_TIME", "07:30")
TIMEZONE = "Asia/Shanghai"

# ---------- 预警邮件（不填则不发） ----------
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")  # 默认同 SMTP_USER
SITE_URL = os.getenv("SITE_URL", "http://localhost:8000").rstrip("/")

DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "data" / "investwise.db"))

DEFAULT_ALERT_THRESHOLD = 6      # |score| >= 阈值 触发预警
ALERT_MIN_CONFIDENCE = 0.6       # 置信度低于此值不预警（逻辑被证伪除外）
NEWS_LOOKBACK_HOURS = 36         # 每日检索回看的时间窗口
BACKFILL_DAYS = 7                # 新增/更换持仓后立即回溯的天数
