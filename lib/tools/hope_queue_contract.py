"""Owner Hope queue policy shared with hope-mcp and the CLI guard.

The Global Router publishes this document through its normal reviewed
publication. Router-home creation seeds it once with the contract's
fail-safe default (``lib.router.home``); no code overwrites it. An entry may scope a ban to projects;
an omitted `projects` applies globally. Unknown fields are invalid. If the
file is missing or cannot be read, the fail-safe default
(``lib.tools.hope_queue_policy.DEFAULT_ENTRIES``) applies.
"""
from __future__ import annotations

import json
from pathlib import Path


def schema() -> dict:
    return json.loads(Path(__file__).resolve().parents[2].joinpath(
        'contracts', 'hope_queue_policy_v1.schema.json').read_text(encoding='utf-8'))


def validate_document(document: dict) -> None:
    """Validate the Router's JSON before publication, using the same rules as the guard."""
    from lib.tools.hope_queue_policy import _validated
    _validated(document)
