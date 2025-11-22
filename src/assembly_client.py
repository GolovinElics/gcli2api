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
_failed_keys = {}  # 记录失败的 Key 和失败时间

def _next_key_index(n: int) -> int:
    """
    智能 Key 选择：
    1. 优先选择未失败的 Key
    2. 如果所有 Key 都失败过，选择失败时间最早的
    3. 失败记录会在 60 秒后自动清除
    """
    import time
    current_time = time.time()
    
    # 清理过期的失败记录（60秒后清除）
    expired_keys = [k for k, t in _failed_keys.items() if current_time - t > 60]
    for k in expired_keys:
        del _failed_keys[k]
    
    # 如果没有失败记录，使用 Round-Robin
    if not _failed_keys:
        i = next(_rr_counter)
        return i % n
    
    # 找到未失败的 Key
    available_indices = [i for i in range(n) if i not in _failed_keys]
    if available_indices:
        # 从可用的 Key 中轮询
        i = next(_rr_counter)
        return available_indices[i % len(available_indices)]
    
    # 所有 Key 都失败过，选择失败时间最早的
    oldest_idx = min(_failed_keys.keys(), key=lambda k: _failed_keys[k])
    return oldest_idx

def _mark_key_failed(idx: int):
    """标记 Key 失败"""
    import time
    _failed_keys[idx] = time.time()
    log.debug(f"Marked key index {idx} as failed, total failed keys: {len(_failed_keys)}")

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
                
                # INFO 级别：简要日志
                log.info(f"REQ model={openai_request.model} key={_mask_key(api_key)} attempt={attempt+1}/{max_retries+1} key_idx={idx}")
                
                # DEBUG 级别：详细请求信息
                log.debug(f"REQ Details - Endpoint: {endpoint}")
                log.debug(f"REQ Details - Headers: {{'Authorization': '{_mask_key(api_key)}', 'Content-Type': 'application/json'}}")
                log.debug(f"REQ Details - Payload: {post_data[:500]}{'...' if len(post_data) > 500 else ''}")
                
                resp = await client.post(endpoint, content=post_data, headers=headers)
                
                # 检查是否需要重试（429 或 400 速率限制错误）
                should_retry = False
                retry_reason = ""
                
                if resp.status_code == 429:
                    should_retry = True
                    retry_reason = "429 Too Many Requests"
                    _mark_key_failed(idx)
                elif resp.status_code == 400:
                    # 优先检查响应头中的速率限制信息
                    ratelimit_remaining = resp.headers.get('x-ratelimit-remaining')
                    ratelimit_limit = resp.headers.get('x-ratelimit-limit')
                    
                    # 如果响应头显示速率限制耗尽，则认为是速率限制错误
                    if ratelimit_remaining is not None and ratelimit_limit is not None:
                        try:
                            remaining = int(ratelimit_remaining)
                            limit = int(ratelimit_limit)
                            # 剩余次数很少（<= 10%）或为0时，认为是速率限制
                            if remaining == 0 or (limit > 0 and remaining <= limit * 0.1):
                                should_retry = True
                                retry_reason = f"400 Rate Limit Exhausted (remaining: {remaining}/{limit})"
                                _mark_key_failed(idx)
                                log.warning(f"Key {_mask_key(api_key)} rate limit exhausted: {remaining}/{limit} remaining")
                        except (ValueError, TypeError):
                            pass
                    
                    # 如果响应头没有速率限制信息，检查错误消息
                    if not should_retry:
                        try:
                            error_body = resp.json() if hasattr(resp, 'json') else json.loads(resp.text)
                            error_msg = error_body.get("message", "").lower()
                            # 常见的速率限制错误消息
                            if any(keyword in error_msg for keyword in ["rate", "limit", "quota", "too many"]):
                                should_retry = True
                                retry_reason = f"400 Rate Limit: {error_body.get('message', 'Unknown')}"
                                _mark_key_failed(idx)
                        except Exception:
                            pass
                
                if should_retry and retry_enabled and attempt < max_retries:
                    log.warning(f"[RETRY] {retry_reason}, switching to next key ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_interval)
                    continue  # 下次循环会自动选择新的 Key
                
                status_cat = "OK" if 200 <= resp.status_code < 400 else f"FAIL({resp.status_code})"
                
                # INFO 级别：简要响应
                log.info(f"RES model={openai_request.model} key={_mask_key(api_key)} status={status_cat}")
                
                # DEBUG 级别：详细响应信息
                log.debug(f"RES Details - Status Code: {resp.status_code}")
                log.debug(f"RES Details - Headers: {dict(resp.headers)}")
                try:
                    response_text = resp.text if hasattr(resp, 'text') else str(resp.content)
                    log.debug(f"RES Details - Body: {response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
                except Exception as e:
                    log.debug(f"RES Details - Body: [Unable to decode: {e}]")
                
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