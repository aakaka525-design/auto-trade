"""
Prompt 构造器 - 生成发送给云端 AI 的提示词
"""
from datetime import datetime
from typing import Dict, Any


class PromptBuilder:
    """Prompt 构造器"""
    
    # System Prompt 模板 (TPL_CRYPTO_ANALYST_V1)
    SYSTEM_PROMPT = """# 身份设定
你是一位资深加密货币量化交易员，拥有 10 年以上的技术分析和算法交易经验。
你的专长是永续合约交易，擅长在 15 分钟到 4 小时的时间框架内进行波段操作。

# 核心原则
1. 风险优先：永远把保护本金放在首位
2. 概率思维：交易是概率游戏，专注于正期望值的机会
3. 纪律执行：严格按照技术信号执行，不受情绪干扰
4. 顺势而为：不逆势抄底或摸顶

# 技术指标解读框架
- RSI > 70: 超买区，警惕回调
- RSI < 30: 超卖区，观察反弹信号
- MACD 金叉 + 柱状图放大: 多头动能增强
- MACD 死叉 + 柱状图缩小: 空头动能增强
- 价格站上 EMA20: 短期趋势偏多
- 价格跌破 EMA20: 短期趋势偏空

# 输出格式要求
你必须使用以下 JSON 格式输出，不要添加任何其他文字说明：

{
  "action": "buy" | "sell" | "hold",
  "confidence": 0.0-1.0,
  "position_size_pct": 0-100,
  "stop_loss_pct": 1-10,
  "take_profit_pct": 1-30,
  "reason": "简短决策理由（不超过50字）",
  "analysis": {
    "trend": "bullish" | "bearish" | "sideways",
    "support": 数字,
    "resistance": 数字,
    "key_signals": ["信号1", "信号2"]
  }
}

# 特别注意
- confidence < 0.6 时必须输出 action: "hold"
- 始终设置合理的 stop_loss_pct (建议 2-5%)
- position_size_pct 应与 confidence 成正比"""

    # User Prompt 模板
    USER_PROMPT_TEMPLATE = """# 市场数据快照
交易对：{symbol}
当前时间：{timestamp}
时间框架：{timeframe}

## 价格数据 (最近K线摘要)
- 当前价格: {current_price}
- 最高价: {high_price}
- 最低价: {low_price}
- 24H涨跌幅: {price_change_24h}%

## 技术指标
- RSI(14): {rsi_value}
- MACD: {macd_line}, Signal: {signal_line}, Hist: {histogram}
- EMA20: {ema_20}, EMA50: {ema_50}
- 布林带: Upper={bb_upper}, Middle={bb_middle}, Lower={bb_lower}
- ATR(14): {atr_value}

## 成交量分析
- 当前成交量: {current_volume}
- 成交量MA(20): {volume_ma}
- 量能比: {volume_ratio}x

## 持仓状态
- 当前持仓: {position_side} {position_size} @ {entry_price}
- 未实现盈亏: {unrealized_pnl}%
- 可用余额: {available_balance} USDC

请基于以上数据分析当前市场状态并给出交易信号。"""

    def get_system_prompt(self) -> str:
        """获取系统提示词"""
        return self.SYSTEM_PROMPT
    
    def build_user_prompt(self, market_data: Dict[str, Any]) -> str:
        """
        构建用户提示词
        
        Args:
            market_data: 市场数据字典，包含价格、指标等
            
        Returns:
            格式化的用户提示词
        """
        # 填充默认值
        data = {
            "symbol": market_data.get("symbol", "BTC-USDC"),
            "timestamp": market_data.get("timestamp", datetime.now().isoformat()),
            "timeframe": market_data.get("timeframe", "15m"),
            "current_price": market_data.get("current_price", 0),
            "high_price": market_data.get("high_price", 0),
            "low_price": market_data.get("low_price", 0),
            "price_change_24h": market_data.get("price_change_24h", 0),
            "rsi_value": market_data.get("rsi_value", 50),
            "macd_line": market_data.get("macd_line", 0),
            "signal_line": market_data.get("signal_line", 0),
            "histogram": market_data.get("histogram", 0),
            "ema_20": market_data.get("ema_20", 0),
            "ema_50": market_data.get("ema_50", 0),
            "bb_upper": market_data.get("bb_upper", 0),
            "bb_middle": market_data.get("bb_middle", 0),
            "bb_lower": market_data.get("bb_lower", 0),
            "atr_value": market_data.get("atr_value", 0),
            "current_volume": market_data.get("current_volume", 0),
            "volume_ma": market_data.get("volume_ma", 0),
            "volume_ratio": market_data.get("volume_ratio", 1.0),
            "position_side": market_data.get("position_side", "无"),
            "position_size": market_data.get("position_size", 0),
            "entry_price": market_data.get("entry_price", 0),
            "unrealized_pnl": market_data.get("unrealized_pnl", 0),
            "available_balance": market_data.get("available_balance", 0),
        }
        
        return self.USER_PROMPT_TEMPLATE.format(**data)
