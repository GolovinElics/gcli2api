"""
密钥管理 API 端点模块
提供密钥的增删改查、状态管理和导入导出功能
"""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from log import log
from .key_manager import get_key_manager
from .rate_limiter import get_rate_limiter
from .stats_tracker import get_stats_tracker
from .models_key import AggregationMode


router = APIRouter(prefix="/api/keys", tags=["Key Management"])


# Request/Response Models
class AddKeysRequest(BaseModel):
    """添加密钥请求"""
    keys: List[str] = Field(..., description="密钥列表")
    mode: str = Field("append", description="添加模式: append 或 override")


class UpdateKeyStatusRequest(BaseModel):
    """更新密钥状态请求"""
    enabled: bool = Field(..., description="是否启用")


class BatchUpdateStatusRequest(BaseModel):
    """批量更新状态请求"""
    indices: List[int] = Field(..., description="密钥索引列表")
    enabled: bool = Field(..., description="是否启用")


class SetAggregationModeRequest(BaseModel):
    """设置聚合模式请求"""
    mode: str = Field(..., description="聚合模式: round_robin 或 random")


class SetCallsPerRotationRequest(BaseModel):
    """设置轮换次数请求"""
    calls: int = Field(..., ge=1, description="轮换次数")


class ImportKeysRequest(BaseModel):
    """导入密钥请求"""
    data: Dict[str, Any] = Field(..., description="导入数据")
    mode: str = Field("append", description="导入模式: append 或 override")


class KeyResponse(BaseModel):
    """密钥响应"""
    index: int
    key: str
    masked_key: str
    enabled: bool
    status: str
    success_count: int = 0
    failure_count: int = 0
    rate_limit: Optional[Dict[str, Any]] = None


class KeyListResponse(BaseModel):
    """密钥列表响应"""
    total: int
    active: int
    disabled: int
    keys: List[KeyResponse]


