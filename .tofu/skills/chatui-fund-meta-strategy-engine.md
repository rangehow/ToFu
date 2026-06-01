---
name: chatui-fund-meta-strategy-engine
description: Fund meta-strategy engine (fund_strategy_engine.py): multi-timeframe signal confirmation, ensemble strategies, Monte Carlo simulation, walk-forward optimization, portfolio allocation, and full analysis pipeline
enabled: true
tags: [python, fund, strategy, backtest, monte-carlo, walk-forward, portfolio, architecture]
created: 2026-03-19T08:05:15Z
updated: 2026-03-19T08:05:15Z
---

# Fund Meta-Strategy Engine Architecture

## File: `lib/fund_strategy_engine.py` (~1600 lines)

Pure-computation engine (NO LLM dependency) that sits above `fund_backtest_engine.py`, `fund_signals.py`, and `fund_risk.py`.

## 8 Core Functions

1. **`compute_multi_timeframe_signal(navs)`** — 3-timeframe (short/medium/long) signal synthesis with alignment scoring
2. **`compute_smoothed_signal_series(navs, ...)`** — Whipsaw prevention via EMA smoothing + persistence gates + hysteresis
3. **`run_ensemble_backtest(fund_navs, ...)`** — 5 sub-strategies dynamically weighted by regime performance
4. **`monte_carlo_simulation(fund_navs, ...)`** — Block bootstrap with regime-conditioned sampling, VaR/CVaR/percentiles
5. **`rolling_walk_forward_optimize(fund_navs, ...)`** — Anchored walk-forward: train on expanding window, test on fixed-size folds
6. **`compute_advanced_metrics(equity_curve)`** — Sharpe, Sortino, Calmar, Omega, Information Ratio, Tail Ratio, Ulcer Index, etc.
7. **`optimize_portfolio_allocation(fund_navs, ...)`** — 4 methods: equal, risk_parity, min_vol, risk_signal
8. **`run_full_analysis(fund_navs, ...)`** — Orchestrates all above into one comprehensive report with confidence scoring

## API Routes (in `routes/fund_backtest.py`)

- `GET /api/fund/signals/multi_tf/<code>` — Multi-TF signal
- `POST /api/fund/ensemble_backtest` — Ensemble strategy backtest  
- `POST /api/fund/monte_carlo` — Monte Carlo simulation
- `POST /api/fund/walk_forward` — Walk-forward optimization
- `POST /api/fund/portfolio_optimize` — Portfolio allocation optimizer

## Key Design Decisions

- Walk-forward test folds include 60-day warmup overlap from training data to prevent "insufficient data" errors
- Monte Carlo uses block bootstrap (block_size=5) to preserve serial correlation
- Ensemble weights are regime-conditioned: trend strategies get higher weight in trending markets
- Signal smoothing uses EMA + persistence gate (signal must persist N days before acting)
- All functions handle edge cases: empty data, flat NAV, single fund, insufficient history

## Test File: `debug/test_strategy_engine.py`
10 comprehensive tests with synthetic data generator that produces realistic regime-switching market data.

