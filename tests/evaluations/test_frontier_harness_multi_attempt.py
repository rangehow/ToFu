"""Specs for the FrontierHarness replicate-block analysis (PREREG-2 section 5); offline, no node or FHE checkout."""
import json
import random

import pytest

from evaluations.frontier_harness import multi_attempt as ma

pytestmark = pytest.mark.unit


def test_wilson_and_mcnemar_match_the_phase_a_scorer():
    assert ma.wilson(18, 21) == (0.6536, 0.9502)
    assert ma.wilson(0, 0) is None
    assert ma.mcnemar_exact(3, 0) == 0.25
    assert ma.mcnemar_exact(0, 0) == 1.0


def test_poisson_binomial_reduces_to_the_binomial_and_tests_two_sided():
    assert ma.poisson_binomial_pmf([0.5] * 4) == pytest.approx([1 / 16, 4 / 16, 6 / 16, 4 / 16, 1 / 16])
    assert sum(ma.poisson_binomial_pmf([0.1, 0.7, 0.95, 0.3])) == pytest.approx(1.0)
    assert ma.poisson_binomial_two_sided([0.5] * 4, 2) == 1.0
    assert ma.poisson_binomial_two_sided([0.9] * 10, 3) < 0.001


def test_sign_flip_is_exact_for_few_tasks_and_sampled_for_many():
    rng = random.Random(1)
    assert ma.sign_flip_p([1.0, 1.0, 1.0], 1000, rng) == 0.25
    assert ma.sign_flip_p([1.0, -1.0, 0.0], 1000, rng) == 1.0
    assert ma.sign_flip_p([0.0, 0.0], 1000, rng) == 1.0
    sampled = ma.sign_flip_p([1.0] * (ma.EXACT_SIGN_FLIP_MAX + 5), 2000, rng)
    assert sampled == pytest.approx(1 / 2001)


def _trial(directory, name, **fields):
    path = directory / "trials" / name
    path.mkdir(parents=True)
    (path / "trial.json").write_text(json.dumps({"attempt": 1, "label": "fhm-k1", "run_id": "fhm-k1-w0", **fields}))


def test_canonical_cells_follow_fhe_selection_and_the_robustness_reclassification(tmp_path):
    _trial(tmp_path, "a__a1", id="s/a", status="infra_invalid", status_reason="lost VM",
           started_at="2026-11-13T01:00:00Z")
    _trial(tmp_path, "a__a2", id="s/a", status="failure", status_reason="verifier reward with model usage",
           started_at="2026-11-13T02:00:00Z")
    _trial(tmp_path, "b__a1", id="s/b", status="infra_invalid", status_reason=ma.NO_MODEL_CALL_REASON,
           started_at="2026-11-13T01:00:00Z")
    _trial(tmp_path, "c__a1", id="s/c", status="success", label="smoke-fhm-t4", started_at="2026-11-13T01:00:00Z")
    primary = ma.canonical_cells(tmp_path, reclassify_no_call=False)
    robust = ma.canonical_cells(tmp_path, reclassify_no_call=True)
    assert primary["s/a"]["status"] == robust["s/a"]["status"] == "failure"
    assert primary["s/b"]["status"] == "infra_invalid"
    assert robust["s/b"]["status"] == "failure"
    assert "s/c" not in primary


def test_paired_verdict_needs_both_the_interval_and_the_permutation_test():
    rng = random.Random(2)
    treatment = {f"t{i}": (3, 3) for i in range(6)}
    reference = {f"t{i}": (0, 3) for i in range(6)}
    result = ma.paired(treatment, reference, 2000, 1000, rng)
    assert result["mean_difference"] == 1.0
    assert result["sign_flip_p"] == pytest.approx(2 / 64)
    assert result["verdict"] == "treatment ahead"
    reference["t0"] = (3, 3)
    reference["t1"] = (3, 3)
    weaker = ma.paired(treatment, reference, 2000, 1000, rng)
    assert weaker["sign_flip_p"] == pytest.approx(2 / 16)
    assert weaker["verdict"] == "not distinguishable at this sample"
    assert ma.paired({"x": (1, 1)}, {"x": (0, 0)}, 100, 100, rng)["tasks"] == 0


def test_calibration_uses_smoothed_per_task_rates_and_reports_the_block1_rule():
    reference = {f"t{i}": (3, 3) for i in range(10)}
    published = {f"t{i}": True for i in range(10)}
    first = {f"t{i}": {"status": "success"} for i in range(10)}
    result = ma.calibration(reference, first, published)
    assert result["poisson_binomial_p"] == pytest.approx(min(1.0, 2 * 0.875 ** 10), abs=1e-4)
    assert result["verdict"] == "consistent"
    assert result["block1_rule"]["consistent"] is True
    published = {f"t{i}": i < 2 for i in range(10)}
    assert ma.calibration(reference, first, published)["verdict"] == "conditions differ"


def test_published_value_reads_v1_single_attempts_and_v11_rates():
    assert ma.published_value({"id": "a", "success": True}) is True
    assert ma.published_value({"id": "a", "success": False}) is False
    assert ma.published_value({"id": "a", "passes": 3, "attempts": 4}) == 0.75
    assert ma.published_value({"id": "a", "passes": 0, "attempts": 0}) is None


def test_calibration_against_published_rates_is_a_paired_task_test():
    rng = random.Random(3)
    reference = {f"t{i}": (2, 4) for i in range(8)}
    same = ma.calibration(reference, {}, {f"t{i}": 0.5 for i in range(8)}, 2000, 1000, rng)
    assert same["mode"] == "published per-task rates" and same["sign_flip_p"] == 1.0
    assert same["verdict"] == "consistent" and same["mean_difference"] == 0.0
    higher = ma.calibration(reference, {}, {f"t{i}": 1.0 for i in range(8)}, 2000, 1000, rng)
    assert higher["mean_difference"] == -0.5 and higher["sign_flip_p"] == pytest.approx(2 / 256, abs=1e-5)
    assert higher["verdict"] == "conditions differ"
