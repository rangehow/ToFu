"""Per-trial metrics reconciliation for the OpenCode benchmark proxy."""

from __future__ import annotations

from typing import Any

from evaluations.codex_kimi_proxy.codex_contract import (
    CodexContractError,
    _metrics_rows,
    validate_trial_token,
)


def validate_opencode_proxy_metrics(
    path: str,
    *,
    expected_trial_token: str,
) -> dict[str, Any]:
    """Reconcile one trial's tagged proxy rows and aggregate exact usage."""

    token = validate_trial_token(expected_trial_token)
    records = [
        row for row in _metrics_rows(path)
        if row.get("trialToken") == token
    ]
    if not records:
        raise CodexContractError(
            "opencode proxy metrics do not contain the requested trial")
    completions = [row for row in records
                   if row.get("event") == "chatCompletion"]
    violations = [row for row in records
                  if row.get("event") in {"invalidTrialHeader",
                                          "modelPinViolation"}]
    upstream_calls = sum(int(row.get("upstreamCalls") or 0)
                         for row in completions)
    usage_totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
    }
    for row in completions:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        usage_totals["completion_tokens"] += int(
            usage.get("completion_tokens") or 0)
        usage_totals["total_tokens"] += int(usage.get("total_tokens") or 0)
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            usage_totals["reasoning_tokens"] += int(
                details.get("reasoning_tokens") or 0)
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            usage_totals["cache_read_tokens"] += int(
                prompt_details.get("cached_tokens") or 0)
        usage_totals["cache_read_tokens"] += int(
            usage.get("cache_read_tokens") or 0)
    valid = (
        completions
        and not violations
        and upstream_calls == len(completions)
        and not any(row.get("invalidTrial") for row in completions)
        and all(
            row.get("status") == "completed"
            and row.get("clientDisconnected") is not True
            for row in completions
        )
    )
    return {
        "valid": valid,
        "chatCompletions": len(completions),
        "upstreamCalls": upstream_calls,
        "violations": len(violations),
        "usage": usage_totals,
        "proxyCpuNs": sum(int(row.get("proxyCpuNs") or 0)
                          for row in completions),
        "rawWallNs": sum(int(row.get("rawWallNs") or 0)
                         for row in completions),
        "upstreamWallNs": sum(int(row.get("upstreamWallNs") or 0)
                              for row in completions),
        "model": next(
            (str(row.get("model")) for row in completions if row.get("model")),
            "",
        ),
    }


__all__ = ["validate_opencode_proxy_metrics"]
