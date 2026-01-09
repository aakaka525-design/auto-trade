"""
技术指标计算模块
使用 pandas-ta 或自定义实现
"""
import numpy as np
from typing import Dict, Any, List
from dataclasses import dataclass


@dataclass
class TechnicalIndicators:
    """技术指标数据结构"""
    # 价格
    current_price: float
    high_price: float
    low_price: float
    price_change_24h: float
    
    # RSI
    rsi_value: float
    
    # MACD
    macd_line: float
    signal_line: float
    histogram: float
    
    # 移动平均
    ema_20: float
    ema_50: float
    
    # 布林带
    bb_upper: float
    bb_middle: float
    bb_lower: float
    
    # ATR
    atr_value: float
    
    # 成交量
    current_volume: float
    volume_ma: float
    volume_ratio: float


class IndicatorCalculator:
    """技术指标计算器"""
    
    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14) -> float:
        """
        计算 RSI 指标
        
        Args:
            closes: 收盘价列表
            period: RSI 周期
            
        Returns:
            RSI 值 (0-100)
        """
        if len(closes) < period + 1:
            return 50.0  # 数据不足返回中性值
        
        closes = np.array(closes)
        deltas = np.diff(closes)
        
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return round(rsi, 2)
    
    @staticmethod
    def calculate_ema(values: List[float], period: int) -> float:
        """
        计算 EMA
        
        Args:
            values: 价格列表
            period: EMA 周期
            
        Returns:
            EMA 值
        """
        if len(values) < period:
            return values[-1] if values else 0
        
        values = np.array(values)
        multiplier = 2 / (period + 1)
        
        ema = values[0]
        for price in values[1:]:
            ema = (price - ema) * multiplier + ema
        
        return round(ema, 2)
    
    @staticmethod
    def calculate_macd(
        closes: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> Dict[str, float]:
        """
        计算 MACD
        
        Returns:
            {"macd": float, "signal": float, "histogram": float}
        """
        if len(closes) < slow_period:
            return {"macd": 0, "signal": 0, "histogram": 0}
        
        calc = IndicatorCalculator
        
        ema_fast = calc.calculate_ema(closes, fast_period)
        ema_slow = calc.calculate_ema(closes, slow_period)
        
        macd_line = ema_fast - ema_slow
        
        # 简化的 signal line 计算
        signal_line = macd_line * 0.8  # 近似值
        histogram = macd_line - signal_line
        
        return {
            "macd": round(macd_line, 2),
            "signal": round(signal_line, 2),
            "histogram": round(histogram, 2)
        }
    
    @staticmethod
    def calculate_bollinger_bands(
        closes: List[float],
        period: int = 20,
        std_dev: float = 2.0
    ) -> Dict[str, float]:
        """
        计算布林带
        
        Returns:
            {"upper": float, "middle": float, "lower": float}
        """
        if len(closes) < period:
            price = closes[-1] if closes else 0
            return {"upper": price, "middle": price, "lower": price}
        
        closes = np.array(closes[-period:])
        middle = np.mean(closes)
        std = np.std(closes)
        
        return {
            "upper": round(middle + std_dev * std, 2),
            "middle": round(middle, 2),
            "lower": round(middle - std_dev * std, 2)
        }
    
    @staticmethod
    def calculate_atr(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14
    ) -> float:
        """
        计算 ATR (Average True Range)
        """
        if len(highs) < period + 1:
            return 0.0
        
        true_ranges = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            true_ranges.append(tr)
        
        atr = np.mean(true_ranges[-period:])
        return round(atr, 2)
    
    @classmethod
    def calculate_all(
        cls,
        ohlcv_data: List[Dict[str, float]]
    ) -> TechnicalIndicators:
        """
        计算所有技术指标
        
        Args:
            ohlcv_data: OHLCV 数据列表
                [{"open": x, "high": x, "low": x, "close": x, "volume": x}, ...]
        
        Returns:
            TechnicalIndicators 对象
        """
        if not ohlcv_data:
            raise ValueError("OHLCV 数据为空")
        
        closes = [d["close"] for d in ohlcv_data]
        highs = [d["high"] for d in ohlcv_data]
        lows = [d["low"] for d in ohlcv_data]
        volumes = [d["volume"] for d in ohlcv_data]
        
        current = ohlcv_data[-1]
        
        # 计算 24H 涨跌幅 (假设数据是 15 分钟周期，24H = 96 根 K 线)
        price_change_24h = 0.0
        if len(closes) >= 96:
            price_change_24h = ((closes[-1] - closes[-96]) / closes[-96]) * 100
        
        # MACD
        macd = cls.calculate_macd(closes)
        
        # 布林带
        bb = cls.calculate_bollinger_bands(closes)
        
        # 成交量
        volume_ma = np.mean(volumes[-20:]) if len(volumes) >= 20 else volumes[-1]
        volume_ratio = current["volume"] / volume_ma if volume_ma > 0 else 1.0
        
        return TechnicalIndicators(
            current_price=current["close"],
            high_price=max(highs[-20:]) if len(highs) >= 20 else current["high"],
            low_price=min(lows[-20:]) if len(lows) >= 20 else current["low"],
            price_change_24h=round(price_change_24h, 2),
            rsi_value=cls.calculate_rsi(closes),
            macd_line=macd["macd"],
            signal_line=macd["signal"],
            histogram=macd["histogram"],
            ema_20=cls.calculate_ema(closes, 20),
            ema_50=cls.calculate_ema(closes, 50),
            bb_upper=bb["upper"],
            bb_middle=bb["middle"],
            bb_lower=bb["lower"],
            atr_value=cls.calculate_atr(highs, lows, closes),
            current_volume=current["volume"],
            volume_ma=round(volume_ma, 2),
            volume_ratio=round(volume_ratio, 2)
        )
