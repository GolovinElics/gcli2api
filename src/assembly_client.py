import json
import asyncio
from typing import Dict, Any
import itertools

from fastapi import Response

from log import log
from .models import ChatCompletionRequest
from .httpx_client import http_client
from config import (
    get_assembly_endpoint,
    get_assembly_api_keys,
    get_retry_429_enabled,
    get_retry_429_max_retries,
    get_retry_429_interval,
)


def _sanitize_messages(messages) -> list:
    sanitized = []
    for m in messages:
        role = getattr(m, "role", "user")
        content = getattr(m, "content", None)
        if isinstance(content, list):
            parts_text = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    parts_text.append(part["text"])
            content = "\n".join(parts_text) if parts_text else ""
        sanitized.append({"role": role, "content": content})
    return sanitized


_rr_counter = itertools.count()

def _next_key_index(n: int) -> int:
    i = next(_rr_counter)
    return i % n

def _mask_key(key: str) -> str:
    if not key:
        return ""
    k = str(key)
    if len(k) <= 8:
        return k[:2] + "***"
    return k[:4] + "..." + k[-4:]

async def send_assembly_request(
    openai_request: ChatCompletionRequest,
    is_streaming: bool = False,
):
    """
    调用 AssemblyAI LLM Gateway，支持与 OpenAI 兼容的请求格式。
    目前实现非流式调用；如需流式，建议结合假流式。
    """
    # 构造请求体
    payload: Dict[str, Any] = {
        "model": openai_request.model,
        "messages": _sanitize_messages(openai_request.messages),
    }
    # 透传常用参数
    for key in [
        "temperature",
        "top_p",
        "max_tokens",
        "stop",
        "frequency_penalty",
        "presence_penalty",
        "n",
        "seed",
        "response_format",
        "tools",
        "tool_choice",
    ]:
        val = getattr(openai_request, key, None)
        if val is not None:
            payload[key] = val

    endpoint = await get_assembly_endpoint()
    keys = await get_assembly_api_keys()
    if not keys:
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"error": {"message": "No AssemblyAI API keys configured", "type": "config_error"}}, status_code=500)

    max_retries = await get_retry_429_max_retries()
    retry_enabled = await get_retry_429_enabled()
    retry_interval = await get_retry_429_interval()

    post_data = json.dumps(payload)

    for attempt in range(max_retries + 1):
        try:
            async with http_client.get_client(timeout=None) as client:
                idx = _next_key_index(len(keys))
                api_key = keys[idx]
                headers = {"Authorization": api_key, "Content-Type": "application/json"}
                log.info(f"REQ model={openai_request.model} key={_mask_key(api_key)} attempt={attempt+1}")
                resp = await client.post(endpoint, content=post_data, headers=headers)
                if resp.status_code == 429 and retry_enabled and attempt < max_retries:
                    log.warning(f"[RETRY] 429 from AssemblyAI, retrying ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_interval)
                    continue
                status_cat = "OK" if 200 <= resp.status_code < 400 else f"FAIL({resp.status_code})"
                log.info(f"RES model={openai_request.model} key={_mask_key(api_key)} status={status_cat}")
                return resp
        except Exception as e:
            if attempt < max_retries:
                log.warning(f"[RETRY] AssemblyAI request failed, retrying ({attempt + 1}/{max_retries}): {e}")
                await asyncio.sleep(retry_interval)
                continue
            else:
                log.error(f"AssemblyAI request failed: {e}")
                from fastapi.responses import JSONResponse
                return JSONResponse(content={"error": {"message": f"Request failed: {str(e)}", "type": "api_error"}}, status_code=500)