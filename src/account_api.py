"""
AssemblyAI Account Management API

提供账户信息、使用量、成本、发票等数据的查询接口。
使用 Session 认证访问 AssemblyAI Dashboard API。
"""

import asyncio
import json
import time
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from log import log
from .httpx_client import http_client
from .storage_adapter import get_storage_adapter

router = APIRouter(prefix="/api/account", tags=["account"])

# AssemblyAI Dashboard API 基础 URL
ASSEMBLY_DASHBOARD_BASE = "https://www.assemblyai.com"

# Session 存储键
SESSION_STORAGE_KEY = "assembly_dashboard_session"
SESSION_EXPIRY_HOURS = 24 * 7  # Session 有效期 7 天


class LoginRequest(BaseModel):
    """登录请求模型"""
    email: str
    password: str


class SessionInfo(BaseModel):
    """Session 信息模型"""
    email: str
    logged_in: bool
    expires_at: Optional[str] = None


class AccountInfo(BaseModel):
    """账户信息模型"""
    id: int
    email: str
    customer_type: str
    cc_brand: Optional[str] = None
    cc_last4: Optional[str] = None
    created: str
    api_token: Optional[str] = None


async def _get_session() -> Optional[Dict[str, Any]]:
    """获取保存的 session 信息"""
    try:
        adapter = await get_storage_adapter()
        session_data = await adapter.get_config(SESSION_STORAGE_KEY)
        if session_data:
            # 检查 session 是否过期
            expires_at = session_data.get("expires_at")
            if expires_at:
                expiry_time = datetime.fromisoformat(expires_at)
                if datetime.now() > expiry_time:
                    log.info("Session expired, clearing...")
                    await adapter.delete_config(SESSION_STORAGE_KEY)
                    return None
            return session_data
    except Exception as e:
        log.error(f"Failed to get session: {e}")
    return None


async def _save_session(session_data: Dict[str, Any]) -> bool:
    """保存 session 信息"""
    try:
        adapter = await get_storage_adapter()
        # 设置过期时间
        session_data["expires_at"] = (
            datetime.now() + timedelta(hours=SESSION_EXPIRY_HOURS)
        ).isoformat()
        await adapter.set_config(SESSION_STORAGE_KEY, session_data)
        log.info(f"Session saved for {session_data.get('email', 'unknown')}")
        return True
    except Exception as e:
        log.error(f"Failed to save session: {e}")
        return False


async def _clear_session() -> bool:
    """清除 session 信息"""
    try:
        adapter = await get_storage_adapter()
        await adapter.delete_config(SESSION_STORAGE_KEY)
        log.info("Session cleared")
        return True
    except Exception as e:
        log.error(f"Failed to clear session: {e}")
        return False


