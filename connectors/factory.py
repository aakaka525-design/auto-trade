"""
连接器工厂

设计模式: Factory Pattern
根据配置创建对应的交易所连接器实例。
"""
from typing import Type
from connectors.base import BaseConnector


class ConnectorFactory:
    """连接器工厂"""
    
    _registry: dict[str, Type[BaseConnector]] = {}
    
    @classmethod
    def register(cls, name: str, connector_class: Type[BaseConnector]) -> None:
        """注册连接器类"""
        cls._registry[name.lower()] = connector_class
    
    @classmethod
    def create(cls, name: str, config: dict) -> BaseConnector:
        """
        创建连接器实例
        
        Args:
            name: 连接器名称 (如 "lighter")
            config: 连接器配置
            
        Returns:
            BaseConnector 实例
            
        Raises:
            ValueError: 未知的连接器名称
        """
        connector_class = cls._registry.get(name.lower())
        if not connector_class:
            available = list(cls._registry.keys())
            raise ValueError(f"未知连接器: {name}，可用: {available}")
        
        return connector_class(config)
    
    @classmethod
    def available(cls) -> list[str]:
        """获取可用连接器列表"""
        return list(cls._registry.keys())


# 自动注册 Lighter 连接器
def _auto_register():
    try:
        from connectors.lighter.client import LighterConnector
        ConnectorFactory.register("lighter", LighterConnector)
    except ImportError:
        pass

_auto_register()
