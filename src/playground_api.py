"""
操练场增强 API 端点模块
提供请求报文预览和自定义报文发送功能
"""
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from log import log
from .request_generator import get_request_generator, create_request_generator
from .assembly_client import send_assembly_request
from .models import ChatCompletionRequest
from config import get_assembly_endpoint, get_assembly_api_keys


router = APIRouter(prefix="/api/playground", tags=["Playground"])


# Request/Response Models
class PreviewRequest(BaseModel):
    """请求预览请求"""
    model: str = Field(..., description="模型名称")
    messages: list = Field(..., description="消息列表")
    temperature: Optional[float] = Field(None, description="温度")
    max_tokens: Optional[int] = Field(None, description="最大 token 数")
    top_p: Optional[float] = Field(None, description="Top P")
    stream: Optional[bool] = Field(None, description="是否流式")
    tools: Optional[list] = Field(None, description="工具列表")
    tool_choice: Optional[Any] = Field(None, description="工具选择")


class CustomRequest(BaseModel):
    """自定义请求"""
    request_json: str = Field(..., description="JSON 格式的请求体")
    validate_only: bool = Field(False, description="仅验证不发送")


class PreviewResponse(BaseModel):
    """请求预览响应"""
    method: str
    url: str
    headers: Dict[str, str]
    body: Dict[str, Any]
    body_json: str


class ValidationResponse(BaseModel):
    """验证响应"""
    valid: bool
    error: Optional[str] = None


# API Endpoints
@router.post("/preview", response_model=PreviewResponse)
async def generate_request_preview(request: PreviewRequest):
    """生成请求报文预览"""
    try:
        # 获取配置
        try:
            endpoint = await get_assembly_endpoint()
        except Exception:
            endpoint = "https://llm-gateway.assemblyai.com/v1/chat/completions"
        
        try:
            keys = await get_assembly_api_keys()
            api_key = keys[0] if keys else "sk-your-api-key"
        except Exception:
            api_key = "sk-your-api-key"
        
        generator = create_request_generator(endpoint, api_key)
        
        # 转换 messages 格式
        messages = []
        for msg in request.messages:
            if isinstance(msg, dict):
                messages.append(msg)
            else:
                messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
        
        params = {
            "model": request.model,
            "messages": messages,
        }
        
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            params["top_p"] = request.top_p
        if request.stream is not None:
            params["stream"] = request.stream
        if request.tools is not None:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        
        preview = generator.generate_request_preview(params)
        
        return PreviewResponse(
            method=preview["method"],
            url=preview["url"],
            headers=preview["headers"],
            body=preview["body"],
            body_json=preview["body_json"]
        )
    except Exception as e:
        log.error(f"Failed to generate request preview: {e}")
        import traceback
        log.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/validate", response_model=ValidationResponse)
async def validate_custom_request(request: CustomRequest):
    """验证自定义请求格式"""
    try:
        generator = get_request_generator()
        
        is_valid, error = generator.validate_custom_request(request.request_json)
        
        return ValidationResponse(
            valid=is_valid,
            error=error if not is_valid else None
        )
    except Exception as e:
        log.error(f"Failed to validate custom request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/custom")
async def send_custom_request(request: CustomRequest):
    """发送自定义请求"""
    try:
        generator = get_request_generator()
        
        # 验证请求
        is_valid, error = generator.validate_custom_request(request.request_json)
        
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Invalid request: {error}")
        
        if request.validate_only:
            return {"success": True, "message": "Request is valid"}
        
        # 解析请求
        parsed = generator.parse_custom_request(request.request_json)
        if not parsed:
            raise HTTPException(status_code=400, detail="Failed to parse request")
        
        # 构建 ChatCompletionRequest
        try:
            chat_request = ChatCompletionRequest(**parsed)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid request format: {str(e)}")
        
        # 发送请求
        response = await send_assembly_request(chat_request, is_streaming=False)
        
        # 处理响应
        if hasattr(response, 'json'):
            return response.json()
        elif hasattr(response, 'body'):
            import json
            return json.loads(response.body)
        else:
            return {"error": "Unexpected response format"}
            
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to send custom request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/initial-request")
async def generate_initial_request(request: PreviewRequest):
    """根据操练场参数生成初始自定义请求"""
    try:
        generator = get_request_generator()
        
        params = {
            "model": request.model,
            "messages": request.messages,
        }
        
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            params["top_p"] = request.top_p
        if request.stream is not None:
            params["stream"] = request.stream
        if request.tools is not None:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        
        initial_json = generator.generate_initial_custom_request(params)
        
        return {
            "request_json": initial_json
        }
    except Exception as e:
        log.error(f"Failed to generate initial request: {e}")
        raise HTTPException(status_code=500, detail=str(e))