async def _make_dashboard_request(
    method: str,
    path: str,
    data: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> Optional[Dict[str, Any]]:
    """
    发送 Dashboard API 请求
    
    Args:
        method: HTTP 方法 (GET, POST, etc.)
        path: API 路径
        data: POST 数据
        params: 查询参数
    
    Returns:
        响应数据或 None
    """
    import httpx
    
    session = await _get_session()
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    auth_type = session.get("auth_type", "dashboard")
    
    try:
        # 直接创建客户端，不使用代理（与登录保持一致）
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # 完整的请求头，模拟浏览器行为
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                "Origin": ASSEMBLY_DASHBOARD_BASE,
                "Referer": f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/",
                "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            
            if auth_type == "api_key":
                # 使用 API Key 认证
                api_key = session.get("api_key")
                if not api_key:
                    raise HTTPException(status_code=401, detail="Invalid session. Please login again.")
                headers["Authorization"] = api_key
                url = f"https://api.assemblyai.com{path}"
            elif auth_type == "dashboard":
                # 使用 Dashboard JWT 认证
                session_jwt = session.get("session_jwt")
                session_token = session.get("session_token")
                aai_extended_session = session.get("aai_extended_session")
                
                if session_jwt:
                    headers["Authorization"] = f"Bearer {session_jwt}"
                
                # 构建 cookie - 使用所有必要的 cookie
                cookies = []
                if aai_extended_session:
                    cookies.append(f"aai_extended_session={aai_extended_session}")
                if session_token:
                    cookies.append(f"stytch_session={session_token}")
                if session_jwt:
                    cookies.append(f"stytch_session_jwt={session_jwt}")
                
                if cookies:
                    headers["Cookie"] = "; ".join(cookies)
                
                url = f"{ASSEMBLY_DASHBOARD_BASE}{path}"
                
                log.debug(f"Dashboard request URL: {url}")
                log.debug(f"Dashboard request cookies count: {len(cookies)}")
            else:
                # 使用 Cookie 认证（旧方式）
                cookies = session.get("cookies", {})
                if cookies:
                    cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
                    headers["Cookie"] = cookie_str
                url = f"{ASSEMBLY_DASHBOARD_BASE}{path}"
            
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                resp = await client.post(url, headers=headers, json=data, params=params)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            log.debug(f"Dashboard API {method} {path}: {resp.status_code}")
            log.debug(f"Dashboard API response headers: {dict(resp.headers)}")
            
            if resp.status_code == 401:
                # Session 失效，清除并提示重新登录
                await _clear_session()
                raise HTTPException(status_code=401, detail="Session expired. Please login again.")
            
            if resp.status_code >= 400:
                log.error(f"Dashboard API error: {resp.status_code} - {resp.text[:500]}")
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Dashboard API error: {resp.status_code}"
                )
            
            # 尝试解析 JSON
            try:
                return resp.json()
            except Exception:
                # 可能是 RSC 格式，返回原始文本
                return {"raw": resp.text}
                
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Dashboard request failed: {e}")
        raise HTTPException(status_code=500, detail=f"Request failed: {str(e)}")


