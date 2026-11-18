"""FrontierHarness Eval v1 export: native runner trials to FHE ``trial.json`` runs.

Responsibility: write one FHE run directory per arm (``run.json`` plus
``trials/<task>__a<n>/trial.json``) that the FHE repository's unmodified
``normalize-results.mjs`` scores, and ``calls.jsonl``, one priced row per model
call (face, key tail, usage, cost). Costs apply the frozen FHE price table
(``scripts/pricing.json``) to the relay's per-call usage: a common benchmark
basis, not the gateway's bill.

Status follows FHE (``skills/frontierharness-eval/SKILL.md``, "Accounting and
canonical selection"): a verifier reward >= 1 with observed model usage is a
success; another verifier reward with usage, or an agent timeout after the
agent started, is a failure; every unproven outcome (setup error, lost VM,
missing reward, no model usage) is infra_invalid. Every native attempt is FHE
attempt 1, so the normalizer keeps the earliest valid one; a smoke label
(``smoke*``) keeps a run out of the canonical selection.

Entry point: ``python -m evaluations.frontier_harness export``.
Dependencies: the FHE checkout at ``tasks.FHE_COMMIT`` and node for the normalizer.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from evaluations.frontier_harness.tasks import DEEPSWE_SUITE, FHE_COMMIT
from evaluations.terminal_bench.runner import invalidation

PRICING_VERSION = "kimi-k3-2026-08-20"
MODEL_CALL_KINDS = ("messages", "responses", "chat")
# The artifact a DeepSWE (schema 1.3) task collects and grades.
MODEL_PATCH = "/logs/artifacts/model.patch"
# ``tofu-agent run`` exits 3 when it stops itself at its own run budget.
SELF_TIMEOUT_EXIT_CODES = {"tofu": 3}


def load_rates(fhe_repo: Path) -> dict:
    """Per-token USD rates of the frozen FHE Kimi K3 table."""
    table = json.loads((Path(fhe_repo) / "skills/frontierharness-eval/scripts/pricing.json").read_text("utf-8"))
    if table.get("version") != PRICING_VERSION:
        raise ValueError(f"pricing.json version {table.get('version')!r} != {PRICING_VERSION}")
    unit = float(table["unit_tokens"])
    return {name: float(rate) / unit for name, rate in table["models"]["k3"].items()}


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def call_usage(record: dict) -> dict | None:
    """Token classes of one relay record, or None when the provider reported no usage."""
    kind = record.get("kind")
    if kind == "responses":
        usage = record.get("usage")
        if not isinstance(usage, dict):
            return None
        total = _int(usage.get("input_tokens"))
        cached = _int((usage.get("input_tokens_details") or {}).get("cached_tokens"))
        return {"fresh": max(0, total - cached), "cache_read": cached, "cache_write": 0,
                "output": _int(usage.get("output_tokens")), "input_total": total}
    if kind == "messages":
        parts = [part for part in (record.get("usage"), record.get("usage_start"), record.get("usage_delta"))
                 if isinstance(part, dict)]
        if not parts:
            return None

        def last(field):
            values = [part[field] for part in parts if isinstance(part.get(field), int)]
            return values[-1] if values else 0

        fresh, read, write = (last("input_tokens"), last("cache_read_input_tokens"),
                              last("cache_creation_input_tokens"))
        return {"fresh": fresh, "cache_read": read, "cache_write": write, "output": last("output_tokens"),
                "input_total": fresh + read + write}
    if kind == "chat":
        usage = record.get("usage")
        if not isinstance(usage, dict):
            return None
        total = _int(usage.get("prompt_tokens"))
        cached = _int((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        return {"fresh": max(0, total - cached), "cache_read": cached, "cache_write": 0,
                "output": _int(usage.get("completion_tokens")), "input_total": total}
    return None


def call_cost(usage: dict, rates: dict) -> float:
    return (usage["fresh"] * rates["fresh_input"] + usage["cache_read"] * rates["cache_read"]
            + usage["cache_write"] * rates["cache_write"] + usage["output"] * rates["output"])


def attempt_windows(states: list[dict]) -> list[tuple[float, float]]:
    """Relay-clock window of each sequential attempt of one (arm, task).

    An attempt owns every call logged from its submission until the next
    attempt's submission: attempts of one trial never overlap (the runner stops
    a VM before it starts the next attempt) and the relay logs a call when it
    ends, so no call of attempt n is logged after attempt n + 1 is submitted.
    """
    starts = [float(state.get("submitted_epoch") or 0.0) for state in states]
    return [(start, starts[index + 1] if index + 1 < len(starts) else float("inf"))
            for index, start in enumerate(starts)]


def attempt_records(records: list[dict], label: str, task: str, window: tuple[float, float]) -> list[dict]:
    """Every model-call record of one attempt, whatever its final status.

    A record without a task (missing, or empty from a route that names none) is
    attributed by its arm label and the attempt's time window alone.
    """
    return [record for record in records
            if record.get("kind") in MODEL_CALL_KINDS and record.get("arm") == label
            and (record.get("task") or task) == task
            and window[0] <= float(record.get("ts") or 0.0) <= window[1]]


def attempt_calls(records: list[dict], label: str, task: str, window: tuple[float, float]) -> list[dict]:
    return [record for record in attempt_records(records, label, task, window) if record.get("status") == 200]


def throttle_facts(records: list[dict]) -> dict:
    """Relay retries and upstream 429s of one attempt's model calls (PREREG: a 429 marks it throttled)."""
    retries = [retry for record in records for retry in record.get("retries") or []]
    upstream_429 = (sum(1 for retry in retries if retry.get("status") == 429)
                    + sum(1 for record in records if record.get("status") == 429)
                    # a rate limit FRIDAY sent inside an HTTP 200 stream (relay ``stream_429``)
                    + sum(1 for record in records if record.get("stream_429")))
    return {"relay_retries": len(retries), "upstream_429": upstream_429,
            "failed_model_calls": sum(1 for record in records if record.get("status") != 200),
            "rpm_wait_s": round(sum(float(record.get("rpm_wait_s") or 0.0) for record in records), 3),
            "throttled": upstream_429 > 0}


