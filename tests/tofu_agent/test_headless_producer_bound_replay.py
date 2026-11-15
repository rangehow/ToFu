"""Headless children survive a provider that binds ciphertext to its minting account.

Uses the committed research self-check against a loopback Kunlun-face fake
(tests/support/producer_bound_responses.py). The fake rejects any replayed
reasoning item on a different upstream account — exactly the contract measured
on 2026-11-09 against kunlunbedrock.gpt-6.1-sol-m17 through your-llm-gateway.example.com.
The route declares responses_reasoning_replay, as the research envelope does.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.api
ROOT = Path(__file__).resolve().parents[2]
SELF_CHECK = ROOT / 'tests/support/headless_encrypted_replay_selfcheck.py'


@pytest.mark.parametrize('drop_carrier', [False, True],
                         ids=['carrier-forwarded', 'relay-drops-carrier'])
def test_children_complete_when_ciphertext_is_producer_bound(tmp_path, drop_carrier):
    command = [sys.executable, '-B', str(SELF_CHECK), '--checkout', str(ROOT),
               '--python', sys.executable, '--json']
    if drop_carrier:
        command.append('--drop-affinity-header')
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=360)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    assert 'PASS: 2/2 children completed' in completed.stdout