@router.post("/login")
async def login(request: LoginRequest) -> Dict[str, Any]:
    """
    登录 AssemblyAI Dashboard
    
    使用邮箱和密码登录，成功后保存 session token。
    后续请求无需重复登录。
    
    AssemblyAI 使用 /dashboard/api/auth/authenticate 端点进行认证。
    """
    log.info(f"Attempting login for {request.email}")
    
    try:
        import httpx
        # 直接创建客户端，不使用代理（AssemblyAI Dashboard 可能不支持代理）
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 使用正确的认证端点
            login_url = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/api/auth/authenticate"
            
            # 完整的请求头，模拟浏览器行为
            # 注意：不使用 br (Brotli) 编码，因为 httpx 默认不支持
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                "Origin": ASSEMBLY_DASHBOARD_BASE,
                "Referer": f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/login",
                "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            
            # 登录数据
            login_data = {
                "email": request.email,
                "password": request.password,
                "utm": {}
            }
            
            log.debug(f"Login URL: {login_url}")
            log.debug(f"Login headers: {headers}")
            log.debug(f"Login data: {{'email': '{request.email}', 'password': '***', 'utm': {{}}}}")
            
            resp = await client.post(
                login_url,
                json=login_data,
                headers=headers,
            )
            
            log.debug(f"Login response status: {resp.status_code}")
            log.debug(f"Login response headers: {dict(resp.headers)}")
            
            # 如果不是 200，记录响应内容以便调试
            if resp.status_code != 200:
                try:
                    response_text = resp.text[:500] if resp.text else "empty"
                    log.debug(f"Login response body: {response_text}")
                except Exception:
                    pass
            
            if resp.status_code == 200:
                try:
                    result = resp.json()
                    log.debug(f"Login result keys: {list(result.keys())}")
                    
                    # 检查是否认证成功
                    if result.get("isAuthenticated"):
                        user = result.get("user", {})
                        session_jwt = result.get("sessionJWT")
                        session_token = result.get("sessionToken")
                        
                        # 从响应头中提取 aai_extended_session cookie
                        aai_extended_session = None
                        set_cookie = resp.headers.get("set-cookie", "")
                        if "aai_extended_session=" in set_cookie:
                            # 解析 cookie 值
                            import re
                            match = re.search(r'aai_extended_session=([^;]+)', set_cookie)
                            if match:
                                aai_extended_session = match.group(1)
                                log.debug(f"Extracted aai_extended_session cookie")
                        
                        # 第二步：调用 Stytch API 验证 session（模拟浏览器行为）
                        stytch_session_jwt = session_jwt
                        try:
                            stytch_resp = await client.post(
                                "https://api.stytch.com/sdk/v1/b2b/sessions/authenticate",
                                json={"session_duration_minutes": 43200},
                                headers={
                                    "Content-Type": "application/json",
                                    "Accept": "application/json",
                                    "Authorization": f"Bearer {session_jwt}",
                                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                                    "Origin": ASSEMBLY_DASHBOARD_BASE,
                                    "Referer": f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/login",
                                }
                            )
                            log.debug(f"Stytch authenticate response: {stytch_resp.status_code}")
                            
                            if stytch_resp.status_code == 200:
                                stytch_data = stytch_resp.json()
                                # 更新 session JWT 和 token
                                if "session_jwt" in stytch_data:
                                    stytch_session_jwt = stytch_data["session_jwt"]
                                    log.debug("Updated session_jwt from Stytch")
                                if "session_token" in stytch_data:
                                    session_token = stytch_data["session_token"]
                                    log.debug("Updated session_token from Stytch")
                        except Exception as e:
                            log.warning(f"Stytch session authenticate failed (non-critical): {e}")
                        
                        # 保存 session 数据（包含完整用户信息）
                        session_data = {
                            "email": user.get("email", request.email),
                            "user_id": user.get("id"),
                            "api_token": user.get("api_token"),
                            "session_jwt": stytch_session_jwt,
                            "session_token": session_token,
                            "aai_extended_session": aai_extended_session,
                            "customer_type": user.get("customer_type"),
                            "auth_type": "dashboard",
                            "logged_in_at": datetime.now().isoformat(),
                            # 保存完整用户信息，用于 /api/account/info
                            "user_info": {
                                "id": user.get("id"),
                                "email": user.get("email"),
                                "customer_type": user.get("customer_type"),
                                "cc_brand": user.get("cc_brand"),
                                "cc_last4": user.get("cc_last4"),
                                "created": user.get("created"),
                                "api_token": user.get("api_token"),
                                "metronome_id": user.get("metronome_id"),
                            },
                        }
                        
                        if await _save_session(session_data):
                            return {
                                "success": True,
                                "message": "Login successful",
                                "email": user.get("email", request.email),
                                "user_id": user.get("id"),
                            }
                    else:
                        raise HTTPException(
                            status_code=401,
                            detail="Authentication failed"
                        )
                except HTTPException:
                    raise
                except Exception as e:
                    log.error(f"Failed to parse login response: {e}")
                    raise HTTPException(status_code=500, detail=f"Failed to parse response: {e}")
            
            # 登录失败
            error_msg = "Invalid email or password"
            try:
                error_data = resp.json()
                error_msg = error_data.get("error") or error_data.get("message") or error_msg
            except Exception:
                pass
            
            log.debug(f"Login failed: {error_msg}")
            raise HTTPException(status_code=401, detail=error_msg)
            
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_type = type(e).__name__
        error_msg = str(e) if str(e) else "Unknown error"
        log.error(f"Login failed: [{error_type}] {error_msg}")
        log.error(f"Login traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Login failed: [{error_type}] {error_msg}")


@router.post("/logout")
async def logout() -> Dict[str, Any]:
    """登出并清除 session"""
    await _clear_session()
    return {"success": True, "message": "Logged out successfully"}


@router.get("/session")
async def get_session_info() -> SessionInfo:
    """获取当前 session 状态"""
    session = await _get_session()
    if session:
        return SessionInfo(
            email=session.get("email", ""),
            logged_in=True,
            expires_at=session.get("expires_at"),
        )
    return SessionInfo(email="", logged_in=False)


@router.get("/info")
async def get_account_info() -> Dict[str, Any]:
    """
    获取账户基本信息
    
    返回账户 ID、邮箱、类型、创建时间、支付方式等信息。
    直接使用登录时保存的用户信息，无需再调用 Dashboard API。
    """
    session = await _get_session()
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    # 优先使用保存的用户信息
    user_info = session.get("user_info")
    if user_info:
        return {
            "id": user_info.get("id"),
            "email": user_info.get("email"),
            "customer_type": user_info.get("customer_type", "PAYG"),
            "cc_brand": user_info.get("cc_brand"),
            "cc_last4": user_info.get("cc_last4"),
            "created": user_info.get("created"),
            "api_token": user_info.get("api_token"),
        }
    
    # 兼容旧 session 格式
    return {
        "id": session.get("user_id"),
        "email": session.get("email"),
        "customer_type": session.get("customer_type", "PAYG"),
        "cc_brand": None,
        "cc_last4": None,
        "created": session.get("logged_in_at"),
        "api_token": session.get("api_token"),
    }