def rpm_summary(records: list[dict]) -> list[dict]:
    """Upstream request starts per wall minute for each key and model, retries included."""
    minutes: dict[tuple, dict[int, int]] = {}
    for record in records:
        if record.get("kind") not in MODEL_CALL_KINDS:
            continue
        start = float(record.get("ts") or 0.0) - float(record.get("latency_s") or 0.0)
        key = (str(record.get("key")), str(record.get("sent_model") or (record.get("orig") or {}).get("model")))
        bucket = minutes.setdefault(key, {})
        bucket[int(start // 60)] = bucket.get(int(start // 60), 0) + int(record.get("attempts") or 1)
    rows = []
    for (key, model), counts in sorted(minutes.items()):
        values = sorted(counts.values())
        rows.append({"key": key, "model": model, "active_minutes": len(values), "requests": sum(values),
                     "max_rpm": values[-1], "p95_rpm": values[min(len(values) - 1, int(0.95 * len(values)))],
                     "mean_rpm_active": round(sum(values) / len(values), 2)})
    return rows


def task_ids(run: dict) -> dict[str, str]:
    """FHE task id (``suite/name``) of every task of a native run, from its prepare-tasks manifest."""
    manifest = json.loads((Path(run["tasks_dir"]) / "fhe-task-manifest.json").read_text(encoding="utf-8"))
    ids = {name: row["id"] for name, row in manifest["tasks"].items()}
    missing = sorted(set(run["tasks"]) - set(ids))
    if missing:
        raise ValueError(f"tasks absent from {run['tasks_dir']}/fhe-task-manifest.json: {missing}")
    return ids


def fhe_status(arm: str, result: dict | None, calls_with_usage: int) -> tuple[str, str]:
    if not result:
        return "infra_invalid", "no trial result (start, submit, lost VM or runner failure)"
    agent = (result.get("phases") or {}).get("agent") or {}
    started = "started" in agent
    timed_out = started and (bool(agent.get("timed_out"))
                             or agent.get("exit_code") == SELF_TIMEOUT_EXIT_CODES.get(arm))
    reward = result.get("reward")
    if result.get("outcome") == "scored" and isinstance(reward, (int, float)):
        if calls_with_usage:
            return ("success" if reward >= 1 else "failure"), "verifier reward with model usage"
        if timed_out:
            return "failure", "agent timeout after start (verifier reward, no usage)"
        return "infra_invalid", "verifier reward without observed model usage"
    if timed_out:
        return "failure", "agent timeout after start, no verifier reward"
    verifier = (result.get("phases") or {}).get("verifier") or {}
    if verifier.get("reward_sentinel"):
        return "infra_invalid", f"verifier {verifier['reward_sentinel']} (raw reward {verifier.get('raw_reward')})"
    return "infra_invalid", str(result.get("infra_error") or f"outcome {result.get('outcome')}")[:300]


def export_arm(*, native_dir: Path, arm: str, records: list[dict], rates: dict, out_dir: Path, label: str,
               run_id: str, provider: str, notes: list[str], exclude_throttled: bool = False) -> dict:
    """One FHE run directory for ``arm``; ``exclude_throttled`` omits trials with an upstream 429."""
    run = json.loads((native_dir / "run.json").read_text(encoding="utf-8"))
    ids = task_ids(run)
    relay_label = f"{arm}-{run['run_label']}"
    trials_root = out_dir / "trials"
    trials_root.mkdir(parents=True, exist_ok=True)
    allow_hosts = run.get("allow_hosts")
    policy = ({"mode": "allowlist", "scope": "trial egress proxy (agent and verifier)", "allowed_hosts": allow_hosts}
              if allow_hosts is not None else {"mode": "deny-list", "scope": "trial egress proxy"})
    (out_dir / "run.json").write_text(json.dumps({
        "run_id": run_id, "harness": arm, "label": label, "model": "Kimi K3", "provider": provider,
        "egress_policy": policy, "checkpoint": None, "methodology_comparable": False,
        "methodology_notes": notes, "native_run": str(native_dir), "fhe_commit": FHE_COMMIT,
        "pricing_version": PRICING_VERSION,
    }, indent=2) + "\n", encoding="utf-8")
    ledger = []
    for task in run["tasks"]:
        attempt_dirs = sorted((native_dir / "trials" / arm / task).glob("attempt-*"),
                              key=lambda path: int(path.name.split("-")[1]))
        states = [json.loads((path / "state.json").read_text(encoding="utf-8")) for path in attempt_dirs]
        for attempt_dir, state, window in zip(attempt_dirs, states, attempt_windows(states)):
            result_path = attempt_dir / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
            attempt = int(attempt_dir.name.split("-")[1])
            every_call = attempt_records(records, relay_label, task, window)
            calls = [record for record in every_call if record.get("status") == 200]
            throttle = throttle_facts(every_call)
            usages = []
            for record in calls:
                usage = call_usage(record)
                cost = call_cost(usage, rates) if usage else None
                ledger.append({"arm": arm, "task": task, "attempt": attempt, "ts": record.get("ts"),
                               "face": record.get("kind"), "key": record.get("key"),
                               "model": record.get("sent_model") or (record.get("orig") or {}).get("model"),
                               "usage": usage, "cost_usd": cost, "latency_s": record.get("latency_s"),
                               "rpm_wait_s": record.get("rpm_wait_s", 0.0)})
                if usage:
                    usages.append((usage, cost))
            status, reason = fhe_status(arm, result, len(usages))
            verdict = invalidation(attempt_dir)
            if verdict is not None:
                status, reason = "infra_invalid", f"invalidated by the operator: {verdict.get('rule')}"
            phases = (result or {}).get("phases") or {}
            started_at = (phases.get("prepare") or {}).get("started")
            duration = (round(result["finished_at"] - started_at, 3)
                        if result and result.get("finished_at") and started_at else None)
            first_cached = usages[0][0]["cache_read"] if usages else None
            cost = sum(cost for _, cost in usages) if usages else None
            trial = {
                "id": ids[task], "title": task, "suite": ids[task].split("/", 1)[0], "attempt": 1, "label": label,
                "harness": arm, "run_id": run_id, "status": status, "success": status == "success",
                "status_reason": reason, "reward": (result or {}).get("reward"),
                "started_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
                               if started_at else state.get("created_at")),
                "duration_seconds": duration, "turns": len(usages) if usages else None,
                "input_tokens": sum(usage["input_total"] for usage, _ in usages) if usages else None,
                "cached_input_tokens": sum(usage["cache_read"] for usage, _ in usages) if usages else None,
                "output_tokens": sum(usage["output"] for usage, _ in usages) if usages else None,
                "first_turn_cached_tokens": first_cached,
                "cost_usd": cost,
                "cost_first_cold_usd": (cost + first_cached * (rates["fresh_input"] - rates["cache_read"])
                                        if usages else None),
                "cost_source": "relay per-call usage", "cost_basis": f"frozen FHE table {PRICING_VERSION}",
                "model_calls_without_usage": len(calls) - len(usages),
                "keys": sorted({str(record.get("key")) for record in calls}),
                "native_attempt": attempt, "native_phase": state.get("phase"),
                "exit_code": ((phases.get("agent") or {}).get("exit_code")),
                "agent_timed_out": bool((phases.get("agent") or {}).get("timed_out")),
                "model_patch": ((phases.get("collect") or {}).get("artifacts") or {}).get(MODEL_PATCH),
                **throttle,
                "evidence": str(attempt_dir),
            }
            if exclude_throttled and throttle["throttled"]:
                continue
            directory = trials_root / f"{task}__a{attempt}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "trial.json").write_text(json.dumps(trial, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as handle:
        for row in ledger:
            handle.write(json.dumps(row) + "\n")
    return {"arm": arm, "calls": len(ledger),
            "cost_usd": round(sum(row["cost_usd"] or 0.0 for row in ledger), 6),
            "keys": sorted({str(row["key"]) for row in ledger})}


def normalize(fhe_repo: Path, out_dir: Path, label: str) -> str:
    """Run the FHE repository's own normalizer on one exported arm; returns its report line."""
    script = Path(fhe_repo) / "skills/frontierharness-eval/scripts/normalize-results.mjs"
    reply = subprocess.run(["node", str(script), "--run", str(out_dir), "--label", label,
                            "--benchmark", str(Path(fhe_repo) / "benchmark.json")],
                           check=True, capture_output=True, text=True)
    return reply.stdout.strip()


def export(*, native_dir: Path, relay_log: Path, fhe_repo: Path, out_root: Path, label: str, run_id: str,
           provider: str, run_normalizer: bool = True, exclude_throttled: bool = False) -> dict:
    """``exclude_throttled`` writes the PREREG sensitivity variant: trials with an upstream 429 omitted."""
    native_dir, out_root = Path(native_dir), Path(out_root)
    rates = load_rates(fhe_repo)
    records = [json.loads(line) for line in Path(relay_log).read_text(encoding="utf-8").splitlines() if line.strip()]
    run = json.loads((native_dir / "run.json").read_text(encoding="utf-8"))
    notes = [
        "Non-official environment: Tofu native runner on CodeLab sandboxGateway VMs (one fresh VM and "
        "container per trial), not Runta; unranked unless a same-environment control supports comparison.",
        "Task identity " + str((run.get("task_identity") or {}).get("dataset_commit"))
        + ": Terminal-Bench tasks are FHE published task.toml/instruction.md over terminal-bench@2.0 "
          "registry files.",
        "Model calls go to the shared relay (FRIDAY kimi-k3, one key) outside the trial proxy; "
        "costs price relay-observed usage at the frozen FHE table.",
    ]
    if any(task_id.startswith(DEEPSWE_SUITE + "/") for task_id in task_ids(run).values()):
        notes.append("DeepSWE tasks are the deep-swe corpus at FHE's reproduction pin: the no-network agent "
                     "reaches only the relay; model.patch is collected from its container and graded in a "
                     "separate no-network container of the tests/Dockerfile image (Pier is not used).")
    if exclude_throttled:
        notes.append("Sensitivity variant: trials with any upstream 429 are omitted.")
    summary = {"run_id": run_id, "label": label, "exclude_throttled": exclude_throttled,
               "rpm": rpm_summary(records), "arms": {}}
    for arm in run["arms"]:
        out_dir = out_root / arm
        summary["arms"][arm] = export_arm(native_dir=native_dir, arm=arm, records=records, rates=rates,
                                          out_dir=out_dir, label=label, run_id=run_id, provider=provider,
                                          notes=notes, exclude_throttled=exclude_throttled)
        if run_normalizer:
            summary["arms"][arm]["normalizer"] = normalize(fhe_repo, out_dir, label)
    (out_root / "export-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


__all__ = ["attempt_calls", "attempt_records", "attempt_windows", "call_cost", "call_usage", "export",
           "fhe_status", "load_rates", "rpm_summary", "throttle_facts"]
