"""DeepSeek 调用封装（OpenAI 兼容协议）。以后换模型只需改 .env 里的 base_url / model。"""
import json
import logging
import re

import httpx

from . import costs
from .config import (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, DEEPSEEK_PRICE_CACHED,
                     DEEPSEEK_PRICE_INPUT, DEEPSEEK_PRICE_OUTPUT)

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class BudgetError(LLMError):
    """全站今日预算用完。"""


def chat(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3,
         max_tokens: int = 4000, timeout: float = 120, task: str = "other", user_id: int | None = None) -> str:
    """task / user_id 只用于记账：后台按环节、按用户统计调用量和成本。"""
    if not DEEPSEEK_API_KEY:
        raise LLMError("未配置 DEEPSEEK_API_KEY")
    try:
        costs.check_budget()
    except costs.BudgetExceeded as e:
        raise BudgetError(str(e)) from e
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"DeepSeek 请求失败: {e}") from e
    data = resp.json()
    _record_usage(task, user_id, data.get("usage") or {})
    return data["choices"][0]["message"]["content"] or ""


def _record_usage(task: str, user_id: int | None, usage: dict) -> None:
    prompt = usage.get("prompt_tokens") or 0
    cached = usage.get("prompt_cache_hit_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    cost = ((prompt - cached) * DEEPSEEK_PRICE_INPUT + cached * DEEPSEEK_PRICE_CACHED
            + completion * DEEPSEEK_PRICE_OUTPUT) / 1e6
    costs.record(task, user_id, cost, prompt, cached, completion)


def chat_json(messages: list[dict], **kwargs) -> dict:
    """偶尔会返回格式有瑕疵的 JSON：先本地修复，修不好再重新生成一次。"""
    text = chat(messages, json_mode=True, **kwargs)
    try:
        return parse_json(text)
    except LLMError as e:
        log.warning("invalid JSON from model, retrying: %s", e)
    kwargs["temperature"] = 0
    return parse_json(chat(messages, json_mode=True, **kwargs))


def parse_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    for candidate in (text, _repair(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise LLMError(f"模型返回的不是合法 JSON: {text[:300]}")


def _repair(text: str) -> str:
    text = re.sub(r",\s*([}\]])", r"\1", text)            # 多余的尾逗号
    text = re.sub(r"(?<=[{,])\s*([A-Za-z_]\w*)\s*:", r'"\1":', text)  # 键没加引号
    return text
