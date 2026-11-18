"""FrontierHarness Eval: replicate-block analysis over FHE's unmodified normalizer.

Responsibility: score every replicate block (one or more native work dirs per block
label) per arm with the FHE repository's own ``normalize-results.mjs`` (through
``export.export``), then aggregate the canonical cells across blocks:

- task-weighted pass rates with task-clustered bootstrap CIs, per arm and suite;
- the paired treatment - reference difference with a sign-flip permutation test;
- the reference arm's calibration against an FHE published row (Poisson-binomial
  test over our per-task rates; the block-1 McNemar rule of PREREG.md beside it);
- an FHE-anchored estimate for the treatment, reported only when calibration holds;
- spend per block and arm (frozen FHE table) and requests / 429s / RPM per key.

Three variants are scored: ``primary`` (FHE's canonical cells as its normalizer
selects them), ``harness_fault_as_failure`` (a no-model-call cell whose trial still
got a verifier reward counts as a failure) and ``unthrottled`` (cells with an
upstream 429 omitted). Rules: FRONTIER-HARNESS-2 ``PREREG-2.md`` section 5.

Entry point: ``python -m evaluations.frontier_harness.multi_attempt --block k1=<work dir> ...``.
Dependencies: ``evaluations.frontier_harness.export``, the FHE checkout at e837a70, node.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

from evaluations.frontier_harness.export import MODEL_CALL_KINDS, export, rpm_summary

VARIANTS = ("primary", "harness_fault_as_failure", "unthrottled")
# export.fhe_status's reason when a trial got a verifier reward but its harness never called the model.
NO_MODEL_CALL_REASON = "verifier reward without observed model usage"
SCORED = ("success", "failure")
# PREREG-2 section 5: exact sign-flip enumeration up to this many non-zero task differences.
EXACT_SIGN_FLIP_MAX = 20


def wilson(successes: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 4), round(centre + half, 4))


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def poisson_binomial_pmf(probabilities: list[float]) -> list[float]:
    pmf = [1.0]
    for p in probabilities:
        step = [0.0] * (len(pmf) + 1)
        for k, mass in enumerate(pmf):
            step[k] += mass * (1 - p)
            step[k + 1] += mass * p
        pmf = step
    return pmf


def poisson_binomial_two_sided(probabilities: list[float], observed: int) -> float:
    pmf = poisson_binomial_pmf(probabilities)
    return min(1.0, 2 * min(sum(pmf[:observed + 1]), sum(pmf[observed:])))


def sign_flip_p(differences: list[float], draws: int, rng: random.Random) -> float:
    """Two-sided paired sign-flip permutation p-value of sum(differences) around 0."""
    nonzero = [d for d in differences if abs(d) > 1e-12]
    if not nonzero:
        return 1.0
    observed = abs(sum(nonzero)) - 1e-9
    if len(nonzero) <= EXACT_SIGN_FLIP_MAX:
        hits = sum(1 for signs in itertools.product((1, -1), repeat=len(nonzero))
                   if abs(sum(s * d for s, d in zip(signs, nonzero))) >= observed)
        return hits / 2 ** len(nonzero)
    hits = sum(1 for _ in range(draws)
               if abs(sum(d if rng.random() < 0.5 else -d for d in nonzero)) >= observed)
    return (hits + 1) / (draws + 1)


def percentile(sorted_values: list[float], q: float) -> float:
    index = q * (len(sorted_values) - 1)
    low, high = math.floor(index), math.ceil(index)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (index - low)


def bootstrap_mean_ci(values: list[float], draws: int, rng: random.Random) -> tuple[float, float] | None:
    """Percentile 95% CI of the mean of per-task values, resampling tasks with replacement."""
    if not values:
        return None
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(draws))
    return (round(percentile(means, 0.025), 6), round(percentile(means, 0.975), 6))


def canonical_cells(arm_dir: Path, *, reclassify_no_call: bool) -> dict[str, dict]:
    """FHE canonical selection per task over one exported arm: the earliest valid attempt by
    (started_at, run_id), else the last attempt; smoke and warm labels are excluded, as in
    normalize-results.mjs. ``reclassify_no_call`` applies the harness-fault-as-failure variant."""
    rows: dict[str, list] = {}
    for path in sorted(arm_dir.glob("trials/*/trial.json")):
        trial = json.loads(path.read_text(encoding="utf-8"))
        label = str(trial.get("label") or "").lower()
        if (trial.get("attempt") or 1) != 1 or "warm" in label or label.startswith("smoke"):
            continue
        status = trial["status"]
        if reclassify_no_call and status == "infra_invalid" and trial.get("status_reason") == NO_MODEL_CALL_REASON:
            status = "failure"
        rows.setdefault(trial["id"], []).append((str(trial.get("started_at") or ""), str(trial.get("run_id") or ""),
                                                 status, trial))
    cells = {}
    for task_id, attempts in rows.items():
        attempts.sort(key=lambda row: (row[0], row[1]))
        chosen = next((row for row in attempts if row[2] in SCORED), attempts[-1])
        trial = chosen[3]
        cells[task_id] = {"status": chosen[2], "reason": trial.get("status_reason"),
                          "throttled": bool(trial.get("throttled")), "cost_usd": trial.get("cost_usd"),
                          "turns": trial.get("turns"), "evidence": trial.get("evidence")}
    return cells


def task_rates(cells_by_block: dict[str, dict[str, dict]], tasks: list[str]) -> dict[str, tuple[int, int]]:
    """(passes, valid cells) per task over every block."""
    rates = {}
    for task in tasks:
        statuses = [block[task]["status"] for block in cells_by_block.values() if task in block]
        rates[task] = (sum(1 for s in statuses if s == "success"), sum(1 for s in statuses if s in SCORED))
    return rates


def arm_rate(rates: dict[str, tuple[int, int]], draws: int, rng: random.Random) -> dict:
    values = [s / n for s, n in rates.values() if n]
    return {"tasks": len(values), "cells": sum(n for _, n in rates.values()),
            "passes": sum(s for s, _ in rates.values()),
            "rate": round(sum(values) / len(values), 6) if values else None,
            "ci95_task_bootstrap": bootstrap_mean_ci(values, draws, rng)}


def paired(treatment: dict[str, tuple[int, int]], reference: dict[str, tuple[int, int]], draws: int,
           permutation_draws: int, rng: random.Random) -> dict:
    tasks = [task for task in treatment if treatment[task][1] and reference.get(task, (0, 0))[1]]
    diffs = {task: treatment[task][0] / treatment[task][1] - reference[task][0] / reference[task][1] for task in tasks}
    values = list(diffs.values())
    mean = sum(values) / len(values) if values else None
    ci = bootstrap_mean_ci(values, draws, rng)
    p = sign_flip_p(values, permutation_draws, rng)
    if ci and ci[0] > 0 and p < 0.05:
        verdict = "treatment ahead"
    elif ci and ci[1] < 0 and p < 0.05:
        verdict = "reference ahead"
    else:
        verdict = "not distinguishable at this sample"
    return {"tasks": len(tasks), "mean_difference": round(mean, 6) if mean is not None else None,
            "ci95_task_bootstrap": ci, "sign_flip_p": round(p, 5), "verdict": verdict,
            "treatment_better_tasks": sorted(t for t, d in diffs.items() if d > 0),
            "reference_better_tasks": sorted(t for t, d in diffs.items() if d < 0),
            "per_task_difference": {t: round(d, 4) for t, d in sorted(diffs.items())}}


def published_value(detail: dict) -> bool | float | None:
    """A published task outcome: v1's single attempt (``success``) or, when ``attempts`` is given (v1.1's
    repeated attempts), the task's pass rate; None for a task without a finished attempt."""
    if "attempts" in detail:
        return detail["passes"] / detail["attempts"] if detail["attempts"] else None
    return bool(detail.get("success"))


