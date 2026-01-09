# Project Audit Report

**Date**: 2026-01-09
**Scope**: Codebase Structure, `engine`, `risk`, `scripts`

## 🚨 Critical Issues

### 1. Naming Collision & Logic Break in Risk Module
-   **Problem**: There are two conflicting `RiskManager` classes:
    -   `risk/position_sizer.py`: Defines `RiskManager` (Focus: Position Sizing, Kelly Criterion).
    -   `risk/manager.py`: Defines `RiskManager` (Focus: Hard Checks, Circuit Breaker).
-   **Impact**: `risk/__init__.py` exports the one from `position_sizer.py`. However, `ExecutionEngine` expects the interface of the one from `manager.py` (specifically `check_order()` and `on_fill()`).
-   **Consequence**: `TradingBot` instantiates the "Sizing" RiskManager and passes it to `ExecutionEngine`. When `ExecutionEngine` calls `self.risk_manager.check_order()`, it will crash with `AttributeError`.

### 2. Memory Leak in ExecutionEngine
-   **Problem**: `_exchange_order_map` (Dict[str, str]) tracks `exchange_order_id` -> `task_id`.
-   **Trace**: It is populated in `submit` (via `_execute_order` > `result.order_id`), but **NEVER removed**.
-   **Impact**: While `_tasks` and `_completed` are cleaned up by `_cleanup_loop`, this map will grow indefinitely until OOM.

## ⚠️ Major Issues

### 1. Potential Race Condition in WebSocket Fills
-   **Problem**: In `ExecutionEngine._on_ws_fill`, logic relies on `task` being present in `_tasks`.
-   **Scenario**: If `_cleanup_loop` runs *before* the WS fill arrives (e.g. strict TTL or delayed WS message), the fill is ignored.
-   **Risk**: Risk state might not update if fills arrive very late for expired tasks.

### 2. Unclosed ClientSession in Monitor Script
-   **Problem**: `scripts/run_multi_market_monitor.py` does not cleanly close `aiohttp.ClientSession` on `KeyboardInterrupt` in some paths (e.g. main loop exception), causing noisy error logs on exit.

## ℹ️ Minor Findings

1.  **Dependencies**: `requirements.txt` matches used packages (`lighter-python`, `pydantic`, `fastapi`).
2.  **Configuration**: `config.py` uses `pydantic-settings` correctly for type safety.
3.  **Lighter Connector**: `connectors/lighter/client.py` implements robust retry and rate limiting logic.

## 📋 Recommendations

### 1. Refactor Risk Module (Immediate)
-   **Rename** `risk/position_sizer.py` -> class `RiskManager` to `class PositionSizer`.
-   **Update** `risk/__init__.py` to export both `RiskManager` (from `manager.py`) and `PositionSizer`.
-   **Update** `TradingBot` to instantiate both:
    -   `PositionSizer` for `_calculate_position_size`.
    -   `RiskManager` for `ExecutionEngine` checks.

### 2. Fix Memory Leak (Immediate)
-   **Action**: In `_cleanup_expired_tasks`, when deleting a task, also delete its entry in `_exchange_order_map`.
-   **Implementation**: Need to reverse look-up or store `exchange_order_id` in `OrderTask` reliably to delete from map.

### 3. Improve Type Safety
-   Ensure `ExecutionEngine` imports `RiskManager` type from the correct file for static checking to avoid future confusion.
