---
name: claude-code-cli-sankuai-proxy-setup
description: Claude Code CLI setup via claude-code-proxy to use sankuai OpenAI-compatible gateway with model mapping and custom headers
enabled: true
tags: [claude-code, sankuai, proxy, configuration]
created: 2026-04-07T13:22:24Z
updated: 2026-04-07T13:22:24Z
---

# Claude Code CLI — Sankuai Gateway via Proxy

## Problem
Claude Code CLI uses **Anthropic Messages API** (`/v1/messages`), but the sankuai gateway
at `aigc.sankuai.com/v1/openai/native` uses **OpenAI Chat Completions API** format.
Direct ANTHROPIC_BASE_URL pointing doesn't work — a proxy is needed.

## Solution
Use [claude-code-proxy](https://github.com/fuergaosi233/claude-code-proxy) to translate
between Anthropic and OpenAI API formats.

## Directory Layout
```
claude-code-workspace/           ← Same level as chatui
├── README.md
├── start-proxy.sh               ← Launcher (sets HOST=0.0.0.0 to fix conda HOST env)
└── proxy/                       ← git clone of claude-code-proxy
    ├── .env                     ← Sankuai-specific config
    └── start_proxy.py
```

## Key Config Files

### proxy/.env
```env
OPENAI_API_KEY=<sankuai-api-key>
OPENAI_BASE_URL=https://aigc.sankuai.com/v1/openai/native
CUSTOM_HEADER_M_TRANSFERCONTEXT_INF_CELL=gray-release-ai-gpt-test
BIG_MODEL=aws.claude-opus-4.6
MIDDLE_MODEL=aws.claude-sonnet-4.6
SMALL_MODEL=aws.claude-sonnet-4.6
HOST=127.0.0.1
PORT=8082
MAX_TOKENS_LIMIT=16384
REQUEST_TIMEOUT=300
```

### ~/.claude/settings.json
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8082",
    "ANTHROPIC_API_KEY": "sk-proxy-sankuai",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": 16384,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1
  }
}
```

## Gotchas
1. **HOST env var conflict**: Conda sets `HOST=x86_64-conda-linux-gnu` — must override with `HOST=0.0.0.0` in start script
2. **Custom header format**: Env vars can't have hyphens. Use underscores: `CUSTOM_HEADER_M_TRANSFERCONTEXT_INF_CELL` → HTTP header `M-TRANSFERCONTEXT-INF-CELL` (HTTP headers are case-insensitive)
3. **API key validation**: The proxy's `validate_api_key()` checks for `sk-` prefix — this only affects health check display, not actual API calls
4. **ANTHROPIC_API_KEY in proxy**: Leave unset to disable client validation (proxy accepts any key from Claude Code)
5. **Model mapping**: Proxy maps claude-opus-* → BIG_MODEL, claude-sonnet-* → MIDDLE_MODEL, claude-haiku-* → SMALL_MODEL

## Usage
```bash
# Terminal 1: Start proxy
cd claude-code-workspace && ./start-proxy.sh

# Terminal 2: Use Claude Code
cd <project> && claude
```

## Verified
- Proxy correctly routes to `aws.claude-opus-4.6` and `aws.claude-sonnet-4.6`
- Tested with curl: got valid response "Hello, it's great to meet you!" from opus

