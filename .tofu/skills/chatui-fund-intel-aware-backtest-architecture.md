---
name: chatui-fund-intel-aware-backtest-architecture
description: Fund module architecture: intel-aware backtesting with time-locked DB, mega crawler (15 categories/106 queries), adaptive decision engine (strategy fusion with risk veto), and backtest-driven failure learning engine
enabled: true
tags: [python, fund, backtest, intel, adaptive, learning, architecture]
created: 2026-03-25T16:11:10Z
updated: 2026-04-03T14:23:38Z
---

## Fund Project Intel-Aware Architecture (v2)

### Architecture Changes (2026-04 Refactor)

#### Context Gathering Consolidation
- `lib/trading_autopilot/cycle.py::_gather_context()` is the **single source of truth** for context assembly
- `lib/trading/brain/pipeline.py::_gather_full_context()` now **delegates** to `_gather_context()` and adds brain-specific extras (candidates, fees, alerts)
- Eliminated ~84 lines of duplicated context-gathering logic

#### Route Consolidation
- `routes/trading_autopilot.py` — state/analyze/stream/cycles endpoints now **delegate to brain** (was 362L → 208L)
- `routes/trading_brain.py` — remains the primary decision endpoint (`/api/trading/brain/stream`)
- `routes/trading_decision.py` — removed dead `/api/trading/recommend` and `/api/trading/recommend/stream` endpoints (was 600L → 347L)
- Unified state: `_brain_state` in `trading_brain.py` is the single state dict (no more separate `_autopilot_state`)

### 4 Core Modules (2787 lines total)

#### 1. `lib/trading/intel_mega_crawler.py` (603L)
- `MEGA_INTEL_SOURCES`: 15 categories, 106 queries
- `run_mega_crawl()`: concurrent batch crawling with progress callbacks
- All items MUST have published_date (strict time-categorization)

#### 2. `lib/trading_backtest_engine/intel_backtest.py` (840L)
- `IntelBacktestEngine`: wraps BacktestEngine with time-locked intel DB
- Uses meta-strategy suitability matrix to select strategy types dynamically
- `IntelDecisionRecord`: records every decision for learning

#### 3. `lib/trading_autopilot/adaptive_decision_engine.py` (662L)
- `AdaptiveDecisionEngine`: unified brain with strategy fusion
- `_fuse_signals()`: resolves conflicts (risk_control has VETO power)
- Integrated into `_gather_context()` as Step 7

#### 4. `lib/trading_autopilot/backtest_learner.py` (682L)
- `analyze_backtest_decisions()`: strategy × regime effectiveness matrix
- `auto_update_strategies()`: applies learning to DB
- Failure pattern detection: regime_mismatch, bad_combo, global failure

### API Endpoints
- `POST /api/trading/brain/stream` — PRIMARY endpoint for AI操盘 tab
- `POST /api/trading/brain/analyze` — sync brain analysis
- `GET  /api/trading/brain/state` — unified state
- `POST /api/trading/backtest/intel` — intel-aware backtest
- `POST /api/trading/backtest/intel/learn` — analyze backtest decisions
- `POST /api/trading/intel/mega-crawl` — ultra-large scale crawl
- `POST /api/trading/decision/adaptive` — unified adaptive decision

### Key Design Decisions
1. Time-locked intel: `build_regime_intel_features(db, as_of=date)` ensures zero future leak
2. Strategy fusion: risk_control has VETO power over buy/sell signals
3. Context gathering: `cycle._gather_context()` is single source, `pipeline._gather_full_context()` is thin wrapper
4. Proposals need 5+ samples AND appearance in 2+ sessions for "high confidence"

