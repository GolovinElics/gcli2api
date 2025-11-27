"""
密钥管理器模块
管理 API 密钥的增删改查、启用禁用状态
"""
import time
from typing import Dict, List, Optional, Any

from log import log
from .models_key import KeyInfo, KeyConfig, KeyStatus, AggregationMode
from .storage_adapter import get_storage_adapter


class KeyManager:
    """密钥管理器"""
    
    def __init__(self):
        self._cache: Optional[KeyConfig] = None
        self._key_states: Dict[int, Dict[str, Any]] = {}  # 密钥状态缓存
        self._initialized = False
    
    async def initialize(self):
        """初始化密钥管理器"""
        if self._initialized:
            return
        await self._load_config()
        self._initialized = True
    
    async def _load_config(self):
        """从存储加载配置"""
        try:
            adapter = await get_storage_adapter()
            
            # 加载密钥列表
            keys = await adapter.get_config("assembly_api_keys", [])
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split(",") if k.strip()]
            
            # 加载禁用的密钥索引
            disabled_indices = await adapter.get_config("disabled_key_indices", [])
            if not isinstance(disabled_indices, list):
                disabled_indices = []
            
            # 加载聚合模式
            mode_str = await adapter.get_config("key_aggregation_mode", "round_robin")
            try:
                mode = AggregationMode(mode_str)
            except ValueError:
                mode = AggregationMode.ROUND_ROBIN
            
            # 加载轮换次数
            calls_per_rotation = await adapter.get_config("calls_per_rotation", 100)
            
            # 计算启用的索引
            enabled_indices = [i for i in range(len(keys)) if i not in disabled_indices]
            
            self._cache = KeyConfig(
                keys=keys,
                enabled_indices=enabled_indices,
                disabled_indices=disabled_indices,
                aggregation_mode=mode,
                calls_per_rotation=int(calls_per_rotation),
            )
            
            # 加载密钥状态
            key_states = await adapter.get_config("key_states", {})
            if isinstance(key_states, dict):
                self._key_states = {int(k): v for k, v in key_states.items()}
            
            log.debug(f"Loaded {len(keys)} keys, {len(enabled_indices)} enabled, {len(disabled_indices)} disabled")
        except Exception as e:
            log.error(f"Failed to load key config: {e}")
            self._cache = KeyConfig()
    
    async def _save_config(self):
        """保存配置到存储"""
        if not self._cache:
            return
        
        try:
            adapter = await get_storage_adapter()
            
            # 保存密钥列表
            await adapter.set_config("assembly_api_keys", self._cache.keys)
            
            # 保存禁用的密钥索引
            await adapter.set_config("disabled_key_indices", self._cache.disabled_indices)
            
            # 保存聚合模式
            await adapter.set_config("key_aggregation_mode", self._cache.aggregation_mode.value)
            
            # 保存轮换次数
            await adapter.set_config("calls_per_rotation", self._cache.calls_per_rotation)
            
            # 保存密钥状态
            await adapter.set_config("key_states", {str(k): v for k, v in self._key_states.items()})
            
            log.debug("Key config saved successfully")
        except Exception as e:
            log.error(f"Failed to save key config: {e}")
    
    async def get_all_keys(self) -> List[KeyInfo]:
        """获取所有密钥信息"""
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            return []
        
        keys = []
        for i, key in enumerate(self._cache.keys):
            state = self._key_states.get(i, {})
            enabled = i not in self._cache.disabled_indices
            
            # 确定状态
            if not enabled:
                status = KeyStatus.DISABLED
            elif state.get("exhausted", False):
                status = KeyStatus.EXHAUSTED
            elif state.get("success_count", 0) > 0 or state.get("failure_count", 0) > 0:
                status = KeyStatus.ACTIVE
            else:
                status = KeyStatus.UNUSED
            
            key_info = KeyInfo(
                index=i,
                key=key,
                enabled=enabled,
                success_count=state.get("success_count", 0),
                failure_count=state.get("failure_count", 0),
                rate_limit=state.get("rate_limit"),
                remaining=state.get("remaining"),
                reset_time=state.get("reset_time"),
                reset_in_seconds=state.get("reset_in_seconds"),
                last_used=state.get("last_used"),
                status=status,
                disable_reason=state.get("disable_reason"),
                disable_time=state.get("disable_time"),
            )
            keys.append(key_info)
        
        return keys
    
    async def get_enabled_keys(self) -> List[KeyInfo]:
        """获取所有启用的密钥"""
        all_keys = await self.get_all_keys()
        return [k for k in all_keys if k.enabled]
    
    async def add_keys(self, keys: List[str], mode: str = "append") -> bool:
        """
        添加密钥
        
        Args:
            keys: 新密钥列表
            mode: "append" 追加到末尾，"override" 覆盖现有
        
        Returns:
            是否成功
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            self._cache = KeyConfig()
        
        # 过滤空密钥
        new_keys = [k.strip() for k in keys if k.strip()]
        if not new_keys:
            return False
        
        if mode == "override":
            # 覆盖模式：替换所有密钥
            self._cache.keys = new_keys
            self._cache.enabled_indices = list(range(len(new_keys)))
            self._cache.disabled_indices = []
            self._key_states = {}  # 清空状态
            log.info(f"Replaced all keys with {len(new_keys)} new keys")
        else:
            # 追加模式：添加到末尾
            start_index = len(self._cache.keys)
            self._cache.keys.extend(new_keys)
            # 新添加的密钥默认启用
            new_indices = list(range(start_index, len(self._cache.keys)))
            self._cache.enabled_indices.extend(new_indices)
            log.info(f"Appended {len(new_keys)} keys, total: {len(self._cache.keys)}")
        
        await self._save_config()
        return True
    
    async def update_key_status(self, index: int, enabled: bool) -> bool:
        """
        更新密钥启用状态
        
        Args:
            index: 密钥索引
            enabled: 是否启用
        
        Returns:
            是否成功
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._cache or index < 0 or index >= len(self._cache.keys):
            return False
        
        if enabled:
            # 启用密钥
            if index in self._cache.disabled_indices:
                self._cache.disabled_indices.remove(index)
            if index not in self._cache.enabled_indices:
                self._cache.enabled_indices.append(index)
            # 清除禁用原因
            if index in self._key_states:
                self._key_states[index].pop("disable_reason", None)
                self._key_states[index].pop("disable_time", None)
        else:
            # 禁用密钥
            if index in self._cache.enabled_indices:
                self._cache.enabled_indices.remove(index)
            if index not in self._cache.disabled_indices:
                self._cache.disabled_indices.append(index)
            # 记录禁用信息
            if index not in self._key_states:
                self._key_states[index] = {}
            self._key_states[index]["disable_reason"] = "manual"
            self._key_states[index]["disable_time"] = time.time()
        
        await self._save_config()
        log.info(f"Key {index} {'enabled' if enabled else 'disabled'}")
        return True
    
    async def batch_update_status(self, indices: List[int], enabled: bool) -> bool:
        """
        批量更新密钥状态
        
        Args:
            indices: 密钥索引列表
            enabled: 是否启用
        
        Returns:
            是否成功
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            return False
        
        success_count = 0
        for index in indices:
            if 0 <= index < len(self._cache.keys):
                if enabled:
                    if index in self._cache.disabled_indices:
                        self._cache.disabled_indices.remove(index)
                    if index not in self._cache.enabled_indices:
                        self._cache.enabled_indices.append(index)
                    if index in self._key_states:
                        self._key_states[index].pop("disable_reason", None)
                        self._key_states[index].pop("disable_time", None)
                else:
                    if index in self._cache.enabled_indices:
                        self._cache.enabled_indices.remove(index)
                    if index not in self._cache.disabled_indices:
                        self._cache.disabled_indices.append(index)
                    if index not in self._key_states:
                        self._key_states[index] = {}
                    self._key_states[index]["disable_reason"] = "manual"
                    self._key_states[index]["disable_time"] = time.time()
                success_count += 1
        
        await self._save_config()
        log.info(f"Batch {'enabled' if enabled else 'disabled'} {success_count} keys")
        return success_count > 0
    
    async def delete_key(self, index: int) -> bool:
        """
        删除密钥
        
        Args:
            index: 密钥索引
        
        Returns:
            是否成功
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._cache or index < 0 or index >= len(self._cache.keys):
            return False
        
        # 删除密钥
        self._cache.keys.pop(index)
        
        # 更新索引
        self._cache.enabled_indices = [i if i < index else i - 1 for i in self._cache.enabled_indices if i != index]
        self._cache.disabled_indices = [i if i < index else i - 1 for i in self._cache.disabled_indices if i != index]
        
        # 更新状态缓存
        new_states = {}
        for i, state in self._key_states.items():
            if i < index:
                new_states[i] = state
            elif i > index:
                new_states[i - 1] = state
        self._key_states = new_states
        
        await self._save_config()
        
        # 清理该密钥的统计数据
        try:
            from .stats_tracker import get_stats_tracker
            stats_tracker = await get_stats_tracker()
            # 获取当前活跃的密钥索引列表
            active_indices = list(range(len(self._cache.keys)))
            await stats_tracker.cleanup_inactive_keys(active_indices)
        except Exception as e:
            log.warning(f"Failed to cleanup stats for deleted key: {e}")
        
        log.info(f"Deleted key at index {index}")
        return True
    
    async def get_active_keys_count(self) -> int:
        """获取活跃（启用）密钥数量"""
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            return 0
        
        return len(self._cache.enabled_indices)
    
    async def get_aggregation_mode(self) -> AggregationMode:
        """获取聚合模式"""
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            return AggregationMode.ROUND_ROBIN
        
        return self._cache.aggregation_mode
    
    async def set_aggregation_mode(self, mode: AggregationMode) -> bool:
        """设置聚合模式"""
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            return False
        
        self._cache.aggregation_mode = mode
        await self._save_config()
        log.info(f"Aggregation mode set to {mode.value}")
        return True
    
    async def get_calls_per_rotation(self) -> int:
        """获取轮换次数"""
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            return 100
        
        return self._cache.calls_per_rotation
    
    async def set_calls_per_rotation(self, calls: int) -> bool:
        """设置轮换次数"""
        if not self._initialized:
            await self.initialize()
        
        if not self._cache or calls < 1:
            return False
        
        self._cache.calls_per_rotation = calls
        await self._save_config()
        log.info(f"Calls per rotation set to {calls}")
        return True
    
    async def update_key_state(self, index: int, state_updates: Dict[str, Any]) -> bool:
        """更新密钥状态（统计信息、速率限制等）"""
        if not self._initialized:
            await self.initialize()
        
        if index not in self._key_states:
            self._key_states[index] = {}
        
        self._key_states[index].update(state_updates)
        
        # 异步保存，不阻塞
        try:
            adapter = await get_storage_adapter()
            await adapter.set_config("key_states", {str(k): v for k, v in self._key_states.items()})
        except Exception as e:
            log.error(f"Failed to save key state: {e}")
        
        return True
    
    async def export_keys(self) -> Dict[str, Any]:
        """导出密钥配置"""
        if not self._initialized:
            await self.initialize()
        
        if not self._cache:
            return {"keys": [], "config": {}}
        
        return {
            "keys": self._cache.keys,
            "disabled_indices": self._cache.disabled_indices,
            "aggregation_mode": self._cache.aggregation_mode.value,
            "calls_per_rotation": self._cache.calls_per_rotation,
            "key_states": self._key_states,
        }
    
    async def import_keys(self, config: Dict[str, Any], mode: str = "append") -> bool:
        """
        导入密钥配置
        
        Args:
            config: 配置字典
            mode: "append" 追加，"override" 覆盖
        
        Returns:
            是否成功
        """
        if not self._initialized:
            await self.initialize()
        
        keys = config.get("keys", [])
        if not keys:
            return False
        
        # 添加密钥
        success = await self.add_keys(keys, mode)
        if not success:
            return False
        
        # 如果是覆盖模式，还原其他配置
        if mode == "override":
            disabled_indices = config.get("disabled_indices", [])
            if disabled_indices:
                self._cache.disabled_indices = disabled_indices
                self._cache.enabled_indices = [i for i in range(len(self._cache.keys)) if i not in disabled_indices]
            
            mode_str = config.get("aggregation_mode", "round_robin")
            try:
                self._cache.aggregation_mode = AggregationMode(mode_str)
            except ValueError:
                pass
            
            calls = config.get("calls_per_rotation", 100)
            if isinstance(calls, int) and calls > 0:
                self._cache.calls_per_rotation = calls
            
            key_states = config.get("key_states", {})
            if key_states:
                self._key_states = {int(k): v for k, v in key_states.items()}
            
            await self._save_config()
        
        return True


# 全局实例
_key_manager: Optional[KeyManager] = None


async def get_key_manager() -> KeyManager:
    """获取全局密钥管理器实例"""
    global _key_manager
    if _key_manager is None:
        _key_manager = KeyManager()
        await _key_manager.initialize()
    return _key_manager
