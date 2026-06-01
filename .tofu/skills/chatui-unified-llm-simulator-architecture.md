---
name: chatui-unified-llm-simulator-architecture
description: Unified LLM simulator engine: 4-phase flow, open-universe trading (stocks+ETFs+funds), 10 quick-add groups (3 stock groups added: consumer/medical/new_energy), stock-specific analysis framework in prompt
enabled: true
tags: [python, javascript, trading, simulator, llm, architecture, ui]
created: 2026-03-28T16:08:11Z
updated: 2026-04-04T02:44:00Z
---

# Unified LLM Simulator Architecture

## Core Concept
User picks: **TIME PERIOD** + **RISK LEVEL** + **CAPITAL**
AI picks: **WHICH ASSETS TO BUY/SELL** (stocks, ETFs, funds from risk-appropriate pool)

No financial knowledge required from user.

## Files
- `lib/trading/historical_data.py` — 4-layer data fetcher (prices, indices, macro, news)
- `lib/trading/llm_simulator.py` — LLM-driven simulation engine
- `lib/trading/_common.py` — Asset classification: `classify_asset_code()`, `is_stock_code()`, `stock_secid()`
- `lib/trading/info.py` — Stock quotes via push2delay API (`_fetch_stock_quote_remote`)
- `lib/trading/nav.py` — Price history: K-line for stocks/ETFs, NAV for funds (`_fetch_kline_history`)
- `routes/trading_simulator.py` — SSE streaming API routes
- `static/js/trading/simulator.js` — Frontend UI
- `trading.html` — `#page-simulator` section
- `static/trading.css` — `.sim-*` styles

## Asset Classification (lib/trading/_common.py)
```python
classify_asset_code('600519')  # → 'stock' (SH main board)
classify_asset_code('000858')  # → 'stock' (SZ main board)
classify_asset_code('300750')  # → 'stock' (ChiNext)
classify_asset_code('688981')  # → 'stock' (STAR)
classify_asset_code('510300')  # → 'etf'
classify_asset_code('110011')  # → 'bond'
classify_asset_code('001234')  # → 'fund' (open-end)
```

## Stock Quote API (push2delay.eastmoney.com)
- **MUST use `fltt=2`** parameter for proper float format
- **MUST use HTTP** not HTTPS
- **Use push2delay** not push2

## Quick-Add Groups (10 groups, 7 are stocks)
```javascript
QUICK_ADD_GROUPS = {
  broad_index:   // 5 宽基指数ETFs
  sector:        // 5 行业ETFs
  bond:          // 3 债券ETFs
  cross_border:  // 2 跨境ETFs
  blue_chip:     // 5 蓝筹白马股 (茅台, 五粮液, etc.)
  growth:        // 5 成长科技股 (宁德时代, 比亚迪, etc.)
  consumer:      // 5 消费龙头股 (伊利, 泸州老窖, etc.) ← NEW
  medical:       // 5 医药健康股 (恒瑞, 迈瑞, etc.) ← NEW
  new_energy:    // 5 新能源股 (隆基, 阳光电源, etc.) ← NEW
  dividend:      // 6 高股息红利股 (工商银行, 中国神华, etc.)
};
```

## Simulator Prompt: Stock Analysis Framework
The system prompt explicitly instructs the LLM to:
- Actively discover and trade individual stocks, not just ETFs/funds
- Evaluate PE/PB valuation, industry competitive landscape, moats
- Consider growth potential, dividend yield, market cap tier
- Use stock-specific fee structure (commission ~万2.5, stamp tax 0.05%)

## Phase ID Mapping (critical bug fix)
```javascript
var PHASE_IDS = {
  setup:   'simSetupPhase',
  fetch:   'simFetchPhase',   // NOT 'simFetchingPhase'!
  run:     'simRunPhase',     // NOT 'simRunningPhase'!
  results: 'simResultsPhase'
};
```

