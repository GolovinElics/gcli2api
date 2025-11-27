"""
统计跟踪器模块
统计密钥使用情况和活跃状态
"""
import time
import asyncio
from typing import Dict, List, Optional, Any

from log import log
from .models_key import KeyStats, KeyInfo, RateLimitInfo, KeyStatus
from .storage_adapter import get_storage_adapter


class StatsTracker:
    """统计跟踪器"""
    
    def __init__(self):
        self._stats: Dict[int, Dict[str, Any]] = {}  # 统计数据缓存
        self._initialized = False
        self._save_lock = asyncio.Lock()
        self._dirty = False
        self._last_save_time = 0
        self._save_interval = 30  # 保存间隔（秒）
    
    async def initialize(self):
        """初始化统计跟踪器"""
        if self._initialized:
            return
        await self._load_stats()
        self._initialized = True
        
        # 自动清理无效密钥的统计数据
        await self._auto_cleanup_invalid_keys()
    
    async def _auto_cleanup_invalid_keys(self):
        """自动清理无效密钥的统计数据"""
        try:
            from .key_manager import get_key_manager
            key_manager = await get_key_manager()
            
            # 获取当前所有有效密钥的索引
            all_keys = await key_manager.get_all_keys()
            valid_indices = {key.index for key in all_keys}
            
            # 找出需要清理的统计数据
            stats_to_remove = [idx for idx in self._stats.keys() if idx not in valid_indices]
            
            if stats_to_remove:
                for idx in stats_to_remove:
                    del self._stats[idx]
                
                # 保存清理后的数据
                await self._save_stats(force=True)
                log.info(f"Auto-cleaned stats for {len(stats_to_remove)} invalid keys: {stats_to_remove}")
            else:
                log.debug("No invalid key stats to clean")
                
        except Exception as e:
            log.warning(f"Failed to auto-cleanup invalid key stats: {e}")
    
    async def _load_stats(self):
        """从存储加载统计数据"""
        try:
            adapter = await get_storage_adapter()
            data = await adapter.get_config("key_stats", {})
            
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        idx = int(k)
                        if isinstance(v, dict):
                            self._stats[idx] = v
                    except (ValueError, TypeError):
                        continue
            
            log.debug(f"Loaded stats for {len(self._stats)} keys")
        except Exception as e:
            log.error(f"Failed to load key stats: {e}")
    
    async def _save_stats(self, force: bool = False):
        """保存统计数据到存储"""
        current_time = time.time()
        
        # 检查是否需要保存
        if not force and not self._dirty:
            return
        if not force and current_time - self._last_save_time < self._save_interval:
            return
        
        async with self._save_lock:
            try:
                adapter = await get_storage_adapter()
                await adapter.set_config("key_stats", {str(k): v for k, v in self._stats.items()})
                self._dirty = False
                self._last_save_time = current_time
                log.debug(f"Saved stats for {len(self._stats)} keys")
            except Exception as e:
                log.error(f"Failed to save key stats: {e}")
    
    async def record_call(
        self, 
        key_index: int, 
        success: bool, 
        model: str,
        masked_key: str = ""
    ):
        """
        记录 API 调用
        
        Args:
            key_index: 密钥索引
            success: 是否成功
            model: 模型名称
            masked_key: 脱敏密钥
        """
        if not self._initialized:
            await self.initialize()
        
        if key_index not in self._stats:
            self._stats[key_index] = {
                "success_count": 0,
                "failure_count": 0,
                "model_counts": {},
                "masked_key": masked_key,
                "last_call_time": 0,
            }
        
        stats = self._stats[key_index]
        
        if success:
            stats["success_count"] = stats.get("success_count", 0) + 1
        else:
            stats["failure_count"] = stats.get("failure_count", 0) + 1
        
        # 更新模型计数
        model_counts = stats.get("model_counts", {})
        model_counts[model] = model_counts.get(model, 0) + 1
        stats["model_counts"] = model_counts
        
        # 更新最后调用时间
        stats["last_call_time"] = time.time()
        
        # 更新脱敏密钥
        if masked_key:
            stats["masked_key"] = masked_key
        
        self._dirty = True
        
        log.debug(f"Recorded {'success' if success else 'failure'} call for key {key_index}, model={model}")
        
        # 异步保存
        asyncio.create_task(self._save_stats())
    
    async def get_key_stats(
        self, 
        key_index: int,
        key_info: Optional[KeyInfo] = None,
        rate_limit_info: Optional[RateLimitInfo] = None
    ) -> KeyStats:
        """
        获取密钥统计信息
        
        Args:
            key_index: 密钥索引
            key_info: 密钥信息（可选）
            rate_limit_info: 速率限制信息（可选）
        
        Returns:
            密钥统计信息
        """
        if not self._initialized:
            await self.initialize()
        
        stats = self._stats.get(key_index, {})
        
        return KeyStats(
            key_index=key_index,
            masked_key=stats.get("masked_key", key_info.masked_key if key_info else ""),
            enabled=key_info.enabled if key_info else True,
            success_count=stats.get("success_count", 0),
            failure_count=stats.get("failure_count", 0),
            model_counts=stats.get("model_counts", {}),
            rate_limit_info=rate_limit_info,
        )
    
    async def get_all_stats(
        self, 
        keys: List[KeyInfo],
        rate_limits: Dict[int, RateLimitInfo] = None,
        group_by_status: bool = True
    ) -> Dict[str, Any]:
        """
        获取所有统计信息
        
        Args:
            keys: 密钥列表
            rate_limits: 速率限制信息
            group_by_status: 是否按状态分组
        
        Returns:
            统计信息字典
        """
        if not self._initialized:
            await self.initialize()
        
        if rate_limits is None:
            rate_limits = {}
        
        all_stats = []
        enabled_stats = []
        disabled_stats = []
        
        for key in keys:
            rate_info = rate_limits.get(key.index)
            stats = await self.get_key_stats(key.index, key, rate_info)
            all_stats.append(stats)
            
            if key.enabled:
                enabled_stats.append(stats)
            else:
                disabled_stats.append(stats)
        
        # 计算汇总
        total_success = sum(s.success_count for s in all_stats)
        total_failure = sum(s.failure_count for s in all_stats)
        active_count = len(enabled_stats)
        disabled_count = len(disabled_stats)
        
        result = {
            "total_keys": len(keys),
            "active_keys": active_count,
            "disabled_keys": disabled_count,
            "total_success": total_success,
            "total_failure": total_failure,
            "total_calls": total_success + total_failure,
        }
        
        if group_by_status:
            result["enabled"] = [s.to_dict() for s in enabled_stats]
            result["disabled"] = [s.to_dict() for s in disabled_stats]
        else:
            result["keys"] = [s.to_dict() for s in all_stats]
        
        return result
    
    async def get_active_keys_stats(
        self, 
        keys: List[KeyInfo],
        rate_limits: Dict[int, RateLimitInfo] = None
    ) -> Dict[str, Any]:
        """
        获取活跃密钥统计
        
        Args:
            keys: 密钥列表
            rate_limits: 速率限制信息
        
        Returns:
            活跃密钥统计
        """
        if not self._initialized:
            await self.initialize()
        
        if rate_limits is None:
            rate_limits = {}
        
        enabled_keys = [k for k in keys if k.enabled]
        stats = []
        
        for key in enabled_keys:
            rate_info = rate_limits.get(key.index)
            key_stats = await self.get_key_stats(key.index, key, rate_info)
            stats.append(key_stats)
        
        total_success = sum(s.success_count for s in stats)
        total_failure = sum(s.failure_count for s in stats)
        
        return {
            "active_keys": len(enabled_keys),
            "total_success": total_success,
            "total_failure": total_failure,
            "total_calls": total_success + total_failure,
            "keys": [s.to_dict() for s in stats],
        }
    
    async def reset_stats(self, key_index: Optional[int] = None):
        """
        重置统计数据
        
        Args:
            key_index: 密钥索引，如果为 None 则重置所有
        """
        if not self._initialized:
            await self.initialize()
        
        if key_index is not None:
            if key_index in self._stats:
                self._stats[key_index] = {
                    "success_count": 0,
                    "failure_count": 0,
                    "model_counts": {},
                    "masked_key": self._stats[key_index].get("masked_key", ""),
                    "last_call_time": 0,
                }
                log.info(f"Reset stats for key {key_index}")
        else:
            for idx in self._stats:
                self._stats[idx] = {
                    "success_count": 0,
                    "failure_count": 0,
                    "model_counts": {},
                    "masked_key": self._stats[idx].get("masked_key", ""),
                    "last_call_time": 0,
                }
            log.info("Reset all key stats")
        
        self._dirty = True
        await self._save_stats(force=True)
    
    async def cleanup_inactive_keys(self, active_indices: List[int]):
        """
        清理非活跃密钥的统计数据
        
        Args:
            active_indices: 活跃密钥索引列表
        """
        if not self._initialized:
            await self.initialize()
        
        inactive = [idx for idx in self._stats if idx not in active_indices]
        for idx in inactive:
            del self._stats[idx]
        
        if inactive:
            log.info(f"Cleaned up stats for {len(inactive)} inactive keys")
            self._dirty = True
            await self._save_stats(force=True)


# 全局实例
_stats_tracker: Optional[StatsTracker] = None


async def get_stats_tracker() -> StatsTracker:
    """获取全局统计跟踪器实例"""
    global _stats_tracker
    if _stats_tracker is None:
        _stats_tracker = StatsTracker()
        await _stats_tracker.initialize()
    return _stats_tracker
