"""BYOK LLM 客户端：OpenAI 兼容接口，配置来自 settings 表。

设计：
- 未配置时 configured()=False，上层功能显式降级
- chat_json：低温 + 重试 + 容忍 ```json 围栏与尾随噪声（沿 boss-helper parseGptJson 思路）
- 客户端可注入，便于单测 mock
"""
import json
import re
import time
from typing import Optional


class LLMError(Exception):
    pass


class LLMRateLimitError(LLMError):
    """保留上游 429 语义，供页级任务队列动态降并发和退避。"""

    def __init__(self, message: str, retry_after: float = None):
        super().__init__(message)
        self.status_code = 429
        self.retry_after = retry_after


class LLMCancelledError(LLMError):
    """用户切换简历或主动取消任务。"""


def _retry_after(error) -> Optional[float]:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _wrap_error(error: Exception, prefix: str = "LLM 调用失败") -> LLMError:
    message = str(error).strip() or error.__class__.__name__
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    lowered = message.lower()
    if status == 429 or "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        return LLMRateLimitError(f"{prefix}：{message}", _retry_after(error))
    return LLMError(f"{prefix}：{message}")


def _cancelled(cancelled) -> bool:
    return bool(cancelled and cancelled())


def configured() -> bool:
    from .db import get_setting
    return bool(get_setting("llm_base_url") and get_setting("llm_api_key")
                and get_setting("llm_model"))


def make_client():
    """构造 OpenAI 兼容客户端；未配置时抛 LLMError。"""
    from openai import OpenAI
    from .db import get_setting
    if not configured():
        raise LLMError("LLM 未配置：请在「设置」页填写 Base URL / API Key / 模型")
    return OpenAI(base_url=get_setting("llm_base_url"),
                  api_key=get_setting("llm_api_key"), timeout=120)


def chat(messages, client=None, temperature=0.3, max_tokens=2000,
         on_delta=None, cancelled=None) -> str:
    """普通对话调用；传入 on_delta 时使用流式响应并逐片通知任务层。"""
    from .db import get_setting
    try:
        if _cancelled(cancelled):
            raise LLMCancelledError("生成任务已取消")
        cli = client or make_client()
        try:
            model = get_setting("llm_model")
        except Exception:
            # 单测或探针注入客户端时不强依赖已初始化的设置表。
            if client is None:
                raise
            model = ""
        kwargs = {
            "model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
        }
        if on_delta is None:
            resp = cli.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        stream = cli.chat.completions.create(**kwargs, stream=True)
        parts = []
        try:
            for chunk in stream:
                if _cancelled(cancelled):
                    raise LLMCancelledError("生成任务已取消")
                choices = getattr(chunk, "choices", None) or []
                delta = getattr(choices[0], "delta", None) if choices else None
                text = getattr(delta, "content", None) or ""
                if text:
                    parts.append(text)
                    on_delta(text)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        return "".join(parts)
    except LLMError:
        raise
    except Exception as e:
        raise _wrap_error(e) from e


def test_connection(base_url: str, api_key: str, model: str, client=None) -> dict:
    """用页面当前填写的配置发起最小对话，不读取或写入已保存设置。"""
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    missing = [name for name, value in (("Base URL", base_url), ("API Key", api_key),
                                        ("模型", model)) if not value]
    if missing:
        raise LLMError("请先填写：" + "、".join(missing))

    started = time.perf_counter()
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=20)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "请只回复 OK"}],
            temperature=0,
            max_tokens=8,
        )
        reply = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        raise _wrap_error(e, "LLM 连接失败") from e
    return {"ok": True, "model": model, "reply": reply[:80],
            "latency_ms": round((time.perf_counter() - started) * 1000)}


def extract_json(text: str):
    """从模型输出中提取 JSON 对象：容忍围栏/前后噪声/尾随逗号。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise LLMError("输出中没有 JSON 对象")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # 尾随逗号修复一次
                    fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
                    return json.loads(fixed)
    raise LLMError("JSON 对象未闭合")


def chat_json(messages, client=None, retries=2, on_delta=None, cancelled=None) -> dict:
    """结构化输出：低温 + 失败重试（追加纠错提示）。"""
    cli = client or make_client()
    msgs = list(messages)
    last_err = None
    for _ in range(retries + 1):
        text = chat(msgs, client=cli, temperature=0.1,
                    on_delta=on_delta, cancelled=cancelled)
        try:
            return extract_json(text)
        except (LLMError, json.JSONDecodeError) as e:
            last_err = e
            msgs = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                 f"上一次输出无法解析为 JSON（{e}）。请只输出一个合法 JSON 对象，"
                 f"不要任何解释文字或代码围栏。"}]
    raise LLMError(f"JSON 输出重试耗尽: {last_err}")
