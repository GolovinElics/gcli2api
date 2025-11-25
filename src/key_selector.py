"""
密钥选择器模块
根据聚合模式和状态选择可用密钥
"""
import random
import time
import itertools
from typing import Dict, List, Optional, Any

from log import log
from .models_key import KeyInfo, KeyStatus, AggregationMode


class KeySelector:
    """密钥选择器"""
    
    def __init__(self, mode: AggregationMode = AggregationMode.ROUND_ROBIN):
        """
        初始化选择器
        
        Args:
            mode: 聚合模式，round_robin 或 random
        """
        self._mode = mode
        self._rr_counter = itertools.count()  # 轮询计数器
        self._failed_keys: Dict[int, float] = {}  # 失败的密钥和失败时间
        self._call_counts: Dict[int, int] = {}  # 每个密钥的调用次数
        self._current_key_index: Optional[int] = None  # 当前使用的密钥索引
        self._calls_per_rotation: int = 100  # 轮换次数
        self._failure_timeout: float = 60.0  # 失败密钥恢复时间（秒）
    
    @property
    def mode(self) -> AggregationMode:
        """获取当前模式"""
        return self._mode
    
    @mode.setter
    def mode(self, value: AggregationMode):
        """设置模式"""
        self._mode = value
        log.debug(f"Key selector mode changed to {value.value}")
    
    @property
    def calls_per_rotation(self) -> int:
        """获取轮换次数"""
        return self._calls_per_rotation
    
    @calls_per_rotation.setter
    def calls_per_rotation(self, value: int):
        """设置轮换次数"""
        if value > 0:
            self._calls_per_rotation = value
    
    def _clean_expired_failures(self):
        """清理过期的失败记录"""
        current_time = time.time()
        expired = [k for k, t in self._failed_keys.items() 
                   if current_time - t > self._failure_timeout]
        for k in expired:
            del self._failed_keys[k]
            log.debug(f"Key {k} failure record expired, now available")
    
    def _get_available_keys(self, keys: List[KeyInfo]) -> List[KeyInfo]:
        """获取可用的密钥列表（启用且未失败）"""
        self._clean_expired_failures()
        
        available = []
        for key in keys:
            # 跳过禁用的密钥
            if not key.enabled:
                continue
            # 跳过已用尽的密钥
            if key.status == KeyStatus.EXHAUSTED:
                continue
            # 跳过最近失败的密钥
            if key.index in self._failed_keys:
                continue
            available.append(key)
        
        return available
    
    async def select_next_key(self, keys: List[KeyInfo]) -> Optional[KeyInfo]:
        """
        选择下一个可用密钥
        
        Args:
            keys: 所有密钥列表
        
        Returns:
            选中的密钥，如果没有可用密钥则返回 None
        """
        available = self._get_available_keys(keys)
        
        if not available:
            log.warning("No available keys to select")
            return None
        
        if self._mode == AggregationMode.RANDOM:
            # 随机模式
            selected = random.choice(available)
        else:
            # 轮询模式
            i = next(self._rr_counter)
            selected = available[i % len(available)]
        
        self._current_key_index = selected.index
        
        # 更新调用计数
        if selected.index not in self._call_counts:
            self._call_counts[selected.index] = 0
        self._call_counts[selected.index] += 1
        
        log.debug(f"Selected key {selected.index} (mode={self._mode.value}, calls={self._call_counts[selected.index]})")
        return selected
    
    async def mark_key_failed(self, key_index: int, reason: str = ""):
        """
        标记密钥失败
        
        Args:
            key_index: 密钥索引
            reason: 失败原因
        """
        self._failed_keys[key_index] = time.time()
        log.warning(f"Key {key_index} marked as failed: {reason}")
    
    async def mark_key_exhausted(self, key_index: int, reset_time: Optional[int] = None):
        """
        标记密钥已用尽（速率限制）
        
        Args:
            key_index: 密钥索引
            reset_time: 重置时间（Unix时间戳）
        """
        self._failed_keys[key_index] = time.time()
        log.warning(f"Key {key_index} marked as exhausted, reset_time={reset_time}")
    
    async def clear_key_failure(self, key_index: int):
        """清除密钥的失败标记"""
        if key_index in self._failed_keys:
            del self._failed_keys[key_index]
            log.debug(f"Key {key_index} failure cleared")
    
    def should_rotate(self, key_index: int, rate_limit_remaining: Optional[int] = None) -> bool:
        """
        判断是否应该轮换密钥（智能轮换策略）
        
        智能轮换策略：
        1. 如果达到轮换次数限制，轮换
        2. 如果速率限制剩余配额低于阈值，轮换
        3. 优先考虑较小的值（轮换次数 vs 速率限制）
        
        Args:
            key_index: 当前密钥索引
            rate_limit_remaining: 速率限制剩余配额（可选）
        
        Returns:
            是否应该轮换
        """
        calls = self._call_counts.get(key_index, 0)
        
        # 检查轮换次数
        calls_should_rotate = calls >= self._calls_per_rotation
        
        # 检查速率限制
        rate_limit_should_rotate = False
        if rate_limit_remaining is not None:
            # 如果剩余配额低于 10%，建议轮换
            rate_limit_should_rotate = rate_limit_remaining <= 0
        
        should = calls_should_rotate or rate_limit_should_rotate
        
        if should:
            reason = []
            if calls_should_rotate:
                reason.append(f"calls={calls}/{self._calls_per_rotation}")
            if rate_limit_should_rotate:
                reason.append(f"rate_limit_remaining={rate_limit_remaining}")
            log.debug(f"Key {key_index} should rotate: {', '.join(reason)}")
            # 重置计数
            self._call_counts[key_index] = 0
        
        return should
    
    async def should_rotate_with_rate_limit(self, key_index: int, rate_limiter=None) -> bool:
        """
        判断是否应该轮换密钥（集成速率限制检查）
        
        Args:
            key_index: 当前密钥索引
            rate_limiter: 速率限制管理器实例
        
        Returns:
            是否应该轮换
        """
        rate_limit_remaining = None
        
        if rate_limiter:
            try:
                info = await rate_limiter.get_rate_limit_info(key_index)
                if info:
                    rate_limit_remaining = info.remaining
            except Exception as e:
                log.warning(f"Failed to get rate limit info for key {key_index}: {e}")
        
        return self.should_rotate(key_index, rate_limit_remaining)
    
    def reset_call_count(self, key_index: int):
        """重置密钥的调用计数"""
        self._call_counts[key_index] = 0
    
    def get_call_count(self, key_index: int) -> int:
        """获取密钥的调用计数"""
        return self._call_counts.get(key_index, 0)
    
    def get_failed_keys(self) -> Dict[int, float]:
        """获取失败的密钥列表"""
        self._clean_expired_failures()
        return self._failed_keys.copy()


# 全局实例
_key_selector: Optional[KeySelector] = None


def get_key_selector() -> KeySelector:
    """获取全局密钥选择器实例"""
    global _key_selector
    if _key_selector is None:
        _key_selector = KeySelector()
    return _key_selector


def reset_key_selector():
    """重置全局密钥选择器"""
    global _key_selector
    _key_selector = None
