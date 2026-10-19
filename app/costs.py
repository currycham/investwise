"""成本记账与全站每日预算。模型调用和博查搜索都记在 llm_usage 表（博查的 task 为 search）。"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import DAILY_BUDGET_YUAN, TIMEZONE
from .db import get_conn

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


def today_start_utc() -> str:
    now = datetime.now(ZoneInfo(TIMEZONE))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    return start.strftime("%Y-%m-%d %H:%M:%S")


def today_cost() -> float:
    with get_conn() as conn:
        return conn.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_usage WHERE at >= ?",
                            (today_start_utc(),)).fetchone()[0]


def check_budget() -> None:
    if DAILY_BUDGET_YUAN > 0 and today_cost() >= DAILY_BUDGET_YUAN:
        raise BudgetExceeded(f"今日 AI 预算（¥{DAILY_BUDGET_YUAN:g}）已用完，明天零点恢复")


def record(task: str, user_id: int | None, cost: float, prompt: int = 0, cached: int = 0,
           completion: int = 0) -> None:
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO llm_usage (task, user_id, prompt_tokens, cached_tokens, completion_tokens, cost)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (task, user_id, prompt, cached, completion, cost))
    except Exception:
        log.exception("record usage failed")  # 记账失败不影响主流程
