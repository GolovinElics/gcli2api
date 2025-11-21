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

from config import get_available_models, is_fake_streaming_model, is_anti_truncation_model, get_base_model_from_feature_model, get_anti_truncation_max_attempts
from log import log
from .anti_truncation import apply_anti_truncation_to_stream
from .assembly_client import send_assembly_request
from .models import ChatCompletionRequest, ModelList, Model
from .task_manager import create_managed_task
from .openai_transfer import assembly_response_to_openai, gemini_stream_chunk_to_openai, _convert_usage_metadata

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
    models = get_available_models("openai")
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
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    
    # 创建请求对象
    try:
        request_data = ChatCompletionRequest(**raw_data)
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

    # 过滤空消息
    filtered_messages = []
    for m in request_data.messages:
        content = getattr(m, "content", None)
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
        log.info("使用假流式模式")
        return await fake_stream_response_for_assembly(request_data)
    
    log.info(f"REQ model={model}")
    response = await send_assembly_request(request_data, False)
    
    # 如果是流式响应，直接返回
    if is_streaming:
        return await convert_streaming_response(response, model)
    
    # 转换非流式响应（AssemblyAI → OpenAI）
    try:
        if hasattr(response, 'body'):
            response_data = json.loads(response.body.decode() if isinstance(response.body, bytes) else response.body)
        else:
            response_data = json.loads(response.content.decode() if isinstance(response.content, bytes) else response.content)

        openai_response = assembly_response_to_openai(response_data, model)
        try:
            from .usage_stats import record_successful_call
            await record_successful_call("assemblyai", model)
        except Exception:
            pass
        log.info(f"RES model={model} status=OK")
        return JSONResponse(content=openai_response)
        
    except Exception as e:
        log.error(f"RES model={model} status=FAIL conversion_error")
        raise HTTPException(status_code=500, detail="Response conversion failed")

async def fake_stream_response_for_assembly(openai_request: ChatCompletionRequest) -> StreamingResponse:
    """AssemblyAI 的假流式：周期心跳 + 最终内容块"""
    async def stream_generator():
        try:
            # 发送心跳
            heartbeat = {
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(heartbeat)}\n\n".encode()
            
            # 异步发送实际请求
            async def get_response():
                return await send_assembly_request(openai_request, False)
            
            # 创建请求任务
            response_task = create_managed_task(get_response(), name="openai_fake_stream_request")
            
            try:
                # 每3秒发送一次心跳，直到收到响应
                while not response_task.done():
                    await asyncio.sleep(3.0)
                    if not response_task.done():
                        yield f"data: {json.dumps(heartbeat)}\n\n".encode()
                
                # 获取响应结果
                response = await response_task
                
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

                # 从Gemini响应中提取内容，使用思维链分离逻辑
                content = ""
                reasoning_content = ""
                if "choices" in response_data and response_data["choices"]:
                    content = response_data["choices"][0].get("message", {}).get("content", "")
  
                # 如果没有正常内容但有思维内容，给出警告
                if not content and reasoning_content:
                    log.warning("Fake stream response contains only thinking content")
                    content = "[模型正在思考中，请稍后再试或重新提问]"
                
                if content:
                    # 构建响应块，包括思维内容（如果有）
                    delta = {"role": "assistant", "content": content}
                    if reasoning_content:
                        delta["reasoning_content"] = reasoning_content

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