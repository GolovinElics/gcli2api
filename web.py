"""
Main Web Integration - Integrates all routers and modules
集合router并开启主服务
"""
import asyncio
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Import all routers
from src.openai_router import router as openai_router
from src.admin_routes import router as admin_router
from src.account_api import router as account_router
from src.key_management_api import router as keys_router
from src.playground_api import router as playground_router
from src.key_management_api import router as key_management_router
from src.playground_api import router as playground_router
from src.account_api import router as account_router
# Google/Gemini 相关路由与控制面板已移除

# Import managers and utilities
from src.task_manager import shutdown_all_tasks
from config import get_server_host, get_server_port
from log import log

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    log.info("启动 AMB2API 主服务")
    
    # 初始化速率限制系统
    try:
        from src.assembly_client import initialize_rate_limit_system
        await initialize_rate_limit_system()
    except Exception as e:
        log.error(f"初始化速率限制系统时出错: {e}")
    
    yield
    
    # 清理资源
    log.info("开始关闭 AMB2API 主服务")
    
    # 首先关闭所有异步任务
    try:
        await shutdown_all_tasks(timeout=10.0)
        log.info("所有异步任务已关闭")
    except Exception as e:
        log.error(f"关闭异步任务时出错: {e}")
    
    log.info("AMB2API 主服务已停止")

# 创建FastAPI应用
app = FastAPI(
    title="AMB2API",
    description="AssemblyAI LLM Gateway proxy with OpenAI compatibility",
    version="0.2.6",
    lifespan=lifespan
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由器
# OpenAI兼容路由 - 处理OpenAI格式请求
app.include_router(
    openai_router,
    prefix="",
    tags=["OpenAI Compatible API"]
)

# 管理面板路由 - 简化配置与用量管理
app.include_router(
    admin_router,
    prefix="",
    tags=["Admin Panel"]
)

app.include_router(
    account_router,
    prefix="",
    tags=["account"]
)

app.include_router(
    keys_router,
    prefix="",
    tags=["Key Management"]
)

app.include_router(
    playground_router,
    prefix="",
    tags=["Playground"]
)

# 密钥管理路由 - 密钥增删改查和配置管理
app.include_router(
    key_management_router,
    prefix="",
    tags=["Key Management"]
)

# 操练场增强路由 - 请求预览和自定义报文
app.include_router(
    playground_router,
    prefix="",
    tags=["Playground Enhancement"]
)

# 账户管理路由 - 账户信息、使用量、成本、发票等
app.include_router(
    account_router,
    prefix="",
    tags=["Account Management"]
)

# Gemini原生路由 - 处理Gemini格式请求
# 仅保留 OpenAI 兼容路由

# 静态文件路由移除

# 保活接口（仅响应 HEAD）
@app.head("/keepalive")
async def keepalive() -> Response:
    return Response(status_code=200)

__all__ = ['app']

async def main():
    """异步主启动函数"""
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    
    # 日志系统现在直接使用环境变量，无需初始化
    
    # 从环境变量或配置获取端口和主机
    port = await get_server_port()
    host = await get_server_host()
    
    log.info("=" * 60)
    log.info("启动 AMB2API")
    log.info("=" * 60)
    log.info(f"OpenAI 兼容端点: http://127.0.0.1:{port}/v1")
    log.info("=" * 60)
    # 仅保留 OpenAI 兼容端点日志

    # 配置hypercorn
    config = Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = "-"
    config.errorlog = "-"
    config.loglevel = "INFO"
    config.use_colors = True
    
    # 设置请求体大小限制为100MB
    config.max_request_body_size = 100 * 1024 * 1024
    
    # 设置连接超时
    config.keep_alive_timeout = 300  # 5分钟
    config.read_timeout = 300  # 5分钟读取超时
    config.write_timeout = 300  # 5分钟写入超时
    
    # 增加启动超时时间以支持大量凭证的场景
    config.startup_timeout = 120  # 2分钟启动超时

    await serve(app, config)

if __name__ == "__main__":
    asyncio.run(main())
