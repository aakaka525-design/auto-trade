"""
配置热更新模块

支持运行时修改配置而无需重启服务，使用文件监控或信号触发。

使用方法:
```python
from monitoring.hot_config import HotConfig, get_hot_config

# 初始化
config = get_hot_config()

# 读取配置 (自动热更新)
threshold = config.get("SLIPPAGE_THRESHOLD_LOW", 0.5)

# 手动触发重新加载
config.reload()
```
"""
import os
import json
import signal
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, Callable
from threading import Lock

logger = logging.getLogger(__name__)


class HotConfig:
    """
    配置热更新管理器
    
    特性:
    - 支持 .env 和 JSON 配置文件
    - 文件修改时间检测自动重载
    - SIGHUP 信号触发重载
    - 线程安全
    - 变更回调通知
    """
    
    def __init__(
        self,
        config_path: str = ".env",
        json_path: Optional[str] = None,
        auto_reload: bool = True,
        check_interval: float = 5.0
    ):
        self.config_path = Path(config_path)
        self.json_path = Path(json_path) if json_path else None
        self.auto_reload = auto_reload
        self.check_interval = check_interval
        
        self._config: Dict[str, Any] = {}
        self._last_modified: float = 0
        self._lock = Lock()
        self._callbacks: list[Callable[[Dict[str, Any]], None]] = []
        self._last_check = datetime.now()
        
        # 初始加载
        self._load()
        
        # 注册 SIGHUP 信号处理
        self._register_signal_handler()
    
    def _register_signal_handler(self):
        """注册 SIGHUP 信号处理器"""
        try:
            signal.signal(signal.SIGHUP, self._signal_handler)
            logger.debug("已注册 SIGHUP 信号处理器")
        except (OSError, ValueError):
            # Windows 不支持 SIGHUP
            logger.debug("SIGHUP 信号不可用")
    
    def _signal_handler(self, signum, frame):
        """信号处理器"""
        logger.info("收到 SIGHUP 信号，重新加载配置...")
        self.reload()
    
    def _load(self):
        """加载配置文件"""
        with self._lock:
            new_config = {}
            
            # 加载 .env 文件
            if self.config_path.exists():
                new_config.update(self._parse_env(self.config_path))
                self._last_modified = self.config_path.stat().st_mtime
            
            # 加载 JSON 文件 (覆盖 .env)
            if self.json_path and self.json_path.exists():
                with open(self.json_path) as f:
                    new_config.update(json.load(f))
                json_mtime = self.json_path.stat().st_mtime
                self._last_modified = max(self._last_modified, json_mtime)
            
            # 检查变更
            old_config = self._config
            self._config = new_config
            
            if old_config and old_config != new_config:
                self._notify_changes(new_config)
    
    def _parse_env(self, path: Path) -> Dict[str, str]:
        """解析 .env 文件"""
        config = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"\'')
                    config[key] = value
        return config
    
    def _check_for_updates(self):
        """检查文件是否更新"""
        if not self.auto_reload:
            return
        
        now = datetime.now()
        if (now - self._last_check).total_seconds() < self.check_interval:
            return
        
        self._last_check = now
        
        current_mtime = 0
        if self.config_path.exists():
            current_mtime = self.config_path.stat().st_mtime
        if self.json_path and self.json_path.exists():
            current_mtime = max(current_mtime, self.json_path.stat().st_mtime)
        
        if current_mtime > self._last_modified:
            logger.info("检测到配置文件变更，重新加载...")
            self._load()
    
    def reload(self):
        """手动重新加载配置"""
        logger.info("手动重新加载配置...")
        self._load()
    
    def get(self, key: str, default: Any = None, cast: type = None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键
            default: 默认值
            cast: 类型转换 (如 int, float, bool)
        """
        self._check_for_updates()
        
        with self._lock:
            value = self._config.get(key, default)
        
        if cast is not None and value is not None:
            try:
                if cast == bool:
                    return str(value).lower() in ('true', '1', 'yes')
                return cast(value)
            except (ValueError, TypeError):
                return default
        
        return value
    
    def get_float(self, key: str, default: float = 0.0) -> float:
        """获取浮点数配置"""
        return self.get(key, default, float)
    
    def get_int(self, key: str, default: int = 0) -> int:
        """获取整数配置"""
        return self.get(key, default, int)
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """获取布尔配置"""
        return self.get(key, default, bool)
    
    def on_change(self, callback: Callable[[Dict[str, Any]], None]):
        """注册配置变更回调"""
        self._callbacks.append(callback)
    
    def _notify_changes(self, new_config: Dict[str, Any]):
        """通知配置变更"""
        logger.info(f"配置已更新，共 {len(new_config)} 项")
        for callback in self._callbacks:
            try:
                callback(new_config)
            except Exception as e:
                logger.error(f"配置变更回调异常: {e}")


# 全局实例
_hot_config: Optional[HotConfig] = None


def get_hot_config() -> HotConfig:
    """获取全局热配置实例"""
    global _hot_config
    if _hot_config is None:
        _hot_config = HotConfig()
    return _hot_config
