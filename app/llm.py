"""DeepSeek 调用封装（OpenAI 兼容协议）。以后换模型只需改 .env 里的 base_url / model。"""
import json
import logging
import re

import httpx

from .config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def chat(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3,
         max_tokens: int = 4000, timeout: float = 120) -> str:
    if not DEEPSEEK_API_KEY:
        raise LLMError("未配置 DEEPSEEK_API_KEY")
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
    return resp.json()["choices"][0]["message"]["content"] or ""


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
