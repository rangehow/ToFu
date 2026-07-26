"""Wire-parity guards for pt_00459503 slice 5 (event-forwarding extraction).

The event-forwarding cluster — ``_VU_FORWARD_TYPES``, ``_VUEventForwarder``,
``_emit_vu_setup_phase`` — moves out of ``lib.tasks_pkg.autopilot`` and into
the dedicated leaf ``lib.tasks_pkg.autopilot_event_forwarding``.  The four
guards here lock the contract that keeps the extraction safe:

  1. The leaf module exists AND owns the definitions (not just re-exports).
  2. The facade at ``lib.tasks_pkg.autopilot`` exposes the same three
     public names, and each one IS IDENTITY-EQUAL to the leaf's — this is
     the load-bearing property for
     ``monkeypatch.setattr(ap, '_emit_vu_setup_phase', ...)`` which
     ``tests/test_autopilot_warmup_setup_phase.py`` uses.  A plain
     "``ap._emit_vu_setup_phase = X``" would only rebind the facade if
     re-export preserves identity; a copy would silently break that test.
  3. The class body no longer lives inline in ``autopilot.py`` — it's
     been removed, only re-imported.  (Source-scan guard: catches an
     accidental "we forgot to delete the original" duplication.)
  4. The forwarder actually forwards a whitelisted event onto the parent
     task through ``manager.append_event`` (functional smoke — the class's
     one job).  Uses monkeypatch to intercept the parent append and asserts
     the wrapped envelope carries ``vuMsgId`` + ``inner``.

Guarded against NEUTER: delete the leaf / reinject the class inline in
the facade / drop the re-export.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest


AUTOPILOT_PATH = Path(__file__).resolve().parents[1] / 'lib' / 'tasks_pkg' / 'autopilot.py'
LEAF_PATH = Path(__file__).resolve().parents[1] / 'lib' / 'tasks_pkg' / 'autopilot_event_forwarding.py'


@pytest.fixture
def reload_modules():
    """Force a fresh import so any monkeypatched sys.modules state is dropped."""
    for name in list(sys.modules):
        if name.startswith('lib.tasks_pkg.autopilot'):
            del sys.modules[name]
    yield
    for name in list(sys.modules):
        if name.startswith('lib.tasks_pkg.autopilot'):
            del sys.modules[name]


def test_event_forwarding_leaf_module_exists_and_defines_all_three_symbols(reload_modules):
    """Leaf must exist and OWN the three symbols (not merely re-export)."""
    assert LEAF_PATH.exists(), (
        f'{LEAF_PATH} missing — event-forwarding leaf was not extracted.')
    leaf = importlib.import_module('lib.tasks_pkg.autopilot_event_forwarding')
    assert hasattr(leaf, '_VU_FORWARD_TYPES'), (
        'leaf missing _VU_FORWARD_TYPES')
    assert hasattr(leaf, '_VUEventForwarder'), (
        'leaf missing _VUEventForwarder')
    assert hasattr(leaf, '_emit_vu_setup_phase'), (
        'leaf missing _emit_vu_setup_phase')

    # Source-scan: ensure the leaf actually declares them (defends against
    # a fake leaf that just re-exports from autopilot — which would keep
    # the cycle and defeat the extraction).
    src = LEAF_PATH.read_text(encoding='utf-8')
    tree = ast.parse(src)
    top_level = {node.name: node for node in tree.body
                 if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    top_level_assigns = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    top_level_assigns.add(tgt.id)
    assert '_VUEventForwarder' in top_level, (
        'leaf must declare class _VUEventForwarder at top level '
        '(not just import it — a re-export leaf keeps the extraction cycle).')
    assert '_emit_vu_setup_phase' in top_level, (
        'leaf must declare def _emit_vu_setup_phase at top level.')
    assert '_VU_FORWARD_TYPES' in top_level_assigns, (
        'leaf must declare _VU_FORWARD_TYPES as a top-level assignment.')


def test_autopilot_facade_reexports_by_identity(reload_modules):
    """The facade attribute IS the leaf attribute — required for monkeypatch steering."""
    leaf = importlib.import_module('lib.tasks_pkg.autopilot_event_forwarding')
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    for name in ('_VU_FORWARD_TYPES', '_VUEventForwarder', '_emit_vu_setup_phase'):
        assert hasattr(ap, name), (
            f'autopilot facade missing re-exported name {name!r} — '
            f'existing call sites (autopilot.py L341/L385/L452) and tests '
            f'(test_autopilot_warmup_setup_phase.py:114 monkeypatches on ap) '
            f'would break.')
        assert getattr(ap, name) is getattr(leaf, name), (
            f'{name}: facade must be IDENTITY-EQUAL to leaf — a copy breaks '
            f'monkeypatch.setattr(ap, {name!r}, ...) which relies on rebinding '
            f'the facade attribute.')


def test_autopilot_py_no_longer_declares_event_forwarding_inline():
    """Source-scan: original inline declarations must be gone (only re-exports remain)."""
    src = AUTOPILOT_PATH.read_text(encoding='utf-8')
    tree = ast.parse(src)
    # Collect only top-level *inline* declarations (not import-froms).
    inline_classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    inline_funcs = {n.name for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    inline_assigns = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    inline_assigns.add(tgt.id)

    assert '_VUEventForwarder' not in inline_classes, (
        'autopilot.py must NOT re-declare class _VUEventForwarder inline — '
        'it lives in autopilot_event_forwarding.py now.')
    assert '_emit_vu_setup_phase' not in inline_funcs, (
        'autopilot.py must NOT re-declare def _emit_vu_setup_phase inline — '
        'it lives in autopilot_event_forwarding.py now.')
    assert '_VU_FORWARD_TYPES' not in inline_assigns, (
        'autopilot.py must NOT re-declare _VU_FORWARD_TYPES inline — '
        'it lives in autopilot_event_forwarding.py now.')


def test_vu_event_forwarder_forwards_whitelisted_event_to_parent(monkeypatch, reload_modules):
    """Functional smoke: append(ev) with a whitelisted type must forward to parent."""
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    forwarded = []

    def fake_append_event(parent_task, ev):
        forwarded.append((parent_task, ev))

    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', fake_append_event)

    parent = {'id': 'parent-t1', 'events': []}
    fwd = ap._VUEventForwarder(parent, 'vu-msg-abc')
    fwd.append({'type': 'delta', 'text': 'hi'})           # whitelisted → forwarded
    fwd.append({'type': 'unknown_type', 'x': 1})           # NOT whitelisted → only local

    # Local list retains everything
    assert len(fwd) == 2, 'sub-task local event list must retain every append'
    # Parent stream received only the whitelisted event
    assert len(forwarded) == 1, (
        f'expected exactly 1 whitelisted event forwarded to parent, got {len(forwarded)}')
    parent_task, wrapped = forwarded[0]
    assert parent_task is parent, 'forwarded event must land on the parent task'
    assert wrapped.get('type') == 'autopilot_vu_event', (
        f'forwarded event must be wrapped as autopilot_vu_event, got type={wrapped.get("type")!r}')
    assert wrapped.get('vuMsgId') == 'vu-msg-abc', (
        f'forwarded event must carry vuMsgId, got {wrapped.get("vuMsgId")!r}')
    assert wrapped.get('inner', {}).get('type') == 'delta', (
        f'wrapped inner must carry the original event, got inner={wrapped.get("inner")!r}')
