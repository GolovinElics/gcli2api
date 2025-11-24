"""
Configuration constants for the Geminicli2api proxy server.
Centralizes all configuration to avoid duplication across modules.
"""
import os
from typing import Any, Optional

# Client Configuration

# 需要自动封禁的错误码 (默认值，可通过环境变量或配置覆盖)
AUTO_BAN_ERROR_CODES = [401, 403]

# Default Safety Settings for Google API
DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"}
]

# Helper function to get base model name from any variant
def get_base_model_name(model_name):
    """Convert variant model name to base model name."""
    # Remove all possible suffixes in order
    suffixes = ["-maxthinking", "-nothinking", "-search"]
    for suffix in suffixes:
        if model_name.endswith(suffix):
            return model_name[:-len(suffix)]
    return model_name

# Helper function to check if model uses search grounding
def is_search_model(model_name):
    """Check if model name indicates search grounding should be enabled."""
    return "-search" in model_name

# Helper function to check if model uses no thinking
def is_nothinking_model(model_name):
    """Check if model name indicates thinking should be disabled."""
    return "-nothinking" in model_name

# Helper function to check if model uses max thinking
def is_maxthinking_model(model_name):
    """Check if model name indicates maximum thinking budget should be used."""
    return "-maxthinking" in model_name

# Helper function to get thinking budget for a model
def get_thinking_budget(model_name):
    """Get the appropriate thinking budget for a model based on its name and variant."""
    
    if is_nothinking_model(model_name):
        return 128  # Limited thinking for pro
    elif is_maxthinking_model(model_name):
        return 32768
    else:
        # Default thinking budget for regular models
        return None  # Default for all models

# Helper function to check if thinking should be included in output
def should_include_thoughts(model_name):
    """Check if thoughts should be included in the response."""
    if is_nothinking_model(model_name):
        # For nothinking mode, still include thoughts if it's a pro model
        base_model = get_base_model_name(model_name)
        return "pro" in base_model
    else:
        # For all other modes, include thoughts
        return True

# Dynamic Configuration System - Optimized for memory efficiency
async def get_config_value(key: str, default: Any = None, env_var: Optional[str] = None) -> Any:
    override_env = False
    env_override = os.getenv("CONFIG_OVERRIDE_ENV")
    if env_override:
        if env_override.lower() in ("true", "1", "yes", "on"):
            override_env = True
    if not override_env:
        try:
            from src.storage_adapter import get_storage_adapter
            storage_adapter = await get_storage_adapter()
            ov = await storage_adapter.get_config("override_env")
            if isinstance(ov, str):
                override_env = ov.lower() in ("true", "1", "yes", "on")
            else:
                override_env = bool(ov)
        except Exception:
            override_env = False
    if (not override_env) and env_var and os.getenv(env_var):
        return os.getenv(env_var)
    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()
        value = await storage_adapter.get_config(key)
        if value is not None:
            return value
    except Exception:
        pass
    return default


# Configuration getters - all async
async def get_proxy_config():
    """Get proxy configuration."""
    proxy_url = await get_config_value("proxy", env_var="PROXY")
    return proxy_url if proxy_url else None

async def get_calls_per_rotation() -> int:
    """Get calls per rotation setting."""
    env_value = os.getenv("CALLS_PER_ROTATION")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    
    return int(await get_config_value("calls_per_rotation", 100))

async def get_auto_ban_enabled() -> bool:
    """Get auto ban enabled setting."""
    env_value = os.getenv("AUTO_BAN")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    
    return bool(await get_config_value("auto_ban_enabled", False))

async def get_auto_ban_error_codes() -> list:
    """
    Get auto ban error codes.
    
    Environment variable: AUTO_BAN_ERROR_CODES (comma-separated, e.g., "400,403")
    TOML config key: auto_ban_error_codes
    Default: [400, 403]
    """
    env_value = os.getenv("AUTO_BAN_ERROR_CODES")
    if env_value:
        try:
            return [int(code.strip()) for code in env_value.split(",") if code.strip()]
        except ValueError:
            pass
    
    codes = await get_config_value("auto_ban_error_codes")
    if codes and isinstance(codes, list):
        return codes
    return AUTO_BAN_ERROR_CODES

async def get_retry_429_max_retries() -> int:
    """Get max retries for 429 errors."""
    env_value = os.getenv("RETRY_429_MAX_RETRIES")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    
    return int(await get_config_value("retry_429_max_retries", 5))

async def get_retry_429_enabled() -> bool:
    """Get 429 retry enabled setting."""
    env_value = os.getenv("RETRY_429_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    
    return bool(await get_config_value("retry_429_enabled", True))

async def get_retry_429_interval() -> float:
    """Get 429 retry interval in seconds."""
    env_value = os.getenv("RETRY_429_INTERVAL")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass
    
    return float(await get_config_value("retry_429_interval", 1))


# Model name lists for different features
BASE_MODELS = [
    "gemini-2.5-pro-preview-06-05",
    "gemini-2.5-pro", 
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-image-preview",
    "gemini-2.5-flash-preview-09-2025"
]

PUBLIC_API_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-image-preview"
]

