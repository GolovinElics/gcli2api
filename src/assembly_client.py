import json
import asyncio
from typing import Dict, Any
import itertools

from fastapi import Response

from log import log
from .models import ChatCompletionRequest
from .httpx_client import http_client
from .usage_stats import record_successful_call
from .storage_adapter import get_storage_adapter
from .rate_limiter import get_rate_limiter
from .key_selector import get_key_selector
from config import (
    get_assembly_endpoint,
    get_assembly_api_keys,
    get_retry_429_enabled,
    get_retry_429_max_retries,
    get_retry_429_interval,
)


def _sanitize_messages(messages) -> list:
    """
    清理和标准化消息格式，转换为 OpenAI 协议格式
    
    AssemblyAI LLM Gateway 支持完整的 OpenAI 协议，包括：
    1. 所有角色类型（user、assistant、system、tool）
    2. tool_calls 字段
    3. tool_call_id 字段
    
    处理：
    1. 多模态内容（提取文本）
    2. 保留所有角色类型
    3. 保留 tool_calls 和 tool_call_id
    4. 空 content + tool_calls（保留，这是合法的）
    """
    sanitized = []
    for m in messages:
        role = getattr(m, "role", "user")
        content = getattr(m, "content", None)
        
        # 处理多模态内容
        if isinstance(content, list):
            parts_text = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    parts_text.append(part["text"])
            content = "\n".join(parts_text) if parts_text else ""
        
        # 确保 content 是字符串或 None（对于有 tool_calls 的消息）
        if content is None:
            content = ""
        
        # 构建消息 - 保留所有角色类型
        message = {"role": role, "content": content}
        
        # 保留 tool_calls（如果存在）
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            # 确保 tool_calls 格式正确
            if isinstance(tool_calls, list):
                formatted_calls = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        formatted_calls.append(tc)
                    elif hasattr(tc, "model_dump"):
                        formatted_calls.append(tc.model_dump())
                    elif hasattr(tc, "dict"):
                        formatted_calls.append(tc.dict())
                message["tool_calls"] = formatted_calls
            else:
                message["tool_calls"] = tool_calls
        
        # 保留 tool_call_id（对于 tool 角色的消息）
        tool_call_id = getattr(m, "tool_call_id", None)
        if tool_call_id:
            message["tool_call_id"] = tool_call_id
        
        sanitized.append(message)
    
    return sanitized


_rr_counter = itertools.count()
_failed_keys = {}  # 记录失败的 Key 和失败时间
_rate_limit_info = {}  # 记录每个 Key 的速率限制信息
_rate_limit_loaded = False  # 标记是否已加载速率限制信息
_load_lock = asyncio.Lock()  # 加载锁，防止并发加载

async def _load_rate_limit_info():
    """从存储中加载速率限制信息"""
    global _rate_limit_info, _rate_limit_loaded
    
    # 使用锁防止并发加载
    async with _load_lock:
        if _rate_limit_loaded:
            return
        
        try:
            adapter = await get_storage_adapter()
            data = await adapter.get_config("rate_limit_info")
            if data and isinstance(data, dict):
                # 转换字符串key为整数key
                _rate_limit_info = {}
                for k, v in data.items():
                    try:
                        idx = int(k)
                        _rate_limit_info[idx] = v
                    except (ValueError, TypeError):
                        log.warning(f"Invalid rate limit key: {k}, skipping")
                log.info(f"Loaded rate limit info for {len(_rate_limit_info)} keys from storage")
            else:
                log.info("No existing rate limit info found in storage")
            _rate_limit_loaded = True
        except Exception as e:
            log.error(f"Failed to load rate limit info from storage: {e}")
            # 即使失败也标记为已加载，使用内存数据继续运行
            _rate_limit_loaded = True

async def _save_rate_limit_info():
    """保存速率限制信息到存储"""
    try:
        adapter = await get_storage_adapter()
        # 转换整数key为字符串key以便JSON序列化
        data_to_save = {str(k): v for k, v in _rate_limit_info.items()}
        await adapter.set_config("rate_limit_info", data_to_save)
        log.debug(f"Saved rate limit info for {len(_rate_limit_info)} keys")
    except Exception as e:
        log.error(f"Failed to save rate limit info: {e}")

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