@router.get("/billing")
async def get_billing_info() -> Dict[str, Any]:
    """
    获取账单信息
    
    返回余额、消费趋势等信息。
    
    响应格式:
    {
        "balance": 58.49,  // 当前余额（美元）
        "spend_trend": [   // 30天消费趋势
            {"date": "2025-11-20T00:00:00.000Z", "amount": 0.42},
            ...
        ],
        "total_spend_30_days": 150.07  // 30天总消费
    }
    """
    # 获取账单页面数据
    data = await _make_dashboard_request(
        "GET",
        "/dashboard/account/billing",
        params={"_rsc": "1"}
    )
    
    if not data:
        raise HTTPException(status_code=500, detail="Failed to fetch billing info")
    
    # 使用专门的 billing 数据解析函数
    result = _parse_billing_rsc_data(data)
    
    return result


@router.get("/usage")
async def get_usage_data(
    window_size: str = "month",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
    group_by: str = "model",
    product: Optional[str] = None,
    regions: Optional[str] = None,
    services: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取使用量数据
    
    Args:
        window_size: 时间窗口 (day, week, month)
        starting_on: 开始日期 (YYYY-MM-DD)
        ending_before: 结束日期 (YYYY-MM-DD)
        group_by: 分组方式 (model, date, region)
        product: 产品类型
        regions: 区域列表 (逗号分隔)
        services: 服务列表 (逗号分隔)
    """
    params = {
        "_rsc": "1",
        "window_size": window_size,
        "group_by": group_by,
    }
    
    if starting_on:
        params["starting_on"] = starting_on
    if ending_before:
        params["ending_before"] = ending_before
    if product:
        params["product"] = product
    if regions:
        params["regions"] = regions
    if services:
        params["services"] = services
    
    data = await _make_dashboard_request("GET", "/dashboard/usage", params=params)
    
    if not data:
        raise HTTPException(status_code=500, detail="Failed to fetch usage data")
    
    # 解析 RSC 格式数据
    result = _parse_rsc_data(data)
    
    return result


@router.get("/cost")
async def get_cost_data(
    window_size: str = "month",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
    group_by: str = "model",
    regions: Optional[str] = None,
    services: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取成本数据
    
    Args:
        window_size: 时间窗口 (day, week, month)
        starting_on: 开始日期 (YYYY-MM-DD)
        ending_before: 结束日期 (YYYY-MM-DD)
        group_by: 分组方式 (model, date, region)
        regions: 区域列表 (逗号分隔)
        services: 服务列表 (逗号分隔)
    """
    params = {
        "_rsc": "1",
        "window_size": window_size,
        "group_by": group_by,
    }
    
    if starting_on:
        params["starting_on"] = starting_on
    if ending_before:
        params["ending_before"] = ending_before
    if regions:
        params["regions"] = regions
    if services:
        params["services"] = services
    
    data = await _make_dashboard_request("GET", "/dashboard/cost", params=params)
    
    if not data:
        raise HTTPException(status_code=500, detail="Failed to fetch cost data")
    
    # 解析 RSC 格式数据
    result = _parse_rsc_data(data)
    
    return result


@router.get("/rates")
async def get_rates(region: str = "US") -> Dict[str, Any]:
    """
    获取费率信息
    
    Args:
        region: 区域 (US, Europe)
    
    Returns:
        各产品和模型的费率信息
    """
    # 费率数据（基于 AssemblyAI 官方定价）
    # 这些数据可以从配置文件或数据库加载
    rates = {
        "region": region,
        "speech_to_text": [
            {"model": "Slam-1", "rate": 0.27, "unit": "hour", "beta": True},
            {"model": "Universal", "rate": 0.15, "unit": "hour", "beta": True},
            {"model": "Nano", "rate": 0.12, "unit": "hour", "beta": True},
            {"model": "Best", "rate": 0.65, "unit": "hour", "beta": False},
            {"model": "Conformer-2", "rate": 0.37, "unit": "hour", "beta": False},
        ],
        "streaming": [
            {"model": "Universal Streaming", "rate": 0.30, "unit": "hour", "beta": True},
            {"model": "Nano Streaming", "rate": 0.24, "unit": "hour", "beta": True},
            {"model": "Best Streaming", "rate": 1.30, "unit": "hour", "beta": False},
        ],
        "speech_understanding": [
            {"feature": "Summarization", "rate": 0.10, "unit": "hour"},
            {"feature": "Sentiment Analysis", "rate": 0.05, "unit": "hour"},
            {"feature": "Entity Detection", "rate": 0.05, "unit": "hour"},
            {"feature": "Topic Detection", "rate": 0.05, "unit": "hour"},
            {"feature": "Content Moderation", "rate": 0.05, "unit": "hour"},
            {"feature": "PII Redaction", "rate": 0.05, "unit": "hour"},
            {"feature": "Auto Chapters", "rate": 0.10, "unit": "hour"},
        ],
        "llm_gateway_input": [
            {"model": "GPT-5", "rate": 1.25, "unit": "1M tokens"},
            {"model": "GPT-4.1", "rate": 2.00, "unit": "1M tokens"},
            {"model": "GPT-4.1 mini", "rate": 0.40, "unit": "1M tokens"},
            {"model": "GPT-4.1 nano", "rate": 0.10, "unit": "1M tokens"},
            {"model": "Claude Opus 4", "rate": 15.00, "unit": "1M tokens"},
            {"model": "Claude Sonnet 4", "rate": 3.00, "unit": "1M tokens"},
            {"model": "Claude Sonnet 3.5 v2", "rate": 3.00, "unit": "1M tokens"},
            {"model": "Claude Haiku 3.5", "rate": 0.80, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Pro", "rate": 1.25, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Flash", "rate": 0.15, "unit": "1M tokens"},
            {"model": "Gemini 2.0 Flash", "rate": 0.10, "unit": "1M tokens"},
        ],
        "llm_gateway_output": [
            {"model": "GPT-5", "rate": 10.00, "unit": "1M tokens"},
            {"model": "GPT-4.1", "rate": 8.00, "unit": "1M tokens"},
            {"model": "GPT-4.1 mini", "rate": 1.60, "unit": "1M tokens"},
            {"model": "GPT-4.1 nano", "rate": 0.40, "unit": "1M tokens"},
            {"model": "Claude Opus 4", "rate": 75.00, "unit": "1M tokens"},
            {"model": "Claude Sonnet 4", "rate": 15.00, "unit": "1M tokens"},
            {"model": "Claude Sonnet 3.5 v2", "rate": 15.00, "unit": "1M tokens"},
            {"model": "Claude Haiku 3.5", "rate": 4.00, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Pro", "rate": 10.00, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Flash", "rate": 0.60, "unit": "1M tokens"},
            {"model": "Gemini 2.0 Flash", "rate": 0.40, "unit": "1M tokens"},
        ],
        "notes": [
            "* Beta models - pricing may change",
            "Multi-channel audio is billed per channel",
            "LLM Gateway pricing is per million tokens",
        ],
    }
    
    return rates


def _parse_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析 React Server Component (RSC) 格式数据
    
    RSC 数据格式通常是多行 JSON，每行以数字开头
    """
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {}
    
    try:
        lines = raw_text.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # RSC 格式: 数字:JSON 或直接 JSON
            if ":" in line and line[0].isdigit():
                # 找到第一个 : 后的内容
                colon_idx = line.index(":")
                json_part = line[colon_idx + 1:]
            else:
                json_part = line
            
            # 尝试解析 JSON
            try:
                if json_part.startswith("{") or json_part.startswith("["):
                    parsed = json.loads(json_part)
                    if isinstance(parsed, dict):
                        result.update(parsed)
                    elif isinstance(parsed, list):
                        if "items" not in result:
                            result["items"] = []
                        result["items"].extend(parsed)
            except json.JSONDecodeError:
                continue
    except Exception as e:
        log.warning(f"Failed to parse RSC data: {e}")
        result["raw_data"] = raw_text[:1000]
    
    return result


def _parse_billing_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    专门解析 billing 页面的 RSC 数据，提取余额和消费趋势
    
    RSC 响应中的关键数据：
    - 余额: 格式如 '$$58.49928' 在 span 元素中
    - 消费趋势: PreviewChart 组件的 data 属性
    """
    import re
    
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {
        "balance": None,
        "spend_trend": [],
        "raw_parsed": False
    }
    
    try:
        # 1. 提取余额 - 查找 "$$XX.XXXXX" 格式的余额数据
        # 格式: 14:["$","span",null,{"children":"$$58.49928"...
        balance_pattern = r'\$\$(\d+\.?\d*)'
        balance_matches = re.findall(balance_pattern, raw_text)
        if balance_matches:
            # 取第一个匹配的余额值（通常是账户余额）
            result["balance"] = float(balance_matches[0])
            log.debug(f"Extracted balance: {result['balance']}")
        
        # 2. 提取消费趋势数据 - 查找 PreviewChart 的 data 属性
        # 格式: ["$","$L64",null,{"data":[{"name":"2025-10-26T00:00:00.000Z","value":0},...]}]
        chart_data_pattern = r'"data":\s*\[((?:\{[^}]+\},?\s*)+)\]'
        chart_matches = re.findall(chart_data_pattern, raw_text)
        
        for match in chart_matches:
            try:
                # 尝试解析为 JSON 数组
                json_str = f"[{match}]"
                chart_data = json.loads(json_str)
                
                # 检查是否是消费趋势数据（包含 name 和 value 字段）
                if chart_data and isinstance(chart_data, list):
                    if all("name" in item and "value" in item for item in chart_data[:3]):
                        result["spend_trend"] = [
                            {
                                "date": item.get("name", ""),
                                "amount": item.get("value", 0)
                            }
                            for item in chart_data
                        ]
                        log.debug(f"Extracted spend trend: {len(result['spend_trend'])} data points")
                        break
            except json.JSONDecodeError:
                continue
        
        # 3. 计算 30 天总消费
        if result["spend_trend"]:
            result["total_spend_30_days"] = sum(
                item.get("amount", 0) for item in result["spend_trend"]
            )
        
        result["raw_parsed"] = True
        
    except Exception as e:
        log.warning(f"Failed to parse billing RSC data: {e}")
        result["error"] = str(e)
    
    return result


@router.get("/export/usage")
async def export_usage_data(
    format: str = "json",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
) -> Dict[str, Any]:
    """
    导出使用量数据
    
    Args:
        format: 导出格式 (json, csv)
        starting_on: 开始日期
        ending_before: 结束日期
    """
    # 获取使用量数据
    usage_data = await get_usage_data(
        window_size="day",
        starting_on=starting_on,
        ending_before=ending_before,
        group_by="date",
    )
    
    if format == "csv":
        # 转换为 CSV 格式
        csv_lines = ["date,product,model,usage,unit"]
        items = usage_data.get("items", [])
        for item in items:
            csv_lines.append(
                f"{item.get('date', '')},{item.get('product', '')},"
                f"{item.get('model', '')},{item.get('usage', 0)},{item.get('unit', '')}"
            )
        return {"format": "csv", "data": "\n".join(csv_lines)}
    
    return {"format": "json", "data": usage_data}


@router.get("/export/cost")
async def export_cost_data(
    format: str = "json",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
) -> Dict[str, Any]:
    """
    导出成本数据
    
    Args:
        format: 导出格式 (json, csv)
        starting_on: 开始日期
        ending_before: 结束日期
    """
    # 获取成本数据
    cost_data = await get_cost_data(
        window_size="day",
        starting_on=starting_on,
        ending_before=ending_before,
        group_by="date",
    )
    
    if format == "csv":
        # 转换为 CSV 格式
        csv_lines = ["date,product,model,input_tokens,output_tokens,cost"]
        items = cost_data.get("items", [])
        for item in items:
            csv_lines.append(
                f"{item.get('date', '')},{item.get('product', '')},"
                f"{item.get('model', '')},{item.get('input_tokens', 0)},"
                f"{item.get('output_tokens', 0)},{item.get('cost', 0)}"
            )
        return {"format": "csv", "data": "\n".join(csv_lines)}
    
    return {"format": "json", "data": cost_data}
