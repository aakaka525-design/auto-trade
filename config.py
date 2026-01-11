"""
配置管理 - 所有敏感信息通过环境变量读取

Lighter SDK 配置说明：
- API_PRIVATE_KEY: 从 Lighter 控制台生成的 API 私钥（hex 格式）
- ACCOUNT_INDEX: 账户索引，可通过 accountsByL1Address API 获取
- API_KEY_INDEX: API 密钥索引 (3-254 可用，0/1/2 保留给前端)
"""
from typing import Literal, Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置"""
    
    # AI Provider 配置
    AI_PROVIDER: Literal["openai", "anthropic", "gemini", "custom"] = "openai"
    AI_API_KEY: str = ""
    AI_API_BASE_URL: str = "https://api.openai.com/v1"
    AI_MODEL: str = "gpt-4o"
    AI_MAX_TOKENS: int = 1024
    AI_TEMPERATURE: float = 0.3
    
    # Lighter 交易所配置
    # 主网: https://mainnet.zklighter.elliot.ai
    # 测试网: https://testnet.zklighter.elliot.ai
    LIGHTER_BASE_URL: str = "https://testnet.zklighter.elliot.ai"
    
    # API 私钥 (hex 格式，可以带或不带 0x 前缀)
    # 通过 SignerClient.create_api_key() 生成，或从控制台获取
    LIGHTER_API_PRIVATE_KEY: str = ""
    
    # 账户索引 (通过 accountsByL1Address API 获取)
    # 如果留空或不知道，设为 0 用于测试模式
    LIGHTER_ACCOUNT_INDEX: Optional[int] = 0
    
    # API 密钥索引 (3-254 可用)
    # 每个索引对应一个独立的 nonce 序列
    LIGHTER_API_KEY_INDEX: int = 3
    
    # ETH 私钥 (可选，仅用于需要 L1 签名的操作如 change_api_key)
    LIGHTER_ETH_PRIVATE_KEY: str = ""
    
    # 交易配置
    TRADING_SYMBOL: str = "ETH-USDC"
    TRADING_MARKET_INDEX: int = 0  # 0=ETH永续, 2048=ETH现货
    ANALYSIS_INTERVAL_SECONDS: int = 300  # 5分钟
    MAX_POSITION_SIZE_USDC: float = 1000.0
    
    # 风控配置
    MAX_LOSS_PER_TRADE_PCT: float = 2.0
    MAX_DAILY_LOSS_PCT: float = 5.0
    MIN_CONFIDENCE_THRESHOLD: float = 0.6
    
    # ==================== 交易所选择 ====================
    # 交易执行使用的交易所: "lighter" 或 "binance"
    ACTIVE_EXCHANGE: Literal["lighter", "binance"] = "lighter"
    
    # 监控的交易所列表 (逗号分隔): "lighter", "binance", 或 "lighter,binance" 同时监控
    MONITOR_EXCHANGES: str = "lighter"
    
    # ==================== Binance 配置 ====================
    BINANCE_API_KEY: str = ""
    BINANCE_API_SECRET: str = ""  # HMAC 签名需要
    BINANCE_TESTNET: bool = False  # True = 测试网
    
    # Ed25519 签名 (可选，比 HMAC 更安全)
    # 签名方式: "HMAC" 或 "Ed25519"
    BINANCE_SIGN_TYPE: Literal["HMAC", "Ed25519"] = "HMAC"
    # Ed25519 私钥 (PEM 格式，仅当 SIGN_TYPE=Ed25519 时需要)
    BINANCE_PRIVATE_KEY: str = ""

    
    # API 成本控制
    DAILY_API_CALL_LIMIT: int = 500  # 每日最多调用 AI 次数
    
    # 日志配置
    LOG_FILE: str = "trade.log"  # 日志文件路径
    LOG_LEVEL: str = "INFO"      # 日志级别
    
    # 代理配置 (可选，解决 403 地域限制)
    # 格式: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
    HTTP_PROXY: str = ""
    HTTPS_PROXY: str = ""
    
    # Telegram 警报 (普通级别)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    
    # Telegram 紧急 Bot (极端行情专用)
    TELEGRAM_URGENT_BOT_TOKEN: str = ""
    TELEGRAM_URGENT_CHAT_ID: str = ""
    
    # ==================== 大单监控 - Lighter ====================
    LARGE_ORDER_MIN_VALUE_MAJOR: float = 1000000.0  # 主流币 $1M
    LARGE_ORDER_MIN_VALUE_OTHER: float = 100000.0   # 其他币 $100K
    MAJOR_MARKET_IDS: str = "0,1,2,7,8,9,25"  # ETH, BTC, SOL, XRP, LINK, AVAX, BNB
    
    # 市场监控范围: "all", "perp", 或指定ID如 "0,1,2,3"
    MONITOR_MARKETS: str = ""
    
    # ==================== 大单监控 - Binance (VWAP 滑点 + 分级告警) ====================
    # 分级滑点阈值
    SLIPPAGE_THRESHOLD_LOW: float = 0.5     # 0.5% -> LOW (日志记录)
    SLIPPAGE_THRESHOLD_MED: float = 2.0     # 2.0% -> MEDIUM (普通推送)
    SLIPPAGE_THRESHOLD_HIGH: float = 10.0   # 10% -> HIGH (紧急推送)
    
    # 旧配置兼容 (单一阈值)
    SLIPPAGE_THRESHOLD: Optional[float] = None
    
    # 最低金额阈值 (低于此值不计算滑点)
    MIN_ORDER_VALUE_SPOT: float = 50000.0  # 现货 $50K
    MIN_ORDER_VALUE_FUTURES: float = 20000.0  # 合约 $20K
    
    # 订单簿缓存档位数
    ORDERBOOK_DEPTH: int = 50
    
    # 剔除前 N 档 (减少虚单干扰)
    SKIP_TOP_LEVELS: int = 1
    
    # 监控开关
    BINANCE_MONITOR_FUTURES: bool = True
    BINANCE_MONITOR_SPOT: bool = True
    
    # ==================== 价格异常警报 ====================
    PRICE_PUMP_THRESHOLD: float = 10.0  # 拉升阈值 (%)
    PRICE_DUMP_THRESHOLD: float = -10.0  # 暴跌阈值 (%)
    PRICE_TIME_WINDOW: int = 60  # 时间窗口 (秒)
    PRICE_COOLDOWN: int = 120  # 冷却时间 (秒)
    
    # Lighter 精度常量
    # 价格精度: 2位小数 (5000.00 -> 500000)
    PRICE_SCALE: int = 100
    # 数量精度: ETH 4位小数 (0.1 ETH -> 1000)
    BASE_AMOUNT_SCALE: int = 10000
    # USDC 精度: 6位小数
    USDC_SCALE: int = 1000000
    
    @field_validator('LIGHTER_ACCOUNT_INDEX', mode='before')
    @classmethod
    def parse_account_index(cls, v):
        """处理空字符串的情况"""
        if v == '' or v is None:
            return 0
        return int(v)
    
    @field_validator('LIGHTER_API_KEY_INDEX', mode='before')
    @classmethod
    def parse_api_key_index(cls, v):
        """处理空字符串的情况"""
        if v == '' or v is None:
            return 3
        return int(v)
    
    @field_validator('TRADING_MARKET_INDEX', mode='before')
    @classmethod
    def parse_market_index(cls, v):
        """处理空字符串的情况"""
        if v == '' or v is None:
            return 0
        return int(v)
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