async def _next_key_index_async(n: int) -> int:
    """
    异步智能 Key 选择（集成速率限制和轮换策略）：
    1. 使用 KeySelector 进行智能选择
    2. 考虑速率限制状态
    3. 考虑轮换策略
    4. 失败记录会在 60 秒后自动清除
    """
    import time
    from .models_key import KeyInfo, KeyStatus
    
    current_time = time.time()
    
    # 清理过期的失败记录（60秒后清除）
    expired_keys = [k for k, t in _failed_keys.items() if current_time - t > 60]
    for k in expired_keys:
        del _failed_keys[k]
    
    # 获取速率限制管理器和密钥选择器
    try:
        rate_limiter = await get_rate_limiter()
        key_selector = get_key_selector()
    except Exception as e:
        log.warning(f"Failed to get rate limiter or key selector, falling back to sync selection: {e}")
        return _next_key_index(n)
    
    # 同步失败记录到 KeySelector
    for idx, fail_time in _failed_keys.items():
        if idx not in key_selector.get_failed_keys():
            await key_selector.mark_key_failed(idx, "sync from assembly_client")
    
    # 构建 KeyInfo 列表
    keys = []
    for i in range(n):
        # 检查速率限制状态
        is_exhausted = await rate_limiter.is_key_exhausted(i)
        status = KeyStatus.EXHAUSTED if is_exhausted else KeyStatus.ACTIVE
        
        keys.append(KeyInfo(
            index=i,
            key=f"key_{i}",  # 占位符
            enabled=True,
            status=status
        ))
    
    # 使用 KeySelector 选择密钥
    selected = await key_selector.select_next_key(keys)
    
    if selected is not None:
        # 检查是否需要轮换
        should_rotate = await key_selector.should_rotate_with_rate_limit(selected.index, rate_limiter)
        if should_rotate:
            log.debug(f"Key {selected.index} triggered rotation, selecting next key")
            # 重新选择下一个密钥
            next_selected = await key_selector.select_next_key(keys)
            if next_selected is not None:
                return next_selected.index
        return selected.index
    
    # 所有 Key 都用尽或失败，尝试找最早重置的
    all_indices = list(range(n))
    next_available = await rate_limiter.get_next_available_key(all_indices)
    if next_available is not None:
        return next_available
    
    # 回退到失败时间最早的
    if _failed_keys:
        oldest_idx = min(_failed_keys.keys(), key=lambda k: _failed_keys[k])
        return oldest_idx
    
    # 最后回退到 Round-Robin
    i = next(_rr_counter)
    return i % n

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

async def _update_rate_limit_info(idx: int, api_key: str, headers: dict):
    """更新速率限制信息 - 使用新的 RateLimiter"""
    import time
    try:
        limit = headers.get('x-ratelimit-limit')
        remaining = headers.get('x-ratelimit-remaining')
        reset = headers.get('x-ratelimit-reset')
        
        if limit is not None or remaining is not None:
            masked_key = _mask_key(api_key)
            
            # 同时更新旧的内存缓存（兼容性）
            if idx not in _rate_limit_info:
                _rate_limit_info[idx] = {
                    "key": masked_key,
                    "full_key": api_key,
                }
            
            info = _rate_limit_info[idx]
            info["last_request_time"] = time.time()
            
            limit_val = 0
            remaining_val = 0
            reset_time = 0
            
            if limit is not None:
                try:
                    limit_val = int(limit)
                    info["limit"] = limit_val
                except (ValueError, TypeError):
                    pass
            
            if remaining is not None:
                try:
                    remaining_val = int(remaining)
                    info["remaining"] = remaining_val
                    info["used"] = info.get("limit", 0) - remaining_val
                except (ValueError, TypeError):
                    pass
            
            if reset is not None:
                try:
                    reset_val = int(reset)
                    if reset_val > 0:
                        reset_time = int(time.time() + reset_val)
                        info["reset_time"] = reset_time
                    else:
                        reset_time = int(time.time() + 60)
                        info["reset_time"] = reset_time
                except (ValueError, TypeError):
                    pass
            
            log.debug(f"Updated rate limit info for key {masked_key}: limit={limit_val}, remaining={remaining_val}, reset_in={reset}s")
            
            # 使用新的 RateLimiter 更新
            try:
                rate_limiter = await get_rate_limiter()
                await rate_limiter.update_rate_limit(idx, limit_val, remaining_val, reset_time)
            except Exception as e:
                log.warning(f"Failed to update RateLimiter: {e}")
            
            # 异步保存到存储（兼容旧系统）
            asyncio.create_task(_save_rate_limit_info())
    except Exception as e:
        log.error(f"Failed to update rate limit info: {e}")