def calibration(reference: dict[str, tuple[int, int]], first_block: dict[str, dict], published: dict[str, bool | float],
                draws: int = 10_000, permutation_draws: int = 200_000, rng: random.Random | None = None) -> dict:
    tasks = sorted(task for task in reference if reference[task][1] and task in published)
    if any(not isinstance(published[t], bool) for t in tasks):
        # Published per-task rates (several attempts): paired per-task difference, as the arm comparison.
        rng = rng or random.Random(0)
        diffs = [reference[t][0] / reference[t][1] - published[t] for t in tasks]
        p = sign_flip_p(diffs, permutation_draws, rng)
        return {"tasks": len(tasks), "mode": "published per-task rates",
                "mean_difference": round(sum(diffs) / len(diffs), 6) if diffs else None,
                "ci95_task_bootstrap": bootstrap_mean_ci(diffs, draws, rng), "sign_flip_p": round(p, 5),
                "verdict": "consistent" if p >= 0.05 else "conditions differ",
                "per_task": {t: {"ours": f"{reference[t][0]}/{reference[t][1]}", "published": round(published[t], 4)}
                             for t in tasks}}
    probabilities = [(reference[t][0] + 0.5) / (reference[t][1] + 1) for t in tasks]
    observed = sum(1 for t in tasks if published[t])
    p = poisson_binomial_two_sided(probabilities, observed)
    expected = sum(probabilities)
    block_tasks = [t for t in tasks if first_block.get(t, {}).get("status") in SCORED]
    ours = {t: first_block[t]["status"] == "success" for t in block_tasks}
    b = sum(1 for t in block_tasks if ours[t] and not published[t])
    c = sum(1 for t in block_tasks if not ours[t] and published[t])
    ours_pass, published_pass = sum(ours.values()), sum(1 for t in block_tasks if published[t])
    return {"tasks": len(tasks), "published_passes": observed, "expected_under_our_rates": round(expected, 3),
            "poisson_binomial_p": round(p, 4), "verdict": "consistent" if p >= 0.05 else "conditions differ",
            "block1_rule": {"tasks": len(block_tasks), "ours": ours_pass, "published": published_pass,
                            "ours_only": b, "published_only": c, "mcnemar_exact_p": round(mcnemar_exact(b, c), 4),
                            "consistent": abs(ours_pass - published_pass) <= 3 and mcnemar_exact(b, c) >= 0.05},
            "per_task": {t: {"ours": f"{reference[t][0]}/{reference[t][1]}", "published": published[t]} for t in tasks}}


