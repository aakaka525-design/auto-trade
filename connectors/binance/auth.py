"""
Binance 认证和签名工具

支持两种认证方式:
1. HMAC-SHA256 (传统 API Key + Secret)
2. Ed25519 (自生成公钥/私钥，更安全)
"""
import base64
import hmac
import hashlib
import time
import logging
from typing import Dict, Any, Optional, Literal
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)


# ==================== Ed25519 支持 ====================

def _load_ed25519_private_key(private_key_pem: str):
    """加载 Ed25519 私钥 (PEM 格式)"""
    try:
        from cryptography.hazmat.primitives import serialization
        
        # 处理可能的格式问题
        if not private_key_pem.startswith("-----"):
            # 可能是 base64 编码的原始密钥
            private_key_pem = (
                "-----BEGIN PRIVATE KEY-----\n"
                + private_key_pem
                + "\n-----END PRIVATE KEY-----"
            )
        
        return serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None
        )
    except ImportError:
        raise ImportError("需要安装 cryptography 库: pip install cryptography")
    except Exception as e:
        raise ValueError(f"无效的 Ed25519 私钥: {e}")


def generate_ed25519_keypair() -> tuple[str, str]:
    """
    生成 Ed25519 密钥对
    
    Returns:
        (private_key_pem, public_key_pem) 元组
    
    使用方法:
    1. 调用此函数生成密钥对
    2. 将 public_key 复制到 Binance API 管理页面
    3. 将 private_key 保存到 .env 文件
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
        
        # 生成私钥
        private_key = ed25519.Ed25519PrivateKey.generate()
        
        # 导出私钥 PEM
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode()
        
        # 导出公钥 PEM
        public_key = private_key.public_key()
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        
        return private_pem, public_pem
        
    except ImportError:
        raise ImportError("需要安装 cryptography 库: pip install cryptography")


# ==================== 认证管理器 ====================

class BinanceAuth:
    """
    Binance API 认证管理器
    
    支持两种认证方式:
    1. HMAC-SHA256 (传统方式)
       - 需要: api_key, api_secret
    
    2. Ed25519 (自生成密钥，更安全)
       - 需要: api_key, private_key (PEM 格式)
       - 需要在 Binance 控制台注册对应的公钥
    
    使用示例:
    ```python
    # HMAC 方式
    auth = BinanceAuth(
        api_key="your_key",
        api_secret="your_secret"
    )
    
    # Ed25519 方式
    auth = BinanceAuth(
        api_key="your_key",
        private_key=open("private_key.pem").read(),
        sign_type="Ed25519"
    )
    ```
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str = "",
        private_key: str = "",
        sign_type: Literal["HMAC", "Ed25519"] = "HMAC"
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self._private_key_pem = private_key
        self.sign_type = sign_type
        self._time_offset: int = 0
        
        # 加载 Ed25519 私钥
        self._ed25519_key = None
        if sign_type == "Ed25519":
            if not private_key:
                raise ValueError("Ed25519 签名需要提供 private_key")
            self._ed25519_key = _load_ed25519_private_key(private_key)
    
    def sign(self, params: Dict[str, Any]) -> str:
        """
        生成签名
        
        根据 sign_type 自动选择签名方式
        """
        query_string = urlencode(params)
        
        if self.sign_type == "Ed25519":
            return self._sign_ed25519(query_string)
        else:
            return self._sign_hmac(query_string)
    
    def _sign_hmac(self, query_string: str) -> str:
        """HMAC-SHA256 签名"""
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _sign_ed25519(self, query_string: str) -> str:
        """Ed25519 签名"""
        if not self._ed25519_key:
            raise ValueError("Ed25519 私钥未加载")
        
        signature_bytes = self._ed25519_key.sign(query_string.encode('utf-8'))
        return base64.b64encode(signature_bytes).decode()
    
    def get_timestamp(self) -> int:
        """获取调整后的时间戳 (ms)"""
        return int(time.time() * 1000) + self._time_offset
    
    def sign_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """为请求参数添加时间戳和签名"""
        params = dict(params)
        params['timestamp'] = self.get_timestamp()
        params['signature'] = self.sign(params)
        return params
    
    def get_headers(self) -> Dict[str, str]:
        """获取认证请求头"""
        return {
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded',
        }
    
    async def sync_server_time(self, base_url: str) -> int:
        """同步服务器时间"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/api/v3/time") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        server_time = data['serverTime']
                        local_time = int(time.time() * 1000)
                        self._time_offset = server_time - local_time
                        logger.info(f"Binance 时间同步: offset={self._time_offset}ms")
                        return self._time_offset
        except Exception as e:
            logger.error(f"时间同步失败: {e}")
        return 0
    
    @property
    def time_offset(self) -> int:
        return self._time_offset



class SymbolConverter:
    """
    交易对符号格式转换器
    
    支持:
    - 统一格式 <-> Binance 格式转换
    - 跨交易所自动转换 (USDC <-> USDT)
    
    统一格式: "ETH-USDT" (带连字符)
    Binance 格式: "ETHUSDT" (无连字符)
    """
    
    # 已知的 quote 货币列表
    QUOTE_ASSETS = ['USDT', 'USDC', 'BUSD', 'BTC', 'ETH', 'BNB']
    
    # 稳定币映射 (可互换)
    STABLECOIN_MAPPING = {
        'USDC': 'USDT',  # Lighter 用 USDC -> Binance 用 USDT
        'USDT': 'USDC',  # Binance 用 USDT -> Lighter 用 USDC
    }
    
    @staticmethod
    def to_binance(symbol: str, auto_convert_stable: bool = True) -> str:
        """
        统一格式 -> Binance 格式
        
        Args:
            symbol: 统一格式符号 (如 "ETH-USDC")
            auto_convert_stable: 是否自动转换稳定币 (USDC -> USDT)
        
        Examples:
            "ETH-USDT" -> "ETHUSDT"
            "ETH-USDC" -> "ETHUSDT" (如果 auto_convert_stable=True)
        """
        symbol = symbol.upper()
        
        # 自动转换稳定币
        if auto_convert_stable and '-USDC' in symbol:
            symbol = symbol.replace('-USDC', '-USDT')
        
        return symbol.replace("-", "")
    
    @staticmethod
    def to_lighter(symbol: str, auto_convert_stable: bool = True) -> str:
        """
        任意格式 -> Lighter 格式
        
        Args:
            symbol: 任意格式符号
            auto_convert_stable: 是否自动转换稳定币 (USDT -> USDC)
        
        Examples:
            "ETH-USDT" -> "ETH-USDC"
            "ETHUSDT" -> "ETH-USDC"
        """
        symbol = symbol.upper()
        
        # 处理无连字符格式
        if '-' not in symbol:
            # 尝试匹配已知 quote
            for quote in SymbolConverter.QUOTE_ASSETS:
                if symbol.endswith(quote):
                    symbol = f"{symbol[:-len(quote)]}-{quote}"
                    break
        
        # 自动转换稳定币
        if auto_convert_stable and '-USDT' in symbol:
            symbol = symbol.replace('-USDT', '-USDC')
        
        return symbol
    
    @classmethod
    def from_binance(cls, binance_symbol: str) -> str:
        """
        Binance 格式 -> 统一格式
        
        "ETHUSDT" -> "ETH-USDT"
        "BTCUSDC" -> "BTC-USDC"
        """
        binance_symbol = binance_symbol.upper()
        
        # 尝试匹配已知的 quote 货币
        for quote in cls.QUOTE_ASSETS:
            if binance_symbol.endswith(quote):
                base = binance_symbol[:-len(quote)]
                return f"{base}-{quote}"
        
        # 默认假设后 4 位是 quote (USDT)
        return f"{binance_symbol[:-4]}-{binance_symbol[-4:]}"
    
    @staticmethod
    def normalize(symbol: str) -> str:
        """
        标准化符号格式
        
        确保符号为 "BASE-QUOTE" 格式
        """
        symbol = symbol.upper().strip()
        
        # 已经是标准格式
        if '-' in symbol:
            return symbol
        
        # 尝试匹配 quote 并添加连字符
        for quote in SymbolConverter.QUOTE_ASSETS:
            if symbol.endswith(quote):
                return f"{symbol[:-len(quote)]}-{quote}"
        
        return symbol

