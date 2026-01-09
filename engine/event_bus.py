"""
异步事件总线

设计模式: Publisher-Subscriber (Pub/Sub)
用于解耦系统各模块之间的通信。
"""
import asyncio
import logging
from typing import Callable, Any, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections import defaultdict

logger = logging.getLogger(__name__)


# ==================== 事件类型定义 ====================

class EventType(str, Enum):
    """事件类型枚举"""
    # 行情事件
    PRICE_UPDATE = "EVT_PRICE_UPDATE"
    ORDERBOOK_UPDATE = "EVT_ORDERBOOK_UPDATE"
    TRADE_UPDATE = "EVT_TRADE_UPDATE"
    CANDLE_CLOSE = "EVT_CANDLE_CLOSE"
    
    # 信号事件
    SIGNAL_GENERATED = "EVT_SIGNAL_GENERATED"
    
    # 订单事件
    ORDER_CREATED = "EVT_ORDER_CREATED"
    ORDER_FILLED = "EVT_ORDER_FILLED"
    ORDER_CANCELLED = "EVT_ORDER_CANCELLED"
    ORDER_FAILED = "EVT_ORDER_FAILED"
    
    # 仓位事件
    POSITION_OPENED = "EVT_POSITION_OPENED"
    POSITION_CLOSED = "EVT_POSITION_CLOSED"
    POSITION_UPDATED = "EVT_POSITION_UPDATED"
    
    # 风控事件
    RISK_ALERT = "EVT_RISK_ALERT"
    STOP_LOSS_TRIGGERED = "EVT_STOP_LOSS"
    TAKE_PROFIT_TRIGGERED = "EVT_TAKE_PROFIT"
    
    # 系统事件
    SYSTEM_START = "EVT_SYSTEM_START"
    SYSTEM_STOP = "EVT_SYSTEM_STOP"
    ERROR = "EVT_ERROR"


@dataclass
class Event:
    """事件对象"""
    event_type: EventType
    data: dict
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "system"
    
    def to_global_id(self) -> str:
        """生成 Global Event ID"""
        ts = int(self.timestamp.timestamp() * 1000)
        return f"{self.event_type.value}_{ts}"


# ==================== 事件处理器类型 ====================

EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


# ==================== 事件总线 ====================

class EventBus:
    """
    异步事件总线
    
    支持:
    - 异步事件发布
    - 多订阅者
    - 事件过滤
    - 事件历史记录
    
    使用示例:
    ```python
    bus = EventBus()
    
    async def on_price_update(event: Event):
        price = event.data["price"]
        print(f"Price: {price}")
    
    bus.subscribe(EventType.PRICE_UPDATE, on_price_update)
    
    await bus.publish(Event(
        event_type=EventType.PRICE_UPDATE,
        data={"price": 3097.0, "symbol": "ETH-USDC"}
    ))
    ```
    """
    
    _instance: "EventBus" = None  # Singleton
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._subscribers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._history: list[Event] = []
        self._max_history = 1000
        self._running = True
        self._initialized = True
        
        logger.info("EventBus 初始化完成")
    
    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """
        订阅事件
        
        Args:
            event_type: 事件类型
            handler: 异步处理函数
        """
        self._subscribers[event_type].append(handler)
        logger.debug(f"订阅事件: {event_type.value}")
    
    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """取消订阅"""
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)
    
    async def publish(self, event: Event) -> None:
        """
        发布事件
        
        所有订阅者将异步接收事件。
        """
        if not self._running:
            return
        
        # 记录历史
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        
        # 获取订阅者
        handlers = self._subscribers.get(event.event_type, [])
        
        if not handlers:
            return
        
        # 并发执行所有处理器
        tasks = []
        for handler in handlers:
            tasks.append(asyncio.create_task(self._safe_call(handler, event)))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        """安全调用处理器，捕获异常"""
        try:
            await handler(event)
        except Exception as e:
            logger.error(f"事件处理器异常: {e}")
            # 发布错误事件
            error_event = Event(
                event_type=EventType.ERROR,
                data={"error": str(e), "original_event": event.event_type.value},
                source="event_bus"
            )
            # 避免无限循环
            if event.event_type != EventType.ERROR:
                for h in self._subscribers.get(EventType.ERROR, []):
                    try:
                        await h(error_event)
                    except:
                        pass
    
    def get_history(self, event_type: EventType = None, limit: int = 100) -> list[Event]:
        """获取事件历史"""
        if event_type:
            filtered = [e for e in self._history if e.event_type == event_type]
        else:
            filtered = self._history
        return filtered[-limit:]
    
    def clear_history(self) -> None:
        """清空历史"""
        self._history.clear()
    
    def stop(self) -> None:
        """停止事件总线"""
        self._running = False
        logger.info("EventBus 已停止")
    
    def start(self) -> None:
        """启动事件总线"""
        self._running = True
        logger.info("EventBus 已启动")


# ==================== 便捷函数 ====================

def get_event_bus() -> EventBus:
    """获取单例 EventBus"""
    return EventBus()
