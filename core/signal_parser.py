"""
AI 响应 JSON 解析器 - 提取结构化交易信号
"""
import json
import re
from dataclasses import dataclass
from typing import Dict, Any, List

from core.exceptions import SignalParseError


@dataclass
class TradingSignal:
    """交易信号数据结构"""
    action: str  # "buy" | "sell" | "hold"
    confidence: float
    position_size_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    reason: str
    analysis: Dict[str, Any]
    raw_response: str  # 原始响应（用于日志）


class SignalParser:
    """解析 AI 返回的交易信号"""
    
    # 用于从混杂文本中提取 JSON 的正则
    JSON_PATTERN = re.compile(r'\{[\s\S]*\}')
    
    VALID_ACTIONS = {"buy", "sell", "hold"}
    
    def parse(self, raw_response: str) -> TradingSignal:
        """
        解析 AI 响应为结构化信号
        
        Args:
            raw_response: AI 返回的原始文本
            
        Returns:
            TradingSignal 对象
            
        Raises:
            SignalParseError: 解析失败
        """
        # 尝试提取 JSON
        json_match = self.JSON_PATTERN.search(raw_response)
        if not json_match:
            raise SignalParseError("响应中未找到有效的 JSON 结构")
        
        json_str = json_match.group()
        
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise SignalParseError(f"JSON 解析失败: {e}")
        
        # 验证必填字段
        self._validate_fields(data)
        
        return TradingSignal(
            action=data["action"].lower(),
            confidence=float(data["confidence"]),
            position_size_pct=float(data.get("position_size_pct", 0)),
            stop_loss_pct=float(data.get("stop_loss_pct", 2.0)),
            take_profit_pct=float(data.get("take_profit_pct", 5.0)),
            reason=data.get("reason", ""),
            analysis=data.get("analysis", {}),
            raw_response=raw_response
        )
    
    def _validate_fields(self, data: dict):
        """验证必填字段"""
        required = ["action", "confidence"]
        for field in required:
            if field not in data:
                raise SignalParseError(f"缺少必填字段: {field}")
        
        if data["action"].lower() not in self.VALID_ACTIONS:
            raise SignalParseError(f"无效的 action 值: {data['action']}")
        
        confidence = float(data["confidence"])
        if not 0.0 <= confidence <= 1.0:
            raise SignalParseError(f"confidence 超出范围 [0, 1]: {confidence}")
    
    def extract_key_signals(self, analysis: Dict[str, Any]) -> List[str]:
        """提取关键信号列表"""
        return analysis.get("key_signals", [])
