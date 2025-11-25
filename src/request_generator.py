"""
请求生成器模块
生成请求报文预览和验证自定义请求
"""
import json
from typing import Dict, Any, Tuple, Optional, List

from log import log


class RequestGenerator:
    """请求生成器"""
    
    # OpenAI 兼容的请求参数
    SUPPORTED_PARAMS = [
        "model",
        "messages",
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
        "stream",
    ]
    
    def __init__(self, endpoint: str = "", api_key: str = ""):
        """
        初始化请求生成器
        
        Args:
            endpoint: API 端点
            api_key: API 密钥（用于预览）
        """
        self._endpoint = endpoint
        self._api_key = api_key
    
    def set_endpoint(self, endpoint: str):
        """设置 API 端点"""
        self._endpoint = endpoint
    
    def set_api_key(self, api_key: str):
        """设置 API 密钥"""
        self._api_key = api_key
    
    def _mask_key(self, key: str) -> str:
        """脱敏密钥"""
        if not key:
            return ""
        k = str(key)
        if len(k) <= 8:
            return k[:2] + "***"
        return k[:4] + "..." + k[-4:]
    
    def generate_request_preview(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成请求报文预览
        
        Args:
            params: 请求参数
        
        Returns:
            包含请求头、请求体等信息的字典
        """
        # 构建请求体
        body = {}
        for key in self.SUPPORTED_PARAMS:
            if key in params and params[key] is not None:
                body[key] = params[key]
        
        # 确保必需字段存在
        if "model" not in body:
            body["model"] = "gpt-4"
        if "messages" not in body:
            body["messages"] = [{"role": "user", "content": "Hello"}]
        
        # 构建请求头
        masked_key = self._mask_key(self._api_key) if self._api_key else "<API_KEY>"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {masked_key}",
        }
        
        return {
            "method": "POST",
            "url": self._endpoint or "<ENDPOINT>",
            "headers": headers,
            "body": body,
            "body_json": json.dumps(body, indent=2, ensure_ascii=False),
        }
    
    def validate_custom_request(self, request_json: str) -> Tuple[bool, str]:
        """
        验证自定义请求格式
        
        Args:
            request_json: JSON 格式的请求字符串
        
        Returns:
            (是否有效, 错误信息或空字符串)
        """
        if not request_json or not request_json.strip():
            return False, "Request body cannot be empty"
        
        try:
            data = json.loads(request_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON format: {str(e)}"
        
        if not isinstance(data, dict):
            return False, "Request body must be a JSON object"
        
        # 检查必需字段
        if "model" not in data:
            return False, "Missing required field: model"
        
        if "messages" not in data:
            return False, "Missing required field: messages"
        
        # 验证 messages 格式
        messages = data.get("messages")
        if not isinstance(messages, list):
            return False, "Field 'messages' must be an array"
        
        if len(messages) == 0:
            return False, "Field 'messages' cannot be empty"
        
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return False, f"Message at index {i} must be an object"
            if "role" not in msg:
                return False, f"Message at index {i} missing 'role' field"
            if "content" not in msg and "tool_calls" not in msg:
                return False, f"Message at index {i} missing 'content' or 'tool_calls' field"
        
        # 验证可选字段类型
        if "temperature" in data:
            temp = data["temperature"]
            if not isinstance(temp, (int, float)) or temp < 0 or temp > 2:
                return False, "Field 'temperature' must be a number between 0 and 2"
        
        if "max_tokens" in data:
            max_tokens = data["max_tokens"]
            if not isinstance(max_tokens, int) or max_tokens < 1:
                return False, "Field 'max_tokens' must be a positive integer"
        
        if "top_p" in data:
            top_p = data["top_p"]
            if not isinstance(top_p, (int, float)) or top_p < 0 or top_p > 1:
                return False, "Field 'top_p' must be a number between 0 and 1"
        
        if "n" in data:
            n = data["n"]
            if not isinstance(n, int) or n < 1:
                return False, "Field 'n' must be a positive integer"
        
        if "stream" in data:
            if not isinstance(data["stream"], bool):
                return False, "Field 'stream' must be a boolean"
        
        if "tools" in data:
            tools = data["tools"]
            if not isinstance(tools, list):
                return False, "Field 'tools' must be an array"
        
        return True, ""
    
    def generate_initial_custom_request(self, params: Dict[str, Any]) -> str:
        """
        根据操练场参数生成初始自定义请求
        
        Args:
            params: 操练场参数
        
        Returns:
            JSON 格式的请求字符串
        """
        preview = self.generate_request_preview(params)
        return preview["body_json"]
    
    def parse_custom_request(self, request_json: str) -> Optional[Dict[str, Any]]:
        """
        解析自定义请求
        
        Args:
            request_json: JSON 格式的请求字符串
        
        Returns:
            解析后的请求字典，如果解析失败则返回 None
        """
        valid, error = self.validate_custom_request(request_json)
        if not valid:
            log.warning(f"Invalid custom request: {error}")
            return None
        
        try:
            return json.loads(request_json)
        except Exception:
            return None
    
    def merge_with_defaults(
        self, 
        custom_request: Dict[str, Any],
        defaults: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        将自定义请求与默认值合并
        
        Args:
            custom_request: 自定义请求
            defaults: 默认值
        
        Returns:
            合并后的请求
        """
        result = defaults.copy()
        result.update(custom_request)
        return result


# 全局实例
_request_generator: Optional[RequestGenerator] = None


def get_request_generator() -> RequestGenerator:
    """获取全局请求生成器实例"""
    global _request_generator
    if _request_generator is None:
        _request_generator = RequestGenerator()
    return _request_generator


def create_request_generator(endpoint: str = "", api_key: str = "") -> RequestGenerator:
    """创建新的请求生成器实例"""
    return RequestGenerator(endpoint, api_key)
