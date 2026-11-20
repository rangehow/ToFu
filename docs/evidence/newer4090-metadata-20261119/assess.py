"""Offline assessment of a retained aggregate receipt, not benchmark execution."""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED = {
    "schema", "cohort", "source", "source_lines", "evidence_level",
    "reported_metadata_qualified_observations", "reported_cutoff",
    "timestamp_timezone", "reported_tofu_artifact_hash_count",
    "reported_control_experimental_directories", "reported_complete_89_task_groups",
    "exact_group_membership", "ordinary_matched_support", "historical_overlap",
    "referenced_inventory_receipts", "underlying_receipt_access",
}


def assess(receipt):
    if set(receipt) != REQUIRED:
        raise ValueError("unexpected aggregate receipt fields")
    if receipt["schema"] != "newer4090-summary-receipt/v1" or receipt["cohort"] != "newer4090-only":
        raise ValueError("wrong receipt universe")
    count = receipt["reported_metadata_qualified_observations"]
    if type(count) is not int or count != 4090:
        raise ValueError("fixed newer universe must be exactly 4090")
    if any(receipt[key] is not None for key in (
        "exact_group_membership", "ordinary_matched_support", "historical_overlap",
    )):
        raise ValueError("this summary assessor cannot qualify unreviewed membership evidence")
    if receipt["underlying_receipt_access"] != "not_available_in_this_assessment":
        raise ValueError("receipt availability requires reassessment")
    return {
        "schema": "newer4090-coverage-ledger/v1",
        "cohort": receipt["cohort"],
        "count_basis": receipt["evidence_level"],
        "reported_metadata_qualified_observations": count,
        "independently_recounted_membership": False,
        "partition": {
            "demonstrably_comparison_qualified": 0,
            "demonstrably_ineligible": 0,
            "qualification_unresolved": count,
        },
        "exclusion_from_comparison": [{
            "count": count,
            "reason": "underlying_metadata_membership_and_comparability_not_established",
            "unit": "aggregate_observations_not_enumerated_rows",
        }],
        "exact_group_count": None,
        "exact_group_eligible_counts": None,
        "ordinary_matched_denominators": None,
        "historical_intersection_count": None,
        "valid_ordinary_comparison_established": False,
        "valid_ordinary_comparison_impossible": False,
        "pooling_authorized_by_evidence": False,
        "nonpoolability": [
            "three_reported_tofu_artifact_hashes_not_mapped_to_membership",
            "control_experimental_arms_not_resolved",
            "exact_model_harness_build_policy_protocol_equivalence_unverified",
            "historical_nonoverlap_unverified",
        ],
        "minimal_metadata_gap": [
            "content_free_membership_or_auditable_aggregate_with_input_digest_and_selection_dedup_invalidation_rules",
            "exact_harness_build_model_policy_protocol_and_arm_group_keys_with_counts",
            "canonical_observation_identity_intersections_against_historical_cohort_and_between_groups",
            "compatible_opaque_task_revision_trial_keys_with_nonduplicate_common_counts_and_matched_outcome_counts",
        ],
        "complete_89_task_groups_reported": receipt["reported_complete_89_task_groups"],
        "complete_runs_required_for_ordinary_description": False,
        "certifies_containment": False,
        "sandbox_task_eligibility_or_attempts_assessed": False,
        "leaderboard": False,
    }


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def bundle():
    receipt_path = HERE / "receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    ledger = encoded(assess(json.loads(receipt_bytes)))
    source = HERE.parent / "retained-matrix-20261119" / "README.md"
    inputs = {
        "receipt.json": receipt_bytes,
        "assess.py": Path(__file__).read_bytes(),
        "../retained-matrix-20261119/README.md": source.read_bytes(),
    }
    manifest = {
        "schema": "newer4090-manifest/v1",
        "inputs_sha256": {key: hashlib.sha256(value).hexdigest() for key, value in inputs.items()},
        "ledger_sha256": hashlib.sha256(ledger).hexdigest(),
        "scope": "aggregate_receipt_only_no_raw_tasks_no_prior_report_mutation",
    }
    return {"ledger.json": ledger, "manifest.json": encoded(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    files = bundle()
    args.out.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (args.out / name).write_bytes(content)


if __name__ == "__main__":
    main()
