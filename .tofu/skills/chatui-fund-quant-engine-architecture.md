---
name: chatui-fund-quant-engine-architecture
description: Fund quantitative engine architecture: fund_signals.py, fund_risk.py, fund_backtest_engine.py; dead v1 backtest functions removed (2026-04), portfolio analytics in portfolio_analytics.py
enabled: true
tags: [python, fund, quantitative, backtest, risk-management, architecture]
created: 2026-03-19T06:47:34Z
updated: 2026-04-03T14:23:09Z
---

# Fund Quantitative Engine Architecture

## Architecture Changes (2026-04 Refactor)

### Dead Code Removed
- `lib/trading/backtest.py` — **stripped to 30 lines** (was 552L). Old v1 backtest functions removed:
  - `backtest_hold()`, `backtest_dca()`, `backtest_portfolio()`, `run_portfolio_backtest()`, `analyze_correlation()`
  - These were never called from any route or JS frontend
- `backtest.py` now only re-exports from `portfolio_analytics.py` for backward compat

### New Files
- `lib/trading/portfolio_analytics.py` (183L) — extracted from `backtest.py`:
  - `calculate_portfolio_value()` — enriched holdings with current NAV, PnL
  - `check_rebalance_alerts()` — detect allocation drift
  - `calculate_avg_cost_after_add()` — cost dilution calculator
- `lib/trading/strategy_interface.py` (250L) — unified `TradingStrategy` Protocol:
  - `TradeOrder` dataclass — universal trade output format
  - `SignalContext` dataclass — universal strategy input format
  - `StrategyResult` dataclass — result wrapper
  - `TradingStrategy` Protocol — implemented by both backtest and live
- `lib/trading/news_gathering.py` (88L) — extracted from `routes/trading_decision.py`:
  - `gather_news_cached()` — 5-min cached news from DB + web fallback
- `lib/trading/WINRATE_DIAGNOSTIC.md` — root cause analysis of 75% backtest → 41% live gap

### Import Path Changes
- `from lib.trading.portfolio_analytics import calculate_portfolio_value` (new canonical path)
- `from lib.trading.backtest import calculate_portfolio_value` (still works via re-export)
- `from lib.trading.news_gathering import gather_news_cached` (replaces `routes.trading_decision._gather_news_cached`)
- `from routes.trading_decision import _gather_news_cached` (still works via re-export)

## Existing Files (unchanged)

### lib/fund_signals.py (742 lines) — Quantitative Signal Engine
- EMA/SMA crossover, MACD, RSI, Bollinger Bands, Volume analysis
- `compute_signal_snapshot(navs)` — single fund consolidated signal
- Signal strength scoring: -1.0 to +1.0

### lib/fund_risk.py (684 lines) — Risk Management Engine
- DrawdownProtector, StopLossManager, CorrelationAllocator, KellyPositionSizer
- `compute_portfolio_risk()`, `apply_risk_checks()`, `volatility_target_position()`

### lib/fund_backtest_engine.py (1134 lines) — Event-Driven Backtesting Engine v2
- 7 strategies in StrategyMixin (signal_driven, dca, mean_reversion, trend_following, adaptive, dca_signal, buy_and_hold)
- Walk-forward validation, comparison, analysis modules

## Key Design Decision
**Win-Rate Gap:** Backtest uses deterministic quant strategies while live uses LLM decisions — they test different systems entirely. Use LLM simulator results for realistic live performance expectations.

