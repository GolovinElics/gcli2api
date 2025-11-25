"""
代理管理器模块
管理 HTTP/HTTPS 代理配置和热更新
"""
import re
from typing import Optional
from urllib.parse import urlparse

from log import log
from .storage_adapter import get_storage_adapter


class ProxyManager:
    """代理管理器"""
    
    # 支持的代理协议
    SUPPORTED_SCHEMES = ["http", "https", "socks5", "socks5h"]
    
    # 代理 URL 正则表达式
    PROXY_URL_PATTERN = re.compile(
        r'^(https?|socks5h?)://'  # 协议
        r'(?:([^:@]+)(?::([^@]+))?@)?'  # 可选的用户名:密码@
        r'([a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\])'  # 主机名或 IPv6
        r'(?::(\d+))?'  # 可选的端口
        r'/?$'  # 可选的尾部斜杠
    )
    
    def __init__(self):
        self._current_proxy: Optional[str] = None
        self._initialized = False
    
    async def initialize(self):
        """初始化代理管理器"""
        if self._initialized:
            return
        await self._load_proxy_config()
        self._initialized = True
    
    async def _load_proxy_config(self):
        """从存储加载代理配置"""
        try:
            adapter = await get_storage_adapter()
            proxy = await adapter.get_config("proxy", "")
            
            if proxy and isinstance(proxy, str) and proxy.strip():
                if self.validate_proxy_url(proxy.strip()):
                    self._current_proxy = proxy.strip()
                    log.info(f"Loaded proxy config: {self._mask_proxy_url(self._current_proxy)}")
                else:
                    log.warning(f"Invalid proxy URL in config, ignoring: {proxy}")
                    self._current_proxy = None
            else:
                self._current_proxy = None
                log.debug("No proxy configured")
        except Exception as e:
            log.error(f"Failed to load proxy config: {e}")
            self._current_proxy = None
    
    def _mask_proxy_url(self, url: str) -> str:
        """脱敏代理 URL（隐藏密码）"""
        if not url:
            return ""
        
        try:
            parsed = urlparse(url)
            if parsed.password:
                # 隐藏密码
                masked = url.replace(f":{parsed.password}@", ":***@")
                return masked
            return url
        except Exception:
            return url
    
    async def get_proxy_config(self) -> Optional[str]:
        """
        获取当前代理配置
        
        Returns:
            代理 URL，如果未配置则返回 None
        """
        if not self._initialized:
            await self.initialize()
        
        # 每次获取时重新加载，支持热更新
        await self._load_proxy_config()
        
        return self._current_proxy
    
    async def set_proxy_config(self, proxy_url: str) -> bool:
        """
        设置代理配置
        
        Args:
            proxy_url: 代理 URL，空字符串表示清除代理
        
        Returns:
            是否成功
        """
        if not self._initialized:
            await self.initialize()
        
        # 空字符串表示清除代理
        if not proxy_url or not proxy_url.strip():
            self._current_proxy = None
            try:
                adapter = await get_storage_adapter()
                await adapter.set_config("proxy", "")
                log.info("Proxy config cleared")
                return True
            except Exception as e:
                log.error(f"Failed to clear proxy config: {e}")
                return False
        
        # 验证代理 URL
        proxy_url = proxy_url.strip()
        if not self.validate_proxy_url(proxy_url):
            log.error(f"Invalid proxy URL: {proxy_url}")
            return False
        
        # 保存配置
        try:
            adapter = await get_storage_adapter()
            await adapter.set_config("proxy", proxy_url)
            self._current_proxy = proxy_url
            log.info(f"Proxy config set to: {self._mask_proxy_url(proxy_url)}")
            return True
        except Exception as e:
            log.error(f"Failed to save proxy config: {e}")
            return False
    
    def validate_proxy_url(self, proxy_url: str) -> bool:
        """
        验证代理 URL 格式
        
        Args:
            proxy_url: 代理 URL
        
        Returns:
            是否有效
        """
        if not proxy_url:
            return False
        
        # 使用正则表达式验证
        if not self.PROXY_URL_PATTERN.match(proxy_url):
            return False
        
        # 使用 urlparse 进一步验证
        try:
            parsed = urlparse(proxy_url)
            
            # 检查协议
            if parsed.scheme not in self.SUPPORTED_SCHEMES:
                return False
            
            # 检查主机名
            if not parsed.hostname:
                return False
            
            # 检查端口（如果指定）
            if parsed.port is not None:
                if parsed.port < 1 or parsed.port > 65535:
                    return False
            
            return True
        except Exception:
            return False
    
    async def test_proxy_connection(self, proxy_url: str = None) -> dict:
        """
        测试代理连接
        
        Args:
            proxy_url: 代理 URL，如果为 None 则使用当前配置
        
        Returns:
            测试结果字典，包含 success、latency、error 等字段
        """
        import httpx
        import time
        
        if proxy_url is None:
            if not self._initialized:
                await self.initialize()
            proxy_url = self._current_proxy
        
        if not proxy_url:
            return {
                "success": False,
                "error": "No proxy configured",
                "latency": None,
            }
        
        if not self.validate_proxy_url(proxy_url):
            return {
                "success": False,
                "error": "Invalid proxy URL format",
                "latency": None,
            }
        
        # 测试连接
        test_url = "https://httpbin.org/ip"
        start_time = time.time()
        
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=10.0) as client:
                response = await client.get(test_url)
                latency = (time.time() - start_time) * 1000  # 毫秒
                
                if response.status_code == 200:
                    return {
                        "success": True,
                        "latency": round(latency, 2),
                        "status_code": response.status_code,
                        "proxy_ip": response.json().get("origin", "unknown"),
                    }
                else:
                    return {
                        "success": False,
                        "error": f"HTTP {response.status_code}",
                        "latency": round(latency, 2),
                        "status_code": response.status_code,
                    }
        except httpx.ProxyError as e:
            return {
                "success": False,
                "error": f"Proxy error: {str(e)}",
                "latency": None,
            }
        except httpx.ConnectError as e:
            return {
                "success": False,
                "error": f"Connection error: {str(e)}",
                "latency": None,
            }
        except httpx.TimeoutException:
            return {
                "success": False,
                "error": "Connection timeout",
                "latency": None,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Unknown error: {str(e)}",
                "latency": None,
            }


# 全局实例
_proxy_manager: Optional[ProxyManager] = None


async def get_proxy_manager() -> ProxyManager:
    """获取全局代理管理器实例"""
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager()
        await _proxy_manager.initialize()
    return _proxy_manager
