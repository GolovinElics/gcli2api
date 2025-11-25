"""
速率限制管理器模块
跟踪和管理 API 密钥的速率限制状态
"""
import time
import asyncio
from typing import Dict, List, Optional, Any

from log import log
from .models_key import RateLimitInfo, KeyStatus
from .storage_adapter import get_storage_adapter


class RateLimiter:
    """速率限制管理器"""
    
    def __init__(self):
        self._rate_limits: Dict[int, RateLimitInfo] = {}  # 速率限制信息缓存
        self._initialized = False
        self._save_lock = asyncio.Lock()
    
    async def initialize(self):
        """初始化速率限制管理器"""
        if self._initialized:
            return
        await self._load_rate_limits()
        self._initialized = True
    
    async def _load_rate_limits(self):
        """从存储加载速率限制信息"""
        try:
            adapter = await get_storage_adapter()
            data = await adapter.get_config("rate_limit_info", {})
            
            if isinstance(data, dict):
                for k, v in data.items():
                    try:
                        idx = int(k)
                        if isinstance(v, dict):
                            self._rate_limits[idx] = RateLimitInfo.from_dict({
                                "key_index": idx,
                                **v
                            })
                    except (ValueError, TypeError):
                        continue
            
            log.debug(f"Loaded rate limit info for {len(self._rate_limits)} keys")
        except Exception as e:
            log.error(f"Failed to load rate limit info: {e}")
    
    async def _save_rate_limits(self):
        """保存速率限制信息到存储"""
        async with self._save_lock:
            try:
                adapter = await get_storage_adapter()
                data = {str(k): v.to_dict() for k, v in self._rate_limits.items()}
                await adapter.set_config("rate_limit_info", data)
                log.debug(f"Saved rate limit info for {len(self._rate_limits)} keys")
            except Exception as e:
                log.error(f"Failed to save rate limit info: {e}")
    
    async def update_rate_limit(
        self, 
        key_index: int, 
        limit: int, 
        remaining: int, 
        reset_time: int
    ):
        """
        更新速率限制信息
        
        Args:
            key_index: 密钥索引
            limit: 总限制
            remaining: 剩余配额
            reset_time: 重置时间（Unix时间戳或秒数）
        """
        if not self._initialized:
            await self.initialize()
        
        current_time = time.time()
        
        # 如果 reset_time 是秒数（小于当前时间戳的一半），转换为时间戳
        if reset_time > 0 and reset_time < current_time / 2:
            reset_time = int(current_time + reset_time)
        
        # 计算距离重置的秒数
        reset_in_seconds = max(0, reset_time - int(current_time)) if reset_time > 0 else 0
        
        # 确定状态
        if remaining <= 0:
            status = KeyStatus.EXHAUSTED
        else:
            status = KeyStatus.ACTIVE
        
        info = RateLimitInfo(
            key_index=key_index,
            limit=limit,
            remaining=remaining,
            used=limit - remaining,
            reset_time=reset_time,
            reset_in_seconds=reset_in_seconds,
            status=status,
        )
        
        self._rate_limits[key_index] = info
        
        log.debug(f"Updated rate limit for key {key_index}: limit={limit}, remaining={remaining}, reset_in={reset_in_seconds}s")
        
        # 异步保存
        asyncio.create_task(self._save_rate_limits())
    
    async def is_key_exhausted(self, key_index: int) -> bool:
        """
        检查密钥是否已用尽
        
        Args:
            key_index: 密钥索引
        
        Returns:
            是否已用尽
        """
        if not self._initialized:
            await self.initialize()
        
        # 先检查是否需要重置
        await self.reset_key_if_time_reached(key_index)
        
        info = self._rate_limits.get(key_index)
        if not info:
            return False
        
        return info.status == KeyStatus.EXHAUSTED or info.remaining <= 0
    
    async def get_rate_limit_info(self, key_index: int) -> Optional[RateLimitInfo]:
        """
        获取速率限制信息
        
        Args:
            key_index: 密钥索引
        
        Returns:
            速率限制信息
        """
        if not self._initialized:
            await self.initialize()
        
        # 先检查是否需要重置
        await self.reset_key_if_time_reached(key_index)
        
        info = self._rate_limits.get(key_index)
        if info:
            # 更新 reset_in_seconds
            current_time = time.time()
            info.reset_in_seconds = max(0, info.reset_time - int(current_time)) if info.reset_time > 0 else 0
        
        return info
    
    async def get_all_rate_limits(self) -> Dict[int, RateLimitInfo]:
        """获取所有速率限制信息"""
        if not self._initialized:
            await self.initialize()
        
        current_time = time.time()
        result = {}
        
        for key_index, info in self._rate_limits.items():
            # 检查是否需要重置
            if info.reset_time > 0 and current_time >= info.reset_time:
                info.remaining = info.limit
                info.used = 0
                info.status = KeyStatus.ACTIVE
                info.reset_in_seconds = 0
            else:
                info.reset_in_seconds = max(0, info.reset_time - int(current_time)) if info.reset_time > 0 else 0
            
            result[key_index] = info
        
        return result
    
    async def reset_key_if_time_reached(self, key_index: int) -> bool:
        """
        如果重置时间到达，重置密钥状态
        
        Args:
            key_index: 密钥索引
        
        Returns:
            是否进行了重置
        """
        if not self._initialized:
            await self.initialize()
        
        info = self._rate_limits.get(key_index)
        if not info:
            return False
        
        current_time = time.time()
        
        if info.reset_time > 0 and current_time >= info.reset_time:
            # 重置
            info.remaining = info.limit
            info.used = 0
            info.status = KeyStatus.ACTIVE
            info.reset_in_seconds = 0
            
            log.info(f"Key {key_index} rate limit reset (limit={info.limit})")
            
            # 异步保存
            asyncio.create_task(self._save_rate_limits())
            return True
        
        return False
    
    async def get_next_available_key(self, key_indices: List[int]) -> Optional[int]:
        """
        获取下一个可用密钥（考虑速率限制）
        
        Args:
            key_indices: 候选密钥索引列表
        
        Returns:
            可用的密钥索引，如果都不可用则返回 None
        """
        if not self._initialized:
            await self.initialize()
        
        for idx in key_indices:
            if not await self.is_key_exhausted(idx):
                return idx
        
        # 所有密钥都用尽，找最早重置的
        earliest_reset = None
        earliest_idx = None
        
        for idx in key_indices:
            info = self._rate_limits.get(idx)
            if info and info.reset_time > 0:
                if earliest_reset is None or info.reset_time < earliest_reset:
                    earliest_reset = info.reset_time
                    earliest_idx = idx
        
        if earliest_idx is not None:
            log.warning(f"All keys exhausted, earliest reset: key {earliest_idx} in {earliest_reset - time.time():.0f}s")
        
        return earliest_idx
    
    async def get_earliest_reset_time(self, key_indices: List[int]) -> Optional[int]:
        """
        获取最早的重置时间
        
        Args:
            key_indices: 密钥索引列表
        
        Returns:
            最早的重置时间（Unix时间戳）
        """
        if not self._initialized:
            await self.initialize()
        
        earliest = None
        for idx in key_indices:
            info = self._rate_limits.get(idx)
            if info and info.reset_time > 0:
                if earliest is None or info.reset_time < earliest:
                    earliest = info.reset_time
        
        return earliest


# 全局实例
_rate_limiter: Optional[RateLimiter] = None


async def get_rate_limiter() -> RateLimiter:
    """获取全局速率限制管理器实例"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
        await _rate_limiter.initialize()
    return _rate_limiter