def anchored(treatment: dict[str, tuple[int, int]], reference: dict[str, tuple[int, int]], published: dict[str, bool],
             published_rows: dict[str, dict[str, bool]], draws: int, rng: random.Random) -> dict:
    tasks = sorted(t for t in treatment if treatment[t][1] and reference.get(t, (0, 0))[1] and t in published)
    values = [float(published[t]) + treatment[t][0] / treatment[t][1] - reference[t][0] / reference[t][1] for t in tasks]
    estimate = sum(values) / len(values) if values else None
    ci = bootstrap_mean_ci(values, draws, rng)
    rates = {name: round(sum(float(row.get(t) or 0) for t in tasks) / len(tasks), 4)
             for name, row in published_rows.items()} if tasks else {}
    within = sorted(name for name, rate in rates.items() if ci and ci[0] <= rate <= ci[1])
    return {"tasks": len(tasks), "estimate": round(estimate, 6) if estimate is not None else None,
            "ci95_task_bootstrap": ci, "published_rates_on_these_tasks": rates,
            "published_within_ci": within, "methodology_comparable": False}


def run_normalizer(fhe_repo: Path, run_dir: Path, label: str) -> dict:
    script = fhe_repo / "skills/frontierharness-eval/scripts/normalize-results.mjs"
    reply = subprocess.run(["node", str(script), "--run", str(run_dir), "--label", label,
                            "--benchmark", str(fhe_repo / "benchmark.json")], check=True, capture_output=True, text=True)
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate["normalizer_line"] = reply.stdout.strip()
    return candidate


def merge_arm(export_dirs: list[Path], arm: str, target: Path) -> None:
    """One FHE run directory holding every exported attempt of ``arm`` across a block's work dirs."""
    if target.exists():
        shutil.rmtree(target)
    (target / "trials").mkdir(parents=True)
    run_meta = None
    for index, source in enumerate(export_dirs):
        arm_dir = source / arm
        if not arm_dir.is_dir():
            continue
        run_meta = run_meta or json.loads((arm_dir / "run.json").read_text(encoding="utf-8"))
        for trial_file in sorted(arm_dir.glob("trials/*/trial.json")):
            directory = target / "trials" / f"{trial_file.parent.name}__w{index}"
            directory.mkdir()
            shutil.copyfile(trial_file, directory / "trial.json")
    if run_meta is None:
        raise ValueError(f"no exported {arm} run in {export_dirs}")
    (target / "run.json").write_text(json.dumps(run_meta, indent=2) + "\n", encoding="utf-8")


def block_traffic(records: list[dict], run_labels: list[str]) -> dict:
    """Requests, upstream 429s and RPM per key for the model calls of one block's run labels."""
    mine = [r for r in records if r.get("kind") in MODEL_CALL_KINDS
            and any(str(r.get("arm") or "").endswith("-" + label) for label in run_labels)]
    keys: dict[str, dict] = {}
    for record in mine:
        row = keys.setdefault(str(record.get("key")), {"requests": 0, "upstream_429": 0, "non_200": 0})
        row["requests"] += int(record.get("attempts") or 1)
        retries = record.get("retries") or []
        row["upstream_429"] += (sum(1 for retry in retries if retry.get("status") == 429)
                                + (record.get("status") == 429) + bool(record.get("stream_429")))
        row["non_200"] += record.get("status") != 200
    return {"keys": keys, "rpm": rpm_summary(mine),
            "rpm_wait_s": round(sum(float(r.get("rpm_wait_s") or 0.0) for r in mine), 1)}


