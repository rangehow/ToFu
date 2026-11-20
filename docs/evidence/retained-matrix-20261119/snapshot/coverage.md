# RETAINED-A10-ISOLATION-METADATA-20261119

Historical snapshot, descriptive only. Isolation means retained rule metadata, not independently proven sandbox containment.

| Model | Harness | Pass/n | Tasks / 89 | Complete 89-task runs | Recorded list $ |
|---|---|---:|---:|---:|---:|
| Doubao-Seed-2.0-pro | claude-code | 2/4 | 4 | 0 | 1.8425 |
| Doubao-Seed-2.0-pro | codex | 1/4 | 4 | 0 | 1.7082 |
| Doubao-Seed-2.0-pro | goose | 3/7 | 7 | 0 | 1.1583 |
| Doubao-Seed-2.0-pro | hermes | 9/25 | 22 | 0 | 7.0142 |
| Doubao-Seed-2.0-pro | kimi-code | 3/8 | 8 | 0 | 3.2777 |
| Doubao-Seed-2.0-pro | mini-swe-agent | 2/7 | 7 | 0 | 3.1927 |
| Doubao-Seed-2.0-pro | oh-my-pi | 3/8 | 8 | 0 | 2.6391 |
| Doubao-Seed-2.0-pro | opencode | 0/4 | 4 | 0 | 1.4201 |
| Doubao-Seed-2.0-pro | qwen-code | 3/8 | 8 | 0 | 3.0482 |
| Doubao-Seed-2.0-pro | tofu@948e9b264f | 3/4 | 4 | 0 | 1.5065 |
| MiniMax-M2.7 | claude-code | 2/7 | 7 | 0 | 0.6913 |
| MiniMax-M2.7 | codex | 9/22 | 22 | 0 | 2.5197 |
| MiniMax-M2.7 | goose | 10/22 | 22 | 0 | 1.4323 |
| MiniMax-M2.7 | hermes | 11/21 | 21 | 0 | 2.2786 |
| MiniMax-M2.7 | kimi-code | 9/23 | 23 | 0 | 2.8889 |
| MiniMax-M2.7 | mini-swe-agent | 11/22 | 22 | 0 | 3.2083 |
| MiniMax-M2.7 | oh-my-pi | 8/22 | 22 | 0 | 2.3283 |
| MiniMax-M2.7 | qwen-code | 8/22 | 22 | 0 | 1.6992 |
| MiniMax-M2.7 | tofu@948e9b264f | 20/40 | 40 | 0 | 2.9049 |
| MiniMax-M3 | claude-code | 17/26 | 16 | 0 | 5.5649 |
| MiniMax-M3 | codex | 16/25 | 16 | 0 | 5.8489 |
| MiniMax-M3 | opencode | 5/8 | 8 | 0 | 1.1538 |
| MiniMax-M3 | pi | 7/9 | 9 | 0 | 1.2730 |
| MiniMax-M3 | tofu@948e9b264f | 15/24 | 16 | 0 | 5.9072 |
| deepseek-v4-flash | claude-code | 17/20 | 20 | 0 | 0.3964 |
| deepseek-v4-flash | codex | 17/19 | 19 | 0 | 0.3758 |
| deepseek-v4-flash | opencode | 12/14 | 14 | 0 | 0.1994 |
| deepseek-v4-flash | pi | 10/14 | 14 | 0 | 0.2156 |
| deepseek-v4-flash | tofu | 13/13 | 13 | 0 | 0.2586 |
| gemini-3.5-flash | tofu | 24/34 | 34 | 0 | 60.9598 |
| glm-5.2 | claude-code | 16/23 | 23 | 0 | 22.4301 |
| glm-5.2 | codex | 15/22 | 22 | 0 | 14.0816 |
| glm-5.2 | opencode | 5/7 | 7 | 0 | 3.2461 |
| glm-5.2 | pi | 6/7 | 7 | 0 | 1.4621 |
| glm-5.2 | tofu | 17/22 | 22 | 0 | 11.8316 |
| grok-4.6 | claude-code | 23/37 | 23 | 0 | 57.2719 |
| grok-4.6 | codex | 29/36 | 23 | 0 | 27.1749 |
| grok-4.6 | opencode | 29/38 | 24 | 0 | 15.5453 |
| grok-4.6 | pi | 32/38 | 24 | 0 | 15.3365 |
| grok-4.6 | tofu | 27/38 | 24 | 0 | 18.3129 |
| kimi-k2.7-code-highspeed | codex | 6/7 | 7 | 0 | 1.2205 |
| kimi-k2.7-code-highspeed | kimi-code | 9/14 | 14 | 0 | 5.6195 |
| kimi-k2.7-code-highspeed | opencode | 5/13 | 13 | 0 | 3.9132 |
| kimi-k2.7-code-highspeed | pi | 6/7 | 7 | 0 | 0.7787 |
| kimi-k2.7-code-highspeed | tofu@948e9b264f | 6/7 | 7 | 0 | 1.5890 |

Exclusion counts (first applicable reason):

```json
{
  "csv_invalid": 5,
  "eligible": 802,
  "missing_raw_metadata": 266,
  "outside_extended_E": 238,
  "pre_isolation_or_unknown": 5654,
  "subsequently_invalidated": 7
}
```

Matched comparisons are in coverage.json: exact (model, run, task) intersections, no unmatched denominator pooling. No significance or ranking claim.
