"""Out-of-band repair dispose executor (SELF-REPAIR O5).

Responsibility: the deterministic launcher that executes what the repair
session proposes. The model never lands, restarts or reverts anything itself.

A code fix is published only in this order: its new spec fails on the base
and passes on the fix, focused tests pass, an independent review approves,
publication and the gated restart happen, and the incident's own grounded
signal clears. Any failure stops the sequence; a failed or unhelpful restart
reverts the landed change and restarts once more.

Owner-only actions are refused here, including a fix that touches the
self-repair boundary (watcher, relay, sandbox, prompt, authority or the
notification policy).

Entry points: ``execute(proposal, ports)``. Dependencies: the standard
library; the caller supplies every effect as a port.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Files a fix may never change automatically (the repair agent's own scope).
FORBIDDEN_PREFIXES = (
    'serverctl_pkg/oob_alert.py', 'serverctl_pkg/oob_repair.py', 'serverctl_pkg/oob_relay.py',
    'serverctl_pkg/oob_runtime.py', 'serverctl_pkg/oob_dispose.py',
    'lib/router/', 'docs/ROUTER.md', 'lib/session_control/', 'lib/turn_initiation',
)


def _reject(session_dir: Path, reason: str, **extra) -> dict[str, Any]:
    outcome = {'status': 'rejected', 'reason': reason, **extra}
    _write_outcome(session_dir, outcome)
    return outcome


def _write_outcome(session_dir: Path | None, outcome: dict[str, Any]) -> None:
    if session_dir is None:
        return  # no session directory: the caller keeps the returned record only
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / 'dispose-outcome.json'
    path.write_text(json.dumps(outcome, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')


def execute(proposal: dict[str, Any], *, ports, session_dir: Path | None = None) -> dict[str, Any]:
    """Execute one proposal through the deterministic gate sequence."""
    kind = proposal.get('kind')
    workspace = Path(proposal.get('workspace') or '.')
    session = session_dir  # only an explicit session directory writes a file
    if kind == 'storage_recover':
        status, detail = ports.recover()
        outcome = {'status': 'executed' if status == 0 else 'failed', 'detail': detail}
        _write_outcome(session, outcome)
        return outcome
    if kind != 'code_fix':
        return _reject(session, f'unknown action kind: {kind!r}')
    touches = proposal.get('touches') or []
    for path in touches:
        if any(str(path).startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            return _reject(session, f'self-repair boundary: {path} may not be fixed automatically')

    status, detail = ports.run_spec(workspace, base=True)
    if status == 0:
        return _reject(session, 'the new spec does not fail on the base')
    status, detail = ports.run_spec(workspace, base=False)
    if status != 0:
        return _reject(session, f'the new spec still fails on the fix: {detail}')
    status, detail = ports.run_tests(workspace)
    if status != 0:
        return _reject(session, f'focused tests failed: {detail}')
    verdict = ports.review(workspace)
    if str(verdict).lower() not in ('approve', 'approved'):
        return _reject(session, f'independent review: {verdict}')
    status, detail = ports.land(workspace)
    if status != 0:
        return _reject(session, f'publication failed: {detail}')
    status, detail = ports.restart()
    if status != 0:
        revert_status, revert_detail = ports.revert(workspace)
        outcome = {'status': 'rolled_back', 'reason': f'restart failed: {detail}',
                   'revert': revert_detail}
        _write_outcome(session, outcome)
        return outcome
    if not ports.symptom_clear():
        revert_status, revert_detail = ports.revert(workspace)
        restart_status, restart_detail = ports.restart()
        outcome = {'status': 'rolled_back', 'reason': 'the incident signal did not clear',
                   'revert': revert_detail, 'restart': restart_detail}
        _write_outcome(session, outcome)
        return outcome
    outcome = {'status': 'published', 'detail': 'fix landed, restarted, incident cleared'}
    _write_outcome(session, outcome)
    return outcome


__all__ = ['FORBIDDEN_PREFIXES', 'execute']
