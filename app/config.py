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

INVITE_CODE = os.getenv("INVITE_CODE", "change-me")
DAILY_RUN_TIME = os.getenv("DAILY_RUN_TIME", "07:30")
TIMEZONE = "Asia/Shanghai"

DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "data" / "investwise.db"))

DEFAULT_ALERT_THRESHOLD = 6      # |score| >= 阈值 触发预警
ALERT_MIN_CONFIDENCE = 0.6       # 置信度低于此值不预警（逻辑被证伪除外）
NEWS_LOOKBACK_HOURS = 36         # 每日检索回看的时间窗口
BACKFILL_DAYS = 7                # 新增/更换持仓后立即回溯的天数
