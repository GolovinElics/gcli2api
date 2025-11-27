import os
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import re
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from log import log
from config import (
    get_api_password,
    get_panel_password,
    get_assembly_api_key,
    get_assembly_api_keys,
    get_use_assembly,
    get_server_port,
    get_server_host,
)
from .storage_adapter import get_storage_adapter
from .usage_stats import get_usage_stats, get_aggregated_stats, get_usage_stats_instance
from .assembly_client import fetch_assembly_models, get_rate_limit_info


router = APIRouter()
security = HTTPBearer()


async def authenticate(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    token = credentials.credentials
    password = await get_panel_password()
    if token != password:
        # 兼容 API 密码
        api_pwd = await get_api_password()
        if token != api_pwd:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")
    return token


@router.get("/ui")
async def admin_ui():
    base_dir = os.path.dirname(os.path.dirname(__file__))
    file_path = os.path.join(base_dir, "front", "control_panel.html")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                html = f.read()
        except Exception:
            html = "<html><body>无法加载控制面板文件</body></html>"
    else:
        html = "<html><body>控制面板文件未找到</body></html>"
    return HTMLResponse(content=html)


@router.get("/config/get")
async def get_config(token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    cfg: Dict[str, Any] = {}
    # 读取关键配置
    cfg["assembly_api_key"] = await get_assembly_api_key()
    cfg["assembly_api_keys"] = await get_assembly_api_keys()
    cfg["use_assembly"] = await get_use_assembly()
    # 密码
    cfg["api_password"] = await get_api_password()
    cfg["panel_password"] = await get_panel_password()
    cfg["port"] = await get_server_port()
    cfg["host"] = await get_server_host()
    # 其他配置从适配器读取
    try:
        cfg["calls_per_rotation"] = await adapter.get_config("calls_per_rotation", 100)
        cfg["retry_429_enabled"] = await adapter.get_config("retry_429_enabled", True)
        cfg["retry_429_max_retries"] = await adapter.get_config("retry_429_max_retries", 5)
        cfg["retry_429_interval"] = await adapter.get_config("retry_429_interval", 1.0)
        cfg["auto_ban_enabled"] = await adapter.get_config("auto_ban_enabled", False)
        cfg["auto_ban_error_codes"] = await adapter.get_config("auto_ban_error_codes", [401,403])
    except Exception:
        pass
    try:
        adapter_val = await (await get_storage_adapter()).get_config("override_env")
        if isinstance(adapter_val, str):
            cfg["override_env"] = adapter_val.lower() in ("true","1","yes","on")
        else:
            cfg["override_env"] = bool(adapter_val)
    except Exception:
        cfg["override_env"] = False
    env_locked = [k for k in [
        "ASSEMBLY_API_KEYS","ASSEMBLY_API_KEY","USE_ASSEMBLY","API_PASSWORD","PANEL_PASSWORD","PORT","HOST",
        "CALLS_PER_ROTATION","RETRY_429_ENABLED","RETRY_429_MAX_RETRIES","RETRY_429_INTERVAL","AUTO_BAN","AUTO_BAN_ERROR_CODES"
    ] if os.getenv(k)]
    return JSONResponse(content={"config": cfg, "env_locked": env_locked})

@router.get("/config/all")
async def get_all_config(token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    cfg = await adapter.get_all_config()
    backend = "file"
    if os.getenv("REDIS_URI"):
        backend = "redis"
    elif os.getenv("POSTGRES_DSN"):
        backend = "postgres"
    prefix = os.getenv("REDIS_PREFIX", "AMB2API")
    return JSONResponse(content={"backend": backend, "prefix": prefix, "config": cfg})


@router.post("/config/save")
async def save_config(payload: Dict[str, Any], token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    updates = {}
    override_flag = payload.get("override_env")
    if override_flag is not None:
        updates["override_env"] = bool(override_flag)
        ok = await adapter.set_config("override_env", updates["override_env"])
        if not ok:
            raise HTTPException(status_code=500, detail="保存失败: override_env")
    allow_override = updates.get("override_env")
    if allow_override is None:
        try:
            cfg_override = await adapter.get_config("override_env")
            if isinstance(cfg_override, str):
                allow_override = cfg_override.lower() in ("true","1","yes","on")
            else:
                allow_override = bool(cfg_override)
        except Exception:
            allow_override = False
    if payload.get("assembly_api_keys") is not None:
        val = payload.get("assembly_api_keys")
        if isinstance(val, str):
            items = [x.strip() for x in val.replace("\n", ",").split(",") if x.strip()]
        elif isinstance(val, list):
            items = [str(x).strip() for x in val if str(x).strip()]
        else:
            items = []
        updates["assembly_api_keys"] = items
    if payload.get("assembly_api_key") is not None:
        updates["assembly_api_key"] = payload.get("assembly_api_key")
    if payload.get("use_assembly") is not None:
        updates["use_assembly"] = bool(payload.get("use_assembly"))
    if payload.get("api_password") is not None:
        updates["api_password"] = payload.get("api_password")
    if payload.get("panel_password") is not None:
        updates["panel_password"] = payload.get("panel_password")
    if payload.get("password") is not None:
        updates["password"] = payload.get("password")
    if payload.get("port") is not None:
        updates["port"] = int(payload.get("port"))
    if payload.get("host") is not None:
        updates["host"] = str(payload.get("host"))
    # 性能与重试配置
    if payload.get("calls_per_rotation") is not None:
        try:
            updates["calls_per_rotation"] = int(payload.get("calls_per_rotation"))
        except Exception:
            updates["calls_per_rotation"] = 100
    if payload.get("retry_429_enabled") is not None:
        updates["retry_429_enabled"] = bool(payload.get("retry_429_enabled"))
    if payload.get("retry_429_max_retries") is not None:
        try:
            updates["retry_429_max_retries"] = int(payload.get("retry_429_max_retries"))
        except Exception:
            updates["retry_429_max_retries"] = 5
    if payload.get("retry_429_interval") is not None:
        try:
            updates["retry_429_interval"] = float(payload.get("retry_429_interval"))
        except Exception:
            updates["retry_429_interval"] = 1.0
    # 自动封禁配置
    if payload.get("auto_ban_enabled") is not None:
        updates["auto_ban_enabled"] = bool(payload.get("auto_ban_enabled"))
    if payload.get("auto_ban_error_codes") is not None:
        val = payload.get("auto_ban_error_codes")
        codes = []
        try:
            if isinstance(val, str):
                codes = [int(x.strip()) for x in val.split(',') if x.strip()]
            elif isinstance(val, list):
                codes = [int(x) for x in val]
        except Exception:
            codes = [401,403]
        updates["auto_ban_error_codes"] = codes
    # 写入
    for k, v in updates.items():
        ok = await adapter.set_config(k, v)
        if not ok:
            log.error(f"Failed to set config: {k}")
            raise HTTPException(status_code=500, detail=f"保存失败: {k}")
    return JSONResponse(content={"saved": list(updates.keys())})


@router.get("/usage/stats")
async def usage_stats(token: str = Depends(authenticate)):
    stats = await get_usage_stats()
    # 过滤掉无效的key（如 "assemblyai"）
    filtered_stats = {}
    for key, value in stats.items():
        # 只保留以 "key:" 开头的有效统计
        if key.startswith("key:") or key.startswith("creds/"):
            filtered_stats[key] = value
    return JSONResponse(content=filtered_stats)


@router.get("/usage/aggregated")
async def usage_aggregated(model: str = None, key: str = None, only: str = None, limit: int = 0, token: str = Depends(authenticate)):
    agg = await get_aggregated_stats()
    log_file = log.get_log_file()
    models = {}
    keys = {}
    ok_total = 0
    fail_total = 0
    if os.path.exists(log_file):
        pattern = re.compile(r"RES model=([^\s]+)(?: key=([^\s]+))? status=([A-Z]+(?:\([^\)]*\))?)")
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if limit and limit > 0:
                lines = lines[-limit:]
        for ln in lines:
            m = pattern.search(ln)
            if not m:
                continue
            mod = m.group(1)
            k = m.group(2) or ""
            st = m.group(3)
            if model and mod != model:
                continue
            if key and k != key:
                continue
            ok = st.startswith("OK")
            if ok:
                ok_total += 1
            else:
                fail_total += 1
            if mod not in models:
                models[mod] = {"ok": 0, "fail": 0}
            if ok:
                models[mod]["ok"] += 1
            else:
                models[mod]["fail"] += 1
            # 只统计有 key 的记录，忽略空 key
            if k and k.strip():
                if k not in keys:
                    keys[k] = {"ok": 0, "fail": 0, "models": {}, "model_counts": {}}
                if ok:
                    keys[k]["ok"] += 1
                else:
                    keys[k]["fail"] += 1
                if mod not in keys[k]["models"]:
                    keys[k]["models"][mod] = {"ok": 0, "fail": 0}
                if ok:
                    keys[k]["models"][mod]["ok"] += 1
                else:
                    keys[k]["models"][mod]["fail"] += 1
                # Add to model_counts (total calls per model for this key)
                if mod not in keys[k]["model_counts"]:
                    keys[k]["model_counts"][mod] = 0
                keys[k]["model_counts"][mod] += 1
    # 过滤失效 key：不在当前配置中的 key 视为失效
    # 同时确保配置中的所有密钥都显示（即使没有使用记录）
    try:
        adapter = await get_storage_adapter()
        cfg_keys = await adapter.get_config("assembly_api_keys", [])
        if isinstance(cfg_keys, str):
            cfg_keys = [x.strip() for x in cfg_keys.replace("\n", ",").split(",") if x.strip()]
        cfg_set = set(cfg_keys or [])
        # 只显示当前配置中的密钥
        # 构造过滤后的 keys
        filtered = {}
        for kname, kv in keys.items():
            if kname in cfg_set:
                filtered[kname] = kv
        # 添加配置中的密钥（即使没有使用记录）
        for cfg_key in cfg_set:
            if cfg_key not in filtered:
                filtered[cfg_key] = {
                    "ok": 0,
                    "fail": 0,
                    "models": {},
                    "model_counts": {}
                }
        keys = filtered
        
        # 重新计算模型统计，只包含当前有效密钥使用的模型
        filtered_models = {}
        for key_data in keys.values():
            for model_name, model_stats in key_data.get("models", {}).items():
                if model_name not in filtered_models:
                    filtered_models[model_name] = {"ok": 0, "fail": 0}
                filtered_models[model_name]["ok"] += model_stats.get("ok", 0)
                filtered_models[model_name]["fail"] += model_stats.get("fail", 0)
        models = filtered_models
        
        # 重新计算总数
        ok_total = sum(d.get("ok", 0) for d in keys.values())
        fail_total = sum(d.get("fail", 0) for d in keys.values())
    except Exception as e:
        log.warning(f"Failed to filter keys: {e}")
    if only == "success":
        for d in models.values():
            d["fail"] = 0
        for d in keys.values():
            d["fail"] = 0
            for md in d["models"].values():
                md["fail"] = 0
        fail_total = 0
    elif only == "fail":
        for d in models.values():
            d["ok"] = 0
        for d in keys.values():
            d["ok"] = 0
            for md in d["models"].values():
                md["ok"] = 0
        ok_total = 0
    agg["log_summary"] = {"models": models, "keys": keys, "total": {"ok": ok_total, "fail": fail_total}}
    return JSONResponse(content=agg)


@router.get("/models/query")
async def models_query(token: str = Depends(authenticate)):
    """查询上游模型列表并按供应商分类返回（含元数据）"""
    data = await fetch_assembly_models()
    models = [str(m) for m in data.get("models", [])]
    meta = data.get("meta", {})
    # 缓存到配置，便于后续列表和操练场使用
    try:
        adapter = await get_storage_adapter()
        await adapter.set_config("available_models", models)
        await adapter.set_config("available_models_meta", meta)
    except Exception:
        pass
    grouped: Dict[str, Any] = {"Anthropic": [], "OpenAI": [], "Google": [], "Other": []}
    for m in models:
        ms = str(m)
        if ms.startswith("claude"):
            grouped["Anthropic"].append(ms)
        elif ms.startswith("gpt") or ms.startswith("chatgpt"):
            grouped["OpenAI"].append(ms)
        elif ms.startswith("gemini"):
            grouped["Google"].append(ms)
        else:
            grouped["Other"].append(ms)
    return JSONResponse(content={"models": models, "grouped": grouped, "meta": meta})


@router.post("/models/save")
async def models_save(payload: Dict[str, Any], token: str = Depends(authenticate)):
    """保存所选模型到配置"""
    selected = payload.get("selected_models") or []
    if not isinstance(selected, list):
        raise HTTPException(status_code=400, detail="selected_models 必须是数组")
    adapter = await get_storage_adapter()
    ok = await adapter.set_config("available_models_selected", [str(m) for m in selected])
    if not ok:
        raise HTTPException(status_code=500, detail="保存失败: available_models_selected")
    return JSONResponse(content={"saved_count": len(selected)})


@router.post("/usage/update-limits")
async def usage_update_limits(payload: Dict[str, Any], token: str = Depends(authenticate)):
    filename = payload.get("filename")
    gemini_limit = payload.get("gemini_2_5_pro_limit")
    total_limit = payload.get("total_limit")
    stats = await get_usage_stats_instance()
    await stats.update_daily_limits(filename, gemini_limit, total_limit)
    return JSONResponse(content={"message": "限制已更新"})


@router.post("/usage/reset")
async def usage_reset(payload: Dict[str, Any], token: str = Depends(authenticate)):
    filename = payload.get("filename")
    stats = await get_usage_stats_instance()
    await stats.reset_stats(filename)
    return JSONResponse(content={"message": "使用统计已重置"})

@router.get("/storage/info")
async def storage_info(token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    info = await adapter.get_backend_info()
    return JSONResponse(content=info)


@router.get("/usage/summary")
async def usage_summary(model: str = None, key: str = None, only: str = None, limit: int = 0, token: str = Depends(authenticate)):
    log_file = log.get_log_file()
    if not os.path.exists(log_file):
        return JSONResponse(content={"models": {}, "keys": {}, "total": {"ok": 0, "fail": 0}})
    pattern = re.compile(r"RES model=([^\s]+)(?: key=([^\s]+))? status=([A-Z]+(?:\([^\)]*\))?)")
    models = {}
    keys = {}
    ok_total = 0
    fail_total = 0
    lines = []
    with open(log_file, "r", encoding="utf-8") as f:
        if limit and limit > 0:
            lines = f.readlines()[-limit:]
        else:
            lines = f.readlines()
    for ln in lines:
        m = pattern.search(ln)
        if not m:
            continue
        mod = m.group(1)
        k = m.group(2) or ""
        st = m.group(3)
        if model and mod != model:
            continue
        if key and k != key:
            continue
        ok = st.startswith("OK")
        if ok:
            ok_total += 1
        else:
            fail_total += 1
        if mod not in models:
            models[mod] = {"ok": 0, "fail": 0}
        if ok:
            models[mod]["ok"] += 1
        else:
            models[mod]["fail"] += 1
        # 只统计有 key 的记录，忽略空 key
        if k and k.strip():
            if k not in keys:
                keys[k] = {"ok": 0, "fail": 0, "models": {}}
            if ok:
                keys[k]["ok"] += 1
            else:
                keys[k]["fail"] += 1
            if mod not in keys[k]["models"]:
                keys[k]["models"][mod] = {"ok": 0, "fail": 0}
            if ok:
                keys[k]["models"][mod]["ok"] += 1
            else:
                keys[k]["models"][mod]["fail"] += 1
    if only == "success":
        for d in models.values():
            d["fail"] = 0
        for d in keys.values():
            d["fail"] = 0
            for md in d["models"].values():
                md["fail"] = 0
        fail_total = 0
    elif only == "fail":
        for d in models.values():
            d["ok"] = 0
        for d in keys.values():
            d["ok"] = 0
            for md in d["models"].values():
                md["ok"] = 0
        ok_total = 0
    return JSONResponse(content={"models": models, "keys": keys, "total": {"ok": ok_total, "fail": fail_total}})


@router.websocket("/auth/logs/stream")
async def logs_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        token = websocket.query_params.get("token")
        panel_pwd = await get_panel_password()
        api_pwd = await get_api_password()
        if token not in (panel_pwd, api_pwd):
            await websocket.send_text("[ERROR] 未授权的日志访问")
            await websocket.close(code=1008)
            return
        log_file = log.get_log_file()
        pos = 0
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
        except Exception:
            pos = 0
        while True:
            await asyncio.sleep(0.5)
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    f.seek(pos)
                    data = f.read()
                    if data:
                        lines = data.splitlines()
                        for ln in lines:
                            await websocket.send_text(ln)
                        pos = f.tell()
            except FileNotFoundError:
                await websocket.send_text("[INFO] 日志文件未找到")
            except Exception:
                # 避免泄露错误细节
                await websocket.send_text("[ERROR] 读取日志失败")
    except WebSocketDisconnect:
        return


@router.get("/auth/logs/download")
async def logs_download(token: str = Depends(authenticate)):
    log_file = log.get_log_file()
    if not os.path.exists(log_file):
        raise HTTPException(status_code=404, detail="日志文件不存在")
    headers = {"Content-Disposition": "attachment; filename=amb2api_logs.txt"}
    return FileResponse(log_file, media_type="text/plain", headers=headers)


@router.post("/auth/logs/clear")
async def logs_clear(token: str = Depends(authenticate)):
    log_file = log.get_log_file()
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("")
        return JSONResponse(content={"message": "日志已清空"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空失败: {e}")
@router.post("/auth/login")
async def login(payload: Dict[str, Any]):
    password = str(payload.get("password", ""))
    panel_pwd = await get_panel_password()
    if password == panel_pwd:
        return JSONResponse(content={"token": password})
    api_pwd = await get_api_password()
    if password == api_pwd:
        return JSONResponse(content={"token": password})
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")


@router.get("/rate-limits")
async def rate_limits(token: str = Depends(authenticate)):
    """获取所有API Key的速率限制信息"""
    rate_info = await get_rate_limit_info()
    
    # 获取配置的所有keys用于显示完整列表
    keys = await get_assembly_api_keys()
    
    # 构建完整的速率限制信息
    result = []
    for idx, key in enumerate(keys):
        from .assembly_client import _mask_key
        masked = _mask_key(key)
        
        if idx in rate_info:
            info = rate_info[idx]
            result.append({
                "index": idx,
                "key": masked,
                "limit": info.get("limit", 0),
                "remaining": info.get("remaining", 0),
                "used": info.get("used", 0),
                "reset_in_seconds": info.get("reset_in_seconds", 0),
                "last_request_time": info.get("last_request_time", 0),
                "status": "active" if info.get("remaining", 0) > 0 else "exhausted"
            })
        else:
            # 未使用过的key
            result.append({
                "index": idx,
                "key": masked,
                "limit": 0,
                "remaining": 0,
                "used": 0,
                "reset_in_seconds": 0,
                "last_request_time": 0,
                "status": "unused"
            })
    
    return JSONResponse(content={"rate_limits": result})
@router.get("/keys/invalid")
async def invalid_keys(token: str = Depends(authenticate)):
    """
    列出失效 key
    
    失效判断逻辑：
    1. 配置中的密钥，但在日志中只有失败记录（fail > 0 且 ok == 0）
    2. 不在配置中但在日志中出现的密钥（已删除的密钥）
    """
    log_file = log.get_log_file()
    found: Dict[str, Dict[str, int]] = {}
    if os.path.exists(log_file):
        pattern = re.compile(r"RES model=([^\s]+)(?: key=([^\s]+))? status=([A-Z]+(?:\([^\)]*\))?)")
        with open(log_file, "r", encoding="utf-8") as f:
            for ln in f:
                m = pattern.search(ln)
                if not m:
                    continue
                k = m.group(2) or ""
                if not k:
                    continue
                if k not in found:
                    found[k] = {"ok": 0, "fail": 0}
                if (m.group(3) or "").startswith("OK"):
                    found[k]["ok"] += 1
                else:
                    found[k]["fail"] += 1
    
    adapter = await get_storage_adapter()
    cfg_keys = await adapter.get_config("assembly_api_keys", [])
    if isinstance(cfg_keys, str):
        cfg_keys = [x.strip() for x in cfg_keys.replace("\n", ",").split(",") if x.strip()]
    cfg_set = set(cfg_keys or [])
    # 只检查当前配置中的密钥
    
    invalid = []
    
    # 1. 检查配置中的密钥是否失效（有失败记录但没有成功记录）
    # 注意：只有当失败次数较多（>=3次）且没有成功记录时，才判断为失效
    # 这样可以避免偶尔的失败导致密钥被误判为失效
    for cfg_key in cfg_set:
        if cfg_key in found:
            kv = found[cfg_key]
            ok_count = kv.get("ok", 0)
            fail_count = kv.get("fail", 0)
            
            # 失效条件：失败次数 >= 3 且没有成功记录
            # 或者：失败次数 > 成功次数的10倍（说明成功率极低）
            is_invalid = False
            reason = ""
            
            if fail_count >= 3 and ok_count == 0:
                is_invalid = True
                reason = f"只有失败记录（{fail_count}次），无成功记录"
            elif ok_count > 0 and fail_count > ok_count * 10:
                is_invalid = True
                reason = f"成功率极低（成功{ok_count}次，失败{fail_count}次）"
            
            # 调试日志
            log.debug(f"Checking key {cfg_key[:8]}...{cfg_key[-4:]}: ok={ok_count}, fail={fail_count}, is_invalid={is_invalid}")
            
            if is_invalid:
                invalid.append({
                    "key": cfg_key,
                    "ok": ok_count,
                    "fail": fail_count,
                    "is_configured": True,
                    "ignored": False,
                    "status": "invalid/configured",
                    "reason": reason
                })
    
    # 2. 不再检查已删除的密钥，因为它们已经被删除了
    # 只返回当前配置中的无效密钥
    
    return JSONResponse(content={"invalid_keys": invalid, "ignored": []})


@router.post("/keys/delete-invalid")
async def delete_invalid_keys(token: str = Depends(authenticate)):
    """
    批量删除失效密钥数据
    
    操作内容：
    1. 从日志文件中删除失效密钥的所有记录
    2. 清理失效密钥的统计数据
    3. 将失效密钥加入忽略列表（防止再次出现）
    """
    adapter = await get_storage_adapter()
    
    # 1. 识别失效密钥
    inv = await invalid_keys(token)
    data = inv.body if hasattr(inv, "body") else inv
    invalid_list = []
    try:
        if isinstance(data, dict):
            invalid_list = [item.get("key") for item in data.get("invalid_keys", [])]
        else:
            import json
            data_dict = json.loads(data)
            invalid_list = [item.get("key") for item in data_dict.get("invalid_keys", [])]
    except Exception as e:
        log.warning(f"Failed to parse invalid keys: {e}")
        invalid_list = []
    
    if not invalid_list:
        return JSONResponse(content={"success": True, "ignored_count": 0, "invalid_deleted": 0, "log_lines_removed": 0})
    
    # 2. 从日志文件中删除失效密钥的记录
    log_file = log.get_log_file()
    lines_removed = 0
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            # 过滤掉包含失效密钥的行
            new_lines = []
            for line in lines:
                should_keep = True
                for invalid_key in invalid_list:
                    if f"key={invalid_key}" in line:
                        should_keep = False
                        lines_removed += 1
                        break
                if should_keep:
                    new_lines.append(line)
            
            # 写回日志文件
            with open(log_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            
            log.info(f"Removed {lines_removed} log lines for {len(invalid_list)} invalid keys")
        except Exception as e:
            log.error(f"Failed to clean log file: {e}")
    
    # 3. 不再保存 invalid_keys_ignored，直接删除
    # 清空忽略列表，因为我们已经删除了无效密钥
    await adapter.delete_config("invalid_keys_ignored")
    
    # 4. 清理 StatsTracker 非活跃索引统计
    try:
        from .key_manager import get_key_manager
        from .stats_tracker import get_stats_tracker
        key_manager = await get_key_manager()
        stats_tracker = await get_stats_tracker()
        all_keys = await key_manager.get_all_keys()
        active_indices = [key.index for key in all_keys]
        await stats_tracker.cleanup_inactive_keys(active_indices)
    except Exception as e:
        log.warning(f"Failed to cleanup stats: {e}")
    
    return JSONResponse(content={
        "success": True,
        "ignored_count": len(new_ignore),
        "invalid_deleted": len(invalid_list),
        "log_lines_removed": lines_removed
    })
