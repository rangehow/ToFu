"""Owner Hope queue policy shared with hope-mcp and the CLI guard.

The Global Router publishes this document through its normal reviewed
publication. No Tofu code writes it. An entry may scope a ban to projects;
an omitted `projects` applies globally. Unknown fields are invalid. If the
file is missing or cannot be read, the fail-safe default below applies.
"""
from __future__ import annotations

import json
from pathlib import Path

from lib.tools.hope_queue_policy import DEFAULT_ENTRIES


def schema() -> dict:
    return json.loads(Path(__file__).resolve().parents[2].joinpath(
        'contracts', 'hope_queue_policy_v1.schema.json').read_text(encoding='utf-8'))


def policy_default() -> list[dict]:
    """Return a fresh copy of the fail-safe entries; never mutate the constant."""
    return json.loads(json.dumps(list(DEFAULT_ENTRIES), ensure_ascii=False))


def validate_document(document: dict) -> None:
    """Validate the Router's JSON before publication, using the same rules as the guard."""
    from lib.tools.hope_queue_policy import _validated
    _validated(document)
