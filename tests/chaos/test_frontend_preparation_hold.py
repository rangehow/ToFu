"""Missing-graph diagnostics expose status, never fixture or installation paths."""
import json

import pytest

from tests.chaos import instance as owner

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('opaque', ['opaque-root-alpha-7f31', 'opaque-root-beta-9d52'])
def test_missing_graph_diagnostic_contains_no_root_or_opaque_segment(tmp_path, monkeypatch, opaque):
    checkout = tmp_path / opaque / 'checkout'
    checkout.mkdir(parents=True)
    instance = owner.ChaosInstance(tmp_path / opaque / 'runtime-installation', faultfs=False)
    monkeypatch.setattr(owner, 'CHECKOUT', checkout)
    def no_child(*args, **kwargs):
        pytest.fail('missing-manifest diagnosis must not launch a child')
    monkeypatch.setattr(owner.subprocess, 'Popen', no_child)
    monkeypatch.setattr(owner.subprocess, 'run', no_child)
    with pytest.raises(RuntimeError) as error:
        instance.start_api()
    message = str(error.value)
    assert opaque not in message
    assert str(tmp_path) not in message
    assert str(checkout) not in message
    assert str(instance.source) not in message
    assert 'runtime-installation' not in message
    assert '/' not in message and '\\' not in message
    assert json.loads(message) == {
        'code': 'frontend_preparation_evidence_unavailable',
        'message': 'the chaos API host needs the built frontend graph',
        'fixture': 'chaos', 'stage': 'api-start',
        'manifest': 'manifest.json', 'manifestStatus': 'missing',
        'lockfile': 'package-lock.json',
        'installedByteProvenance': 'unverified',
        'authoritativeEvidence': 'unavailable', 'buildAttempted': False,
        'qualification': 'missing graph input only; not full startup diagnosis',
    }
    assert instance.api is None and instance.api_generation == 0
    assert instance._storage is None and instance._execution is None
    assert not instance.root.exists()

