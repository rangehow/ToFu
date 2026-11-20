"""The differential HTTP matrix covers every anonymously reachable declared read.

``tests/blackbox/http_matrix.py`` is the executable parity gate for a Rust
front door: it replays one authored script against the Python reference and the
target and compares status, headers and body bytes. An authored script drifts:
a new ``public: true`` GET lands in ``contracts/api_endpoint_index_v1.json``
and the gate silently stops covering it.

This spec derives the set from the contract instead of counting steps, so the
only way to add a public read is to give the matrix a step for it (with the
projection its body needs). It deliberately does not assert the reverse
direction: the matrix also carries undeclared probes (an unknown v4 path, the
retired ``/api/v2`` prefix, an unknown page) and dynamic Vite assets, which have
no index entry by design.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from tests.blackbox import http_matrix  # noqa: E402

pytestmark = pytest.mark.unit

ENDPOINT_INDEX = json.loads(
    (REPOSITORY / 'contracts/api_endpoint_index_v1.json').read_text('utf-8'))


def declared_public_static_reads() -> set[str]:
    """The same selection ``test_blackbox_http.public_static_get_paths`` uses."""
    return {entry['path'] for entry in ENDPOINT_INDEX['endpoints']
            if entry['method'] == 'GET' and entry['transport'] == 'http'
            and entry['public'] is True and '<' not in entry['path']}


def authored_paths() -> dict[str, http_matrix.Step]:
    """The matrix's fixed steps, by path (asset steps are named from a manifest)."""
    steps = [*http_matrix.PUBLIC_STEPS, *http_matrix.CREDENTIAL_FREE_STEPS]
    return {step.path: step for step in steps}


def test_every_declared_public_read_has_a_matrix_step():
    missing = sorted(declared_public_static_reads() - set(authored_paths()))
    assert not missing, (
        'add a step for each newly declared public read to '
        f'tests/blackbox/http_matrix.py: {missing}')


def test_step_names_are_unique():
    """Replies are keyed by step name; a duplicate would compare one step twice."""
    steps = [*http_matrix.PUBLIC_STEPS, *http_matrix.CREDENTIAL_FREE_STEPS]
    names = [step.name for step in steps]
    assert len(names) == len(set(names)), sorted(
        name for name in set(names) if names.count(name) > 1)


def test_the_matrix_keeps_a_refusal_and_an_undeclared_probe():
    """Parity of the error envelope is as load-bearing as parity of a success."""
    paths = authored_paths()
    assert '/api/v1/conversations' in paths, 'the retired-major refusal must stay covered'
    assert '/api/v4/blackbox-harness/no-such-resource' in paths, \
        'an undeclared v4 path must stay covered'


@pytest.mark.parametrize('path', ['/', '/admin', '/api/v4/meta'])
def test_grant_dependent_reads_are_marked_as_such(path: str):
    """The group that needs the open-mode loopback grant must stay skippable."""
    assert authored_paths()[path].credential_free is True
