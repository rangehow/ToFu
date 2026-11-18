"""Venue-style review prompt and its calibration against real decisions.

Responsibility: the one reviewer prompt the pipeline's critique stage and the
calibration evaluation (``evaluations/paper_review_calibration/``) both use,
and the reading of that evaluation's committed result. The review gate's
accept/reject threshold is the calibrated one; when no calibration matches the
current prompt and judge model, the verdict says ``calibrated: false`` and no
threshold is applied.

Entry points: ``review_messages``, ``parse_review``, ``prompt_digest``,
``review_verdict``.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

__all__ = ['review_messages', 'parse_review', 'prompt_digest',
           'review_verdict', 'CALIBRATION_PATH']

CALIBRATION_PATH = Path(__file__).with_name('calibration') / 'paper_review.json'
_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def review_messages(doc_type, *, venue: str, text: str,
                    checklist: tuple = (), section_ids: tuple = ()) -> list[dict]:
    rubric = '\n'.join(f'- {name}: {meaning}' for name, meaning in doc_type.rubric)
    items = '\n'.join(f'- {rule.id}: {rule.text}' for rule in checklist)
    sections = ', '.join(section_ids) or 'any'
    instruction = (
        f'{doc_type.reviewer} Venue: {venue}.\n'
        f'Score each criterion from 1 (very weak) to 10 (outstanding):\n{rubric}\n'
        'Then give "overall" from 1 to 10: how likely this submission is to be '
        'accepted at the venue as written (1 = certain reject, 10 = certain '
        'accept).\n'
        + (f'Mark each checklist item pass or fail, quoting the text that '
           f'decides it:\n{items}\n' if items else '')
        + 'List concrete findings a revision can fix, each tied to one section '
          f'id ({sections}).\n'
        'Reply with ONLY JSON: {"scores":{"<criterion>":0},"overall":0,'
        '"checklist":[{"rule":"<id>","pass":true,"quote":"..."}],'
        '"findings":[{"section":"<id>","issue":"...","fix":"...",'
        '"rule":"<id or empty>"}],"summary":"..."}')
    return [{'role': 'system', 'content': instruction},
            {'role': 'user', 'content': 'Submission:\n\n' + text}]


def prompt_digest(doc_type, checklist: tuple = ()) -> str:
    messages = review_messages(doc_type, venue='{venue}', text='{text}',
                               checklist=checklist, section_ids=('{sections}',))
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()


def parse_review(content: str, doc_type, *, checklist: tuple = (),
                 section_ids: tuple = ()) -> tuple[dict, list[str]]:
    """Parse a review reply; return (review, structural errors)."""
    match = _JSON_RE.search(content or '')
    if not match:
        return {}, ['review reply has no JSON object']
    review = json.loads(match.group(0))
    errors = []
    scores = review.get('scores') if isinstance(review.get('scores'), dict) else {}
    for name, _meaning in doc_type.rubric:
        value = scores.get(name)
        if not isinstance(value, int) or not 1 <= value <= 10:
            errors.append(f'review score {name!r} is not an integer 1..10')
    overall = review.get('overall')
    if not isinstance(overall, int) or not 1 <= overall <= 10:
        errors.append('review overall is not an integer 1..10')
    marked = {row.get('rule') for row in review.get('checklist') or []
              if isinstance(row, dict) and isinstance(row.get('pass'), bool)}
    missing = [rule.id for rule in checklist if rule.id not in marked]
    if missing:
        errors.append(f'review left checklist items unmarked: {missing}')
    findings = [row for row in review.get('findings') or [] if isinstance(row, dict)]
    if section_ids:
        stray = [row.get('section') for row in findings
                 if row.get('section') not in section_ids]
        if stray:
            errors.append(f'review findings name unknown sections: {stray}')
    review['findings'] = findings
    return review, errors


def load_calibration() -> dict | None:
    if not CALIBRATION_PATH.is_file():
        return None
    return json.loads(CALIBRATION_PATH.read_text(encoding='utf-8'))


def review_verdict(overall: int, *, judge_model: str, digest: str) -> dict:
    """Accept/reject prediction at the calibrated threshold, or why none applies."""
    calibration = load_calibration()
    if calibration is None:
        return {'calibrated': False, 'reason': 'no calibration has been run'}
    if calibration.get('judge_model') != judge_model:
        return {'calibrated': False,
                'reason': f'calibrated for judge {calibration.get("judge_model")!r}, '
                          f'not {judge_model!r}'}
    if calibration.get('prompt_digest') != digest:
        return {'calibrated': False,
                'reason': 'the review prompt changed since calibration'}
    threshold = calibration['threshold']
    return {'calibrated': True, 'threshold': threshold,
            'predicted': 'accept' if overall >= threshold else 'reject',
            'heldout_auc': calibration.get('heldout_auc'),
            'heldout_balanced_accuracy': calibration.get('heldout_balanced_accuracy'),
            'dataset': calibration.get('dataset')}