# API Endpoints
@router.get("", response_model=KeyListResponse)
async def get_all_keys(
    status_filter: Optional[str] = Query(None, description="状态过滤: enabled, disabled, all"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    sort_by: Optional[str] = Query("index", description="排序字段: index, status"),
    sort_order: Optional[str] = Query("asc", description="排序顺序: asc, desc")
):
    """获取所有密钥信息"""
    try:
        key_manager = await get_key_manager()
        rate_limiter = await get_rate_limiter()
        stats_tracker = await get_stats_tracker()
        
        all_keys = await key_manager.get_all_keys()
        rate_limits = await rate_limiter.get_all_rate_limits()
        
        # 构建响应
        keys_response = []
        for key_info in all_keys:
            # 获取统计信息
            key_stats = await stats_tracker.get_key_stats(key_info.index, key_info)
            
            # 获取速率限制信息
            rate_info = rate_limits.get(key_info.index)
            rate_limit_data = None
            if rate_info:
                rate_limit_data = {
                    "limit": rate_info.limit,
                    "remaining": rate_info.remaining,
                    "used": rate_info.used,
                    "reset_in_seconds": rate_info.reset_in_seconds,
                    "status": rate_info.status.value
                }
            
            keys_response.append(KeyResponse(
                index=key_info.index,
                key=key_info.key,
                masked_key=key_info.masked_key,
                enabled=key_info.enabled,
                status=key_info.status.value,
                success_count=key_stats.success_count,
                failure_count=key_stats.failure_count,
                rate_limit=rate_limit_data
            ))
        
        # 应用过滤
        if status_filter == "enabled":
            keys_response = [k for k in keys_response if k.enabled]
        elif status_filter == "disabled":
            keys_response = [k for k in keys_response if not k.enabled]
        elif status_filter == "invalid":
            # 失效密钥：未启用且状态为exhausted或有错误
            keys_response = [k for k in keys_response if not k.enabled and (k.status in ["exhausted", "invalid"] or k.failure_count > 0)]
        
        # 应用搜索
        if search:
            search_lower = search.lower()
            keys_response = [k for k in keys_response if search_lower in k.masked_key.lower()]
        
        # 应用排序
        reverse = sort_order == "desc"
        if sort_by == "status":
            keys_response.sort(key=lambda k: (not k.enabled, k.index), reverse=reverse)
        else:
            keys_response.sort(key=lambda k: k.index, reverse=reverse)
        
        # 统计
        total = len(all_keys)
        active = sum(1 for k in all_keys if k.enabled)
        disabled = total - active
        
        return KeyListResponse(
            total=total,
            active=active,
            disabled=disabled,
            keys=keys_response
        )
    except Exception as e:
        log.error(f"Failed to get keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def add_keys(request: AddKeysRequest):
    """添加密钥"""
    try:
        key_manager = await get_key_manager()
        
        if request.mode not in ["append", "override"]:
            raise HTTPException(status_code=400, detail="Invalid mode. Use 'append' or 'override'")
        
        success = await key_manager.add_keys(request.keys, request.mode)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to add keys")
        
        return {"success": True, "message": f"Added {len(request.keys)} keys in {request.mode} mode"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to add keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{index}/status")
async def update_key_status(index: int, request: UpdateKeyStatusRequest):
    """更新单个密钥状态"""
    try:
        key_manager = await get_key_manager()
        
        success = await key_manager.update_key_status(index, request.enabled)
        
        if not success:
            raise HTTPException(status_code=404, detail=f"Key at index {index} not found")
        
        return {"success": True, "message": f"Key {index} {'enabled' if request.enabled else 'disabled'}"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to update key status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch-status")
async def batch_update_status(request: BatchUpdateStatusRequest):
    """批量更新密钥状态"""
    try:
        key_manager = await get_key_manager()
        
        success = await key_manager.batch_update_status(request.indices, request.enabled)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to batch update status")
        
        return {
            "success": True, 
            "message": f"{'Enabled' if request.enabled else 'Disabled'} {len(request.indices)} keys"
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to batch update status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{index}")
async def delete_key(index: int):
    """删除密钥"""
    try:
        key_manager = await get_key_manager()
        
        success = await key_manager.delete_key(index)
        
        if not success:
            raise HTTPException(status_code=404, detail=f"Key at index {index} not found")
        
        return {"success": True, "message": f"Key {index} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to delete key: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
async def search_keys(
    q: str = Query(..., description="搜索关键词"),
    status: Optional[str] = Query(None, description="状态过滤: enabled, disabled"),
    sort_by: Optional[str] = Query("index", description="排序字段"),
    limit: int = Query(50, ge=1, le=100, description="返回数量限制")
):
    """搜索密钥"""
    try:
        key_manager = await get_key_manager()
        
        all_keys = await key_manager.get_all_keys()
        
        # 搜索
        q_lower = q.lower()
        results = [k for k in all_keys if q_lower in k.masked_key.lower() or q_lower in k.key.lower()]
        
        # 状态过滤
        if status == "enabled":
            results = [k for k in results if k.enabled]
        elif status == "disabled":
            results = [k for k in results if not k.enabled]
        
        # 排序
        results.sort(key=lambda k: k.index)
        
        # 限制数量
        results = results[:limit]
        
        return {
            "total": len(results),
            "keys": [
                {
                    "index": k.index,
                    "masked_key": k.masked_key,
                    "enabled": k.enabled,
                    "status": k.status.value
                }
                for k in results
            ]
        }
    except Exception as e:
        log.error(f"Failed to search keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export")
async def export_keys():
    """导出密钥配置"""
    try:
        key_manager = await get_key_manager()
        
        exported = await key_manager.export_keys()
        
        return exported
    except Exception as e:
        log.error(f"Failed to export keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/import")
async def import_keys(request: ImportKeysRequest):
    """导入密钥配置"""
    try:
        key_manager = await get_key_manager()
        
        if request.mode not in ["append", "override"]:
            raise HTTPException(status_code=400, detail="Invalid mode. Use 'append' or 'override'")
        
        success = await key_manager.import_keys(request.data, request.mode)
        
        if not success:
            raise HTTPException(status_code=400, detail="Failed to import keys. Check data format.")
        
        return {"success": True, "message": f"Keys imported in {request.mode} mode"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to import keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_key_config():
    """获取密钥配置"""
    try:
        key_manager = await get_key_manager()
        
        mode = await key_manager.get_aggregation_mode()
        calls = await key_manager.get_calls_per_rotation()
        active_count = await key_manager.get_active_keys_count()
        
        return {
            "aggregation_mode": mode.value,
            "calls_per_rotation": calls,
            "active_keys_count": active_count
        }
    except Exception as e:
        log.error(f"Failed to get key config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/config/aggregation-mode")
async def set_aggregation_mode(request: SetAggregationModeRequest):
    """设置聚合模式"""
    try:
        key_manager = await get_key_manager()
        
        try:
            mode = AggregationMode(request.mode)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid mode: {request.mode}")
        
        success = await key_manager.set_aggregation_mode(mode)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to set aggregation mode")
        
        return {"success": True, "message": f"Aggregation mode set to {mode.value}"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to set aggregation mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/config/calls-per-rotation")
async def set_calls_per_rotation(request: SetCallsPerRotationRequest):
    """设置轮换次数"""
    try:
        key_manager = await get_key_manager()
        
        success = await key_manager.set_calls_per_rotation(request.calls)
        
        if not success:
            raise HTTPException(status_code=400, detail="Invalid calls per rotation value")
        
        return {"success": True, "message": f"Calls per rotation set to {request.calls}"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to set calls per rotation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_key_stats():
    """获取密钥统计信息"""
    try:
        key_manager = await get_key_manager()
        rate_limiter = await get_rate_limiter()
        stats_tracker = await get_stats_tracker()
        
        all_keys = await key_manager.get_all_keys()
        rate_limits = await rate_limiter.get_all_rate_limits()
        
        stats = await stats_tracker.get_all_stats(all_keys, rate_limits, group_by_status=True)
        
        return stats
    except Exception as e:
        log.error(f"Failed to get key stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cleanup-stats")
async def cleanup_key_stats():
    """
    清理过期密钥的统计数据
    
    删除不再存在的密钥的统计数据，保持数据一致性
    """
    try:
        key_manager = await get_key_manager()
        stats_tracker = await get_stats_tracker()
        
        # 获取当前所有密钥
        all_keys = await key_manager.get_all_keys()
        active_indices = [key.index for key in all_keys]
        
        # 清理非活跃密钥的统计数据
        await stats_tracker.cleanup_inactive_keys(active_indices)
        
        return {
            "success": True,
            "message": f"Cleaned up stats, {len(active_indices)} active keys remaining"
        }
    except Exception as e:
        log.error(f"Failed to cleanup key stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))
