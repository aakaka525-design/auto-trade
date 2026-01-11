"""
DEX 特定异常类

分层异常设计，支持精细化错误处理和自动恢复逻辑。
"""


class TradingError(Exception):
    """交易系统基础异常类"""
    pass


class AIProviderError(Exception):
    """AI 服务调用错误"""
    pass


class AIResponseError(Exception):
    """AI 响应格式错误"""
    pass


class SignalParseError(Exception):
    """交易信号解析错误"""
    pass


# RiskLimitExceeded 已移至 risk.manager.RiskLimitExceededError
# 保留此别名以兼容旧代码
from risk.manager import RiskLimitExceededError as RiskLimitExceeded


# ==================== 交易所连接异常 ====================

class ExchangeConnectionError(Exception):
    """交易所连接错误 (基类)"""
    
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class RPCTimeoutError(ExchangeConnectionError):
    """RPC 节点超时
    
    触发条件: 请求超过设定超时时间
    恢复策略: 指数退避重试
    """
    
    def __init__(self, message: str = "RPC 请求超时", timeout: float = 0):
        super().__init__(message, retryable=True)
        self.timeout = timeout


class RateLimitExceededError(ExchangeConnectionError):
    """API 限流 (HTTP 429)
    
    触发条件: 请求频率超过限制
    恢复策略: 等待 retry-after 后重试
    """
    
    def __init__(self, message: str = "请求频率超限", retry_after: float = 60):
        super().__init__(message, retryable=True)
        self.retry_after = retry_after


class WebSocketDisconnectedError(ExchangeConnectionError):
    """WebSocket 断开连接
    
    触发条件: WS 连接异常关闭
    恢复策略: 自动重连
    """
    
    def __init__(self, message: str = "WebSocket 连接断开"):
        super().__init__(message, retryable=True)


# ==================== 订单执行异常 ====================

class OrderExecutionError(Exception):
    """订单执行错误 (基类)"""
    
    def __init__(self, message: str, order_id: str = None, retryable: bool = False):
        super().__init__(message)
        self.order_id = order_id
        self.retryable = retryable


class NonceConflictError(OrderExecutionError):
    """Nonce 冲突
    
    触发条件: 相同 API Key 的 Nonce 重复使用
    恢复策略: 重新获取 next_nonce 并重试
    """
    
    def __init__(self, message: str = "Nonce 冲突", nonce: int = 0):
        super().__init__(message, retryable=True)
        self.nonce = nonce


class GasEstimationError(OrderExecutionError):
    """Gas 估算失败
    
    触发条件: 链端 Gas 估算失败
    恢复策略: 使用默认 Gas 或用户指定值
    """
    
    def __init__(self, message: str = "Gas 估算失败"):
        super().__init__(message, retryable=False)


class InsufficientLiquidityError(OrderExecutionError):
    """流动性不足
    
    触发条件: 订单簿深度不足以执行订单
    恢复策略: 降低订单尺寸或等待
    """
    
    def __init__(self, message: str = "流动性不足", available: float = 0):
        super().__init__(message, retryable=False)
        self.available = available


class InsufficientBalanceError(OrderExecutionError):
    """余额不足
    
    触发条件: 账户可用余额小于订单所需
    恢复策略: 无自动恢复，需人工处理
    """
    
    def __init__(self, message: str = "余额不足", required: float = 0, available: float = 0):
        super().__init__(message, retryable=False)
        self.required = required
        self.available = available


class OrderRejectedError(OrderExecutionError):
    """订单被拒绝
    
    触发条件: 交易所拒绝订单（价格/数量不合规等）
    恢复策略: 检查参数后重新提交
    """
    
    def __init__(self, message: str, error_code: int = 0):
        super().__init__(message, retryable=False)
        self.error_code = error_code


class OrderTimeoutError(OrderExecutionError):
    """订单超时
    
    触发条件: 订单在指定时间内未成交
    恢复策略: 取消并重新提交
    """
    
    def __init__(self, message: str = "订单超时", order_id: str = None):
        super().__init__(message, order_id=order_id, retryable=True)


# ==================== 错误码映射 ====================

# Lighter API 错误码映射
LIGHTER_ERROR_MAP = {
    21500: ("TransactionNotFound", False),
    21601: ("OrderBookFull", False),
    21700: ("InvalidOrderIndex", False),
    21702: ("InvalidPrice", False),
    21703: ("InvalidSize", False),
    23000: ("TooManyRequests", True),  # Rate Limit
}


def parse_lighter_error(error_code: int, message: str) -> OrderExecutionError:
    """解析 Lighter API 错误码，返回对应异常"""
    if error_code == 23000:
        return RateLimitExceededError(message)
    
    error_info = LIGHTER_ERROR_MAP.get(error_code)
    if error_info:
        return OrderRejectedError(f"[{error_info[0]}] {message}", error_code=error_code)
    
    return OrderExecutionError(f"[{error_code}] {message}")
