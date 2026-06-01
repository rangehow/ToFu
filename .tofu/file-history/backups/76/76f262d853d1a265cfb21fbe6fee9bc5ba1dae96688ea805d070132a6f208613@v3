"""debug/test_optimizer.py — Smoke test for the Daily Optimizer.

Validates the full round-trip:
  1. run_once(dry_run=False) with a monkey-patched LLM → block_search_domain
     action is applied; server_config.json::search.skip_domains contains
     the target domain; lib.SKIP_DOMAINS reflects the change live.
  2. A synthetic row + a second run_once() → outcome_metric is recorded.
  3. An artificially-expired action is auto-reverted; the domain is
     removed from skip_domains.

Exits 0 on success, non-zero on any failure.  Prints 'ALL TESTS PASSED'.

Run:
    python debug/test_optimizer.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

# ── Arrange: isolated data/config + sqlite DB ──
os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')

_TMP = tempfile.mkdtemp(prefix='opt_test_')
os.environ['TOFU_DB_PATH'] = os.path.join(_TMP, 'tofu.db')

# Point config_dir at a fresh per-test directory so we never touch the real
# server_config.json.  We do this by monkey-patching lib.config_dir BEFORE
# anything else imports it.
import lib.config_dir as _cfg_dir  # noqa: E402
_cfg_dir.CONFIG_DIR = os.path.join(_TMP, 'config')
os.makedirs(_cfg_dir.CONFIG_DIR, exist_ok=True)


def _cfg_path(*parts):
    return os.path.join(_cfg_dir.CONFIG_DIR, *parts)


_cfg_dir.config_path = _cfg_path

# Also patch the copy already imported by lib.__init__ (it took a snapshot).
import lib  # noqa: E402
lib._SERVER_CONFIG_PATH = _cfg_path('server_config.json')

# Patch the block_search_domain module's cached _CONFIG_FILE constant
import lib.optimizer.actions.block_search_domain as _bsd  # noqa: E402
_bsd._CONFIG_FILE = _cfg_path('server_config.json')

# Re-patch analyzer's daily reports path
import lib.optimizer.analyzer as _analyzer  # noqa: E402
_analyzer._config_path = _cfg_path


def _init_db():
    from lib.database import init_db
    init_db()


def _ensure_baseline_config():
    """Write a minimal server_config.json with an empty skip_domains."""
    path = _cfg_path('server_config.json')
    with open(path, 'w') as f:
        json.dump({'search': {'skip_domains': []}}, f)
    lib.reload_config()


# ── Fake LLM ──

_TARGET_DOMAIN = 'loginwall-example.test'


def _fake_llm(messages):
    proposal = {
        'proposals': [{
            'title': f'Block {_TARGET_DOMAIN} from web search',
            'rationale': ('5/6 search rounds surfaced this domain and '
                          'content_filter dropped them as IRRELEVANT login walls.'),
            'action_type': 'block_search_domain',
            'action_args': {'domain': _TARGET_DOMAIN, 'ttl_days': 7},
            'severity': 'low',
            'confidence': 0.9,
            'evidence_ids': ['synthetic_1'],
            'ttl_days': 7,
        }],
    }
    return (json.dumps(proposal), {'input_tokens': 100, 'output_tokens': 80})


# ── Assertion helpers ──

def _assert(cond, msg):
    if not cond:
        print(f'❌ FAIL: {msg}', file=sys.stderr)
        raise AssertionError(msg)


def _cleanup():
    try:
        shutil.rmtree(_TMP, ignore_errors=True)
    except Exception as e:
        print(f'(cleanup warning) {e}', file=sys.stderr)


# ── Tests ──

def test_apply_and_storage():
    """run_once with a fake LLM should apply and persist."""
    from lib.optimizer import run_once
    from lib.optimizer import storage
    summary = run_once(dry_run=False, llm_override=_fake_llm)

    _assert(len(summary['applied']) == 1,
            f'expected 1 applied, got {summary["applied"]}')
    _assert(summary['applied'][0]['action_type'] == 'block_search_domain',
            f'unexpected action_type: {summary["applied"][0]}')
    _assert(_TARGET_DOMAIN in lib.SKIP_DOMAINS,
            f'lib.SKIP_DOMAINS should contain {_TARGET_DOMAIN}; got '
            f'{sorted(lib.SKIP_DOMAINS)[:20]}')

    # server_config.json on disk
    with open(_cfg_path('server_config.json')) as f:
        cfg = json.load(f)
    _assert(_TARGET_DOMAIN in cfg.get('search', {}).get('skip_domains', []),
            'domain missing from server_config.json')

    # DB row with status='applied'
    rows = storage.list_proposals(limit=10)
    _assert(any(r['status'] == 'applied'
                and r['action_type'] == 'block_search_domain' for r in rows),
            f'no applied proposal row found: {rows}')

    proposal_id = [r['id'] for r in rows
                   if r['status'] == 'applied'
                   and r['action_type'] == 'block_search_domain'][0]
    print(f'  ✓ applied proposal_id={proposal_id}')
    return proposal_id


def test_learning_loop(proposal_id: str):
    """A second run_once should record outcome_metric for the prior action."""
    from lib.optimizer import run_once
    from lib.optimizer import storage
    summary2 = run_once(dry_run=True, llm_override=_fake_llm)
    prior = summary2.get('prior_actions') or []
    _assert(any(p.get('proposal_id') == proposal_id for p in prior),
            f'prior_actions missing proposal_id {proposal_id}: {prior}')

    log_row = storage.get_action_log_for_proposal(proposal_id)
    _assert(log_row is not None, 'action_log row missing')
    outcome = log_row.get('outcome_metric') or ''
    _assert(outcome and outcome not in ('{}', 'null'),
            f'outcome_metric not recorded: {log_row!r}')
    parsed = json.loads(outcome)
    _assert(parsed.get('domain') == _TARGET_DOMAIN,
            f'outcome_metric domain mismatch: {parsed}')
    print(f'  ✓ outcome_metric recorded: {parsed}')


def test_auto_revert_on_expiry(proposal_id: str):
    """Force expires_at into the past and confirm next run reverts."""
    from lib.database import DOMAIN_SYSTEM, db_execute_with_retry, get_thread_db
    from lib.optimizer import run_once

    past = (datetime.now() - timedelta(days=1)).isoformat()
    db = get_thread_db(DOMAIN_SYSTEM)
    db_execute_with_retry(
        db,
        'UPDATE optimizer_action_log SET expires_at=? WHERE proposal_id=?',
        [past, proposal_id])

    # Sanity: domain still in skip_domains before the revert
    _assert(_TARGET_DOMAIN in lib.SKIP_DOMAINS,
            'pre-revert: domain should still be present')

    summary3 = run_once(dry_run=False, llm_override=lambda _m: ('{"proposals":[]}', {}))
    reverts = summary3.get('reverts') or []
    _assert(any(r.get('args', {}).get('domain') == _TARGET_DOMAIN
                or (r.get('log_id') and r.get('status') == 'expired')
                for r in reverts),
            f'no expected revert found: {reverts}')

    # lib.SKIP_DOMAINS must no longer contain the target
    _assert(_TARGET_DOMAIN not in lib.SKIP_DOMAINS,
            f'post-revert: {_TARGET_DOMAIN} still in SKIP_DOMAINS='
            f'{sorted(lib.SKIP_DOMAINS)[:20]}')

    # Proposal status should be 'expired'
    from lib.optimizer import storage
    prop = storage.get_proposal(proposal_id)
    _assert(prop.get('status') == 'expired',
            f'expected status=expired, got {prop.get("status")}')
    print(f'  ✓ auto-revert moved proposal to status=expired')


def main():
    print('▶ Daily Optimizer smoke test')
    print(f'  tmp dir: {_TMP}')

    try:
        _init_db()
        _ensure_baseline_config()

        print('▶ test_apply_and_storage')
        pid = test_apply_and_storage()

        print('▶ test_learning_loop')
        test_learning_loop(pid)

        print('▶ test_auto_revert_on_expiry')
        test_auto_revert_on_expiry(pid)

        print('ALL TESTS PASSED')
        return 0
    except AssertionError as e:
        print(f'TEST FAILED: {e}', file=sys.stderr)
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'TEST ERROR: {e}', file=sys.stderr)
        return 2
    finally:
        _cleanup()


if __name__ == '__main__':
    sys.exit(main())
