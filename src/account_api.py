"""
AssemblyAI Account Management API

提供账户信息、使用量、成本、发票等数据的查询接口。
使用 Session 认证访问 AssemblyAI Dashboard API。
"""

import asyncio
import json
import time
from typing import Dict, Any, Optional, List
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
_cache_store: Dict[str, Dict[str, Any]] = {}
_cache_ttl_seconds = 300
_api_keys_fetch_task = None
_api_keys_last_fetch_ts = 0.0

def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _cache_store.get(key)
    if not entry:
        return None
    ts = entry.get("ts", 0)
    if (time.time() - ts) < _cache_ttl_seconds:
        return entry.get("data")
    return None

def _cache_set(key: str, data: Dict[str, Any]) -> None:
    _cache_store[key] = {"ts": time.time(), "data": data}


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
        client = await _get_dashboard_client()
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
            api_key = session.get("api_key")
            if not api_key:
                raise HTTPException(status_code=401, detail="Invalid session. Please login again.")
            headers["Authorization"] = api_key
            url = f"https://api.assemblyai.com{path}"
        elif auth_type == "dashboard":
            session_jwt = session.get("session_jwt")
            session_token = session.get("session_token")
            aai_extended_session = session.get("aai_extended_session")

            if session_jwt:
                headers["Authorization"] = f"Bearer {session_jwt}"

            cookies = []
            if aai_extended_session:
                cookies.append(f"aai_extended_session={aai_extended_session}")
            if session_token:
                cookies.append(f"session_token={session_token}")
            if session_jwt:
                cookies.append(f"session_jwt={session_jwt}")

            if cookies:
                headers["Cookie"] = "; ".join(cookies)

            if path.startswith("/dashboard/"):
                headers["Accept"] = "text/x-component"
                headers["RSC"] = "1"
                if params:
                    try:
                        from urllib.parse import urlencode
                        # Next-Url 不应携带 _rsc，保持与页面真实查询一致
                        next_params = {k: v for k, v in params.items() if k != "_rsc"}
                        if next_params:
                            qs = urlencode(next_params, doseq=True)
                            headers["Next-Url"] = f"{path}?{qs}"
                        else:
                            headers["Next-Url"] = path
                    except Exception:
                        headers["Next-Url"] = path
                else:
                    headers["Next-Url"] = path
                headers.setdefault("priority", "u=1, i")
                headers["X-Requested-With"] = "NextJS-RSC"
                if path.endswith("/usage"):
                    headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/usage"
                elif path.endswith("/code"):
                    headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/code"
                elif path.endswith("/cost"):
                    headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/cost"
                elif "/account/billing" in path:
                    headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/account/billing"

                url = f"{ASSEMBLY_DASHBOARD_BASE}{path}"

                log.debug(f"Dashboard request URL: {url}")
                log.debug(f"Dashboard request cookies count: {len(cookies)}")
                log.debug(f"Dashboard request cookies: {', '.join([c.split('=')[0] for c in cookies])}")
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
                        # 注意：Stytch API 可能不需要额外认证，直接使用登录返回的 JWT
                        stytch_session_jwt = session_jwt
                        
                        # 跳过 Stytch 认证步骤，因为登录已经返回了有效的 JWT
                        # AssemblyAI Dashboard 使用的是 Stytch 的 B2B 认证，
                        # 登录响应中的 sessionJWT 已经是有效的，不需要额外刷新
                        log.info("Using session JWT from login response directly")
                        
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
    获取账户基本信息（快速响应）
    
    返回账户 ID、邮箱、类型、创建时间、支付方式等基础信息。
    不包含 API key 信息，API key 通过 /api/account/api-keys 单独获取。
    """
    session = await _get_session()
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    # 优先使用保存的用户信息
    user_info = session.get("user_info")
    
    # 基础账户信息（快速响应，不请求外部 API）
    result = {
        "id": str((user_info or {}).get("id") or session.get("user_id") or ""),
        "email": str((user_info or {}).get("email") or session.get("email") or ""),
        "customer_type": str((user_info or {}).get("customer_type") or "PAYG"),
        "cc_brand": (user_info or {}).get("cc_brand"),
        "cc_last4": (user_info or {}).get("cc_last4"),
        "created": str((user_info or {}).get("created") or session.get("logged_in_at") or ""),
        "api_token": (user_info or {}).get("api_token") or session.get("api_token"),
    }
    
    return result


@router.get("/api-keys")
async def get_account_api_keys(force: bool = False) -> Dict[str, Any]:
    """
    获取账户的 API key 列表（独立端点）
    
    优先从 session 中获取 api_token（登录时已保存），
    如果需要完整的项目信息，则从 Dashboard API 获取。
    """
    session = await _get_session()
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    cache_key = "api_keys"
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            return cached
    
    result = {
        "projects": [],
        "api_keys": [],
    }
    
    # 优先从 session 中获取 api_token（快速响应）
    user_info = session.get("user_info", {})
    api_token = user_info.get("api_token") or session.get("api_token")
    
    if api_token:
        # 直接使用登录时保存的 api_token
        result["api_keys"].append({
            "id": None,
            "project_id": None,
            "project_name": "Default",
            "api_key": api_token,
            "name": "Default API Key",
            "is_disabled": False,
            "created": session.get("logged_in_at"),
        })
        log.info("Using api_token from session")
    
    _cache_set(cache_key, result)
    if force:
        try:
            rsc = await _make_dashboard_request(
                "GET",
                "/dashboard/cost",
                params={"_rsc": "125dm"},
            )
            if rsc and "raw" in rsc:
                projects = _parse_projects_from_rsc(rsc["raw"])
                if projects:
                    updated = {
                        "projects": projects,
                        "api_keys": [],
                    }
                    for project in projects:
                        for token in project.get("tokens", []):
                            updated["api_keys"].append({
                                "id": token.get("id"),
                                "project_id": token.get("project_id"),
                                "project_name": project.get("project", {}).get("name"),
                                "api_key": token.get("api_key"),
                                "name": token.get("name"),
                                "is_disabled": token.get("is_disabled"),
                                "created": token.get("created"),
                            })
                    if updated["api_keys"]:
                        _cache_set(cache_key, updated)
                        return updated
        except Exception as e:
            log.warning(f"Force refresh API keys failed: {e}")
        return result
    else:
        try:
            import asyncio, time
            global _api_keys_fetch_task, _api_keys_last_fetch_ts
            if (_api_keys_fetch_task is None or _api_keys_fetch_task.done()) and (time.time() - _api_keys_last_fetch_ts > 60):
                _api_keys_last_fetch_ts = time.time()
                async def _refresh():
                    try:
                        r = await _make_dashboard_request(
                            "GET",
                            "/dashboard/cost",
                            params={"_rsc": "125dm"},
                        )
                        if r and "raw" in r:
                            projects = _parse_projects_from_rsc(r["raw"])
                            if projects:
                                updated = {"projects": projects, "api_keys": []}
                                for project in projects:
                                    for token in project.get("tokens", []):
                                        updated["api_keys"].append({
                                            "id": token.get("id"),
                                            "project_id": token.get("project_id"),
                                            "project_name": project.get("project", {}).get("name"),
                                            "api_key": token.get("api_key"),
                                            "name": token.get("name"),
                                            "is_disabled": token.get("is_disabled"),
                                            "created": token.get("created"),
                                        })
                                if updated["api_keys"]:
                                    _cache_set(cache_key, updated)
                                    log.info(f"API keys cache updated: {len(updated['api_keys'])} keys")
                    except Exception as ex:
                        log.debug(f"Background API keys refresh failed: {ex}")
                _api_keys_fetch_task = asyncio.create_task(_refresh())
        except Exception:
            pass
        return result


def _parse_projects_from_rsc(raw_text: str) -> List[Dict[str, Any]]:
    """
    从 RSC 数据中解析项目和 API key 信息
    
    格式: "projects":[{"project":{...},"tokens":[{...}]}]
    """
    import re
    
    projects = []
    
    try:
        # 查找 "projects":[ 开始的 JSON 数组
        # 格式: "projects":[{"project":{...},"tokens":[...]}]
        projects_pattern = r'"projects":\s*\[((?:\{[^}]*"project":[^}]*\}[^]]*)+)\]'
        
        # 更简单的方法：直接查找 projects 数组
        start_marker = '"projects":['
        start_pos = raw_text.find(start_marker)
        
        if start_pos >= 0:
            # 找到 projects 数组的开始位置
            array_start = start_pos + len(start_marker) - 1  # 包含 [
            
            # 找到匹配的 ] 结束位置
            bracket_count = 0
            array_end = -1
            for i in range(array_start, min(array_start + 5000, len(raw_text))):
                if raw_text[i] == '[':
                    bracket_count += 1
                elif raw_text[i] == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        array_end = i + 1
                        break
            
            if array_end > array_start:
                json_str = raw_text[array_start:array_end]
                try:
                    projects = json.loads(json_str)
                    log.info(f"Parsed {len(projects)} projects from RSC data")
                except json.JSONDecodeError as e:
                    log.warning(f"Failed to parse projects JSON: {e}")
        
    except Exception as e:
        log.error(f"Failed to parse projects from RSC: {e}")
    
    return projects


@router.get("/billing")
async def get_billing_info(force: bool = False) -> Dict[str, Any]:
    """
    获取账单信息
    
    返回余额、消费趋势等信息。
    使用 Metronome API 获取准确的账单数据。
    
    响应格式:
    {
        "balance": 58.49,  // 当前余额（美元）
        "total_spend_30_days": 1.65,  // 30天总消费
        "cost_breakdown": {...}  // 按服务分类的成本
    }
    """
    session = await _get_session()
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    # 获取 metronome_id
    user_info = session.get("user_info", {})
    metronome_id = user_info.get("metronome_id")
    
    cache_key = "billing"
    cached = None if force else _cache_get(cache_key)
    if cached:
        return cached

    result = {
        "balance": 0.0,
        "total_spend_30_days": 0.0,
        "cost_breakdown": {},
        "spend_trend": [],
    }
    
    # 计算日期范围（最近30天）
    from datetime import datetime, timedelta
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    
    try:
        billing_data = None
        for params in [
            {"view": "US", "_rsc": "10s30"},
            {"_rsc": "1"},
            {"_rsc": "1mzsd"},
            {}
        ]:
            page = await _make_dashboard_request(
                "GET",
                "/dashboard/account/billing",
                params=params
            )
            if page and "raw" in page:
                raw_text = page["raw"].strip()
                if raw_text and not raw_text.startswith("<!DOCTYPE"):
                    billing_data = page
                    result.setdefault("debug_info", {})
                    result["debug_info"]["source_params"] = params
                    break
        
        if billing_data:
            parsed = _parse_billing_rsc_data(billing_data)
            result["balance"] = parsed.get("balance") or 0.0
            result["total_spend_30_days"] = parsed.get("total_spend_30_days") or 0.0
            result["spend_trend"] = parsed.get("spend_trend", [])
            if parsed.get("by_service"):
                result["cost_breakdown"] = {s.get("service"): s.get("cost", 0.0) for s in parsed.get("by_service", [])}
            log.info(f"Parsed billing from RSC: balance=${result['balance']}, spend=${result['total_spend_30_days']}")
            
            # 如果解析失败，记录原始数据用于调试
            if result["balance"] == 0.0 and result["total_spend_30_days"] == 0.0:
                if "raw" in billing_data:
                    raw_sample = billing_data["raw"][:500] if len(billing_data.get("raw", "")) > 500 else billing_data.get("raw", "")
                    log.warning(f"RSC parsing returned no data. Raw sample: {raw_sample}")
                    result["debug_info"] = "RSC parsing returned no data"
    
    except Exception as e:
        log.error(f"Failed to fetch billing info: {e}")
        # 返回默认值而不是抛出异常
        result["error"] = str(e)
    
    _cache_set(cache_key, result)
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
    force: bool = False,
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
    
    Returns:
        使用量数据，包括总 tokens、按模型分类的使用量等
    """
    # 计算默认日期范围
    # 参考 AssemblyAI API: starting_on=2025-11-26&ending_before=2025-11-27 表示查询 11/26 当天
    # ending_before 是 exclusive 的（不包含该日期）
    cache_key = "usage:summary"
    cached = None if force else _cache_get(cache_key)
    if cached:
        return cached

    result = {
        "total_tokens": 0,
        "items": [],
        "by_model": [],
        "segments": [],
        "debug_info": {}
    }
    
    try:
        base_params = {}
        rsc_params_list = [
            {"_rsc": "sd5d8"},
            {"_rsc": "1mzsd"},
            {"_rsc": "1"},
            base_params,
        ]

        rsc_data = None

        # 优先从 usage 页面获取 RSC 数据，其次回退到 code 页面
        for path in ["/dashboard/usage", "/dashboard/code"]:
            for rsc_params in rsc_params_list:
                try:
                    rsc_data = await _make_dashboard_request(
                        "GET",
                        path,
                        params=rsc_params
                    )

                    if rsc_data and "raw" in rsc_data:
                        raw_text = rsc_data["raw"]
                        if not (raw_text.strip().startswith("<!DOCTYPE") or raw_text.strip().startswith("<html")):
                            log.info(f"Got valid RSC data from {path} with params: {rsc_params}")
                            break
                        else:
                            rsc_data = None
                except Exception:
                    rsc_data = None
            if rsc_data:
                break

        if rsc_data:
            parsed = _parse_usage_rsc_data(rsc_data)
            result["total_tokens"] = parsed.get("total_tokens", 0)
            result["items"] = parsed.get("items", [])
            result["by_model"] = parsed.get("by_model", [])
            result["segments"] = parsed.get("segments", [])
            result["debug_info"] = parsed.get("debug_info", {})

            if "error" in parsed:
                result["error"] = parsed["error"]

            # 如果没有获得by_model数据（最重要的数据），尝试从多个来源获取
            if not result["by_model"] or len(result["by_model"]) == 0:
                log.warning("No by_model data in first attempt, trying fallback sources")
                
                # 尝试所有可能的路径和参数组合
                fallback_attempts = [
                    ("/dashboard/usage", {"_rsc": "sd5d8"}),
                    ("/dashboard/usage", {"_rsc": "1mzsd"}),
                    ("/dashboard/code", {"_rsc": "sd5d8"}),
                    ("/dashboard/code", {"_rsc": "1mzsd"}),
                    ("/dashboard/code", {"_rsc": "1"}),
                ]
                
                for fallback_path, rsc_params in fallback_attempts:
                    try:
                        fb_data = await _make_dashboard_request(
                            "GET",
                            fallback_path,
                            params=rsc_params
                        )
                        if fb_data and "raw" in fb_data:
                            raw_text = fb_data["raw"]
                            if not (raw_text.strip().startswith("<!DOCTYPE") or raw_text.strip().startswith("<html")):
                                parsed_fb = _parse_usage_rsc_data(fb_data)
                                if parsed_fb and parsed_fb.get("by_model"):
                                    # 找到了by_model数据，合并结果
                                    result["total_tokens"] = parsed_fb.get("total_tokens", result["total_tokens"])
                                    result["items"] = parsed_fb.get("items", result["items"])
                                    result["by_model"] = parsed_fb.get("by_model", result["by_model"])
                                    result["segments"] = parsed_fb.get("segments", result["segments"])
                                    result["debug_info"]["fallback_used"] = True
                                    result["debug_info"]["fallback_source"] = f"{fallback_path} with {rsc_params}"
                                    log.info(f"Found by_model data from fallback: {fallback_path} with {rsc_params}")
                                    break
                    except Exception as e:
                        log.debug(f"Fallback attempt failed for {fallback_path}: {e}")
                        continue

            log.info(f"Parsed usage from RSC: {result['total_tokens']} tokens, {len(result['by_model'])} models, {len(result['segments'])} segments")

    except Exception as e:
        log.error(f"Failed to fetch usage data: {e}")
        result["error"] = str(e)
        result["debug_info"]["exception"] = type(e).__name__
    
    _cache_set(cache_key, result)
    return result


