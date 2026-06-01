---
name: system-param-vs-messages0-ab-test-result
description: A/B test result: body['system'] parameter gets ZERO cache hits through OpenAI-compatible gateway — messages[0] wins definitively for cache performance
enabled: true
tags: [cache, a/b-test, system-prompt, anthropic, verified]
created: 2026-04-08T06:33:46Z
updated: 2026-04-08T06:33:46Z
---

# System Placement A/B Test Results (2026-04-08)

## Test: CLAUDE.md in messages[0] vs top-level body['system'] parameter

### Result: messages[0] WINS decisively

**Arm A (MESSAGES0)**: messages[0] with role='system' — **current behavior**
- Cache writes on R1, cache reads from R2+ (82-96% hit rate)
- Total: cr=47,642, cw=22,990, pt=13
- 47.9% cache savings vs no-cache baseline
- Cost: $0.6058

**Arm B (SYSTEM_PARAM)**: top-level body['system'] parameter  
- **ZERO cache hits across ALL rounds** (cr=0, cw=0)
- All tokens counted as uncached prompt (pt=22,726)
- Negative cache savings (-6.1%)
- Cost: $0.5913 (similar only because model got less context → less tokens)

### Why Arm B Failed

The OpenAI-compatible `/chat/completions` gateway (`aws.claude-opus-4.6` via Bedrock):
1. Does NOT forward `body['system']` as the native Anthropic `system` parameter
2. `cache_control` annotations on `body['system']` blocks are completely ignored
3. The content may be converted to a regular message but without caching support

### Conclusion

**Always use `messages[0]` with `role='system'` for the system prompt.**
The top-level `system` body parameter is only supported by the native Anthropic Messages API 
(`/v1/messages`), not by OpenAI-compatible gateways. Since our infrastructure uses 
`/chat/completions`, this approach is not viable.

### Test Details
- Model: aws.claude-opus-4.6
- 8 rounds per arm + Task 2 inter-task test
- Arm isolation: unique arm seeds to prevent cross-arm cache sharing
- Mixed TTL: CACHE_EXTENDED_TTL=True (1h for system+tools, 5m for tail)

