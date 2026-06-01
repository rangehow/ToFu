---
name: chatui-llm-simulator-engine-architecture
description: LLM simulator engine: fully open-universe trading (zero pre-selected symbols OK), on-demand price fetch, historical data backfill, T+1/stop-loss/take-profit, strategy toolbox
enabled: true
tags: [python, trading, simulation, llm, backtest, historical-data, architecture]
created: 2026-03-28T14:15:15Z
updated: 2026-04-03T10:40:43Z
---

# LLM Simulator Engine Architecture

## Open-Universe Trading
- Users can start simulations with **zero** pre-selected symbols — AI discovers autonomously
- Pre-selected symbols are labeled "关注的标的" (not "种子") — optional, AI prioritizes them
- LLM prompt adapts: with symbols → shows "用户关注的标的" section + open universe; without → only open universe
- Frontend validation removed: `symbols.length === 0` no longer blocks simulation start
- Backend routes accept `symbols=[]` — only `start_date`/`end_date` required

## On-Demand Price Fetch
- `_ensure_price_data()` calls `fetch_and_store_price_history()` for single new symbols mid-simulation
- `_validate_symbol_code()` rejects non-6-digit codes before network fetch
- `_resolve_symbol_name()` queries `search_asset_universal()` → registers in `_SIM_FUND_NAMES` + `_FUND_NAMES`
- SSE event `sim_fetching_symbol` emitted during on-demand fetch
- Dynamically-discovered symbols added to `config.symbols` after successful buy

## Data Flow
- `run_full_historical_fetch()` handles empty symbols: skips price phase, still fetches indices/macro/intel
- `_build_signal_context()` and `_extract_signal_highlights()` use `_all_tracked_symbols` (seeds + positions)
- `_generate_decision_dates()` falls back to calendar weekdays when no price data exists

## Key Files
- `lib/trading/llm_simulator.py` — Core engine, LLM prompt, trade execution
- `lib/trading/historical_data.py` — Price fetch, `run_full_historical_fetch`
- `routes/trading_simulator.py` — API routes (fetch-data, sim-run)
- `static/js/trading/simulator.js` — Frontend UI
- `trading.html` — HTML structure

## Decision Loop
1. Update prices for all tracked symbols
2. Check stop-loss / take-profit
3. Build signal context for tracked symbols
4. Build market snapshot + intel
5. Build LLM prompt (open-universe capable)
6. Call LLM → parse decisions
7. For new symbols: validate code → `_ensure_price_data` → `_resolve_symbol_name`
8. Execute trades, record journal

