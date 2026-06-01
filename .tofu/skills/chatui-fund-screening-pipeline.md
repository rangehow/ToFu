---
name: chatui-fund-screening-pipeline
description: Fund & stock screening pipeline: lib/trading/screening.py provides multi-dimensional scoring (5 dims), stock screening via eastmoney, brain pipeline integrates both fund+stock candidates, stock-aware fee context
enabled: true
tags: [python, fund, screening, stock, backtest, pipeline, architecture]
created: 2026-03-23T13:25:16Z
updated: 2026-04-04T02:09:21Z
---

# Fund & Stock Screening Pipeline

## Architecture

### Core Library: `lib/trading/screening.py`

Multi-dimensional screening engine for funds AND stocks:

**Exports:**
- `fetch_asset_ranking()` — Fund ranking from eastmoney by type & period
- `fetch_stock_list()` — A-share stock list from eastmoney push API
- `fetch_asset_detail_batch()` — Parallel fund info fetch
- `screen_stocks()` — Stock screening with PE/PB/market_cap/turnover filters
- `screen_assets()` — Multi-dimensional fund screening with 5-dimension scoring
- `score_asset_candidate()` — Score a single fund across 5 dimensions
- `smart_select_assets()` — Strategy-driven smart fund selection
- `run_screening_pipeline()` — Full pipeline: discover → screen → score → backtest

### Brain Integration: `lib/trading/brain/pipeline.py`

Phase 2b in `_gather_full_context()` runs BOTH:
1. `screen_assets()` — fund/ETF candidates with 5-dim scoring
2. `screen_stocks()` — A-share stocks filtered by market_cap, PE, PB, volume

Brain prompt includes both fund and stock candidates with type-specific metrics:
- Funds: total_score, recommendation, 3m return
- Stocks: price, PE, PB, market_cap, turnover

### Fee Context
Brain builds asset-type-aware fee context:
- Stocks/ETFs: commission + stamp tax summary
- Funds: subscription + redemption + management fees

### Stock Screening via eastmoney
Uses push2delay.eastmoney.com API (`fltt=2` for float format):
- f2=price, f3=pct, f5=volume, f6=amount, f8=turnover, f9=PE, f20=market_cap, f23=PB
- Markets: all, sh(沪), sz(深), cyb(创业板), kc(科创板)
- Supports sorting by any field + post-filtering by PE/PB/market_cap/turnover ranges

### Asset Classification
`lib/trading/_common.py` provides `classify_asset_code()`:
- `is_stock_code()`, `is_etf_code()`, `is_fund_code()`
- `stock_secid()` for push2 API calls

### Routes: `routes/fund_screening.py`

| Endpoint | Method | Description |
|---|---|---|
| `/api/fund/screen/stocks` | POST | A-share stock screening |
| `/api/fund/screen/funds` | POST | Multi-dimensional fund screening |
| `/api/fund/screen/smart` | POST | Strategy-driven smart selection |

### Holdings Search
`routes/trading_holdings.py` `/api/trading/search` uses `search_asset_universal()`
(not the old fund-only `search_asset()`), returning stocks, ETFs, and funds.

