"""Adversarial admission checks for the independent serialization oracle."""
import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("domain_roots_measure", Path(__file__).parents[1] / "measure.py")
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


def fixture():
    p = {"domains": 2, "slots": 2, "evidence_bytes": 0, "workers": 1, "operations": 2, "cross_percent": 0, "hot_percent": 100}
    requests = [measure.request(i, p) for i in range(2)]
    records = [{"phase": "configuration", "parameters": p, "backend": "native",
                "configuration": {"wal_durability": "Strict", "owner": "one existing Engine"},
                "input_sha256": hashlib.sha256(measure.encoded(requests)).hexdigest()}]
    for i, req in enumerate(requests):
        receipt = {"sequence": i + 2, "read_sequence": i + 1, "scope": req["scope"], "command_id": req["command_id"],
                   "request_hash": list(hashlib.sha256(measure.encoded(req)).digest()),
                   "roots": [{"key": {"scope": req["scope"], "domain": 0}, "generation": i + 1, "slots": 2, "root": [0] * 32}],
                   "values": [{"domain": 0, "slot": 0, "value": 999999 - i}, {"domain": 0, "slot": 1, "value": 1000001 + i}]}
        records.append({"phase": "sample", "sample": {"index": i, "attempts": 1, "conflicts": 0, "prepare_ns": 30, "commit_ns": 60,
                       "total_ns": 100, "discarded_prepared_bytes": 0, "outcome": {"receipt": receipt, "replayed": False}}})
    records.append({"phase": "complete", "operations": 2, "receipt_count": 2, "reopen_verified": True, "wall_ns": 1000, "cpu_us": 1,
                    "conflicts": 0, "discarded_prepared_bytes": 0, "max_parallel_preparations": 1,
                    "state_sha256": hashlib.sha256(measure.encoded([[999998, 1000002], [1000000, 1000000]])).hexdigest()})
    return records, p


def test_valid_serialization_history_is_admitted():
    records, p = fixture()
    assert measure.validate(records, "native", p, "3.53.4")["operations"] == 2


@pytest.mark.parametrize("attack", ["generation", "stale_read", "missing_receipt", "retry_clock", "retry_bytes", "weakened_durability"])
def test_corrupted_work_is_refused(attack):
    records, p = fixture()
    records = copy.deepcopy(records)
    sample = records[2]["sample"]
    if attack == "generation":
        sample["outcome"]["receipt"]["roots"][0]["generation"] = 91
    elif attack == "stale_read":
        sample["outcome"]["receipt"]["read_sequence"] = 1
    elif attack == "missing_receipt":
        records.pop(1)
    elif attack == "retry_clock":
        sample["total_ns"] = 10
    elif attack == "retry_bytes":
        sample["attempts"] = 2
        sample["conflicts"] = 1
        records[-1]["conflicts"] = 1
    else:
        records[0]["configuration"]["wal_durability"] = "Normal"
    with pytest.raises(ValueError):
        measure.validate(records, "native", p, "3.53.4")
