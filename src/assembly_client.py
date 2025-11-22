import json
import asyncio
from typing import Dict, Any
import itertools

from fastapi import Response

from log import log
from .models import ChatCompletionRequest
from .httpx_client import http_client
from .usage_stats import record_successful_call
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

async def fetch_assembly_models() -> Dict[str, Any]:
    """
    查询 AssemblyAI LLM Gateway 的可用模型列表。
    尝试使用第一个可用的 API Key 调用 /v1/models。
    返回 {"models": ["id1","id2",...]} 格式。
    """
    endpoint = await get_assembly_endpoint()
    # 将 chat/completions 替换为 models 列表端点
    models_url = endpoint.replace("/chat/completions", "/models")
    keys = await get_assembly_api_keys()
    if not keys:
        return {"models": []}
    api_key = keys[0]
    try:
        async with http_client.get_client(timeout=30.0) as client:
            headers = {"Authorization": api_key}
            resp = await client.get(models_url, headers=headers)
            if 200 <= resp.status_code < 400:
                try:
                    data = resp.json()
                except Exception:
                    import json as _json
                    data = _json.loads(resp.text or "{}")
                # 支持多种返回结构
                models = []
                meta: Dict[str, Any] = {}
                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list):
                        for item in data["data"]:
                            mid = item.get("id") if isinstance(item, dict) else str(item)
                            if mid:
                                models.append(mid)
                                # 解析元数据
                                tp = item.get("top_provider", {}) if isinstance(item, dict) else {}
                                meta[mid] = {
                                    "name": item.get("name") or mid,
                                    "description": item.get("description") or "",
                                    "context_length": tp.get("context_length") or item.get("context_length"),
                                    "max_tokens": tp.get("max_completion_tokens"),
                                    "default_parameters": item.get("default_parameters") or {},
                                }
                    elif "models" in data and isinstance(data["models"], list):
                        for item in data["models"]:
                            mid = item.get("id") if isinstance(item, dict) else str(item)
                            if mid:
                                models.append(mid)
                                tp = item.get("top_provider", {}) if isinstance(item, dict) else {}
                                meta[mid] = {
                                    "name": item.get("name") or mid,
                                    "description": item.get("description") or "",
                                    "context_length": tp.get("context_length") or item.get("context_length"),
                                    "max_tokens": tp.get("max_completion_tokens"),
                                    "default_parameters": item.get("default_parameters") or {},
                                }
                elif isinstance(data, list):
                    for item in data:
                        mid = item.get("id") if isinstance(item, dict) else str(item)
                        if mid:
                            models.append(mid)
                            tp = item.get("top_provider", {}) if isinstance(item, dict) else {}
                            meta[mid] = {
                                "name": item.get("name") or mid,
                                "description": item.get("description") or "",
                                "context_length": tp.get("context_length") or item.get("context_length"),
                                "max_tokens": tp.get("max_completion_tokens"),
                                "default_parameters": item.get("default_parameters") or {},
                            }
                return {"models": models, "meta": meta}
            else:
                log.error(f"Fetch models failed: {resp.status_code}")
                return {"models": [], "meta": {}}
    except Exception as e:
        log.error(f"Fetch models error: {e}")
        return {"models": [], "meta": {}}

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
                try:
                    if 200 <= resp.status_code < 400:
                        import hashlib
                        key_id = f"key:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:16]}"
                        await record_successful_call(key_id, openai_request.model, _mask_key(api_key))
                except Exception:
                    pass
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