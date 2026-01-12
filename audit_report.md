# Monitoring System Audit Report

**Date**: 2026-01-12
**Focus**: Binance Monitoring Module (`scripts/run_binance_monitor.py` & `monitoring/`)

---

## 🚨 Critical Stability Issues

### 1. Memory Leaks in State Trackers
- **`smart_filter.py`**: `SmartAlertFilter` accumulates `SymbolStats` objects for every symbol encountered. While it has a `cleanup_all_expired()` method, **it is never called** by the main loop. Over days of running with full-market monitoring, this will bloat memory.
- **`whale_tracker.py`**: `WhaleTracker` creates a `SymbolTracker` for every symbol. It has **no cleanup mechanism** for stale symbols. If the monitor cycles through different symbols or potential "spam" symbols appear, memory usage will grow indefinitely.

### 2. Unused Module
- **`price_monitor.py`**: This module defines `PriceMonitor` but appears to be **completely unused** in `run_binance_monitor.py` or `binance_processor.py`. It is dead code.

---

## ℹ️ Logic & Performance Verification

### 1. Book Imbalance (WBI v3.1)
- **Status**: ✅ **Excellent**.
- **Reasoning**: Includes robust state management (`_cleanup_zombie_states`), confirmation ticks (to prevent false alarms from flickering order books), and "Flip Protection" (handling trend reversals).
- **Recommendation**: Keep as is.

### 2. Smart Filter
- **Status**: ⚠️ **Needs Fix**.
- **Reasoning**: sophisticated `SortedList` based quantile calculation. However, the missing periodic cleanup is a ticking time bomb for long-running processes.

### 3. Whale Tracker
- **Status**: ⚠️ **Needs Fix**.
- **Reasoning**: Logic for detecting accumulation/distribution is sound. Missing stale tracker cleanup is an issue.

---

## 📋 Action Plan (Monitoring Focused)

### Phase 1: Stability Hardening
- [ ] **Fix Smart Filter Memory**:
    - Add periodic call to `smart_filter.cleanup_all_expired()` in `run_binance_monitor.py` stats loop.
    - Implement `_cleanup_zombie_states` (similar to WBI) to remove empty `SymbolStats`.
- [ ] **Fix Whale Tracker Memory**:
    - Implement `_cleanup_stale_trackers` in `WhaleTracker` to remove symbols inactive for >1 hour.
    - Call this cleanup periodically.

### Phase 2: Code Cleanup
- [ ] **Remove Dead Code**: Delete `monitoring/price_monitor.py` or integrate it if intended.

### Phase 3: Configuration
- [ ] **Externalize Thresholds**: Move strict hardcoded values (like `MAX_PENDING_SIGNALS`, `warmup_ticks`) to `config.py` for easier tuning without restarts/redeploys.
