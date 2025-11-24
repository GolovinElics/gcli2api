"""
OpenAI Router - Handles OpenAI format API requests
处理OpenAI格式请求的路由模块
"""
import json
import time
import uuid
import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import get_available_models_async, is_fake_streaming_model, is_anti_truncation_model, get_base_model_from_feature_model, get_anti_truncation_max_attempts
from log import log
from .anti_truncation import apply_anti_truncation_to_stream
from .assembly_client import send_assembly_request
from .models import ChatCompletionRequest, ModelList, Model
from .task_manager import create_managed_task
from .openai_transfer import assembly_response_to_openai

# 创建路由器
router = APIRouter()
security = HTTPBearer()

# AssemblyAI 适配不需要 Google 凭证管理器

async def authenticate(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """验证用户密码"""
    from config import get_api_password
    password = await get_api_password()
    token = credentials.credentials
    if token != password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")
    return token

@router.get("/v1/models", response_model=ModelList)
async def list_models():
    """返回OpenAI格式的模型列表"""
    models = await get_available_models_async("openai")
    return ModelList(data=[Model(id=m) for m in models])

@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    token: str = Depends(authenticate)
):
    """处理OpenAI格式的聊天完成请求"""
    
    # 获取原始请求数据
    try:
        raw_data = await request.json()
        log.debug(f"Received chat completion request: {json.dumps(raw_data, ensure_ascii=False)[:500]}...")
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    
    # 创建请求对象
    try:
        request_data = ChatCompletionRequest(**raw_data)
        log.debug(f"Request validated - model: {request_data.model}, messages: {len(request_data.messages)}, stream: {getattr(request_data, 'stream', False)}")
        
        # 详细记录接收到的消息结构
        log.debug(f"Received messages structure:")
        for i, m in enumerate(request_data.messages):
            role = getattr(m, "role", "unknown")
            has_tool_calls = bool(getattr(m, "tool_calls", None))
            has_tool_call_id = bool(getattr(m, "tool_call_id", None))
            content_preview = str(getattr(m, "content", ""))[:50]
            log.debug(f"  [{i}] role={role}, tool_calls={has_tool_calls}, tool_call_id={has_tool_call_id}, content={content_preview}...")
    except Exception as e:
        log.error(f"Request validation failed: {e}")
        raise HTTPException(status_code=400, detail=f"Request validation error: {str(e)}")
    
    # 健康检查
    if (len(request_data.messages) == 1 and 
        getattr(request_data.messages[0], "role", None) == "user" and
        getattr(request_data.messages[0], "content", None) == "Hi"):
        return JSONResponse(content={
            "choices": [{"message": {"role": "assistant", "content": "amb2api正常工作中"}}]
        })
    
    # 限制max_tokens
    if getattr(request_data, "max_tokens", None) is not None and request_data.max_tokens > 65535:
        request_data.max_tokens = 65535
        
    # 覆写 top_k 为 64
    setattr(request_data, "top_k", 64)

    # 过滤空消息（但保留有 tool_calls 的消息和 assistant/tool 消息）
    filtered_messages = []
    for m in request_data.messages:
        content = getattr(m, "content", None)
        tool_calls = getattr(m, "tool_calls", None)
        role = getattr(m, "role", "unknown")
        
        # 如果有 tool_calls，即使 content 为空也保留
        if tool_calls:
            log.debug(f"Keeping message with tool_calls: role={role}, content={'[empty]' if not content else content[:50]+'...'}")
            filtered_messages.append(m)
            continue
        
        # 保留 assistant 和 tool 消息，即使 content 为空
        # 这对于多轮对话很重要
        if role in ["assistant", "tool"]:
            filtered_messages.append(m)
            continue
        
        # 对于其他角色，检查 content 是否有效
        if content:
            if isinstance(content, str) and content.strip():
                filtered_messages.append(m)
            elif isinstance(content, list) and len(content) > 0:
                has_valid_content = False
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text", "").strip():
                            has_valid_content = True
                            break
                        elif part.get("type") == "image_url" and part.get("image_url", {}).get("url"):
                            has_valid_content = True
                            break
                if has_valid_content:
                    filtered_messages.append(m)
    
    request_data.messages = filtered_messages
    
    log.debug(f"After filtering: {len(request_data.messages)} messages")
    for i, m in enumerate(request_data.messages):
        role = getattr(m, "role", "unknown")
        has_tool_calls = bool(getattr(m, "tool_calls", None))
        content_preview = str(getattr(m, "content", ""))[:50]
        log.debug(f"  [{i}] role={role}, has_tool_calls={has_tool_calls}, content={content_preview}...")
    
    # AssemblyAI 支持完整的 OpenAI 协议，不需要重建消息
    
    # 优化消息历史，避免超出 token 限制
    from .message_optimizer import optimize_messages
    try:
        optimized_messages = optimize_messages(request_data.messages)
        request_data.messages = optimized_messages
        log.debug(f"Messages optimized: {len(filtered_messages)} -> {len(optimized_messages)}")
    except Exception as e:
        log.warning(f"Message optimization failed: {e}, using original messages")
    
    # 处理模型名称和功能检测
    model = request_data.model
    use_fake_streaming = is_fake_streaming_model(model)
    use_anti_truncation = is_anti_truncation_model(model)
    
    # AssemblyAI 直接使用传入模型名，无需特征前缀转换
    
    # 处理假流式
    if use_fake_streaming and getattr(request_data, "stream", False):
        request_data.stream = False
        return await fake_stream_response_for_assembly(request_data)
    
    # 处理抗截断 (仅流式传输时有效)
    is_streaming = getattr(request_data, "stream", False)
    if use_anti_truncation and is_streaming:
        log.warning("AssemblyAI 暂不支持原生流式抗截断，将作为普通请求处理")
        request_data.stream = False
        is_streaming = False
    
    # 发送到 AssemblyAI（非流式）
    is_streaming = getattr(request_data, "stream", False)
    if is_streaming:
        # 检查是否启用真实流式
        from config import get_enable_real_streaming
        enable_real_streaming = await get_enable_real_streaming()
        
        if enable_real_streaming:
            log.info("使用真实流式模式（实验性）")
            # 真实流式模式：直接发送流式请求到 AssemblyAI
            # 注意：当前 AssemblyAI 的流式响应可能存在解析问题
            response = await send_assembly_request(request_data, True)
            return await convert_streaming_response(response, model)
        else:
            log.info("使用假流式模式")
            return await fake_stream_response_for_assembly(request_data)
    
    log.info(f"REQ model={model}")
    log.debug(f"Sending request to AssemblyAI - stream: {is_streaming}, messages: {len(request_data.messages)}")
    
    response = await send_assembly_request(request_data, False)
    
    # 如果是流式响应，直接返回
    if is_streaming:
        log.debug(f"Converting to streaming response for model: {model}")
        return await convert_streaming_response(response, model)
    
    # 转换非流式响应（AssemblyAI → OpenAI）
    try:
        try:
            if hasattr(response, 'text') and isinstance(getattr(response, 'text'), str):
                text = response.text
            elif hasattr(response, 'body'):
                body = response.body
                text = body.decode('utf-8', errors='replace') if isinstance(body, bytes) else str(body)
            elif hasattr(response, 'content'):
                content = response.content
                text = content.decode('utf-8', errors='replace') if isinstance(content, bytes) else str(content)
            else:
                text = str(response)
        except Exception as de:
            log.warning(f"Response decode failed: {de}")
            text = str(response)
        parsed = None
        try:
            parsed = json.loads(text.strip())
        except Exception:
            if 'data:' in text:
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                for l in reversed(lines):
                    if not l.startswith('data:'):
                        continue
                    payload = l[5:].strip()
                    if payload == '[DONE]':
                        continue
                    try:
                        parsed = json.loads(payload)
                        break
                    except Exception:
                        pass
            if parsed is None and hasattr(response, 'json'):
                try:
                    parsed = response.json()
                except Exception:
                    parsed = None

        if isinstance(parsed, dict):
            # 检查是否是错误响应
            if 'code' in parsed and parsed.get('code') != 200:
                error_message = parsed.get('message', 'Unknown error')
                log.error(f"AssemblyAI returned error: {parsed.get('code')} - {error_message}")
                raise HTTPException(
                    status_code=parsed.get('code', 500),
                    detail=f"AssemblyAI error: {error_message}"
                )
            
            # AssemblyAI 返回 OpenAI 格式，直接使用或进行微调
            openai_response = assembly_response_to_openai(parsed, model)
        else:
            openai_response = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text.strip()},
                    "finish_reason": "stop"
                }]
            }
        # 如果最终choices为空，构造一个兜底消息避免前端空白
        try:
            if isinstance(openai_response, dict):
                ch = openai_response.get('choices')
                if isinstance(ch, list) and len(ch) == 0:
                    fallback_content = ''
                    if isinstance(parsed, dict):
                        fallback_content = str(parsed.get('output_text') or parsed.get('text') or '')
                    if not fallback_content:
                        fallback_content = text.strip()
                    openai_response['choices'] = [{
                        'index': 0,
                        'message': {'role': 'assistant', 'content': fallback_content},
                        'finish_reason': 'stop'
                    }]
        except Exception:
            pass

        log.info(f"RES model={model} status=OK")
        log.debug(f"RES Details - Converted response: {json.dumps(openai_response, ensure_ascii=False)[:1000]}...")
        return JSONResponse(content=openai_response)
    except Exception as e:
        try:
            sample = (text[:200] + '...') if isinstance(text, str) and len(text) > 200 else text
            log.error(f"RES model={model} status=FAIL conversion_error sample={sample}")
            log.debug(f"RES Details - Conversion error: {str(e)}, Full text: {text[:500]}...")
        except Exception:
            log.error(f"RES model={model} status=FAIL conversion_error")
        raise HTTPException(status_code=500, detail="Response conversion failed")

