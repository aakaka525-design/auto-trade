"""
告警持久化模块

使用 SQLite 存储告警历史，支持查询和统计。
"""
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AlertRecord:
    """告警记录"""
    id: Optional[int]
    timestamp: datetime
    market: str          # spot | futures
    symbol: str          # ETH-USDT
    alert_type: str      # trade | bid | ask
    level: str           # low | medium | high
    value: float         # 金额
    price: float         # 成交/挂单价格
    slippage: float      # 滑点百分比
    side: Optional[str] = None  # BUY | SELL | BID | ASK


class AlertStorage:
    """
    SQLite 告警持久化
    
    使用示例:
    ```python
    storage = AlertStorage("alerts.db")
    
    # 保存告警
    storage.save(AlertRecord(
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
    ))
    
    # 查询最近告警
    alerts = storage.get_recent(limit=10)
    
    # 统计
    stats = storage.get_stats_by_level()
    ```
    """
    
    def __init__(self, db_path: str = "data/alerts.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    @contextmanager
    def _connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _init_db(self):
        """初始化数据库表"""
        with self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    level TEXT NOT NULL,
                    value REAL NOT NULL,
                    price REAL NOT NULL,
                    slippage REAL NOT NULL,
                    side TEXT
                )
            """)
            
            # 创建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp 
                ON alerts(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alerts_level 
                ON alerts(level)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alerts_symbol 
                ON alerts(symbol)
            """)
            
            logger.info(f"SQLite 数据库初始化完成: {self.db_path}")
    
    def save(self, alert: AlertRecord) -> int:
        """保存告警记录，返回 ID"""
        with self._connection() as conn:
            cursor = conn.execute("""
                INSERT INTO alerts (
                    timestamp, market, symbol, alert_type, 
                    level, value, price, slippage, side
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert.timestamp.isoformat(),
                alert.market,
                alert.symbol,
                alert.alert_type,
                alert.level,
                alert.value,
                alert.price,
                alert.slippage,
                alert.side
            ))
            return cursor.lastrowid
    
    def get_recent(self, limit: int = 100) -> List[AlertRecord]:
        """获取最近的告警"""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM alerts 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,)).fetchall()
            
            return [self._row_to_record(row) for row in rows]
    
    def get_by_symbol(self, symbol: str, limit: int = 50) -> List[AlertRecord]:
        """按币种查询"""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM alerts 
                WHERE symbol = ?
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (symbol, limit)).fetchall()
            
            return [self._row_to_record(row) for row in rows]
    
    def get_stats_by_level(self) -> Dict[str, int]:
        """按级别统计"""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT level, COUNT(*) as count 
                FROM alerts 
                GROUP BY level
            """).fetchall()
            
            return {row['level']: row['count'] for row in rows}
    
    def get_today_count(self) -> Dict[str, int]:
        """今日告警数量"""
        today = datetime.now().strftime('%Y-%m-%d')
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT level, COUNT(*) as count 
                FROM alerts 
                WHERE date(timestamp) = ?
                GROUP BY level
            """, (today,)).fetchall()
            
            return {row['level']: row['count'] for row in rows}
    
    def _row_to_record(self, row: sqlite3.Row) -> AlertRecord:
        """将数据库行转换为 AlertRecord"""
        return AlertRecord(
            id=row['id'],
            timestamp=datetime.fromisoformat(row['timestamp']),
            market=row['market'],
            symbol=row['symbol'],
            alert_type=row['alert_type'],
            level=row['level'],
            value=row['value'],
            price=row['price'],
            slippage=row['slippage'],
            side=row['side']
        )


# 全局存储实例
_storage: Optional[AlertStorage] = None


def get_alert_storage() -> AlertStorage:
    """获取全局存储实例"""
    global _storage
    if _storage is None:
        _storage = AlertStorage()
    return _storage
