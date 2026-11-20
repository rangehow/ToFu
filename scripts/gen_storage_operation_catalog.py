#!/usr/bin/env python3
"""Check the sidecar operation registry against the authored storage catalog.

Authority: ``contracts/storage_operations_v1.json`` is authored JSON (Rust
rewrite Phase 0, docs/RUST_REWRITE_EVIDENCE.md). Change it first, then the
sidecar registry (``lib/storage_authority_sql/operation_domains``), which stays
the executable Python authority until the SQL mode moves to Rust. This script
writes nothing: it checks that ``operationCount`` and ``operationsSha256``
describe the catalog's operations and that the sidecar registry implements
exactly the catalog (an operation or attribute on one side only fails with the
difference). The clients derived from the catalog

    packages/tofu-db/src/generated_storage_operations.rs
    scripts/generated_storage_operations.py, tofu_db/generated_contract.py

are rendered by ``tofu-xtask contracts`` (packages/tofu-xtask; ``make
contracts`` / ``make contracts-check``). Their header still names this script:
those bytes are part of the storage contract identity the engine shares, so the
text is frozen (packages/tofu-xtask/src/storage_operations.rs).

The catalog records no SQL or handler implementation details. Its
``generatedBy`` key is a frozen legacy field name: the file is an external
input of the Tofu-DB engine fingerprint, so renaming the key would demand a
new engine build for no behaviour change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from lib.storage_authority_sql.operation_domains import REGISTRY_VERSION  # noqa: E402
from lib.storage_authority_sql.operation_registry import _DOMAIN_MODULES  # noqa: E402
from lib.storage_authority_sql.schema import SCHEMA_VERSION  # noqa: E402


CATALOG = REPOSITORY_ROOT / "contracts" / "storage_operations_v1.json"


def _document() -> dict:
    from importlib import import_module

    from lib.task_replay.task_event_contract import (
        EVENT_READ_GUARD_CONTRACT, EVENT_READ_GUARD_SCOPED_CONTRACT,
        EVENT_READ_GUARD_MAX_TYPES,
    )

    operations: list[dict] = []
    for domain in _DOMAIN_MODULES:
        module = import_module(
            f"lib.storage_authority_sql.operation_domains.{domain}"
        )
        for name, spec in module.OPERATIONS.items():
            operations.append({
                "name": name,
                "domain": domain,
                "kind": spec.kind,
                "receiptRequired": bool(spec.receipt_required),
                **({"receiptWhenTrue": spec.receipt_when_true}
                   if spec.receipt_when_true is not None else {}),
                "transactionTimeoutSeconds": spec.transaction_timeout_s,
                # Existing handlers enforce owner scope.  Making that fact
                # explicit prevents a new backend from mistaking omission in
                # this legacy catalog for permission to run unscoped.
                "ownerScope": "handler-enforced",
            })
    operations.sort(key=lambda item: item["name"])
    canonical_operations = json.dumps(
        operations, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "format": "tofu.storage-operation-catalog.v1",
        "generatedBy": "scripts/gen_storage_operation_catalog.py",
        "sourceProtocol": "storage.v1",
        "sourceSchemaVersion": SCHEMA_VERSION,
        "sourceRegistryVersion": REGISTRY_VERSION,
        "operationCount": len(operations),
        "operationsSha256": hashlib.sha256(canonical_operations).hexdigest(),
        "operations": operations,
        "eventReadGuardContract": EVENT_READ_GUARD_CONTRACT,
        "eventReadGuardScopedContract": EVENT_READ_GUARD_SCOPED_CONTRACT,
        "eventReadGuardMaxTypes": EVENT_READ_GUARD_MAX_TYPES,
    }


def _catalog_problems(catalog: dict, registry: dict) -> list[str]:
    problems: list[str] = []
    canonical = json.dumps(
        catalog["operations"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != catalog["operationsSha256"]:
        problems.append("operationsSha256 does not describe the catalog operations")
    if len(catalog["operations"]) != catalog["operationCount"]:
        problems.append("operationCount does not match the catalog operations")
    names = [operation["name"] for operation in catalog["operations"]]
    if names != sorted(set(names)):
        problems.append("catalog operations are not unique and sorted by name")
    in_catalog = {operation["name"]: operation for operation in catalog["operations"]}
    in_code = {operation["name"]: operation for operation in registry["operations"]}
    for name in sorted(in_code.keys() - in_catalog.keys()):
        problems.append(f"{name}: registered by the sidecar but missing from the catalog")
    for name in sorted(in_catalog.keys() - in_code.keys()):
        problems.append(f"{name}: in the catalog but not registered by the sidecar")
    for name in sorted(in_catalog.keys() & in_code.keys()):
        if in_catalog[name] != in_code[name]:
            problems.append(f"{name}: catalog {in_catalog[name]} != sidecar {in_code[name]}")
    for key, value in registry.items():
        if key not in ("operations", "operationsSha256") and catalog.get(key) != value:
            problems.append(f"{key}: catalog {catalog.get(key)!r} != sidecar {value!r}")
    if not problems and catalog != registry:
        problems.append("catalog key order or extra keys differ from the sidecar projection")
    return problems


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n", 1)[0]).parse_args(argv)
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    problems = _catalog_problems(document, _document())
    if problems:
        print("contracts/storage_operations_v1.json and the sidecar registry disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