@router.get("/cost")
async def get_cost_data(
    window_size: str = "month",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
    group_by: str = "model",
    regions: Optional[str] = None,
    services: Optional[str] = None,
    force: bool = False,
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
    
    Returns:
        成本数据，包括总成本、按服务/模型分类的成本等
    """
    # 计算默认日期范围
    # 参考 AssemblyAI API: starting_on=2025-11-26&ending_before=2025-11-27 表示查询 11/26 当天
    # ending_before 是 exclusive 的（不包含该日期）
    cache_key = "cost:summary"
    cached = None if force else _cache_get(cache_key)
    if cached:
        return cached

    result = {
        "total_cost": 0.0,
        "items": [],
        "by_service": []
    }
    
    try:
        rsc_data = None
        rsc_params_list = [
            {"_rsc": "sd5d8"},
            {"_rsc": "1mzsd"},
            {"_rsc": "1"},
            {},
        ]
        
        for rsc_params in rsc_params_list:
            try:
                rsc_data = await _make_dashboard_request(
                    "GET",
                    "/dashboard/cost",
                    params=rsc_params
                )
                if rsc_data and "raw" in rsc_data:
                    raw_text = rsc_data["raw"]
                    if not (raw_text.strip().startswith("<!DOCTYPE") or raw_text.strip().startswith("<html")):
                        if "chartExportData" in raw_text or '"data":' in raw_text or "total" in raw_text:
                            log.info(f"Got valid cost data with params: {rsc_params}")
                            break
                    rsc_data = None
            except Exception as e:
                log.debug(f"Cost request failed with {rsc_params}: {e}")
                rsc_data = None
        
        if rsc_data:
            parsed = _parse_cost_rsc_data(rsc_data)
            result["total_cost"] = parsed.get("total_cost", 0.0)
            result["by_service"] = parsed.get("by_service", [])
            result["by_model"] = parsed.get("by_model", [])
            result["spend_trend"] = parsed.get("spend_trend", [])
            
            # 如果没有获得by_model数据，尝试fallback
            if not result["by_model"] or len(result["by_model"]) == 0:
                log.warning("No by_model data in cost, trying fallback")
                # 尝试其他参数组合
                for rsc_params in rsc_params_list:
                    try:
                        fb_data = await _make_dashboard_request("GET", "/dashboard/cost", params=rsc_params)
                        if fb_data and "raw" in fb_data:
                            parsed_fb = _parse_cost_rsc_data(fb_data)
                            if parsed_fb.get("by_model"):
                                result["by_model"] = parsed_fb["by_model"]
                                result["debug_info"] = {"fallback_used": True}
                                log.info(f"Found by_model data from fallback with {rsc_params}")
                                break
                    except Exception:
                        continue
            
            log.info(f"Parsed cost from RSC: ${result['total_cost']}, {len(result['by_model'])} models")
    
    except Exception as e:
        log.error(f"Failed to fetch cost data: {e}")
        result["error"] = str(e)
    
    _cache_set(cache_key, result)
    return result


@router.get("/rates")
async def get_rates(region: str = "US", force: bool = False) -> Dict[str, Any]:
    cache_key = f"rates:{region}"
    cached = None if force else _cache_get(cache_key)
    if cached:
        return cached

    parsed = {}
    try:
        # 优先从 billing 页面获取费率数据（包含完整的费率表格）
        rsc = await _make_dashboard_request(
            "GET",
            "/dashboard/account/billing",
            params={"view": region, "_rsc": "10s30"},
        )
        parsed = _parse_rates_rsc_data(rsc)
        log.info(f"Parsed rates from billing API: {len(parsed.get('llm_gateway_input', []))} input, {len(parsed.get('llm_gateway_output', []))} output")
    except Exception as e:
        log.warning(f"Failed to fetch rates from API: {e}")
        parsed = {}

    fallback = {
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
            {"model": "GPT OSS 120b", "rate": 0.15, "unit": "1M tokens"},
            {"model": "Claude Opus 4", "rate": 15.00, "unit": "1M tokens"},
            {"model": "GPT-5", "rate": 1.25, "unit": "1M tokens"},
            {"model": "GPT-5 Mini", "rate": 0.25, "unit": "1M tokens"},
            {"model": "Claude Haiku 4.5", "rate": 1.00, "unit": "1M tokens"},
            {"model": "GPT OSS 20b", "rate": 0.07, "unit": "1M tokens"},
            {"model": "GPT-5 Nano", "rate": 0.05, "unit": "1M tokens"},
            {"model": "Claude 3.5 Haiku", "rate": 0.80, "unit": "1M tokens"},
            {"model": "Claude Sonnet 4.5", "rate": 3.00, "unit": "1M tokens"},
            {"model": "ChatGPT 4o Latest", "rate": 5.00, "unit": "1M tokens"},
            {"model": "GPT-4.1", "rate": 2.00, "unit": "1M tokens"},
            {"model": "Gemini 3 Pro Preview <200k Tokens", "rate": 2.00, "unit": "1M tokens"},
            {"model": "Claude Sonnet 4", "rate": 3.00, "unit": "1M tokens"},
            {"model": "Claude 3 Haiku", "rate": 0.25, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Flash Lite", "rate": 0.10, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Pro", "rate": 1.25, "unit": "1M tokens"},
        ],
        "llm_gateway_output": [
            {"model": "GPT-5", "rate": 10.00, "unit": "1M tokens"},
            {"model": "GPT-4.1", "rate": 8.00, "unit": "1M tokens"},
            {"model": "GPT-4.1 mini", "rate": 1.60, "unit": "1M tokens"},
            {"model": "GPT-4.1 nano", "rate": 0.40, "unit": "1M tokens"},
            {"model": "ChatGPT 4o Latest", "rate": 15.00, "unit": "1M tokens"},
            {"model": "GPT-5 Mini", "rate": 2.00, "unit": "1M tokens"},
            {"model": "GPT-5 Nano", "rate": 0.40, "unit": "1M tokens"},
            {"model": "GPT OSS 120b", "rate": 0.60, "unit": "1M tokens"},
            {"model": "GPT OSS 20b", "rate": 0.30, "unit": "1M tokens"},
            {"model": "Gemini 3 Pro Preview <200k Tokens", "rate": 12.00, "unit": "1M tokens"},
            {"model": "Gemini 3 Pro Preview >200k Tokens", "rate": 18.00, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Flash", "rate": 2.50, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Flash Lite", "rate": 0.40, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Pro", "rate": 10.00, "unit": "1M tokens"},
            {"model": "Claude Sonnet 4", "rate": 15.00, "unit": "1M tokens"},
            {"model": "Claude 3.5 Haiku", "rate": 4.00, "unit": "1M tokens"},
            {"model": "Claude 3 Haiku", "rate": 1.25, "unit": "1M tokens"},
            {"model": "Claude Opus 4", "rate": 75.00, "unit": "1M tokens"},
        ],
        "notes": [
            "LLM Gateway pricing is per million tokens",
        ],
    }

    result = {"region": region}
    result["speech_to_text"] = parsed.get("speech_to_text") or fallback["speech_to_text"]
    result["streaming"] = parsed.get("streaming") or fallback["streaming"]
    result["speech_understanding"] = parsed.get("speech_understanding") or fallback["speech_understanding"]
    result["llm_gateway_input"] = parsed.get("llm_gateway_input") or fallback["llm_gateway_input"]
    result["llm_gateway_output"] = parsed.get("llm_gateway_output") or fallback["llm_gateway_output"]
    result["notes"] = fallback["notes"]

    _cache_set(cache_key, result)
    return result


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
    - 余额: 格式如 '$58.49928' 或 '$$58.49928' 在 span 元素中
    - 消费趋势: PreviewChart 组件的 data 属性
    """
    import re
    
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {
        "balance": None,
        "spend_trend": [],
        "total_spend_30_days": 0.0,
        "by_service": [],
        "raw_parsed": False
    }
    
    try:
        log.debug(f"Parsing billing RSC data, length: {len(raw_text)}")
        
        # 1. 提取余额 - 多种格式尝试
        # 格式1: "children":"$$58.49928" (双美元符号)
        balance_pattern1 = r'"children":\s*"\$\$(\d+\.?\d*)"'
        balance_matches = re.findall(balance_pattern1, raw_text)
        
        if not balance_matches:
            # 格式2: "children":"$58.49928" (单美元符号)
            balance_pattern2 = r'"children":\s*"\$(\d+\.?\d*)"'
            balance_matches = re.findall(balance_pattern2, raw_text)
        
        if not balance_matches:
            # 格式3: 直接匹配 $$数字 格式
            balance_pattern3 = r'\$\$(\d+\.?\d*)'
            balance_matches = re.findall(balance_pattern3, raw_text)
        
        if balance_matches:
            # 取第一个匹配的余额值（通常是账户余额）
            result["balance"] = float(balance_matches[0])
            log.info(f"Extracted balance: ${result['balance']}")
        else:
            log.warning("No balance found in RSC data")
        
        # 2. 提取消费趋势数据 - 查找 PreviewChart 的 data 属性
        # 格式: ["$","$L64",null,{"data":[{"name":"2025-10-26T00:00:00.000Z","value":0},...]}]
        chart_data_pattern = r'"data":\s*\[((?:\{[^}]+\},?\s*)+)\]'
        chart_matches = re.findall(chart_data_pattern, raw_text)
        
        for match in chart_matches:
            try:
                json_str = f"[{match}]"
                chart_data = json.loads(json_str)
                if chart_data and isinstance(chart_data, list):
                    first_items = chart_data[:3] if len(chart_data) >= 3 else chart_data
                    if all("name" in item and "value" in item for item in first_items):
                        spend = []
                        for item in chart_data:
                            try:
                                v = item.get("value", 0)
                                amount = float(v)
                            except Exception:
                                amount = 0.0
                            spend.append({
                                "date": item.get("name", ""),
                                "amount": amount
                            })
                        total = sum(s.get("amount", 0.0) for s in spend)
                        if total > 20:
                            for s in spend:
                                s["amount"] = round(s["amount"] / 100.0, 8)
                            total = sum(s.get("amount", 0.0) for s in spend)
                        result["spend_trend"] = spend
                        log.info(f"Extracted spend trend: {len(spend)} data points")
                        break
            except (json.JSONDecodeError, ValueError) as e:
                log.debug(f"Failed to parse chart data: {e}")
                continue
        
        # 3. 提取总额按服务分类（如果存在）
        try:
            m_total = re.search(r'"total":\s*\{([^}]+)\}', raw_text)
            if m_total:
                import json as _json
                json_str = '{' + m_total.group(1) + '}'
                total_obj = _json.loads(json_str)
                by_service = []
                for k, v in total_obj.items():
                    name = str(k)
                    try:
                        amount = float(v)
                    except Exception:
                        amount = 0.0
                    by_service.append({
                        "service": name,
                        "cost": amount,
                    })
                if by_service:
                    result["by_service"] = by_service
        except Exception:
            pass

        # 4. 计算 30 天总消费
        if result["spend_trend"]:
            result["total_spend_30_days"] = sum(
                item.get("amount", 0.0) for item in result["spend_trend"]
            )
            log.info(f"Total 30-day spend: ${result['total_spend_30_days']:.5f}")
        
        result["raw_parsed"] = True
        
    except Exception as e:
        log.warning(f"Failed to parse billing RSC data: {e}")
        result["error"] = str(e)
    
    return result

def _sanitize_model_name(model_name: str) -> str:
    """
    清理和验证模型名称，防止 XSS 和编码问题
    
    Args:
        model_name: 原始模型名称
    
    Returns:
        清理后的模型名称
    """
    if not model_name:
        return ""
    
    # 移除潜在的 HTML 标签
    import html
    sanitized = html.escape(model_name)
    
    # 限制长度
    max_length = 200
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    
    return sanitized


def _extract_segments_from_rsc(raw_text: str) -> List[Dict[str, Any]]:
    """
    从 RSC 数据中提取 segments 数组
    
    Segments 通常在组件 props 中，格式如：
    ["$","$L14",null,{"size":"1","segments":[{"value":22449,"color":"var(--blue-10)"},...]}]
    
    Args:
        raw_text: RSC 原始文本
    
    Returns:
        Segments 列表，每个包含 value 和 color
    """
    import re
    
    segments = []
    
    try:
        # 方法1: 查找完整的 segments 数组（更精确的匹配）
        # 匹配: "segments":[{...},{...}]
        segments_pattern = r'"segments":\s*\[((?:\{[^}]+\},?\s*)+)\]'
        segments_matches = re.findall(segments_pattern, raw_text)
        
        for match in segments_matches:
            try:
                # 添加数组括号
                json_str = f"[{match}]"
                parsed_segments = json.loads(json_str)
                
                # 验证是否是有效的 segments 数据（包含 value 字段）
                if parsed_segments and isinstance(parsed_segments, list) and len(parsed_segments) > 0:
                    first_item = parsed_segments[0] if parsed_segments else {}
                    if isinstance(first_item, dict) and "value" in first_item:
                        segments = parsed_segments
                        log.info(f"Extracted {len(segments)} segments from RSC data (method 1)")
                        break
            except json.JSONDecodeError as e:
                log.debug(f"JSON decode failed for segments: {e}")
                continue
        
        # 方法2: 如果方法1失败，尝试查找独立的 segment 对象
        if not segments:
            segment_pattern = r'\{"value":\s*(\d+),\s*"color":\s*"([^"]+)"\}'
            segment_matches = re.findall(segment_pattern, raw_text)
            
            if segment_matches:
                segments = [
                    {"value": int(value), "color": color}
                    for value, color in segment_matches
                ]
                log.info(f"Extracted {len(segments)} segments using fallback method (method 2)")
    
    except Exception as e:
        log.error(f"Failed to extract segments: {e}")
    
    return segments


def _extract_model_names_from_rsc(raw_text: str) -> List[Dict[str, Any]]:
    """
    从 RSC 数据中提取模型名称和对应的 token 数量
    
    模型信息通常在 div 元素中，格式如：
    ["$","div","LLM Gateway + LeMUR-GPT 5 Mini",{"children":[...]}]
    或在 span 中：
    ["$","span",null,{"children":[...," ","GPT 5 Mini"]}]
    
    Args:
        raw_text: RSC 原始文本
    
    Returns:
        模型列表，每个包含 model 和 tokens
    """
    import re

    import json
    
    models = []
    
    # 将解析范围尽量限定在目标视图区域（例如 granularity=day 的 UsageChart），避免误取 30 天摘要卡片
    scan_text = raw_text
    try:
        pos = raw_text.find('"granularity":"day"')
        if pos >= 0:
            # 只扫描该区域后面的内容，降低误匹配概率
            scan_text = raw_text[pos: pos + 60000]
    except Exception:
        scan_text = raw_text
    
    # 方法1: 尝试按行解析 JSON (针对 RSC 流式响应)
    try:
        lines = scan_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 提取 JSON 部分 (处理 "1a:..." 格式)
            json_str = line
            if ':' in line and line[0].isdigit():
                parts = line.split(':', 1)
                if len(parts) > 1:
                    json_str = parts[1]
            
            try:
                # 尝试解析为 JSON
                if not (json_str.startswith('[') or json_str.startswith('{')):
                    continue
                    
                data = json.loads(json_str)
                
                # 检查是否是模型统计行
                # 结构: ["$","div","LLM Gateway + LeMUR-MODEL NAME",{"children":[PART1, PART2]}]
                if (isinstance(data, list) and len(data) >= 4 and 
                    isinstance(data[2], str) and 
                    "LLM Gateway + LeMUR-" in data[2]):
                    
                    # 提取模型名称
                    full_key = data[2]
                    model_name_from_key = full_key.replace("LLM Gateway + LeMUR-", "").strip()
                    
                    # 尝试从 children 中提取 token 数量
                    props = data[3]
                    if isinstance(props, dict) and "children" in props:
                        children = props["children"]
                        if isinstance(children, list) and len(children) >= 2:
                            # PART 2: Token Count 通常在第二个 child 中
                            # ["$","span",null,{"children":["12,089"," ",...]}]
                            token_count = 0
                            
                            # 遍历 children 寻找包含数字的部分
                            for child in children:
                                if isinstance(child, list) and len(child) >= 4:
                                    child_props = child[3]
                                    if isinstance(child_props, dict) and "children" in child_props:
                                        grand_children = child_props["children"]
                                        
                                        # 情况A: ["12,089", " ", ...]
                                        if isinstance(grand_children, list) and len(grand_children) > 0:
                                            first_item = grand_children[0]
                                            if isinstance(first_item, str) and re.match(r'^[\d,]+$', first_item.strip()):
                                                try:
                                                    token_count = int(first_item.replace(",", "").strip())
                                                    break
                                                except ValueError:
                                                    pass
                            
                            if model_name_from_key and token_count > 0:
                                sanitized_name = _sanitize_model_name(model_name_from_key)
                                models.append({
                                    "model": sanitized_name,
                                    "tokens": token_count
                                })
                                continue
            except json.JSONDecodeError:
                continue
            except Exception as e:
                # 单行解析失败不影响其他行
                continue
                
        if models:
            log.info(f"Extracted {len(models)} model entries via JSON parsing")
            return models
            
    except Exception as e:
        log.warning(f"JSON parsing method failed: {e}")

    # 方法2: 正则表达式回退 (保留原有逻辑但稍作改进)
    try:
        # 查找 "LLM Gateway + LeMUR-{model_name}" 格式的 div key
        div_pattern = r'\["[^"]*","div","LLM Gateway \+ LeMUR-([^"]+)"'
        div_matches = re.findall(div_pattern, scan_text)
        
        if div_matches:
            log.debug(f"Found {len(div_matches)} model names in div keys (regex)")
        
        # 改进的正则: 尝试匹配 token 数量
        # 寻找 "children":["12,089" 这样的模式
        # 注意: 这种简单的正则无法可靠地关联模型名称和 token 数，
        # 除非它们在文本中紧邻。在 RSC 中它们通常是分开的组件。
        
        # 如果 JSON 解析失败，尝试旧的正则作为最后的手段
        model_token_pattern = r'"children":\s*\[[^\]]*,\s*" ",\s*"([^"]+)"\][^}]*"children":\s*\["([\d,]+)"'
        model_token_matches = re.findall(model_token_pattern, scan_text)
        
        for model_name, token_str in model_token_matches:
            if model_name and not model_name.lower() in ['tokens', 'total', 'hours']:
                try:
                    tokens = int(token_str.replace(",", ""))
                    sanitized_name = _sanitize_model_name(model_name)
                    if sanitized_name:
                        models.append({
                            "model": sanitized_name,
                            "tokens": tokens
                        })
                except ValueError:
                    continue
        
        if models:
            log.debug(f"Extracted {len(models)} model entries via regex")
        if not models:
            try:
                div_token_pattern = r'\["\$","div","LLM Gateway \+ LeMUR-([^"]+)",[\s\S]*?"children":\s*\[[\s\S]*?"([\d,]+)"[\s\S]*?"tokens"'
                for m in re.finditer(div_token_pattern, scan_text):
                    name = m.group(1)
                    val = int(m.group(2).replace(',', ''))
                    sanitized_name = _sanitize_model_name(name)
                    if sanitized_name:
                        models.append({"model": sanitized_name, "tokens": val})
            except Exception:
                pass
    
    except Exception as e:
        log.warning(f"Failed to extract model names via regex: {e}")
    
    return models


def _parse_usage_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析使用量页面的 RSC 数据
    
    提取总 tokens、按模型分类的使用量和可视化 segments
    
    Args:
        data: 包含 'raw' 字段的 RSC 响应数据
    
    Returns:
        解析后的使用量数据，包含：
        - total_tokens: 总 token 数
        - by_model: 按模型分类的使用量列表
        - segments: 可视化分段数据
        - items: 其他数据项
        - error: 错误信息（如果有）
        - debug_info: 调试信息
    """
    import re
    
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {
        "total_tokens": 0,
        "items": [],
        "by_model": [],
        "segments": [],
        "debug_info": {
            "raw_length": len(raw_text),
            "parsing_method": None,
            "extracted_segments_count": 0,
            "extracted_models_count": 0
        }
    }
    
    try:
        log.debug(f"Parsing usage RSC data, length: {len(raw_text)}")
        
        # 1. 先提取 segments 与按模型统计
        segments = _extract_segments_from_rsc(raw_text)
        if segments:
            result["segments"] = segments
            result["debug_info"]["extracted_segments_count"] = len(segments)
            log.info(f"Extracted {len(segments)} visualization segments")
        
        # 2. 提取模型名称和 token 数量
        models = _extract_model_names_from_rsc(raw_text)
        if models:
            result["by_model"] = models
            result["debug_info"]["extracted_models_count"] = len(models)
            log.info(f"Extracted {len(models)} model entries")
        
        # 3. 如果有 segments 但没有模型名称，尝试关联
        if segments and not models:
            # 尝试从 raw_text 中查找模型名称列表
            # 这些名称通常在 segments 附近
            log.debug("Attempting to associate segments with model names")
            
            # 查找所有可能的模型名称
            model_name_pattern = r'"children":\s*\[[^\]]*"([^"]+)"\][^}]*(?="children":\s*\["[\d,]+")'
            potential_names = re.findall(model_name_pattern, raw_text)
            
            # 过滤和关联
            if len(potential_names) >= len(segments):
                for i, segment in enumerate(segments):
                    if i < len(potential_names):
                        sanitized_name = _sanitize_model_name(potential_names[i])
                        if sanitized_name:
                            result["by_model"].append({
                                "model": sanitized_name,
                                "tokens": segment["value"],
                                "color": segment.get("color")
                            })
                log.debug(f"Associated {len(result['by_model'])} segments with model names")
        
        # 4. 总数优先来自当前解析的模型或分段，以避免误取 30 天摘要的总额
        if result["by_model"]:
            result["total_tokens"] = sum(m.get("tokens", 0) for m in result["by_model"])
            result["debug_info"]["parsing_method"] = "sum_by_model"
            log.info(f"Calculated total tokens from models: {result['total_tokens']}")
        elif segments:
            result["total_tokens"] = sum(s.get("value", 0) for s in segments)
            result["debug_info"]["parsing_method"] = "sum_segments"
            log.info(f"Calculated total tokens from segments: {result['total_tokens']}")
        else:
            # 5. 兜底：从 "Total tokens" 模式提取（可能是摘要卡片）
            total_matches = re.findall(r'"children":\s*\["([\d,]+)",\s*" ",\s*\["\$","span",null,\{"children":\["Total ","tokens"\]\}\]\]', raw_text)
            if total_matches:
                total_str = total_matches[0].replace(",", "")
                result["total_tokens"] = int(total_str)
                result["debug_info"]["parsing_method"] = "pattern1_fallback"
                log.info(f"Extracted total tokens (pattern1 fallback): {result['total_tokens']}")
            else:
                total_pattern2 = r'"([\d]{1,3},[\d]{3}(?:,[\d]{3})?)"[^}]{0,100}[Tt]otal[^}]{0,50}tokens'
                total_matches = re.findall(total_pattern2, raw_text)
                if total_matches:
                    max_tokens = 0
                    for match in total_matches:
                        tokens = int(match.replace(",", ""))
                        if tokens > max_tokens:
                            max_tokens = tokens
                    if max_tokens > 0:
                        result["total_tokens"] = max_tokens
                        result["debug_info"]["parsing_method"] = "pattern2_fallback"
                        log.info(f"Extracted total tokens (pattern2 fallback): {result['total_tokens']}")
        
        # 6. 记录调试信息
        if result["total_tokens"] == 0 and not result["by_model"]:
            log.warning("No usage data extracted from RSC response")
            result["debug_info"]["raw_sample"] = raw_text[:500] if len(raw_text) > 500 else raw_text
        
    except Exception as e:
        log.error(f"Failed to parse usage RSC data: {e}")
        result["error"] = str(e)
        result["debug_info"]["error_type"] = type(e).__name__
        result["debug_info"]["raw_sample"] = raw_text[:500] if len(raw_text) > 500 else raw_text
    
    return result


def _parse_cost_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析成本页面的 RSC 数据
    
    提取总成本、按服务分类的成本和消费趋势
    """
    import re
    import json
    
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {
        "total_cost": 0.0,
        "items": [],
        "by_service": [],
        "by_model": [],
        "spend_trend": [],
    }
    
    try:
        # 1. 优先从 title 属性提取总成本（最准确）
        title_match = re.search(r'"title":\s*"Total spend:\s*\$(\d+\.?\d*)"', raw_text)
        if title_match:
            result["total_cost"] = float(title_match.group(1))
        else:
            cost_matches = re.findall(r'"children":\s*"\$\$?(\d+\.?\d*)"', raw_text)
            if cost_matches:
                costs = [float(c) for c in cost_matches]
                result["total_cost"] = max(costs) if costs else 0.0
            else:
                ts_matches = re.findall(r'Total\s+spend:\s*\$(\d+\.?\d*)', raw_text)
                if ts_matches:
                    result["total_cost"] = float(ts_matches[0])
        
        # 2. 优先从 chartExportData 提取详细数据（最完整）
        # chartExportData 可能包含嵌套的对象，需要更复杂的匹配
        chart_export_start = raw_text.find('"chartExportData":[')
        log.debug(f"Looking for chartExportData, found at position: {chart_export_start}")
        if chart_export_start >= 0:
            # 找到数组的开始位置
            array_start = chart_export_start + len('"chartExportData":')
            # 找到匹配的 ] 结束位置
            bracket_count = 0
            array_end = -1
            for i in range(array_start, min(array_start + 50000, len(raw_text))):
                if raw_text[i] == '[':
                    bracket_count += 1
                elif raw_text[i] == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        array_end = i + 1
                        break
            
            if array_end > array_start:
                json_str = raw_text[array_start:array_end]
                try:
                    chart_data = json.loads(json_str)
                    if chart_data:
                        # 按模型聚合（分别统计输入、输出，以及未知方向的额外成本）
                        model_costs: Dict[str, Dict[str, float]] = {}
                        for item in chart_data:
                            model = item.get("model", "")
                            value = float(item.get("value", 0))
                            
                            # 解析模型名和方向
                            base = model
                            is_input = False
                            is_output = False
                            if base.endswith(" (Input)"):
                                is_input = True
                                base = base[:-8].strip()
                            elif base.endswith(" (Output)"):
                                is_output = True
                                base = base[:-9].strip()
                            
                            if base not in model_costs:
                                model_costs[base] = {"input_cost": 0.0, "output_cost": 0.0, "extra_cost": 0.0}
                            
                            if is_input:
                                model_costs[base]["input_cost"] += value
                            elif is_output:
                                model_costs[base]["output_cost"] += value
                            else:
                                # 未标注方向的值累加到额外成本，再合并到总额
                                model_costs[base]["extra_cost"] += value
                        
                        by_model = []
                        for model, costs in model_costs.items():
                            total = costs["input_cost"] + costs["output_cost"] + costs.get("extra_cost", 0.0)
                            by_model.append({
                                "model": model,
                                "input_cost": costs["input_cost"],
                                "output_cost": costs["output_cost"],
                                "cost": total,
                            })
                        by_model.sort(key=lambda x: x.get("cost", 0.0), reverse=True)
                        result["by_model"] = by_model
                        
                        # 如果没有从 title 获取到总成本，从模型数据计算
                        if result["total_cost"] == 0.0:
                            result["total_cost"] = sum(m.get("cost", 0.0) for m in by_model)
                        
                        log.info(f"Parsed {len(by_model)} models from chartExportData")
                except Exception as e:
                    log.warning(f"Failed to parse chartExportData: {e}")
                    # 记录原始数据样本用于调试
                    sample = json_str[:500] if len(json_str) > 500 else json_str
                    log.debug(f"chartExportData sample: {sample}")

        # 提取总额按服务分类（TotalCard）
        try:
            m_total = re.search(r'"total":\s*\{([^}]+)\}', raw_text)
            if m_total:
                import json
                json_str = '{' + m_total.group(1) + '}'
                total_obj = json.loads(json_str)
                by_service = []
                for k, v in total_obj.items():
                    name = str(k)
                    try:
                        amount = float(v)
                    except Exception:
                        amount = 0.0
                    by_service.append({
                        "service": name,
                        "cost": amount,
                    })
                if by_service:
                    result["by_service"] = by_service
                    if result["total_cost"] == 0.0:
                        result["total_cost"] = sum(s.get("cost", 0.0) for s in by_service)
        except Exception:
            pass
        
        # 2. 提取按模型分类的成本（CostChart data 映射）
        # 只有在chartExportData没有提供by_model数据时才使用这个方法
        if not result["by_model"]:
            log.debug("chartExportData didn't provide by_model, trying alternative parsing")
            data_matches = re.findall(r'"data":\s*\[((?:\{[^}]+\},?\s*)+)\]', raw_text)
            log.debug(f"Found {len(data_matches)} data matches")
            for match in data_matches:
                try:
                    import json
                    json_str = '[' + match + ']'
                    arr = json.loads(json_str)
                    if arr and isinstance(arr, list):
                        # 跳过趋势数据（包含 name/value 键的对象）
                        first_obj = arr[0] if len(arr) > 0 else {}
                        if isinstance(first_obj, dict) and ("name" in first_obj and "value" in first_obj):
                            pass
                        else:
                            agg: Dict[str, Dict[str, float]] = {}
                            for obj in arr:
                                if isinstance(obj, dict):
                                    for name, val in obj.items():
                                        # 过滤无关键
                                        if name in ("name", "value"):
                                            continue
                                        base = name
                                        dir_in = False
                                        dir_out = False
                                        if base.endswith(" (Input)"):
                                            dir_in = True
                                            base = base[:-8]
                                        elif base.endswith(" (Output)"):
                                            dir_out = True
                                            base = base[:-9]
                                        if not base or base == "name":
                                            continue
                                        if base not in agg:
                                            agg[base] = {"input_cost": 0.0, "output_cost": 0.0, "extra_cost": 0.0}
                                        try:
                                            amount = float(val)
                                        except Exception:
                                            amount = 0.0
                                        if dir_in:
                                            agg[base]["input_cost"] += amount
                                        elif dir_out:
                                            agg[base]["output_cost"] += amount
                                        else:
                                            # 未标注方向的值累加到额外成本，再合并到总额
                                            agg[base]["extra_cost"] += amount
                            by_model = []
                            for model, costs in agg.items():
                                total = float(costs.get("input_cost", 0.0)) + float(costs.get("output_cost", 0.0)) + float(costs.get("extra_cost", 0.0))
                                by_model.append({
                                    "model": model,
                                    "input_cost": float(costs.get("input_cost", 0.0)),
                                    "output_cost": float(costs.get("output_cost", 0.0)),
                                    "cost": total,
                                })
                            if by_model:
                                by_model.sort(key=lambda x: x.get("cost", 0.0), reverse=True)
                                result["by_model"] = by_model
                                if result["total_cost"] == 0.0:
                                    result["total_cost"] = sum(m.get("cost", 0.0) for m in by_model)
                                log.info(f"Parsed {len(by_model)} models from alternative data source")
                                break
                except Exception as e:
                    log.debug(f"Failed to parse alternative data source: {e}")
                    continue
        
        # 3. 提取消费趋势数据
        chart_matches = re.findall(r'"data":\s*\[((?:\{[^}]+\},?\s*)+)\]', raw_text)
        for match in chart_matches:
            try:
                json_str = f"[{match}]"
                chart_data = json.loads(json_str)
                if chart_data and isinstance(chart_data, list):
                    first_item = chart_data[0] if chart_data else {}
                    if "name" in first_item and "value" in first_item:
                        spend = []
                        for item in chart_data:
                            try:
                                amount = float(item.get("value", 0))
                            except Exception:
                                amount = 0.0
                            spend.append({
                                "date": item.get("name", ""),
                                "amount": amount
                            })
                        total = sum(s.get("amount", 0.0) for s in spend)
                        if total > 20:
                            for s in spend:
                                s["amount"] = round(s["amount"] / 100.0, 8)
                        result["spend_trend"] = spend
                        break
            except (json.JSONDecodeError, ValueError):
                continue
        # 如果 by_model 仍为空，尝试从文本提取模型名（兜底）
        if not result["by_model"]:
            models = _extract_model_names_from_rsc(raw_text)
            if models:
                result["by_model"] = [{"model": m.get("model"), "input_cost": 0.0, "output_cost": 0.0, "cost": 0.0} for m in models]
        
    except Exception as e:
        log.warning(f"Failed to parse cost RSC data: {e}")
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
    window_size: str = "month",
) -> Dict[str, Any]:
    """
    导出成本数据
    
    Args:
        format: 导出格式 (json, csv)
        starting_on: 开始日期
        ending_before: 结束日期
        window_size: 时间窗口
    """
    # 获取成本数据
    cost_data = await get_cost_data(force=True)
    
    if format == "csv":
        csv_lines = ["service,total_cost"]
        by_service = cost_data.get("by_service", [])
        for item in by_service:
            csv_lines.append(f"{item.get('service', '')},{item.get('cost', 0)}")
        total_cost = cost_data.get("total_cost", 0)
        csv_lines.append(f"Total,{total_cost}")
        return {"format": "csv", "data": "\n".join(csv_lines)}
    
    return {"format": "json", "data": cost_data}
