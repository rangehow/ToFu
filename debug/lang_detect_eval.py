"""debug/lang_detect_eval.py — measurement-first eval for the language-detection cascade.

Reports, on a small labeled short-text set (Tatoeba-style short sentences +
single words/greetings + the Intercom Fin hard cases + zh/en/mixed/typo):

  * per-tier accuracy: script-only (Tier-0 + heuristic fallback) vs
    fastText-backed cascade (Tier-0→1);
  * the LLM-escalation RATE (how often ``_needs_llm_correction`` would fire on
    the fastText tier) — the cost lever;
  * a breakdown of WHICH tier decided each case.

Run:
    TOFU_LANGDETECT_BACKEND=fasttext python3 debug/lang_detect_eval.py

The LLM tier itself is NOT called here (it needs a live model); we measure the
escalation TRIGGER rate, which is what bounds its cost. English-vs-other-Latin
is scored as a binary ``is_english`` task too, since that is the gate's actual
job (translate vs skip).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import text_lang as tl  # noqa: E402
from lib.text_lang import _needs_llm_correction, _get_ft_detector  # noqa: E402

# (text, gold_code) — gold is the ISO-639-1 (or 'zh'/'ja'/...) language.
# Deliberately weighted toward SHORT + noisy input, the failure regime.
CASES: list[tuple[str, str]] = [
    # ── Fin's documented hard cases (short, casual, typos) ──
    ('im need help', 'en'),
    ('im julia', 'en'),
    ('buenas dias', 'es'),          # typo for "buenos días"
    ('How to compute csat?', 'en'),
    ('App store?', 'en'),
    ('combien coute le pass eleve', 'fr'),
    ('how can i write a query?', 'en'),
    ('kontingentregel zuweisen', 'de'),
    # ── Clean short greetings / phrases ──
    ('Hello, how are you today?', 'en'),
    ('Bonjour, comment allez-vous?', 'fr'),
    ('Buenos días a todos', 'es'),
    ('Guten Tag, wie geht es dir?', 'de'),
    ('Ciao, come stai oggi?', 'it'),
    ('Olá, tudo bem com você?', 'pt'),
    # ── CJK / non-Latin scripts (Tier-0 fast path) ──
    ('你好世界，今天天气不错', 'zh'),
    ('我想问一下这个功能怎么用', 'zh'),
    ('こんにちは、元気ですか', 'ja'),
    ('안녕하세요 만나서 반갑습니다', 'ko'),
    ('Привет, как у тебя дела', 'ru'),
    ('مرحبا كيف حالك اليوم', 'ar'),
    # ── Longer clean sentences (easy) ──
    ('The quick brown fox jumps over the lazy dog every morning.', 'en'),
    ('La inteligencia artificial está cambiando el mundo rápidamente.', 'es'),
    ('Die künstliche Intelligenz verändert die Welt sehr schnell.', 'de'),
    # ── Mixed (gate should still lean non-en so we translate) ──
    ('今天开会讨论了 deep learning 的最新进展', 'zh'),
]

# Codes fastText may return that we fold to the gold label for scoring
# (dialect/script variants). Kept tiny + explicit.
_FOLD = {'nb': 'no', 'nn': 'no'}


def _norm(code: str) -> str:
    return _FOLD.get(code, code)


def _score(label: str, allow_ft: bool):
    if allow_ft:
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'fasttext'
    else:
        os.environ['TOFU_LANGDETECT_BACKEND'] = 'script'
    tl.reset_for_test()
    if allow_ft and _get_ft_detector() is None:
        print(f'[{label}] fastText backend requested but unavailable — skipping')
        return
    correct = eng_correct = escalate = 0
    by_source: dict[str, int] = {}
    rows = []
    for text, gold in CASES:
        res = tl.detect_language(text)          # Tier-0/1 only, no LLM
        code = _norm(res.code)
        ok = (code == gold)
        correct += ok
        # Binary "is English" gate accuracy (the translate-vs-skip decision).
        gate_ok = ((code == 'en') == (gold == 'en'))
        eng_correct += gate_ok
        by_source[res.source] = by_source.get(res.source, 0) + 1
        esc = False
        if res.source == 'fasttext':
            det = _get_ft_detector()
            cands = det(tl._WHITESPACE_RE.sub(' ', text).strip()[:200],
                        model='lite', k=2) if det else []
            esc = _needs_llm_correction(res, text, cands)
            escalate += esc
        rows.append((text, gold, code, res.source, f'{res.confidence:.2f}',
                     'ok' if ok else 'MISS', 'ESC' if esc else ''))
    n = len(CASES)
    print(f'\n===== {label} =====')
    print(f'{"text":42} {"gold":5} {"pred":6} {"src":9} {"conf":5} {"":4} esc')
    for text, gold, code, src, conf, mark, esc in rows:
        print(f'{text[:42]:42} {gold:5} {code:6} {src:9} {conf:5} {mark:4} {esc}')
    print(f'\n  exact-code accuracy : {correct}/{n} = {correct/n:.1%}')
    print(f'  is-English gate acc : {eng_correct}/{n} = {eng_correct/n:.1%}')
    print(f'  tier breakdown      : {by_source}')
    if allow_ft:
        ft_n = by_source.get('fasttext', 0)
        rate = escalate / ft_n if ft_n else 0.0
        print(f'  LLM-escalation rate : {escalate}/{ft_n} fastText cases '
              f'= {rate:.1%} (only these hit the billed tier)')


if __name__ == '__main__':
    _score('Tier-0 + heuristic (no fastText)', allow_ft=False)
    _score('Tier-0 + fastText cascade', allow_ft=True)