async def fake_stream_response_for_assembly(openai_request: ChatCompletionRequest) -> StreamingResponse:
    """AssemblyAI 的假流式：周期心跳 + 最终内容块"""
    async def stream_generator():
        try:
            log.debug(f"Starting fake stream for model: {openai_request.model}")
            
            # 发送心跳
            heartbeat = {
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(heartbeat)}\n\n".encode()
            log.debug("Sent initial heartbeat")
            
            # 异步发送实际请求
            async def get_response():
                return await send_assembly_request(openai_request, False)
            
            # 创建请求任务
            response_task = create_managed_task(get_response(), name="openai_fake_stream_request")
            
            try:
                # 每3秒发送一次心跳，直到收到响应
                heartbeat_count = 0
                while not response_task.done():
                    await asyncio.sleep(3.0)
                    if not response_task.done():
                        heartbeat_count += 1
                        yield f"data: {json.dumps(heartbeat)}\n\n".encode()
                        log.debug(f"Sent heartbeat #{heartbeat_count}")
                
                # 获取响应结果
                response = await response_task
                log.debug(f"Received response after {heartbeat_count} heartbeats")
                
            except asyncio.CancelledError:
                # 取消任务并传播取消
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                raise
            except Exception as e:
                # 取消任务并处理其他异常
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                log.error(f"Fake streaming request failed: {e}")
                raise
            
            # 发送实际请求
            # response 已在上面获取
            
            # 处理结果
            if hasattr(response, 'body'):
                body_str = response.body.decode() if isinstance(response.body, bytes) else str(response.body)
            elif hasattr(response, 'content'):
                body_str = response.content.decode() if isinstance(response.content, bytes) else str(response.content)
            else:
                body_str = str(response)
            
            try:
                response_data = json.loads(body_str)
                log.debug(f"Parsed response data: {json.dumps(response_data, ensure_ascii=False)[:500]}...")

                # 从响应中提取内容和工具调用
                content = ""
                reasoning_content = ""
                tool_calls = None
                
                if "choices" in response_data and response_data["choices"]:
                    message = response_data["choices"][0].get("message", {})
                    content = message.get("content", "")
                    tool_calls = message.get("tool_calls")
  
                # 如果没有正常内容但有思维内容，给出警告
                if not content and reasoning_content:
                    log.warning("Fake stream response contains only thinking content")
                    content = "[模型正在思考中，请稍后再试或重新提问]"
                
                log.debug(f"Extracted content length: {len(content)}, tool_calls: {bool(tool_calls)}")
                
                # 如果有内容或工具调用，都需要返回
                if content or tool_calls:
                    # 构建响应块，包括思维内容（如果有）和工具调用
                    delta = {"role": "assistant"}
                    
                    # 添加 content（如果有）
                    if content:
                        delta["content"] = content
                    
                    # 添加 reasoning_content（如果有）
                    if reasoning_content:
                        delta["reasoning_content"] = reasoning_content
                    
                    # 添加 tool_calls（如果有）
                    if tool_calls:
                        # 确保 arguments 是 JSON 字符串格式（OpenAI API 要求）
                        formatted_tool_calls = []
                        for tool_call in tool_calls:
                            formatted_call = tool_call.copy()
                            if "function" in formatted_call:
                                function = formatted_call["function"].copy()
                                # 如果 arguments 是对象，转换为 JSON 字符串
                                if "arguments" in function and isinstance(function["arguments"], dict):
                                    function["arguments"] = json.dumps(function["arguments"], ensure_ascii=False)
                                formatted_call["function"] = function
                            formatted_tool_calls.append(formatted_call)
                        delta["tool_calls"] = formatted_tool_calls

                    # 转换usageMetadata为OpenAI格式
                    usage_raw = response_data.get("usage") or {}
                    usage = {
                        "prompt_tokens": usage_raw.get("input_tokens", 0),
                        "completion_tokens": usage_raw.get("output_tokens", 0),
                        "total_tokens": usage_raw.get("total_tokens", 0)
                    } if usage_raw else None

                    # 构建完整的OpenAI格式的流式响应块
                    content_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "amb2api-streaming",
                        "choices": [{
                            "index": 0,
                            "delta": delta,
                            "finish_reason": "stop"
                        }]
                    }

                    # 只有在有usage数据时才添加usage字段（确保在最后一个chunk中）
                    if usage:
                        content_chunk["usage"] = usage

                    yield f"data: {json.dumps(content_chunk)}\n\n".encode()
                else:
                    log.warning(f"No content found in response: {response_data}")
                    # 如果完全没有内容，提供默认回复
                    error_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "amb2api-streaming",
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": "[响应为空，请重新尝试]"},
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            except json.JSONDecodeError:
                error_chunk = {
                    "id": str(uuid.uuid4()),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                        "model": "amb2api-streaming",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": body_str},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            
            yield "data: [DONE]\n\n".encode()
            
        except Exception as e:
            log.error(f"Fake streaming error: {e}")
            error_chunk = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "gcli2api-streaming",
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": f"Error: {str(e)}"},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield "data: [DONE]\n\n".encode()

    return StreamingResponse(stream_generator(), media_type="text/event-stream")

async def convert_streaming_response(gemini_response, model: str) -> StreamingResponse:
    """转换流式响应为OpenAI格式"""
    response_id = str(uuid.uuid4())
    
    async def openai_stream_generator():
        try:
            # 处理不同类型的响应对象
            if hasattr(gemini_response, 'body_iterator'):
                # FastAPI StreamingResponse
                async for chunk in gemini_response.body_iterator:
                    if not chunk:
                        continue
                    
                    # 处理不同数据类型的startswith问题
                    if isinstance(chunk, bytes):
                        if not chunk.startswith(b'data: '):
                            continue
                        payload = chunk[len(b'data: '):]
                    else:
                        chunk_str = str(chunk)
                        if not chunk_str.startswith('data: '):
                            continue
                        payload = chunk_str[len('data: '):].encode()
                    try:
                        gemini_chunk = json.loads(payload.decode())
                        openai_chunk = gemini_stream_chunk_to_openai(gemini_chunk, model, response_id)
                        yield f"data: {json.dumps(openai_chunk, separators=(',',':'))}\n\n".encode()
                    except json.JSONDecodeError:
                        continue
            else:
                # 其他类型的响应，尝试直接处理
                log.warning(f"Unexpected response type: {type(gemini_response)}")
                error_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Response type error"},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            
            # 发送结束标记
            yield "data: [DONE]\n\n".encode()
            
        except Exception as e:
            log.error(f"Stream conversion error: {e}")
            error_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": f"Stream error: {str(e)}"},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield "data: [DONE]\n\n".encode()

    return StreamingResponse(openai_stream_generator(), media_type="text/event-stream")