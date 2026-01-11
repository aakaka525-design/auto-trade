"""
代理轮换管理器 (简化版)

从环境变量读取代理列表，支持轮换以规避 IP 限制。
环境变量格式:
- PROXY_LIST: 逗号分隔的代理列表，如 "http://user:pass@ip:port,http://user2:pass2@ip2:port2"
- 或单个 HTTP_PROXY: "http://user:pass@ip:port"
"""
import logging
import os
from typing import Optional, List, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector

logger = logging.getLogger(__name__)


@dataclass
class ProxyInfo:
    """代理信息"""
    url: str  # 完整的代理 URL
    request_count: int = 0
    
    @property
    def display_name(self) -> str:
        """返回用于显示的代理地址 (隐藏密码)"""
        try:
            parsed = urlparse(self.url)
            if parsed.username:
                return f"{parsed.hostname}:{parsed.port}"
            return f"{parsed.hostname}:{parsed.port}"
        except:
            return self.url


class ProxyRotator:
    """
    代理轮换器 (简化版)
    
    从环境变量读取代理:
    - PROXY_LIST: 逗号分隔的多个代理
    - HTTP_PROXY: 单个代理 (fallback)
    """
    
    def __init__(self):
        self.proxies: List[ProxyInfo] = []
        self._current_index = 0
        self._load_from_env()
    
    def _load_from_env(self):
        """从环境变量加载代理"""
        # 优先使用 PROXY_LIST (支持多个代理)
        proxy_list = os.getenv('PROXY_LIST', '')
        if proxy_list:
            for url in proxy_list.split(','):
                url = url.strip()
                if url:
                    self.proxies.append(ProxyInfo(url=url))
            if self.proxies:
                logger.info(f"从 PROXY_LIST 加载了 {len(self.proxies)} 个代理")
                return
        
        # Fallback: 使用 HTTP_PROXY
        http_proxy = os.getenv('HTTP_PROXY', '')
        if http_proxy:
            self.proxies.append(ProxyInfo(url=http_proxy))
            logger.info(f"从 HTTP_PROXY 加载了 1 个代理")
            return
        
        logger.warning("未配置代理，将使用直连模式")
    
    def get_next_proxy(self) -> Optional[ProxyInfo]:
        """获取下一个代理 (Round-Robin)"""
        if not self.proxies:
            return None
        
        proxy = self.proxies[self._current_index]
        proxy.request_count += 1
        
        # 轮换到下一个
        self._current_index = (self._current_index + 1) % len(self.proxies)
        
        return proxy
    
    def get_connector(self) -> Optional[ProxyConnector]:
        """获取 aiohttp 代理连接器"""
        proxy = self.get_next_proxy()
        if proxy:
            return ProxyConnector.from_url(proxy.url)
        return None
    
    @property
    def count(self) -> int:
        """代理数量"""
        return len(self.proxies)
    
    def status(self) -> str:
        """返回状态摘要"""
        if not self.proxies:
            return "代理: 直连模式"
        return f"代理: {len(self.proxies)} 个可用"


# 全局代理轮换器
_proxy_rotator: Optional[ProxyRotator] = None


def get_proxy_rotator() -> ProxyRotator:
    """获取全局代理轮换器"""
    global _proxy_rotator
    if _proxy_rotator is None:
        _proxy_rotator = ProxyRotator()
    return _proxy_rotator


async def create_session_with_proxy() -> Tuple[aiohttp.ClientSession, str]:
    """
    创建带代理的 aiohttp 会话
    
    Returns:
        (session, proxy_display): 会话和代理显示名称
    """
    rotator = get_proxy_rotator()
    proxy = rotator.get_next_proxy()
    
    if proxy:
        connector = ProxyConnector.from_url(proxy.url)
        return aiohttp.ClientSession(connector=connector), proxy.display_name
    else:
        return aiohttp.ClientSession(), "DIRECT"
