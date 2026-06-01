"""routes/optimizer.py — REST endpoints for the Daily Optimizer.

Exposed endpoints:
    GET  /api/optimizer/proposals?status=&limit=
    GET  /api/optimizer/proposals/<id>
    POST /api/optimizer/proposals/<id>/approve
    POST /api/optimizer/proposals/<id>/reject
    POST /api/optimizer/proposals/<id>/revert
    POST /api/optimizer/run-now

Everything is read/write-heavy but small (single-digit QPS), so the
endpoints call the module directly without a background thread.  The
UI can poll at will.
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

import lib as _lib
from lib.log import audit_log, get_logger
from lib.optimizer import run_once as _run_once
from lib.optimizer import storage as _storage
from lib.optimizer.actions import ACTION_REGISTRY

logger = get_logger(__name__)

optimizer_bp = Blueprint('optimizer', __name__)


def _disabled_guard():
    """Returns a (response, status) tuple if the feature is disabled, else None.

    Read-only endpoints (GET list / GET by id) are still served so the panel
    can show historical proposals even after the user disables the feature.
    Only mutations and run-now are blocked.
    """
    if getattr(_lib, 'OPTIMIZER_ENABLED', True):
        return None
    return jsonify({
        'ok': False,
        'error': 'Daily Optimizer is disabled. Enable it in Settings → '
                 '功能模块 → 每日优化器.',
    }), 403


# ══════════════════════════════════════════════════════════
#  Read
# ══════════════════════════════════════════════════════════

@optimizer_bp.route('/api/optimizer/proposals', methods=['GET'])
def list_proposals():
    status = (request.args.get('status') or '').strip() or None
    try:
        limit = max(1, min(500, int(request.args.get('limit') or 50)))
    except (TypeError, ValueError) as e:
        logger.debug('[Optimizer.route] bad limit arg, defaulting to 50: %s', e)
        limit = 50
    try:
        rows = _storage.list_proposals(status=status, limit=limit)
    except Exception as e:
        logger.error('[Optimizer.route] list_proposals failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': 'list_proposals failed'}), 500

    # Decode JSON-ish columns for the client
    for r in rows:
        for col in ('action_args', 'evidence'):
            raw = r.get(col)
            if isinstance(raw, str) and raw:
                try:
                    r[col] = json.loads(raw)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug('[Optimizer.route] decode %s failed: %s', col, e)
    return jsonify({'ok': True, 'proposals': rows})


@optimizer_bp.route('/api/optimizer/proposals/<proposal_id>', methods=['GET'])
def get_proposal(proposal_id):
    try:
        prop = _storage.get_proposal(proposal_id)
    except Exception as e:
        logger.error('[Optimizer.route] get_proposal failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': 'lookup failed'}), 500
    if not prop:
        return jsonify({'ok': False, 'error': 'Proposal not found'}), 404

    for col in ('action_args', 'evidence'):
        raw = prop.get(col)
        if isinstance(raw, str) and raw:
            try:
                prop[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Optimizer.route] decode %s failed: %s', col, e)

    try:
        action_log = _storage.get_action_log_for_proposal(proposal_id)
    except Exception as e:
        logger.warning('[Optimizer.route] action_log lookup failed: %s', e)
        action_log = None
    if action_log:
        for col in ('pre_metric', 'outcome_metric'):
            raw = action_log.get(col)
            if isinstance(raw, str) and raw:
                try:
                    action_log[col] = json.loads(raw)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug('[Optimizer.route] decode %s failed: %s', col, e)

    return jsonify({'ok': True, 'proposal': prop, 'action_log': action_log})


# ══════════════════════════════════════════════════════════
#  Mutations — approve / reject / revert
# ══════════════════════════════════════════════════════════

@optimizer_bp.route('/api/optimizer/proposals/<proposal_id>/approve', methods=['POST'])
def approve_proposal(proposal_id):
    blocked = _disabled_guard()
    if blocked is not None:
        return blocked
    """Apply a pending_review proposal on demand.

    Falls back to 'pending_review' with a reason if the action_type is not
    auto-apply — approvals for non-whitelisted action types must be
    wired in a subsequent, separately-approved change (see CLAUDE.md §10).
    """
    prop = _storage.get_proposal(proposal_id)
    if not prop:
        return jsonify({'ok': False, 'error': 'Proposal not found'}), 404
    if prop.get('status') == 'applied':
        return jsonify({'ok': True, 'status': 'applied',
                        'detail': 'already applied'})

    action_type = prop['action_type']
    entry = ACTION_REGISTRY.get(action_type)
    if not entry or not entry.get('auto_apply') or not callable(entry.get('apply')):
        logger.warning('[Optimizer.route] approve rejected — %s is not in '
                       'auto-apply whitelist', action_type)
        _storage.update_proposal_status(
            proposal_id, 'rejected',
            reason='manual approve blocked: action_type not in auto-apply whitelist')
        return jsonify({
            'ok': False,
            'error': f'action_type {action_type} is not in the auto-apply whitelist',
        }), 400

    try:
        args = json.loads(prop.get('action_args') or '{}')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Optimizer.route] bad action_args JSON for %s: %s',
                       proposal_id, e)
        args = {}
    ttl_days = int(args.get('ttl_days') or 7)

    try:
        detail = entry['apply'](args) or {}
    except Exception as e:
        logger.error('[Optimizer.route] approve apply failed: %s', e, exc_info=True)
        _storage.update_proposal_status(
            proposal_id, 'rejected',
            reason=f'manual approve failed: {type(e).__name__}: {str(e)[:120]}')
        audit_log('optimizer_action_failed',
                  proposal_id=proposal_id, action_type=action_type,
                  error=str(e)[:200], path='manual_approve')
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500

    _storage.update_proposal_status(proposal_id, 'applied',
                                    reason='manual approve')
    _storage.record_applied(proposal_id=proposal_id, ttl_days=ttl_days,
                            pre_metric={})
    audit_log('optimizer_action_manual_approve',
              proposal_id=proposal_id, action_type=action_type,
              ttl_days=ttl_days, detail=detail)
    return jsonify({'ok': True, 'status': 'applied', 'detail': detail})


@optimizer_bp.route('/api/optimizer/proposals/<proposal_id>/reject', methods=['POST'])
def reject_proposal(proposal_id):
    blocked = _disabled_guard()
    if blocked is not None:
        return blocked
    prop = _storage.get_proposal(proposal_id)
    if not prop:
        return jsonify({'ok': False, 'error': 'Proposal not found'}), 404
    reason = ''
    try:
        body = request.get_json(silent=True) or {}
        reason = str(body.get('reason') or '')[:400]
    except Exception as e:
        logger.debug('[Optimizer.route] reject body parse failed: %s', e)
    _storage.update_proposal_status(proposal_id, 'rejected',
                                    reason=reason or 'manual reject')
    audit_log('optimizer_proposal_reject',
              proposal_id=proposal_id, reason=reason[:200])
    return jsonify({'ok': True, 'status': 'rejected'})


@optimizer_bp.route('/api/optimizer/proposals/<proposal_id>/revert', methods=['POST'])
def revert_proposal(proposal_id):
    blocked = _disabled_guard()
    if blocked is not None:
        return blocked
    prop = _storage.get_proposal(proposal_id)
    if not prop:
        return jsonify({'ok': False, 'error': 'Proposal not found'}), 404
    if prop.get('status') != 'applied':
        return jsonify({'ok': False,
                        'error': f'can only revert applied proposals '
                                 f'(current status={prop.get("status")})'}), 400

    action_type = prop['action_type']
    entry = ACTION_REGISTRY.get(action_type)
    if not entry or not callable(entry.get('revert')):
        return jsonify({'ok': False,
                        'error': f'no revert handler for {action_type}'}), 400

    try:
        args = json.loads(prop.get('action_args') or '{}')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Optimizer.route] bad action_args on revert %s: %s',
                       proposal_id, e)
        args = {}
    try:
        detail = entry['revert'](args) or {}
    except Exception as e:
        logger.error('[Optimizer.route] revert failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500

    action_log = _storage.get_action_log_for_proposal(proposal_id)
    if action_log and not action_log.get('reverted_at'):
        _storage.mark_reverted(action_log['id'], reason='manual revert')
    _storage.update_proposal_status(proposal_id, 'reverted',
                                    reason='manual revert')
    audit_log('optimizer_revert_manual',
              proposal_id=proposal_id, action_type=action_type, detail=detail)
    return jsonify({'ok': True, 'status': 'reverted', 'detail': detail})


# ══════════════════════════════════════════════════════════
#  Manual run
# ══════════════════════════════════════════════════════════

@optimizer_bp.route('/api/optimizer/run-now', methods=['POST'])
def run_now():
    blocked = _disabled_guard()
    if blocked is not None:
        return blocked
    """Synchronously run the optimiser pipeline.

    Accepts optional JSON body ``{"dry_run": true, "window_hours": 24}``.
    """
    try:
        body = request.get_json(silent=True) or {}
    except Exception as e:
        logger.debug('[Optimizer.route] run-now body parse failed: %s', e)
        body = {}
    dry_run = bool(body.get('dry_run', False))
    try:
        window_hours = int(body.get('window_hours') or 24)
    except (TypeError, ValueError):
        window_hours = 24
    window_hours = max(1, min(24 * 14, window_hours))

    try:
        summary = _run_once(dry_run=dry_run, window_hours=window_hours)
    except Exception as e:
        logger.error('[Optimizer.route] run_once crashed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500

    return jsonify({'ok': True, 'summary': summary})


__all__ = ['optimizer_bp']
