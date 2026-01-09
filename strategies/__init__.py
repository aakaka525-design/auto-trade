"""Strategies package"""
from strategies.base import BaseStrategy, Signal, SignalAction
from strategies.hft_scalper import HFTScalperStrategy, ScalperConfig
from strategies.momentum import MomentumStrategy, MomentumConfig
from strategies.trend_follower import TrendFollowerStrategy, TrendConfig

__all__ = [
    "BaseStrategy", "Signal", "SignalAction",
    "HFTScalperStrategy", "ScalperConfig",
    "MomentumStrategy", "MomentumConfig",
    "TrendFollowerStrategy", "TrendConfig",
]
