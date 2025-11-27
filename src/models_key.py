"""
数据模型定义 - API 密钥管理增强功能
定义 KeyInfo、KeyConfig、RateLimitInfo、KeyStats 等数据模型
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum


class KeyStatus(str, Enum):
    """密钥状态枚举"""
    ACTIVE = "active"           # 活跃可用
    DISABLED = "disabled"       # 手动禁用
    EXHAUSTED = "exhausted"     # 速率限制用尽
    UNUSED = "unused"           # 未使用过
    INVALID = "invalid"         # 失效密钥（认证失败、被封禁等）


class AggregationMode(str, Enum):
    """密钥聚合模式枚举"""
    ROUND_ROBIN = "round_robin"  # 轮询模式
    RANDOM = "random"            # 随机模式


@dataclass
class KeyInfo:
    """密钥信息"""
    index: int                              # 密钥序号
    key: str                                # 完整密钥值
    masked_key: str = ""                    # 脱敏后的密钥
    enabled: bool = True                    # 启用状态
    success_count: int = 0                  # 成功请求次数
    failure_count: int = 0                  # 失败请求次数
    rate_limit: Optional[int] = None        # 速率限制
    remaining: Optional[int] = None         # 剩余配额
    reset_time: Optional[int] = None        # 重置时间（Unix时间戳）
    reset_in_seconds: Optional[int] = None  # 距离重置的秒数
    last_used: Optional[float] = None       # 最后使用时间
    status: KeyStatus = KeyStatus.ACTIVE    # 密钥状态
    disable_reason: Optional[str] = None    # 禁用原因
    disable_time: Optional[float] = None    # 禁用时间
    
    def __post_init__(self):
        """初始化后处理"""
        if not self.masked_key and self.key:
            self.masked_key = self._mask_key(self.key)
    
    @staticmethod
    def _mask_key(key: str) -> str:
        """脱敏密钥"""
        if not key:
            return ""
        k = str(key)
        if len(k) <= 8:
            return k[:2] + "***"
        return k[:4] + "..." + k[-4:]
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "index": self.index,
            "key": self.key,
            "masked_key": self.masked_key,
            "enabled": self.enabled,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "rate_limit": self.rate_limit,
            "remaining": self.remaining,
            "reset_time": self.reset_time,
            "reset_in_seconds": self.reset_in_seconds,
            "last_used": self.last_used,
            "status": self.status.value if isinstance(self.status, KeyStatus) else self.status,
            "disable_reason": self.disable_reason,
            "disable_time": self.disable_time,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KeyInfo":
        """从字典创建"""
        status = data.get("status", "active")
        if isinstance(status, str):
            try:
                status = KeyStatus(status)
            except ValueError:
                status = KeyStatus.ACTIVE
        
        return cls(
            index=data.get("index", 0),
            key=data.get("key", ""),
            masked_key=data.get("masked_key", ""),
            enabled=data.get("enabled", True),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
            rate_limit=data.get("rate_limit"),
            remaining=data.get("remaining"),
            reset_time=data.get("reset_time"),
            reset_in_seconds=data.get("reset_in_seconds"),
            last_used=data.get("last_used"),
            status=status,
            disable_reason=data.get("disable_reason"),
            disable_time=data.get("disable_time"),
        )


@dataclass
class KeyConfig:
    """密钥配置"""
    keys: List[str] = field(default_factory=list)           # 密钥列表
    enabled_indices: List[int] = field(default_factory=list) # 启用的密钥索引
    disabled_indices: List[int] = field(default_factory=list) # 禁用的密钥索引
    aggregation_mode: AggregationMode = AggregationMode.ROUND_ROBIN  # 聚合模式
    calls_per_rotation: int = 100                           # 每个密钥使用次数后轮换
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "keys": self.keys,
            "enabled_indices": self.enabled_indices,
            "disabled_indices": self.disabled_indices,
            "aggregation_mode": self.aggregation_mode.value if isinstance(self.aggregation_mode, AggregationMode) else self.aggregation_mode,
            "calls_per_rotation": self.calls_per_rotation,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KeyConfig":
        """从字典创建"""
        mode = data.get("aggregation_mode", "round_robin")
        if isinstance(mode, str):
            try:
                mode = AggregationMode(mode)
            except ValueError:
                mode = AggregationMode.ROUND_ROBIN
        
        return cls(
            keys=data.get("keys", []),
            enabled_indices=data.get("enabled_indices", []),
            disabled_indices=data.get("disabled_indices", []),
            aggregation_mode=mode,
            calls_per_rotation=data.get("calls_per_rotation", 100),
        )


@dataclass
class RateLimitInfo:
    """速率限制信息"""
    key_index: int                          # 密钥索引
    limit: int = 0                          # 总限制
    remaining: int = 0                      # 剩余配额
    used: int = 0                           # 已使用
    reset_time: int = 0                     # 重置时间（Unix时间戳）
    reset_in_seconds: int = 0               # 距离重置的秒数
    status: KeyStatus = KeyStatus.ACTIVE    # 状态
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "key_index": self.key_index,
            "limit": self.limit,
            "remaining": self.remaining,
            "used": self.used,
            "reset_time": self.reset_time,
            "reset_in_seconds": self.reset_in_seconds,
            "status": self.status.value if isinstance(self.status, KeyStatus) else self.status,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RateLimitInfo":
        """从字典创建"""
        status = data.get("status", "active")
        if isinstance(status, str):
            try:
                status = KeyStatus(status)
            except ValueError:
                status = KeyStatus.ACTIVE
        
        return cls(
            key_index=data.get("key_index", 0),
            limit=data.get("limit", 0),
            remaining=data.get("remaining", 0),
            used=data.get("used", 0),
            reset_time=data.get("reset_time", 0),
            reset_in_seconds=data.get("reset_in_seconds", 0),
            status=status,
        )


@dataclass
class KeyStats:
    """密钥统计信息"""
    key_index: int                                      # 密钥索引
    masked_key: str = ""                                # 脱敏密钥
    enabled: bool = True                                # 启用状态
    success_count: int = 0                              # 成功次数
    failure_count: int = 0                              # 失败次数
    total_count: int = 0                                # 总次数
    model_counts: Dict[str, int] = field(default_factory=dict)  # 各模型调用次数
    rate_limit_info: Optional[RateLimitInfo] = None     # 速率限制信息
    
    def __post_init__(self):
        """初始化后计算总次数"""
        self.total_count = self.success_count + self.failure_count
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "key_index": self.key_index,
            "masked_key": self.masked_key,
            "enabled": self.enabled,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "total_count": self.total_count,
            "model_counts": self.model_counts,
            "rate_limit_info": self.rate_limit_info.to_dict() if self.rate_limit_info else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KeyStats":
        """从字典创建"""
        rate_limit_data = data.get("rate_limit_info")
        rate_limit_info = RateLimitInfo.from_dict(rate_limit_data) if rate_limit_data else None
        
        return cls(
            key_index=data.get("key_index", 0),
            masked_key=data.get("masked_key", ""),
            enabled=data.get("enabled", True),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
            model_counts=data.get("model_counts", {}),
            rate_limit_info=rate_limit_info,
        )