def get_available_models(router_type="openai"):
    """
    返回可用模型列表。
    优先级：已选模型(available_models_selected) > 缓存模型(available_models) > 默认列表。
    """
    env_value = os.getenv("USE_ASSEMBLY")
    if env_value is None:
        use_assembly = True
    else:
        use_assembly = env_value.lower() in ("true", "1", "yes", "on")

    if use_assembly:
        try:
            # 已选模型优先
            from src.storage_adapter import get_storage_adapter
            storage_adapter = None
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                storage_adapter = loop.run_until_complete(get_storage_adapter()) if not loop.is_running() else None
            except Exception:
                storage_adapter = None
            if storage_adapter:
                try:
                    selected = loop.run_until_complete(storage_adapter.get_config("available_models_selected")) if not loop.is_running() else None
                except Exception:
                    selected = None
                if isinstance(selected, list) and selected:
                    return [str(m) for m in selected]
                try:
                    cached = loop.run_until_complete(storage_adapter.get_config("available_models")) if not loop.is_running() else None
                except Exception:
                    cached = None
                if isinstance(cached, list) and cached:
                    return [str(m) for m in cached]
        except Exception:
            pass
        return [
            "gpt-5",
            "gpt-5-nano",
            "gpt-5-mini",
            "gpt-4.1",
            "claude-4.5-sonnet-20250929",
            "claude-4-sonnet-20250514",
            "claude-3.5-haiku-20241022",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite"
        ]

async def get_available_models_async(router_type: str = "openai"):
    """异步版本：优先返回已选模型或缓存模型"""
    env_value = os.getenv("USE_ASSEMBLY")
    if env_value is None:
        use_assembly = True
    else:
        use_assembly = env_value.lower() in ("true", "1", "yes", "on")

    if use_assembly:
        selected = await get_config_value("available_models_selected")
        if isinstance(selected, list) and selected:
            return [str(m) for m in selected]
        cached = await get_config_value("available_models")
        if isinstance(cached, list) and cached:
            return [str(m) for m in cached]
        return [
            "gpt-5",
            "gpt-5-nano",
            "gpt-5-mini",
            "gpt-4.1",
            "claude-4.5-sonnet-20250929",
            "claude-4-sonnet-20250514",
            "claude-3.5-haiku-20241022",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite"
        ]
    # 非 Assembly 模式维持旧逻辑
    return get_available_models(router_type)

    models = []
    for base_model in BASE_MODELS:
        models.append(base_model)
        if (base_model in PUBLIC_API_MODELS):
            return models
        models.append(f"假流式/{base_model}")
        models.append(f"流式抗截断/{base_model}")
        for thinking_suffix in ["-maxthinking", "-nothinking", "-search"]:
            models.append(f"{base_model}{thinking_suffix}")
            models.append(f"假流式/{base_model}{thinking_suffix}")
            models.append(f"流式抗截断/{base_model}{thinking_suffix}")
    return models

def is_fake_streaming_model(model_name: str) -> bool:
    """Check if model name indicates fake streaming should be used."""
    return model_name.startswith("假流式/")

def is_anti_truncation_model(model_name: str) -> bool:
    """Check if model name indicates anti-truncation should be used."""
    return model_name.startswith("流式抗截断/")

def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    # Remove feature prefixes
    for prefix in ["假流式/", "流式抗截断/"]:
        if model_name.startswith(prefix):
            return model_name[len(prefix):]
    return model_name

async def get_anti_truncation_max_attempts() -> int:
    """
    Get maximum attempts for anti-truncation continuation.
    
    Environment variable: ANTI_TRUNCATION_MAX_ATTEMPTS
    TOML config key: anti_truncation_max_attempts
    Default: 3
    """
    return 3

# Server Configuration
async def get_server_host() -> str:
    """
    Get server host setting.
    
    Environment variable: HOST
    TOML config key: host
    Default: 0.0.0.0
    """
    return str(await get_config_value("host", "0.0.0.0", "HOST"))

async def get_server_port() -> int:
    """
    Get server port setting.
    
    Environment variable: PORT
    TOML config key: port
    Default: 7861
    """
    env_value = os.getenv("PORT")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    
    return int(await get_config_value("port", 7861))

async def get_api_password() -> str:
    """
    Get API password setting for chat endpoints.
    
    Environment variable: API_PASSWORD
    TOML config key: api_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 API_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    api_password = await get_config_value("api_password", None, "API_PASSWORD")
    if api_password is not None:
        return str(api_password)
    
    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))

async def get_panel_password() -> str:
    """
    Get panel password setting for web interface.
    
    Environment variable: PANEL_PASSWORD
    TOML config key: panel_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 PANEL_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    panel_password = await get_config_value("panel_password", None, "PANEL_PASSWORD")
    if panel_password is not None:
        return str(panel_password)
    
    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))

