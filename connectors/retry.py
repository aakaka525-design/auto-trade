"""
可复用的重试和限流工具

设计用于处理 DEX 交互中的常见问题：
- RPC 超时
- API 限流 (HTTP 429)
- 瞬时网络错误
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Type, TypeVar

from core.exceptions import (
    RPCTimeoutError,
    RateLimitExceededError,
    ExchangeConnectionError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ==================== 重试配置 ====================

@dataclass
class RetryConfig:
    """重试配置"""
    max_retries: int = 3
    base_delay: float = 0.5  # 秒
    max_delay: float = 10.0  # 秒
    exponential_base: float = 2.0
    jitter: bool = True  # 添加随机抖动防止雷群效应
    
    # 可重试的异常类型
    retryable_exceptions: tuple = field(default_factory=lambda: (
        RPCTimeoutError,
        RateLimitExceededError,
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
    ))


def calculate_delay(attempt: int, config: RetryConfig) -> float:
    """计算指数退避延迟"""
    delay = config.base_delay * (config.exponential_base ** attempt)
    delay = min(delay, config.max_delay)
    
    if config.jitter:
        # 添加 ±25% 的随机抖动
        jitter_range = delay * 0.25
        delay += random.uniform(-jitter_range, jitter_range)
    
    return max(0.1, delay)  # 最小 100ms


async def retry_async(
    coro_factory: Callable[[], Any],
    config: Optional[RetryConfig] = None,
    on_retry: Optional[Callable[[Exception, int], None]] = None,
) -> T:
    """
    通用异步重试函数
    
    Args:
        coro_factory: 返回协程的工厂函数 (每次重试会重新调用)
        config: 重试配置
        on_retry: 重试回调函数 (exception, attempt)
    
    Returns:
        协程执行结果
    
    Raises:
        最后一次重试的异常
    
    Example:
        result = await retry_async(
            lambda: connector.get_orderbook("ETH-USDC"),
            config=RetryConfig(max_retries=5)
        )
    """
    config = config or RetryConfig()
    last_exception: Optional[Exception] = None
    
    for attempt in range(config.max_retries + 1):
        try:
            return await coro_factory()
        
        except config.retryable_exceptions as e:
            last_exception = e
            
            if attempt >= config.max_retries:
                logger.error(f"重试耗尽 ({config.max_retries}次): {e}")
                raise
            
            # 计算延迟
            if isinstance(e, RateLimitExceededError):
                delay = e.retry_after
            else:
                delay = calculate_delay(attempt, config)
            
            logger.warning(
                f"重试 {attempt + 1}/{config.max_retries}: {type(e).__name__} - "
                f"等待 {delay:.2f}s"
            )
            
            if on_retry:
                on_retry(e, attempt)
            
            await asyncio.sleep(delay)
    
    # 不应该到达这里
    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected retry loop exit")


# ==================== 令牌桶限流器 ====================

class TokenBucketLimiter:
    """
    令牌桶限流器
    
    用于控制 API 请求频率，防止触发限流。
    
    Lighter 限制:
    - Premium: 24000 权重/分钟
    - Standard: 60 请求/分钟
    
    Example:
        limiter = TokenBucketLimiter(
            rate=400,  # 400 权重/秒 (24000/60)
            capacity=1000  # 允许短时突发
        )
        
        async with limiter:
            await api_call()
    """
    
    def __init__(
        self, 
        rate: float,  # 每秒令牌生成数
        capacity: float = None,  # 桶容量 (默认=rate)
    ):
        self.rate = rate
        self.capacity = capacity or rate
        self.tokens = self.capacity
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: float = 1) -> float:
        """
        获取令牌
        
        Args:
            tokens: 需要的令牌数 (默认1)
        
        Returns:
            等待时间 (秒)
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            
            # 补充令牌
            self.tokens = min(
                self.capacity, 
                self.tokens + elapsed * self.rate
            )
            self._last_update = now
            
            # 计算等待时间
            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0
            
            # 需要等待
            deficit = tokens - self.tokens
            wait_time = deficit / self.rate
            
            self.tokens = 0
            
            logger.debug(f"限流器等待: {wait_time:.3f}s")
            await asyncio.sleep(wait_time)
            
            return wait_time
    
    async def __aenter__(self):
        await self.acquire()
        return self
    
    async def __aexit__(self, *args):
        pass


# ==================== Nonce 管理器 ====================

class NonceManager:
    """
    线程安全的 Nonce 管理器
    
    每个 API Key 有独立的 Nonce 序列。
    支持批量获取和冲突回滚。
    
    使用:
        manager = NonceManager(initial_nonce=100)
        nonce = manager.get_next()
        
        try:
            await send_order(nonce=nonce)
        except NonceConflictError:
            manager.rollback(nonce)
            # 或者同步最新 nonce
            await manager.sync_from_api(api)
    """
    
    def __init__(self, initial_nonce: int = 0):
        self._nonce = initial_nonce
        self._lock = asyncio.Lock()
        self._pending: set[int] = set()  # 正在使用中的 nonce
    
    async def get_next(self) -> int:
        """获取下一个可用 nonce"""
        async with self._lock:
            self._nonce += 1
            self._pending.add(self._nonce)
            return self._nonce
    
    async def confirm(self, nonce: int) -> None:
        """确认 nonce 已成功使用"""
        async with self._lock:
            self._pending.discard(nonce)
    
    async def rollback(self, nonce: int) -> None:
        """回滚未使用的 nonce (用于失败重试)"""
        async with self._lock:
            self._pending.discard(nonce)
            # 注意: 不回退 _nonce 计数器，因为中间可能有其他请求
    
    async def sync_from_api(
        self, 
        fetch_nonce: Callable[[], int]
    ) -> int:
        """
        从 API 同步最新 nonce
        
        Args:
            fetch_nonce: 获取远程 nonce 的异步函数
        
        Returns:
            同步后的 nonce
        """
        async with self._lock:
            remote_nonce = await fetch_nonce()
            self._nonce = max(self._nonce, remote_nonce)
            self._pending.clear()
            logger.info(f"Nonce 已同步: {self._nonce}")
            return self._nonce
    
    @property
    def current(self) -> int:
        """当前 nonce 值 (不递增)"""
        return self._nonce


# ==================== 便捷装饰器 ====================

def with_retry(
    max_retries: int = 3,
    base_delay: float = 0.5,
):
    """
    重试装饰器
    
    Example:
        @with_retry(max_retries=5)
        async def fetch_data():
            ...
    """
    from functools import wraps
    config = RetryConfig(max_retries=max_retries, base_delay=base_delay)
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_async(
                lambda: func(*args, **kwargs),
                config=config
            )
        return wrapper
    return decorator
