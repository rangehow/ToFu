"""EVAL-MATRIX status tables: per (model, harness) cell score, n, runs, CI, cost and tokens, from raw run data.

Responsibility: regenerate the matrix's results tables (PREREG section 10 statistics) from the files the runs
write, so reported numbers never live only in prose. Two inputs are combined, newest wins per
(model, harness, run, task):
  - ``analysis/per_trial.csv`` (the A10 snapshot of every pre-takeover trial; ``in_extended_E`` selects runs 1-3);
  - attempts under ``runs/<model>/<run>/trials/<arm>/<task>/attempt-*/state.json`` created at or after ``--since``
    (trials of the takeover lane; ``state.json`` outcome, the final measured attempt counts).
Tokens and list-price cost come from every ``relays/*/requests.jsonl`` record of the trial's arm label
(``<arm>-<model>-<run>``) and task. Pass = measured with verifier reward >= 1.0.
Statistics: a cell's score is the mean over tasks of the per-task pass fraction across its runs; 95% CI by
task-cluster percentile bootstrap (B = 20,000, seed 20261112); run-to-run SD of complete runs (89 measured).
A pair of cells is reported as separated only when their CIs do not overlap.
PREREG A10.3 decision rule (F-H family = harness pairs within one model, among its K >= 3 core cells): two-sided
paired sign-flip permutation test on per-task pass fractions (B = 20,000, seed 20261112, plus-one; per task the
mean over the runs both cells share, else each cell's own mean, as analysis/stats.py), Holm within the family;
a difference is claimed iff its Holm-adjusted p < 0.05.

Entry point: ``python -m evaluations.terminal_bench.eval_matrix.status_tables --matrix <eval-matrix dir> --out <md>``.
Dependencies: stdlib and numpy (the sign-flip draws use numpy's default_rng exactly as analysis/stats.py).
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

TASKS = 89
BOOTSTRAP = 20_000
SEED = 20261112
CORE = ("tofu", "opencode", "pi", "claude-code", "codex")
# USD per 1M tokens: (input, output, cache-read multiplier, cache-write multiplier). The matrix's own table
# (analysis/status.py, from lib/pricing/_tables.py) plus glm-5.3-flash and kimi-k3 from lib/pricing/_tables.py.
PRICES = {
    "claude-opus-5.5": (4.0, 20.0, 0.05, 1.25), "deepseek-v4-flash": (0.15, 0.60, 0.02, 1.0),
    "qwen3.5-plus": (0.11, 0.66, 0.10, 1.0), "MiniMax-M3": (0.30, 1.20, 0.20, 1.0),
    "MiniMax-M2.7": (0.30, 1.20, 0.10, 1.0), "glm-5.2": (3.45, 13.81, 0.10, 1.0),
    "Doubao-Seed-2.0-pro": (0.55, 2.21, 0.10, 1.0), "gpt-6-sol": (0.0, 0.0, 0.0, 1.0),
    "grok-4.6": (2.00, 6.00, 0.25, 1.0), "kimi-k2.7-code-highspeed": (0.95, 8.00, 0.20, 1.0),
    "gemini-3.5-flash": (1.49, 8.95, 0.25, 1.0), "gpt-6.1-sol": (2.00, 10.00, 0.05, 1.0),
    "glm-5.3-flash": (0.11, 0.39, 0.29, 1.0), "kimi-k3": (2.76, 13.81, 0.10, 1.0),
}
FACES = ("chat", "messages", "responses")


def usage_of(record: dict) -> tuple[int, int, int, int]:
    """(uncached input, cache read, cache write, output) tokens of one relay record (analysis/status.py rules)."""
    kind = record.get("kind")
    if kind == "chat":
        usage = record.get("usage") or {}
        prompt = usage.get("prompt_tokens") or 0
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or usage.get("effectiveCachedTokens") or 0
        return max(prompt - cached, 0), cached, 0, usage.get("completion_tokens") or 0
    if kind == "messages":
        start, delta = record.get("usage_start") or {}, record.get("usage_delta") or {}
        pick = lambda key: max(start.get(key) or 0, delta.get(key) or 0)  # noqa: E731
        return (pick("input_tokens"), pick("cache_read_input_tokens"), pick("cache_creation_input_tokens"),
                pick("output_tokens"))
    if kind == "responses":
        usage = record.get("usage") or {}
        cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
        return max((usage.get("input_tokens") or 0) - cached, 0), cached, 0, usage.get("output_tokens") or 0
    return 0, 0, 0, 0


def cost(model: str, tokens) -> float:
    price_in, price_out, read_mul, write_mul = PRICES.get(model, (0.0, 0.0, 0.0, 1.0))
    uncached, read, write, out = tokens
    return (uncached * price_in + read * price_in * read_mul + write * price_in * write_mul + out * price_out) / 1e6


def relay_usage(relays: Path) -> dict:
    """(arm label, task) -> {"calls", "tokens"} over every relay log (old relays and the takeover relays)."""
    usage: dict = defaultdict(lambda: {"calls": 0, "tokens": [0, 0, 0, 0], "models": set()})
    for log in sorted(relays.glob("*/requests.jsonl")):
        model = log.parent.name.split("@")[0]
        for line in log.open(encoding="utf-8", errors="replace"):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") not in FACES:
                continue
            cell = usage[(record.get("arm") or "", record.get("task") or "")]
            cell["calls"] += 1
            cell["models"].add(model)
            for index, value in enumerate(usage_of(record)):
                cell["tokens"][index] += value
    return usage


def snapshot_trials(per_trial: Path) -> dict:
    trials = {}
    with per_trial.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["in_extended_E"] != "1" or row["outcome"] not in ("pass", "fail"):
                continue
            trials[(row["model"], row["harness"], row["run"], row["task"])] = {
                "pass": row["outcome"] == "pass", "cost_usd": float(row["cost_usd"] or 0), "label_run": row["source_run_dir"],
                "calls": int(row["calls"] or 0)}
    return trials


def harness_label(arm: str, config: dict) -> str:
    """The matrix's cell label: the R2' Tofu build is ``tofu``; another frozen Tofu build is ``tofu@<commit>``."""
    if arm != "tofu":
        return arm
    manifest = Path(config.get("artifacts_manifest") or "")
    try:
        commit = json.loads(manifest.read_text())["artifacts"]["tofu"].get("commit")
    except (OSError, ValueError, KeyError):
        commit = None
    return f"tofu@{commit[:10]}" if commit and not commit.startswith("214650e6") else "tofu"


def new_trials(runs: Path, since: str) -> dict:
    trials = {}
    for run_file in sorted(runs.glob("*/*/run.json")):
        config = json.loads(run_file.read_text())
        model, run = run_file.parent.parent.name, run_file.parent.name
        for task_dir in sorted(run_file.parent.glob("trials/*/*")):
            arm, task = task_dir.parent.name, task_dir.name
            measured, invalidated = None, False
            for attempt in sorted(task_dir.glob("attempt-*"), key=lambda path: int(path.name.split("-")[1])):
                if (attempt / "invalidated.json").is_file():  # runner.INVALIDATION_FILE: unmeasured
                    invalidated = True
                    continue
                try:
                    state = json.loads((attempt / "state.json").read_text())
                except (OSError, ValueError):
                    continue
                if (state.get("created_at") or "") >= since and state.get("phase") in ("scored", "no_reward"):
                    measured = state
            key = (model, harness_label(arm, config), run, task)
            if measured is not None:
                trials[key] = {"pass": (measured.get("reward") or 0) >= 1.0, "label_run": run, "arm": arm}
            elif invalidated:
                trials[key] = None  # the snapshot's trial was invalidated and not yet re-measured: drop it
    return trials


def merge(snapshot: dict, newer: dict) -> dict:
    """Takeover trials replace snapshot ones; ``None`` (invalidated, not re-measured) removes the snapshot trial."""
    merged = dict(snapshot)
    for key, trial in newer.items():
        if trial is None:
            merged.pop(key, None)
        else:
            merged[key] = trial
    return merged


def bootstrap_ci(per_task: list[float]) -> tuple[float, float]:
    rng = random.Random(SEED)
    count = len(per_task)
    means = sorted(sum(per_task[rng.randrange(count)] for _ in range(count)) / count for _ in range(BOOTSTRAP))
    return means[int(0.025 * BOOTSTRAP)], means[int(0.975 * BOOTSTRAP) - 1]


def cells(trials: dict, usage: dict) -> list[dict]:
    grouped: dict = defaultdict(list)
    for (model, harness, run, task), trial in trials.items():
        grouped[(model, harness)].append((run, task, trial))
    rows = []
    for (model, harness), items in sorted(grouped.items()):
        by_task, by_run, runs_of_task = defaultdict(list), defaultdict(list), defaultdict(dict)
        spend, calls, tokens = 0.0, 0, [0, 0, 0, 0]
        for run, task, trial in items:
            by_task[task].append(1.0 if trial["pass"] else 0.0)
            by_run[run].append(trial["pass"])
            runs_of_task[task][run] = 1.0 if trial["pass"] else 0.0
            label = f"{trial.get('arm', harness.split('@')[0])}-{model}-{trial['label_run']}"
            used = usage.get((label, task))
            if used:
                calls += used["calls"]
                tokens = [a + b for a, b in zip(tokens, used["tokens"])]
                spend += cost(model, used["tokens"])
            else:
                calls += trial.get("calls", 0)
                spend += trial.get("cost_usd", 0.0)
        per_task = [sum(values) / len(values) for values in by_task.values()]
        low, high = bootstrap_ci(per_task)
        complete = [sum(passes) / len(passes) for passes in by_run.values() if len(passes) >= TASKS]
        rows.append({"model": model, "harness": harness, "n": len(items), "passes": sum(t["pass"] for _, _, t in items),
                     "runs": {run: len(passes) for run, passes in sorted(by_run.items())},
                     "complete_runs": len(complete), "score": sum(per_task) / len(per_task), "ci": (low, high),
                     "run_sd": statistics.stdev(complete) if len(complete) >= 2 else None,
                     "cost_usd": spend, "calls": calls, "tokens": tokens, "per_task_runs": dict(runs_of_task)})
    return rows


def sign_flip(diffs: np.ndarray) -> tuple[float, float]:
    observed = float(np.mean(diffs))
    rng = np.random.default_rng(SEED)
    signs = rng.choice(np.array([-1.0, 1.0]), size=(BOOTSTRAP, len(diffs)))
    null = (signs * diffs).mean(axis=1)
    return observed, (1 + int(np.sum(np.abs(null) >= abs(observed) - 1e-12))) / (BOOTSTRAP + 1)


def holm(pvalues: list[float]) -> list[float]:
    order = sorted(range(len(pvalues)), key=lambda index: pvalues[index])
    adjusted, running = [0.0] * len(pvalues), 0.0
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def paired_diffs(a: dict, b: dict) -> np.ndarray:
    diffs = []
    for task in sorted(set(a) & set(b)):
        shared = sorted(set(a[task]) & set(b[task]))
        mean_a = np.mean([a[task][run] for run in shared] if shared else list(a[task].values()))
        mean_b = np.mean([b[task][run] for run in shared] if shared else list(b[task].values()))
        diffs.append(mean_a - mean_b)
    return np.array(diffs)


def harness_families(rows: list[dict]) -> list[dict]:
    """PREREG A10.3 F-H: per model, every pair of its K >= 3 core cells, Holm-adjusted within the model."""
    families = []
    for model in sorted({row["model"] for row in rows}):
        members = [row for row in rows if row["model"] == model and row["harness"].split("@")[0] in CORE]
        group = [row for row in members if row["complete_runs"] >= 3]
        pairs = []
        for high, low in ((x, y) for i, x in enumerate(group) for y in group[i + 1:]):
            diffs = paired_diffs(high["per_task_runs"], low["per_task_runs"])
            diff, p = sign_flip(diffs)
            pairs.append({"a": high["harness"], "b": low["harness"], "tasks": len(diffs), "diff_pp": 100 * diff, "p": p})
        # Holm's denominator is the whole family; until every core cell of the model is complete, a smaller
        # family would understate it, so its adjusted p-values are provisional and nothing is claimed.
        complete = len(group) == len(members)
        for pair, adjusted in zip(pairs, holm([pair["p"] for pair in pairs])):
            pair["p_holm"] = adjusted
            pair["claimed"] = complete and adjusted < 0.05
        if pairs:
            families.append({"model": model, "cells": [row["harness"] for row in group], "complete": complete,
                             "pending": [row["harness"] for row in members if row not in group], "pairs": pairs})
    return families


def render_families(families: list[dict]) -> str:
    out = ["| model | pair (a vs b) | tasks | a - b pp | p | Holm p | claimed |", "|---|---|---|---|---|---|---|"]
    for family in families:
        status = "" if family["complete"] else f" (family incomplete: {', '.join(family['pending'])} < K3)"
        for pair in family["pairs"]:
            claimed = "yes" if pair["claimed"] else ("no" if family["complete"] else "provisional")
            out.append(f"| {family['model']}{status} | {pair['a']} vs {pair['b']} | {pair['tasks']} | "
                       f"{pair['diff_pp']:+.1f} | {pair['p']:.4f} | {pair['p_holm']:.4f} | {claimed} |")
    return "\n".join(out) if families else "- no model has two K >= 3 core cells yet"


def separations(rows: list[dict]) -> list[str]:
    lines = []
    for model in sorted({row["model"] for row in rows}):
        group = sorted((row for row in rows if row["model"] == model and row["complete_runs"] >= 3),
                       key=lambda row: -row["score"])
        for index, high in enumerate(group):
            for low in group[index + 1:]:
                if high["ci"][0] > low["ci"][1]:
                    lines.append(f"- {model}: {high['harness']} > {low['harness']} "
                                 f"({100 * high['score']:.1f} [{100 * high['ci'][0]:.1f}, {100 * high['ci'][1]:.1f}] vs "
                                 f"{100 * low['score']:.1f} [{100 * low['ci'][0]:.1f}, {100 * low['ci'][1]:.1f}])")
    return lines


def render(rows: list[dict], core_only: bool) -> str:
    out = ["| model | harness | runs (measured per run) | complete K | n | pass | score % [95% CI] | run SD pp | "
           "cost $ | calls | tokens in / cached / out (M) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        if core_only and row["harness"].split("@")[0] not in CORE:
            continue
        uncached, read, write, out_tokens = row["tokens"]
        tokens = (f"{(uncached + read + write) / 1e6:.1f} / {read / 1e6:.1f} / {out_tokens / 1e6:.2f}"
                  if any(row["tokens"]) else "-")
        sd = f"{100 * row['run_sd']:.1f}" if row["run_sd"] is not None else "-"
        out.append(f"| {row['model']} | {row['harness']} | {' '.join(f'{r}:{n}' for r, n in row['runs'].items())} | "
                   f"{row['complete_runs']}/3 | {row['n']} | {row['passes']} | {100 * row['score']:.1f} "
                   f"[{100 * row['ci'][0]:.1f}, {100 * row['ci'][1]:.1f}] | {sd} | {row['cost_usd']:.2f} | "
                   f"{row['calls']} | {tokens} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--matrix", type=Path, required=True, help="the eval-matrix-20261112 directory")
    parser.add_argument("--per-trial", type=Path, help="A10 per_trial.csv (default: <matrix>/analysis/per_trial.csv)")
    parser.add_argument("--relays", type=Path, help="relay logs root (default: <matrix>/relays)")
    parser.add_argument("--since", default="2026-11-17", help="attempts created at/after this are takeover trials")
    parser.add_argument("--all-harnesses", action="store_true", help="include the third-party arms")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    trials = snapshot_trials(args.per_trial or args.matrix / "analysis" / "per_trial.csv")
    trials = merge(trials, new_trials(args.matrix / "runs", args.since))
    rows = cells(trials, relay_usage(args.relays or args.matrix / "relays"))
    separated = separations(rows)
    text = (render(rows, core_only=not args.all_harnesses) + "\n\nSeparated pairs (both cells K >= 3, CIs disjoint):\n"
            + ("\n".join(separated) if separated else "- none"))
    families = harness_families(rows)
    text += ("\n\nPREREG A10.3 F-H (paired sign-flip, Holm within model; claimed iff Holm p < 0.05):\n"
             + render_families(families))
    args.out.write_text(text + "\n", encoding="utf-8")
    summary = [{key: value for key, value in row.items() if key != "per_task_runs"} for row in rows]
    args.out.with_suffix(".json").write_text(json.dumps({"cells": summary, "harness_families": families}, indent=1,
                                                        default=list), encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