def analyse(*, blocks: dict[str, list[Path]], relay_logs: list[Path], fhe_repo: Path, out: Path, arms: list[str],
            treatment: str, reference: str, published: str | None, suite: str, draws: int,
            permutation_draws: int, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    combined = out / "relay-combined.jsonl"
    with combined.open("w", encoding="utf-8") as handle:
        for log in relay_logs:
            handle.write(Path(log).read_text(encoding="utf-8"))
    records = [json.loads(line) for line in combined.read_text(encoding="utf-8").splitlines() if line.strip()]
    cells: dict[str, dict[str, dict[str, dict]]] = {v: {arm: {} for arm in arms} for v in VARIANTS}
    planned: dict[str, set[str]] = {}
    blocks_out = {}
    for label, native_dirs in blocks.items():
        if label.lower().startswith("smoke"):
            raise SystemExit(f"block {label}: smoke runs are never analysed as canonical blocks")
        runs = [json.loads((Path(d) / "run.json").read_text(encoding="utf-8")) for d in native_dirs]
        exports = {}
        for exclude, variant in ((False, "primary"), (True, "unthrottled")):
            dirs = []
            for index, native in enumerate(native_dirs):
                target = out / "export" / variant / label / f"w{index}"
                summary = export(native_dir=Path(native), relay_log=combined, fhe_repo=fhe_repo, out_root=target,
                                 label=f"fhm-{label}", run_id=f"fhm-{label}-w{index}",
                                 provider="FRIDAY kimi-k3 via FHE relay", run_normalizer=False,
                                 exclude_throttled=exclude)
                dirs.append(target)
                exports.setdefault(variant, []).append(summary)
            for arm in arms:
                merged = out / "merged" / variant / label / arm
                merge_arm(dirs, arm, merged)
                candidate = run_normalizer(fhe_repo, merged, f"fhm-{label}")
                fhe_cells = {row["id"]: row for row in candidate["task_details"] if row["status"] != "missing"}
                derived = canonical_cells(merged, reclassify_no_call=False)
                mismatch = {t for t in fhe_cells if derived.get(t, {}).get("status") != fhe_cells[t]["status"]}
                if mismatch:
                    raise RuntimeError(f"{label}/{arm}/{variant}: canonical cells differ from FHE's normalizer on "
                                       f"{sorted(mismatch)}")
                cells[variant][arm][label] = {t: derived[t] for t in fhe_cells}
                if variant == "primary":
                    cells["harness_fault_as_failure"][arm][label] = canonical_cells(merged, reclassify_no_call=True)
                    blocks_out.setdefault(label, {}).setdefault("normalizer", {})[arm] = candidate["normalizer_line"]
        tasks = sorted({task for run in runs for task in run["tasks"]})
        ids = {}
        for native in native_dirs:
            manifest = json.loads((Path(json.loads((Path(native) / "run.json").read_text())["tasks_dir"])
                                   / "fhe-task-manifest.json").read_text())
            ids.update({name: row["id"] for name, row in manifest["tasks"].items()})
        planned[label] = {ids[t] for t in tasks}
        spend = {}
        for summary in exports["primary"]:
            for arm, row in summary["arms"].items():
                spend[arm] = round(spend.get(arm, 0.0) + row["cost_usd"], 4)
        blocks_out[label].update({
            "native_dirs": [str(d) for d in native_dirs], "run_labels": [run["run_label"] for run in runs],
            "keys_configured": sorted({str(run.get("relay_url")) for run in runs}),
            "tasks": len(tasks), "spend_usd_frozen_table": spend,
            "traffic": block_traffic(records, [run["run_label"] for run in runs]),
            "per_arm": {}})
        for arm in arms:
            row = {}
            for variant in VARIANTS:
                block_cells = {t: c for t, c in cells[variant][arm][label].items() if t in planned[label]}
                passes = sum(1 for c in block_cells.values() if c["status"] == "success")
                valid = sum(1 for c in block_cells.values() if c["status"] in SCORED)
                row[variant] = {"passes": passes, "valid": valid, "planned": len(planned[label]),
                                "infra_invalid": sum(1 for c in block_cells.values() if c["status"] == "infra_invalid"),
                                "throttled": sum(1 for c in block_cells.values() if c.get("throttled")),
                                "wilson95": wilson(passes, valid)}
            blocks_out[label]["per_arm"][arm] = row
    all_tasks = sorted(set().union(*planned.values()))
    if suite != "all":
        all_tasks = [t for t in all_tasks if t.startswith(suite + "/")]
    published_rows = {}
    if published:
        data = json.loads((fhe_repo / "results/eval-data.json").read_text(encoding="utf-8"))
        published_rows = {h["name"]: {t["id"]: published_value(t) for t in h["task_details"]
                                      if published_value(t) is not None} for h in data["harnesses"]}
    result = {"schema": "fhe.multi-attempt/v1", "blocks": blocks_out, "tasks": all_tasks, "suite": suite,
              "bootstrap": {"draws": draws, "seed": seed}, "variants": {}}
    for variant in VARIANTS:
        rng = random.Random(seed)
        rates = {arm: task_rates({k: {t: c for t, c in v.items() if t in all_tasks}
                                  for k, v in cells[variant][arm].items()}, all_tasks) for arm in arms}
        section = {"arms": {}, "suites": {}}
        for arm in arms:
            section["arms"][arm] = arm_rate(rates[arm], draws, rng)
            for prefix in sorted({t.split("/", 1)[0] for t in all_tasks}):
                section["suites"].setdefault(prefix, {})[arm] = arm_rate(
                    {t: r for t, r in rates[arm].items() if t.startswith(prefix + "/")}, draws, rng)
        if treatment in arms and reference in arms:
            section["paired"] = paired(rates[treatment], rates[reference], draws, permutation_draws, rng)
        if published and reference in arms:
            first = next(iter(cells[variant][reference].values()), {})
            section["calibration"] = calibration(rates[reference], first, published_rows[published], draws,
                                                 permutation_draws, rng)
            if treatment in arms:
                section["anchored"] = anchored(rates[treatment], rates[reference], published_rows[published],
                                               published_rows, draws, rng)
                if section["calibration"]["verdict"] != "consistent":
                    section["anchored"]["withheld"] = "calibration: conditions differ (PREREG-2 section 5 rule 4)"
        section["per_task"] = {t: {arm: "".join({"success": "1", "failure": "0"}.get(
            cells[variant][arm][label].get(t, {}).get("status"), "-" if t in planned[label] else " ")
            for label in blocks) for arm in arms} for t in all_tasks}
        if published:
            for t in all_tasks:
                section["per_task"][t]["fhe_" + published] = "1" if published_rows[published].get(t) else "0"
        result["variants"][variant] = section
    (out / "analysis.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def parse_block(text: str) -> tuple[str, list[Path]]:
    label, _, dirs = text.partition("=")
    if not label or not dirs:
        raise argparse.ArgumentTypeError("--block takes LABEL=DIR[,DIR...]")
    return label, [Path(d) for d in dirs.split(",")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluations.frontier_harness.multi_attempt")
    parser.add_argument("--block", type=parse_block, action="append", required=True,
                        help="LABEL=WORKDIR[,WORKDIR...]: one replicate block (its native work dirs)")
    parser.add_argument("--relay-log", type=Path, action="append", required=True)
    parser.add_argument("--fhe-repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arms", default="tofu,codex")
    parser.add_argument("--treatment", default="tofu")
    parser.add_argument("--reference", default="codex")
    parser.add_argument("--published", default="codex", help="FHE results/eval-data.json harness for calibration")
    parser.add_argument("--suite", default="all", help="all, terminal-bench or datacurve")
    parser.add_argument("--bootstrap", type=int, default=10_000)
    parser.add_argument("--permutations", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=20261113)
    args = parser.parse_args(argv)
    blocks = dict(args.block)
    if len(blocks) != len(args.block):
        parser.error("block labels must be unique")
    result = analyse(blocks=blocks, relay_logs=args.relay_log, fhe_repo=args.fhe_repo, out=args.out,
                     arms=args.arms.split(","), treatment=args.treatment, reference=args.reference,
                     published=args.published or None, suite=args.suite, draws=args.bootstrap,
                     permutation_draws=args.permutations, seed=args.seed)
    brief = {variant: {k: v for k, v in section.items() if k != "per_task"}
             for variant, section in result["variants"].items()}
    print(json.dumps({"blocks": {k: {kk: vv for kk, vv in v.items() if kk in ("per_arm", "spend_usd_frozen_table")}
                                 for k, v in result["blocks"].items()}, "variants": brief}, indent=1)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
