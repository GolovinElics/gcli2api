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
    try:
        adapter_val = await (await get_storage_adapter()).get_config("override_env")
        if isinstance(adapter_val, str):
            cfg["override_env"] = adapter_val.lower() in ("true","1","yes","on")
        else:
            cfg["override_env"] = bool(adapter_val)
    except Exception:
        cfg["override_env"] = False
    env_locked = [k for k in ["ASSEMBLY_API_KEYS","ASSEMBLY_API_KEY","USE_ASSEMBLY","API_PASSWORD","PANEL_PASSWORD","PORT","HOST"] if os.getenv(k)]
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
    # 写入
    for k, v in updates.items():
        ok = await adapter.set_config(k, v)
        if not ok:
            log.error(f"Failed to set config: {k}")
            raise HTTPException(status_code=500, detail=f"保存失败: {k}")
    return JSONResponse(content={"saved": list(updates.keys())})


@router.get("/usage/stats")
async def usage_stats(token: str = Depends(authenticate)):
    stats = await get_usage_stats("assemblyai")
    return JSONResponse(content=stats)


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
            if k:
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
    agg["log_summary"] = {"models": models, "keys": keys, "total": {"ok": ok_total, "fail": fail_total}}
    return JSONResponse(content=agg)


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
        if k:
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