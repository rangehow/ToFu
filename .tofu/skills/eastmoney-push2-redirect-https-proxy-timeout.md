---
name: eastmoney-push2-redirect-https-proxy-timeout
description: Bug fix: EastMoney push2.eastmoney.com redirects HTTP→HTTPS after market close, HTTPS hangs through corporate proxy — use push2delay.eastmoney.com directly and HTTP protocol for all push2 API calls
enabled: true
tags: [python, eastmoney, proxy, https, redirect, timeout, push2, market-data, bug-fix]
created: 2026-03-25T09:32:10Z
updated: 2026-03-25T09:32:10Z
---

# EastMoney push2 API: HTTPS Redirect Timeout Through Proxy

## Problem
After A-share market close (15:00+), `push2.eastmoney.com` sends HTTP 302 redirects to `push2delay.eastmoney.com` with protocol-relative URLs (`//push2delay...`). When Python `requests` follows the redirect, it constructs an HTTPS URL. Through the corporate proxy (`10.229.18.27:8412`), HTTPS SSL handshake to eastmoney CDN servers **hangs indefinitely** (>8s timeout).

This causes **intermittent failures**: during market hours the API returns 200 directly; after hours it redirects and times out.

## Root Causes
1. **HTTPS through proxy**: SSL handshake to `*.eastmoney.com` hangs through the corporate HTTP proxy
2. **Post-market redirect**: `push2.eastmoney.com` → 302 → `//push2delay.eastmoney.com/...` (HTTPS)
3. **Protocol-relative URLs**: The `//` prefix causes `requests` to use HTTPS by default

## Fix
1. **Use `push2delay.eastmoney.com` directly** — works both during and after market hours, no redirects
2. **Always use HTTP** (not HTTPS) for ALL eastmoney API calls — HTTP works reliably through the proxy
3. **Apply to all endpoints**: indices (`ulist.np`), sectors (`clist`), breadth (`clist`), northbound (`kamt.rtmin`), trend (`push2his`), fund ranking (`fund.eastmoney.com`)

```python
# ❌ WRONG — redirects to HTTPS after hours, hangs through proxy
url = 'https://push2.eastmoney.com/api/qt/ulist.np/get?...'

# ❌ ALSO WRONG — HTTP works during hours but 302→HTTPS after hours
url = 'http://push2.eastmoney.com/api/qt/ulist.np/get?...'

# ✅ CORRECT — push2delay works always, HTTP avoids SSL hang
url = 'http://push2delay.eastmoney.com/api/qt/ulist.np/get?...'
```

## Affected Files
- `lib/trading/market.py` — all 5 fetch functions
- `lib/trading/_common.py` — `check_network()` probe
- `lib/trading/screening.py` — stock screening API

## Also Fixed
- **Northbound API format change**: `n2s` field changed from dict `{f1,f2,f3}` to list of minute-level CSV strings
- **Market closed detection**: Added `market_closed` flag to breadth and northbound when API returns `-` strings or all-zero flows