async def get_server_password() -> str:
    """
    Get server password setting (deprecated, use get_api_password or get_panel_password).
    
    Environment variable: PASSWORD
    TOML config key: password
    Default: pwd
    """
    return str(await get_config_value("password", "pwd", "PASSWORD"))

async def get_credentials_dir() -> str:
    """
    Get credentials directory setting.
    
    Environment variable: CREDENTIALS_DIR
    TOML config key: credentials_dir
    Default: ./creds
    """
    return str(await get_config_value("credentials_dir", "./creds", "CREDENTIALS_DIR"))


async def get_auto_load_env_creds() -> bool:
    """
    Get auto load environment credentials setting.
    
    Environment variable: AUTO_LOAD_ENV_CREDS
    TOML config key: auto_load_env_creds
    Default: False
    """
    env_value = os.getenv("AUTO_LOAD_ENV_CREDS")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    
    return bool(await get_config_value("auto_load_env_creds", False))

async def get_compatibility_mode_enabled() -> bool:
    """
    Get compatibility mode setting.
    
    兼容性模式：启用后所有system消息全部转换成user，停用system_instructions。
    该选项可能会降低模型理解能力，但是能避免流式空回的情况。
    
    Environment variable: COMPATIBILITY_MODE
    TOML config key: compatibility_mode_enabled
    Default: True
    """
    return False







# MongoDB Configuration
async def get_mongodb_uri() -> str:
    """
    Get MongoDB connection URI setting.
    
    MongoDB连接URI，用于分布式部署时的数据存储。
    设置此项后将不再使用本地/creds和TOML文件。
    
    Environment variable: MONGODB_URI
    TOML config key: mongodb_uri
    Default: None (使用本地文件存储)
    
    示例格式:
    - mongodb://username:password@localhost:27017/database
    - mongodb+srv://username:password@cluster.mongodb.net/database
    """
    return str(await get_config_value("mongodb_uri", "", "MONGODB_URI"))

async def get_mongodb_database() -> str:
    """
    Get MongoDB database name setting.
    
    MongoDB数据库名称。
    
    Environment variable: MONGODB_DATABASE
    TOML config key: mongodb_database
    Default: gcli2api
    """
    return str(await get_config_value("mongodb_database", "gcli2api", "MONGODB_DATABASE"))

async def is_mongodb_mode() -> bool:
    """
    Check if MongoDB mode is enabled.
    
    检查是否启用了MongoDB模式。
    如果配置了MongoDB URI，则启用MongoDB模式，不再使用本地文件。
    
    Returns:
        bool: True if MongoDB mode is enabled, False otherwise
    """
    mongodb_uri = await get_mongodb_uri()
    return bool(mongodb_uri and mongodb_uri.strip())

# AssemblyAI Configuration
async def get_assembly_endpoint() -> str:
    """
    Get AssemblyAI LLM Gateway endpoint setting.
    
    Environment variable: ASSEMBLY_ENDPOINT
    TOML config key: assembly_endpoint
    Default: https://llm-gateway.assemblyai.com/v1/chat/completions
    """
    return str(await get_config_value("assembly_endpoint", "https://llm-gateway.assemblyai.com/v1/chat/completions", "ASSEMBLY_ENDPOINT"))

async def get_assembly_api_key() -> str:
    """
    Get AssemblyAI API key for upstream authentication.
    
    Environment variable: ASSEMBLY_API_KEY
    TOML config key: assembly_api_key
    Default: empty string
    """
    return str(await get_config_value("assembly_api_key", "", "ASSEMBLY_API_KEY"))

async def get_assembly_api_keys() -> list:
    value = await get_config_value("assembly_api_keys", None, "ASSEMBLY_API_KEYS")
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts
    single = await get_config_value("assembly_api_key", "", "ASSEMBLY_API_KEY")
    return [single] if single else []

async def get_use_assembly() -> bool:
    """
    Toggle to use AssemblyAI as the upstream provider.
    
    Environment variable: USE_ASSEMBLY
    TOML config key: use_assembly
    Default: True
    """
    env_value = os.getenv("USE_ASSEMBLY")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("use_assembly", True))

async def get_enable_real_streaming() -> bool:
    """
    Get real streaming enabled setting.
    
    启用真实流式模式。默认为 False，使用假流式模式。
    当 AssemblyAI 修复流式响应问题后，可以设置为 True 启用真实流式。
    
    Environment variable: ENABLE_REAL_STREAMING
    TOML config key: enable_real_streaming
    Default: False (use fake streaming)
    """
    env_value = os.getenv("ENABLE_REAL_STREAMING")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("enable_real_streaming", False))
