"""Differential parity for ordinary HTTP: one script, a reference and a target.

``http_matrix`` holds the authored, side-effect-free request script and the
comparison helpers; this module replays it against
``TOFU_BLACKBOX_REFERENCE_URL`` (the Python server) and the ``blackbox_url``
target (e.g. a future Rust front door) and fails on the first wire difference
of any step, naming the step and showing the first differing bytes.

Two cases keep the auth boundary honest:

* the public surface (static pages and assets, public v4 reads, retirement and
  refusal envelopes) is compared in every auth mode;
* the credential-free surface (the open-mode loopback grant: index/admin
  shells, ``/api/v4/meta`` and an unknown v4 path) is compared only when both
  servers grant it, and skipped otherwise.

Without ``TOFU_BLACKBOX_REFERENCE_URL`` both cases skip, exactly like the push
differential. Running with the target as its own reference is the harness's
self-differential sanity mode and must pass.
"""

from __future__ import annotations

import os

import pytest

from tests.blackbox import http_matrix
from tests.blackbox.test_blackbox_http import credential_free_access

pytestmark = pytest.mark.api


@pytest.fixture(scope='session')
def matrix(blackbox_url) -> http_matrix.MatrixRun:
    reference = os.environ.get(http_matrix.REFERENCE_URL_ENV, '').strip().rstrip('/')
    if not reference:
        pytest.skip(f'set {http_matrix.REFERENCE_URL_ENV} to the reference (Python) server')
    return http_matrix.play_matrix(reference, blackbox_url)


def _steps(run: http_matrix.MatrixRun, *, credential_free: bool) -> list[http_matrix.Step]:
    return [step for step in run.steps if step.credential_free is credential_free]


def test_public_http_surface_matches_the_reference(matrix):
    mismatches = http_matrix.compare_steps(matrix, _steps(matrix, credential_free=False))
    assert not mismatches, '\n'.join(mismatches)


def test_credential_free_http_surface_matches_the_reference(matrix):
    if not (credential_free_access(matrix.reference)
            and credential_free_access(matrix.target)):
        pytest.skip('the scripted surface needs the open-mode loopback grant on both servers')
    mismatches = http_matrix.compare_steps(matrix, _steps(matrix, credential_free=True))
    assert not mismatches, '\n'.join(mismatches)
