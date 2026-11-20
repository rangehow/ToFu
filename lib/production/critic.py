"""Shared candidate critic: prompt contract, reply parsing and blind A/B.

Responsibility: the one owner of the "independent judge picks the stronger
candidate" contract that the slides outline critic, the film script critic and
the document pipeline's blind comparison all use. Recipes keep their own model
seam (``_llm_chat``), cancellation and digest of what a candidate is; this
module only builds the prompt, validates the reply and, for blind A/B, removes
label and position bias.

Entry points: ``critic_prompt``, ``parse_critic_choice``, ``blind_pairwise``.
Dependencies: standard library only.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

__all__ = ['critic_prompt', 'parse_critic_choice', 'blind_pairwise',
           'CRITIC_SCORE_RANGE']

#: Integer score range every critic reply uses (the prompt states it).
CRITIC_SCORE_RANGE = (0, 10)
_JSON_OBJECT_RE = re.compile(r'\{.*\}', re.DOTALL)
#: Candidate digests are cut at this many characters, the slides and film
#: critics' original prompt contract.
_DIGEST_CHARS = 28000


def critic_prompt(instruction: str, score_keys: tuple[str, ...],
                  digests: list, *,
                  digest_chars: int | None = _DIGEST_CHARS) -> str:
    """Prompt asking for ``{"winner", "reason", "scores"}`` over two candidates.

    ``digest_chars=None`` keeps whole candidates (a document judged on its
    full text); plan critics keep their original cut.
    """
    score = '{' + ','.join(f'"{key}":0' for key in score_keys) + '}'
    candidates = json.dumps(digests, ensure_ascii=False, separators=(',', ':'))
    return (
        instruction + ' Output ONLY JSON: '
        '{"winner":1,"reason":"specific concise reason","scores":['
        + score + ',' + score + ']}. '
        'Scores are integers 0..10. Candidates:\n'
        + (candidates if digest_chars is None else candidates[:digest_chars]))


def parse_critic_choice(content: str, count: int, *, label: str,
                        default_reason: str) -> tuple[int, str, list]:
    """Validate a critic reply; return (0-based winner, reason, scores)."""
    match = _JSON_OBJECT_RE.search(content or '')
    if not match:
        raise ValueError(f'{label} reply has no JSON object')
    raw = json.loads(match.group(0))
    winner = int(raw.get('winner') or 0) - 1
    if winner < 0 or winner >= count:
        raise ValueError(f'{label} selected an invalid candidate')
    reason = re.sub(r'\s+', ' ', str(raw.get('reason') or '')).strip()[:500]
    scores = raw.get('scores') if isinstance(raw.get('scores'), list) else []
    return winner, reason or default_reason, scores


def _blind_label_order(seed: str) -> bool:
    """Deterministic, content-bound coin: True presents the candidate first."""
    return hashlib.sha256(seed.encode('utf-8')).digest()[0] % 2 == 0


def blind_pairwise(judge: Callable[[list], tuple[int, str, list]],
                   candidate: dict, baseline: dict, *, seed: str) -> dict:
    """Judge ``candidate`` against ``baseline`` twice, once in each position.

    ``judge`` receives the two anonymous digests and returns
    ``parse_critic_choice``'s triple. Labels never reveal which is new; the
    first presentation order comes from a content-bound coin and the second
    swaps it, so a position-biased judge produces a split, not a win. The
    candidate is preferred only when it wins both presentations; one win
    each is ``'no preference'``, reported with both orders' scores, their
    position-balanced totals and ``position_bias`` (the judge picked the same
    position both times).
    """
    first_candidate = _blind_label_order(seed)
    rounds = []
    for candidate_first in (first_candidate, not first_candidate):
        pair = [candidate, baseline] if candidate_first else [baseline, candidate]
        winner, reason, scores = judge(pair)
        candidate_index = 0 if candidate_first else 1
        rounds.append({
            'candidate_position': candidate_index + 1,
            'winner_position': winner + 1,
            'candidate_won': winner == candidate_index,
            'reason': reason,
            'candidate_scores': (scores[candidate_index]
                                 if len(scores) == 2 else {}),
            'baseline_scores': (scores[1 - candidate_index]
                                if len(scores) == 2 else {}),
        })
    wins = sum(1 for row in rounds if row['candidate_won'])
    verdict = ('candidate' if wins == 2 else
               'baseline' if wins == 0 else 'no preference')

    def total(side: str) -> int:
        return sum(value for row in rounds for value in (row[side] or {}).values()
                   if isinstance(value, int) and not isinstance(value, bool))
    return {'verdict': verdict, 'preferred': verdict == 'candidate',
            'position_bias': rounds[0]['winner_position'] == rounds[1]['winner_position'],
            'totals': {'candidate': total('candidate_scores'),
                       'baseline': total('baseline_scores')},
            'rounds': rounds}