_dashboard_client = None

async def _get_dashboard_client():
    """
    获取持久化的 AsyncClient（启用HTTP/2与连接池），减少重复TLS握手与DNS耗时
    """
    global _dashboard_client
    if _dashboard_client is None:
        import httpx
        _dashboard_client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=30),
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
    return _dashboard_client


def _normalize_model_name(name: str) -> str:
    """
    标准化模型名称，解决输入/输出名称不一致的问题
    """
    import re
    
    name = name.strip()
    
    # 匹配 "Claude Sonnet X.X" 格式，转换为 "Claude X.X Sonnet"
    pattern1 = r"^(Claude)\s+(Sonnet|Haiku|Opus)\s+(\d+\.?\d*)$"
    match = re.match(pattern1, name, re.IGNORECASE)
    if match:
        return f"{match.group(1)} {match.group(3)} {match.group(2)}"
    
    # 匹配 "Claude X Sonnet" 格式，保持不变
    pattern2 = r"^(Claude)\s+(\d+\.?\d*)\s+(Sonnet|Haiku|Opus)$"
    match = re.match(pattern2, name, re.IGNORECASE)
    if match:
        return f"{match.group(1)} {match.group(2)} {match.group(3)}"
    
    return name


def _parse_rates_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析费率数据的 RSC 响应
    
    从 billing 页面的 RSC 数据中提取费率表格信息。
    优先解析接口返回的数据，解析失败时返回空结果（由调用方使用 fallback）。
    
    Args:
        data: 包含 'raw' 字段的 RSC 响应数据
    
    Returns:
        解析后的费率数据，包含：
        - speech_to_text: 语音转文本模型费率列表
        - streaming: 流式语音转文本模型费率列表
        - speech_understanding: 语音理解功能费率列表
        - llm_gateway_input: LLM Gateway 输入 token 费率列表
        - llm_gateway_output: LLM Gateway 输出 token 费率列表
    """
    import re
    
    if "raw" not in data:
        return {}
    
    raw_text = data["raw"]
    result = {
        "speech_to_text": [],
        "streaming": [],
        "speech_understanding": [],
        "llm_gateway_input": [],
        "llm_gateway_output": [],
    }
    
    try:
        log.debug(f"Parsing rates RSC data from billing page, length: {len(raw_text)}")
        
        # 1. 首先找到所有分类的位置
        stt_pos = raw_text.find('"children":"Speech-to-Text"')
        streaming_pos = raw_text.find('"children":"Streaming Speech-to-Text"')
        understanding_pos = raw_text.find('"children":"Speech Understanding"')
        llm_input_pos = raw_text.find('"children":["LLM Gateway + LeMUR"," Input Tokens"]')
        llm_output_pos = raw_text.find('"children":["LLM Gateway + LeMUR"," Output Tokens"]')
        
        log.debug(f"Category positions: STT={stt_pos}, Streaming={streaming_pos}, "
                  f"Understanding={understanding_pos}, LLM Input={llm_input_pos}, LLM Output={llm_output_pos}")
        
        # 2. 查找所有费率条目
        # 格式: "$$费率"," ",["$","span",null,{"children":[" / ","单位"]}]
        rate_pattern = r'\"\$\$(\d+\.?\d*)\",\" \",\[\"[^\"]*\",\"span\",null,\{\"children\":\[\" / \",\"([^\"]+)\"\]'
        rate_matches = list(re.finditer(rate_pattern, raw_text))
        log.info(f"Found {len(rate_matches)} rate entries in billing RSC data")
        
        # 3. 对于每个费率，向前查找最近的模型名称
        for rate_match in rate_matches:
            rate_str = rate_match.group(1)
            unit = rate_match.group(2)
            rate_pos = rate_match.start()
            
            try:
                rate = float(rate_str)
            except ValueError:
                continue
            
            # 向前查找最近的 "children":"模型名" (在表格单元格中)
            search_start = max(0, rate_pos - 2000)
            search_text = raw_text[search_start:rate_pos]
            
            # 查找最后一个 "children":"XXX" 模式
            model_pattern = r'\"children\":\"([^\"]+)\"'
            model_matches = list(re.finditer(model_pattern, search_text))
            
            if model_matches:
                # 取最后一个匹配（最接近费率的）
                model_name = model_matches[-1].group(1)
                
                # 过滤掉非模型名称
                skip_names = ["Model", "Rate", "table-row", "table-header", "$undefined", 
                              "rt-", "index-module", "style", "className", "ref", "scope",
                              "Speech-to-Text", "Streaming Speech-to-Text", "Speech Understanding",
                              "LLM Gateway + LeMUR", "Input Tokens", "Output Tokens"]
                
                should_skip = False
                for skip in skip_names:
                    if skip in model_name or model_name.startswith("$") or len(model_name) > 50:
                        should_skip = True
                        break
                
                if should_skip:
                    continue
                
                clean_name = model_name.replace("*", "").strip()
                
                # 标准化模型名称（解决输入/输出名称不一致的问题）
                # 例如：输入是 "Claude Sonnet 3.7"，输出是 "Claude 3.7 Sonnet"
                clean_name = _normalize_model_name(clean_name)
                
                # 根据费率位置和单位判断分类
                # 支持 1K tokens 和 1M tokens 两种单位
                if "tokens" in unit.lower():
                    if llm_output_pos > 0 and rate_pos > llm_output_pos:
                        result["llm_gateway_output"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                    elif llm_input_pos > 0 and rate_pos > llm_input_pos:
                        result["llm_gateway_input"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                elif "hour" in unit:
                    if understanding_pos > 0 and rate_pos > understanding_pos and (llm_input_pos < 0 or rate_pos < llm_input_pos):
                        result["speech_understanding"].append({
                            "feature": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                    elif streaming_pos > 0 and rate_pos > streaming_pos and (understanding_pos < 0 or rate_pos < understanding_pos):
                        result["streaming"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                    elif stt_pos > 0 and rate_pos > stt_pos and (streaming_pos < 0 or rate_pos < streaming_pos):
                        result["speech_to_text"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
        
        # 4. 去重
        for key in result:
            seen = set()
            unique = []
            for item in result[key]:
                name = item.get("model") or item.get("feature")
                if name not in seen:
                    seen.add(name)
                    unique.append(item)
            result[key] = unique
        
        # 5. 按费率排序
        result["llm_gateway_input"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["llm_gateway_output"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["speech_to_text"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["streaming"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["speech_understanding"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        
        log.info(f"Parsed rates from billing: {len(result['speech_to_text'])} STT, {len(result['streaming'])} streaming, "
                 f"{len(result['speech_understanding'])} understanding, {len(result['llm_gateway_input'])} LLM input, "
                 f"{len(result['llm_gateway_output'])} LLM output")
        
    except Exception as e:
        log.error(f"Failed to parse rates RSC data: {e}")
        import traceback
        log.error(f"Traceback: {traceback.format_exc()}")
    
    return result