async def initialize_rate_limit_system():
    """初始化速率限制系统，在应用启动时调用"""
    log.info("Initializing rate limit system...")
    await _load_rate_limit_info()
    log.info("Rate limit system initialized")

async def get_rate_limit_info() -> dict:
    """获取所有key的速率限制信息"""
    import time
    
    # 确保数据已加载
    await _load_rate_limit_info()
    
    current_time = time.time()
    result = {}
    
    for idx, info in _rate_limit_info.items():
        key_info = {
            "key": info.get("key", "unknown"),
            "limit": info.get("limit", 0),
            "remaining": info.get("remaining", 0),
            "used": info.get("used", 0),
            "last_request_time": info.get("last_request_time", 0),
        }
        
        # 计算重置剩余时间
        reset_time = info.get("reset_time", 0)
        if reset_time > current_time:
            key_info["reset_in_seconds"] = int(reset_time - current_time)
        else:
            key_info["reset_in_seconds"] = 0
            # 如果已经过了重置时间，重置计数器
            if info.get("limit", 0) > 0:
                info["remaining"] = info["limit"]
                info["used"] = 0
                key_info["remaining"] = info["limit"]
                key_info["used"] = 0
        
        result[idx] = key_info
    
    return result

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
    sanitized_messages = _sanitize_messages(openai_request.messages)
    
    # 详细日志：显示消息结构
    log.debug(f"Message structure before sending to AssemblyAI:")
    for i, msg in enumerate(sanitized_messages):
        role = msg.get("role", "unknown")
        content_preview = str(msg.get("content", ""))[:100]
        has_tool_calls = "tool_calls" in msg
        has_tool_call_id = "tool_call_id" in msg
        log.debug(f"  [{i}] role={role}, content={content_preview}..., tool_calls={has_tool_calls}, tool_call_id={has_tool_call_id}")
    
    payload: Dict[str, Any] = {
        "model": openai_request.model,
        "messages": sanitized_messages,
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
                # 使用异步密钥选择（集成速率限制检查）
                idx = await _next_key_index_async(len(keys))
                api_key = keys[idx]
                headers = {"Authorization": api_key, "Content-Type": "application/json"}
                
                # INFO 级别：简要日志
                log.info(f"REQ model={openai_request.model} key={_mask_key(api_key)} attempt={attempt+1}/{max_retries+1} key_idx={idx}")
                
                # DEBUG 级别：详细请求信息
                log.debug(f"REQ Details - Endpoint: {endpoint}")
                log.debug(f"REQ Details - Headers: {{'Authorization': '{_mask_key(api_key)}', 'Content-Type': 'application/json'}}")
                log.debug(f"REQ Details - Payload: {post_data[:500]}{'...' if len(post_data) > 500 else ''}")
                
                resp = await client.post(endpoint, content=post_data, headers=headers)
                
                # 更新速率限制信息
                await _update_rate_limit_info(idx, api_key, resp.headers)
                
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
                            # 检查是否是请求过大的错误（不标记 key 失败）
                            elif any(keyword in error_msg for keyword in ["too large", "too long", "token", "context", "length", "processing error"]):
                                log.warning(f"Request may be too large or invalid: {error_msg}")
                                # 这是请求问题而非 key 问题，不标记失败
                                # 但如果是最后一次尝试，就不重试了
                                if attempt < max_retries - 1:
                                    should_retry = False  # 不重试，直接返回错误让上层处理
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