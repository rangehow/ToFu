"""The configured pool follows the owner's model-routing change stamp.

Problem (2026-11-19): the resident execution host built its operator pool once
when it started and never rebuilt it after routing revisions committed in the
API process. Every project Worker turn then asked for a model whose route the
later revision had added, found zero candidate slots, and was switched to the
fallback (``DispatchNoCapableSlot: No slot available for model=...; no upstream
request was sent``), while conversation tasks — which mint a request-scoped
route group from the live authority — dispatched the same model normally.

The fix is the existing cross-process signal: the repository replaces the
owner's change stamp after every aggregate commit and every credential-secret
write in whichever process made it. A pick compares that stamp's stat identity
(one ``stat``) and rebuilds only when it moved.
"""

from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest

from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.slot import Slot

pytestmark = pytest.mark.unit


def _slot(model):
    return Slot(key_name=f'configured-{model}', api_key='sk-configured',
                model=model, logical_model=model, capabilities={'text'},
                provider_id='provider-a', latency_ema=1000.0)


@pytest.fixture
def stamp(tmp_path, monkeypatch):
    """A private owner change stamp; rewriting it mimics another process."""
    path = tmp_path / 'model_routing_secrets.1.stamp'
    path.write_text('{"owner_user_id": 1, "revision": 1}')
    monkeypatch.setattr(
        'lib.model_routing.change_notifications.model_routing_secrets_stamp_path',
        lambda *, user_id: str(path))

    def replace(revision: int) -> None:
        staged = tmp_path / '.next-stamp'
        staged.write_text('{"owner_user_id": 1, "revision": %d}' % revision)
        os.replace(staged, path)   # atomic replacement, like the writer

    return replace


@pytest.fixture
def configured(monkeypatch):
    """A dispatcher whose configured build is driven by a mutable model list."""
    state = {'models': ('alpha',), 'builds': 0}

    def build(_dispatcher):
        state['builds'] += 1
        models = state['models']
        return ([_slot(model) for model in models],
                {model: model for model in models})

    monkeypatch.setattr(LLMDispatcher, '_build_slots', build)
    monkeypatch.setattr(
        LLMDispatcher, '_load_benchmark_data', lambda self, slots: None)
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    return LLMDispatcher(), state


def test_unchanged_stamp_never_rebuilds(configured):
    dispatcher, state = configured
    for _ in range(5):
        dispatcher.initialize()
    assert state['builds'] == 1


def test_commit_in_another_process_reaches_the_next_pick(configured, stamp):
    """A model added by a later revision is pickable without a process restart."""
    dispatcher, state = configured
    dispatcher.initialize()
    assert dispatcher.pick_slot('text', prefer_model='beta',
                                strict_model=True) is None

    state['models'] = ('alpha', 'beta')
    stamp(2)

    picked = dispatcher.pick_slot('text', prefer_model='beta',
                                  strict_model=True)
    assert picked is not None and picked.model == 'beta'
    assert state['builds'] == 2

    dispatcher.pick_slot('text', prefer_model='beta', strict_model=True)
    assert state['builds'] == 2, 'an unchanged stamp must not rebuild again'


def test_secret_rotation_alone_also_moves_the_pool(configured, stamp):
    """The pool holds resolved plaintext, so a secret write invalidates it too."""
    dispatcher, state = configured
    dispatcher.initialize()

    stamp(2)   # the repository replaces the stamp after a secret put as well

    dispatcher.initialize()
    assert state['builds'] == 2


def test_concurrent_pickers_adopt_one_change_once(configured, stamp):
    dispatcher, state = configured
    dispatcher.initialize()

    state['models'] = ('alpha', 'beta')
    stamp(2)

    barrier = threading.Barrier(8)

    def pick():
        barrier.wait()
        dispatcher.initialize()

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda _: pick(), range(8)))

    assert state['builds'] == 2, 'one commit rebuilds one pool per process'


def test_change_during_the_build_stays_visible(configured, stamp, monkeypatch):
    """The build captures its stamp before reading, so a mid-build commit is not lost."""
    dispatcher, state = configured
    inside = {'first': True}

    def build(_dispatcher):
        state['builds'] += 1
        if inside['first']:
            inside['first'] = False
            stamp(2)   # a commit lands while this build reads the authority
        return [_slot('alpha')], {'alpha': 'alpha'}

    monkeypatch.setattr(LLMDispatcher, '_build_slots', build)

    dispatcher.initialize()
    dispatcher.initialize()

    assert state['builds'] == 2


def test_explicit_invalidation_still_rebuilds_once(configured):
    dispatcher, state = configured
    dispatcher.initialize()
    dispatcher.invalidate_configuration()
    dispatcher.initialize()
    dispatcher.initialize()
    assert state['builds'] == 2
