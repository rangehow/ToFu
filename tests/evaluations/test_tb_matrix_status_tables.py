"""EVAL-MATRIX status tables: combining the A10 snapshot with takeover trials, cost attribution, CI separation."""
import csv
import json
from pathlib import Path

import pytest

from evaluations.terminal_bench.eval_matrix import status_tables

pytestmark = pytest.mark.unit

FIELDS = ["model", "harness", "run", "task", "outcome", "in_extended_E", "cost_usd", "source_run_dir", "calls"]


def snapshot(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({"in_extended_E": "1", "cost_usd": "0.5", "calls": "3", **row})
    return path


def attempt(matrix: Path, model: str, run: str, arm: str, task: str, number: int, **state) -> None:
    run_dir = matrix / "runs" / model / run
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"artifacts_manifest": str(matrix / "missing.json")}))
    directory = run_dir / "trials" / arm / task / f"attempt-{number}"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps(state))


def test_takeover_trials_override_the_snapshot_and_carry_relay_usage(tmp_path):
    matrix = tmp_path / "eval-matrix"
    csv_path = snapshot(tmp_path / "per_trial.csv", [
        {"model": "glm-5.2", "harness": "pi", "run": "a1", "task": "t1", "outcome": "fail", "source_run_dir": "a1"},
        {"model": "glm-5.2", "harness": "pi", "run": "a1", "task": "t2", "outcome": "pass", "source_run_dir": "a1"},
        {"model": "glm-5.2", "harness": "pi", "run": "r1", "task": "t1", "outcome": "pass", "in_extended_E": "0",
         "source_run_dir": "r1"},
    ])
    attempt(matrix, "glm-5.2", "a1", "pi", "t1", 1, created_at="2026-11-13T10:00:00", phase="lost")
    attempt(matrix, "glm-5.2", "a1", "pi", "t1", 2, created_at="2026-11-17T20:00:00", phase="scored", reward=1.0)
    attempt(matrix, "glm-5.2", "a1", "pi", "t3", 1, created_at="2026-11-17T20:00:00", phase="running")
    relay = matrix / "relays" / "glm-5.2@6672-takeover"
    relay.mkdir(parents=True)
    (relay / "requests.jsonl").write_text(json.dumps({
        "kind": "chat", "arm": "pi-glm-5.2-a1", "task": "t1",
        "usage": {"prompt_tokens": 1_000_000, "prompt_tokens_details": {"cached_tokens": 400_000},
                  "completion_tokens": 100_000}}) + "\n")
    trials = status_tables.merge(status_tables.snapshot_trials(csv_path),
                                 status_tables.new_trials(matrix / "runs", "2026-11-17"))
    [cell] = status_tables.cells(trials, status_tables.relay_usage(matrix / "relays"))
    assert (cell["n"], cell["passes"], cell["runs"]) == (2, 2, {"a1": 2}), "r1 is outside E; t3 is still running"
    assert cell["tokens"] == [600_000, 400_000, 0, 100_000]
    expected = (600_000 * 3.45 + 400_000 * 3.45 * 0.10 + 100_000 * 13.81) / 1e6 + 0.5
    assert abs(cell["cost_usd"] - expected) < 1e-9, "t1 priced from the relay, t2 from the snapshot"


def test_an_invalidated_snapshot_trial_leaves_the_cell_until_it_is_re_measured(tmp_path):
    matrix = tmp_path / "eval-matrix"
    csv_path = snapshot(tmp_path / "per_trial.csv", [
        {"model": "k", "harness": "claude-code", "run": "a1", "task": task, "outcome": "fail", "source_run_dir": "a1"}
        for task in ("t1", "t2")])
    for task in ("t1", "t2"):
        attempt(matrix, "k", "a1", "claude-code", task, 1, created_at="2026-11-13T10:00:00", phase="scored", reward=0.0)
        (matrix / "runs/k/a1/trials/claude-code" / task / "attempt-1" / "invalidated.json").write_text("{}")
    attempt(matrix, "k", "a1", "claude-code", "t2", 2, created_at="2026-11-17T20:00:00", phase="scored", reward=1.0)
    trials = status_tables.merge(status_tables.snapshot_trials(csv_path),
                                 status_tables.new_trials(matrix / "runs", "2026-11-17"))
    assert trials == {("k", "claude-code", "a1", "t2"): {"pass": True, "label_run": "a1", "arm": "claude-code"}}


def test_only_disjoint_cis_of_complete_cells_are_reported_as_separated():
    def row(harness, score, low, high, complete=3):
        return {"model": "m", "harness": harness, "score": score, "ci": (low, high), "complete_runs": complete}

    lines = status_tables.separations([row("a", 0.8, 0.7, 0.9), row("b", 0.6, 0.5, 0.69), row("c", 0.75, 0.65, 0.85),
                                       row("d", 0.2, 0.1, 0.3, complete=2)])
    assert lines == [lines[0]] and lines[0].startswith("- m: a > b")


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    per_task = [1.0] * 60 + [0.0] * 29
    low, high = status_tables.bootstrap_ci(per_task)
    assert (low, high) == status_tables.bootstrap_ci(per_task)
    assert low < 60 / 89 < high


def _cell(harness, per_task, complete=3, model="m"):
    return {"model": model, "harness": harness, "complete_runs": complete, "per_task_runs": per_task}


def test_holm_adjusts_in_rank_order_and_stays_monotone():
    assert status_tables.holm([0.01, 0.04, 0.03]) == [0.03, 0.06, 0.06]


def test_harness_family_claims_only_holm_significant_pairs_of_complete_cells():
    runs = ("r1", "r2", "r3")
    strong = {f"t{i}": {run: 1.0 for run in runs} for i in range(89)}
    weak = {f"t{i}": {run: float(i % 4 == 0) for run in runs} for i in range(89)}
    twin = {task: dict(values) for task, values in strong.items()}
    [family] = status_tables.harness_families([
        _cell("tofu", strong), _cell("pi", weak), _cell("codex", twin), _cell("goose", weak)])
    assert family["cells"] == ["tofu", "pi", "codex"] and family["complete"], "non-core arms are not in F-H"
    claims = {(pair["a"], pair["b"]): pair["claimed"] for pair in family["pairs"]}
    assert claims == {("tofu", "pi"): True, ("tofu", "codex"): False, ("pi", "codex"): True}
    [same] = [pair for pair in family["pairs"] if pair["b"] == "codex" and pair["a"] == "tofu"]
    assert same["p"] == 1.0 and same["diff_pp"] == 0.0


def test_a_family_with_an_incomplete_core_cell_claims_nothing():
    runs = ("r1", "r2", "r3")
    strong = {f"t{i}": {run: 1.0 for run in runs} for i in range(89)}
    weak = {f"t{i}": {run: 0.0 for run in runs} for i in range(89)}
    [family] = status_tables.harness_families([_cell("tofu", strong), _cell("pi", weak),
                                               _cell("codex", weak, complete=1)])
    assert not family["complete"] and family["pending"] == ["codex"]
    [pair] = family["pairs"]
    assert pair["p_holm"] < 0.05 and not pair["claimed"], "Holm over a partial family understates it"
