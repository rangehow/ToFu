import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import assess


class AggregateReceiptChecks(unittest.TestCase):
    def setUp(self):
        self.receipt = json.loads((assess.HERE / "receipt.json").read_text())

    def test_conservation_and_unknown_not_failure(self):
        ledger = assess.assess(self.receipt)
        self.assertEqual(sum(ledger["partition"].values()), 4090)
        self.assertEqual(ledger["partition"], {
            "demonstrably_comparison_qualified": 0,
            "demonstrably_ineligible": 0,
            "qualification_unresolved": 4090,
        })
        self.assertIsNone(ledger["historical_intersection_count"])
        self.assertIsNone(ledger["ordinary_matched_denominators"])
        self.assertIsNone(ledger["exact_group_count"])
        self.assertIsNone(ledger["exact_group_eligible_counts"])

    def test_no_full_run_does_not_prove_no_ordinary_match(self):
        ledger = assess.assess(self.receipt)
        self.assertEqual(ledger["complete_89_task_groups_reported"], 0)
        self.assertFalse(ledger["complete_runs_required_for_ordinary_description"])
        self.assertFalse(ledger["valid_ordinary_comparison_impossible"])
        self.assertFalse(ledger["valid_ordinary_comparison_established"])

    def test_fixed_universe_rejects_historical_pool_and_bad_counts(self):
        for count in (802, 4892, 0, -1, True, "4090", 4090.0):
            with self.subTest(count=count):
                receipt = copy.deepcopy(self.receipt)
                receipt["reported_metadata_qualified_observations"] = count
                with self.assertRaises(ValueError):
                    assess.assess(receipt)

    def test_unreviewed_membership_never_silently_admitted(self):
        for key in ("exact_group_membership", "historical_overlap", "ordinary_matched_support"):
            for value in ([], {}, 0, False, "isolated"):
                with self.subTest(key=key, value=value):
                    receipt = copy.deepcopy(self.receipt)
                    receipt[key] = value
                    with self.assertRaises(ValueError):
                        assess.assess(receipt)

    def test_unexpected_fields_rejected(self):
        receipt = copy.deepcopy(self.receipt)
        receipt["unexpected"] = None
        with self.assertRaises(ValueError):
            assess.assess(receipt)
        del receipt["unexpected"]
        del receipt["cohort"]
        with self.assertRaises(ValueError):
            assess.assess(receipt)

    def test_claim_boundaries(self):
        ledger = assess.assess(self.receipt)
        for key in ("certifies_containment", "leaderboard", "pooling_authorized_by_evidence",
                    "sandbox_task_eligibility_or_attempts_assessed", "independently_recounted_membership"):
            self.assertIs(ledger[key], False)

    def test_reproducible_bundle_and_manifest(self):
        first = assess.bundle()
        self.assertEqual(first, assess.bundle())
        manifest = json.loads(first["manifest.json"])
        self.assertEqual(manifest["ledger_sha256"], hashlib.sha256(first["ledger.json"]).hexdigest())
        for name, digest in manifest["inputs_sha256"].items():
            self.assertEqual(hashlib.sha256((assess.HERE / name).read_bytes()).hexdigest(), digest)

    def test_retained_artifacts_match_current_inputs(self):
        for name, content in assess.bundle().items():
            self.assertEqual((assess.HERE / "verified" / name).read_bytes(), content)

    def test_fresh_output_only_and_reproduction(self):
        with tempfile.TemporaryDirectory() as parent:
            destination = Path(parent) / "new"
            with patch("sys.argv", ["assess.py", "--out", str(destination)]):
                assess.main()
                expected = assess.bundle()
                for name, content in expected.items():
                    self.assertEqual((destination / name).read_bytes(), content)
                with self.assertRaises(FileExistsError):
                    assess.main()
                for name, content in expected.items():
                    self.assertEqual((destination / name).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
