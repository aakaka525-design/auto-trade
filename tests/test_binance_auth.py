"""
单元测试: BinanceAuth & SymbolConverter

运行: pytest tests/test_binance_auth.py -v
"""
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock


class TestSymbolConverter:
    """SymbolConverter 测试"""
    
    def test_to_binance_spot(self):
        """测试转换为 Binance 现货符号"""
        from connectors.binance.auth import SymbolConverter
        
        # 标准格式
        assert SymbolConverter.to_binance("ETH-USDC") == "ETHUSDT"
        assert SymbolConverter.to_binance("BTC-USDC") == "BTCUSDT"
        assert SymbolConverter.to_binance("SOL-USDC") == "SOLUSDT"
        
        # USDT 对
        assert SymbolConverter.to_binance("ETH-USDT") == "ETHUSDT"
        
        # 已经是 Binance 格式
        assert SymbolConverter.to_binance("ETHUSDT") == "ETHUSDT"
    
    def test_from_binance(self):
        """测试从 Binance 格式转换回来"""
        from connectors.binance.auth import SymbolConverter
        
        assert SymbolConverter.from_binance("ETHUSDT") == "ETH-USDT"
        assert SymbolConverter.from_binance("BTCUSDT") == "BTC-USDT"
        assert SymbolConverter.from_binance("SOLUSDC") == "SOL-USDC"
    
    def test_round_trip(self):
        """测试往返转换"""
        from connectors.binance.auth import SymbolConverter
        
        symbols = ["ETH-USDT", "BTC-USDT", "SOL-USDT"]
        for s in symbols:
            binance = SymbolConverter.to_binance(s)
            back = SymbolConverter.from_binance(binance)
            assert back == s


class TestBinanceAuth:
    """BinanceAuth 测试"""
    
    def test_hmac_signature(self):
        """测试 HMAC 签名生成"""
        from connectors.binance.auth import BinanceAuth
        
        auth = BinanceAuth(
            api_key="test_key",
            api_secret="test_secret",
            sign_type="HMAC"
        )
        
        params = {"symbol": "ETHUSDT", "side": "BUY"}
        signature = auth.sign(params)
        
        # 验证签名是十六进制字符串
        assert isinstance(signature, str)
        assert len(signature) == 64  # SHA256 = 32 bytes = 64 hex chars
        
        # 验证签名一致性 (相同参数应生成相同签名)
        signature2 = auth.sign(params)
        assert signature == signature2
    
    def test_sign_params_adds_timestamp(self):
        """测试 sign_params 添加时间戳和签名"""
        from connectors.binance.auth import BinanceAuth
        
        auth = BinanceAuth(
            api_key="test_key",
            api_secret="test_secret",
            sign_type="HMAC"
        )
        
        params = {"symbol": "ETHUSDT"}
        signed = auth.sign_params(params)
        
        assert "timestamp" in signed
        assert "signature" in signed
        assert signed["symbol"] == "ETHUSDT"
    
    def test_get_headers(self):
        """测试获取请求头"""
        from connectors.binance.auth import BinanceAuth
        
        auth = BinanceAuth(
            api_key="my_api_key",
            api_secret="my_secret",
            sign_type="HMAC"
        )
        
        headers = auth.get_headers()
        assert headers["X-MBX-APIKEY"] == "my_api_key"


class TestAlertStorage:
    """AlertStorage 测试"""
    
    def test_save_and_retrieve(self, tmp_path):
        """测试保存和检索告警"""
        from monitoring.alert_storage import AlertStorage, AlertRecord
        
        db_path = tmp_path / "test_alerts.db"
        storage = AlertStorage(str(db_path))
        
        # 保存告警
        record = AlertRecord(
            id=None,
            timestamp=datetime.now(),
            market="spot",
            symbol="ETH-USDT",
            alert_type="trade",
            level="medium",
            value=50000.0,
            price=3097.5,
            slippage=1.2,
            side="BUY"
        )
        record_id = storage.save(record)
        
        assert record_id > 0
        
        # 检索告警
        alerts = storage.get_recent(limit=1)
        assert len(alerts) == 1
        assert alerts[0].symbol == "ETH-USDT"
        assert alerts[0].level == "medium"
    
    def test_stats_by_level(self, tmp_path):
        """测试按级别统计"""
        from monitoring.alert_storage import AlertStorage, AlertRecord
        
        db_path = tmp_path / "test_alerts2.db"
        storage = AlertStorage(str(db_path))
        
        # 保存多个告警
        for level in ["low", "low", "medium", "high"]:
            storage.save(AlertRecord(
                id=None,
                timestamp=datetime.now(),
                market="spot",
                symbol="ETH-USDT",
                alert_type="trade",
                level=level,
                value=50000.0,
                price=3097.5,
                slippage=1.2,
                side="BUY"
            ))
        
        stats = storage.get_stats_by_level()
        assert stats["low"] == 2
        assert stats["medium"] == 1
        assert stats["high"] == 1
