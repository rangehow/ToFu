"""Style measurements shared by the paper-style corpus analysis and the lint gate.

Responsibility: the single definition of every measured style property
(sentence length, hedge/booster/promotional-word density, first person,
passive voice, nominalisation, contribution list, ...). The corpus analysis
(``evaluations/paper_style_corpus/``) computes its features with these
functions, and data lint rules in ``style_rules.json`` carry a ``metric``
that ``check`` evaluates on a drafted section, so a rule measures exactly
what its evidence row measured.

A metric is ``{"name", "section", "op", "value"}``: ``name`` is a key of
``METRICS``, ``op`` is ``<=`` or ``>=``, ``value`` comes from the evidence
row (the corpus statistic named in the rule's ``evidence``).

Entry points: ``check(metric, section_text, plain=False)``, ``METRICS``,
``lexical``, ``sentences``, ``words``, ``prose``.
Dependencies: standard library only.
"""
from __future__ import annotations

import collections
import re
import statistics

__all__ = ['METRICS', 'check', 'lexical', 'sentences', 'words', 'prose',
           'HEDGES', 'BOOSTERS', 'PROMO']

HEDGES = ('may', 'might', 'could', 'possibly', 'perhaps', 'suggest', 'suggests',
          'appear', 'appears', 'seem', 'seems', 'likely', 'potentially', 'somewhat',
          'relatively', 'presumably', 'arguably', 'probably', 'tend', 'tends',
          'approximately', 'roughly', 'partially', 'unclear', 'speculate')
BOOSTERS = ('clearly', 'significantly', 'substantially', 'dramatically', 'remarkably',
            'consistently', 'strongly', 'drastically', 'considerably', 'greatly',
            'always', 'definitely', 'undoubtedly', 'superior', 'surprisingly',
            'striking', 'strikingly', 'crucially', 'importantly')
PROMO = ('novel', 'sota', 'outperform', 'outperforms', 'outperforming',
         'unprecedented', 'groundbreaking', 'superior', 'first')

_ABBR = re.compile(r'\b(e\.g|i\.e|et al|etc|Fig|Figs|Eq|Eqs|Sec|Secs|Tab|vs|cf|resp|approx|No)\.')
_SENT = re.compile(r'(?<=[.!?])\s+(?=[A-Z(\[])')
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_NOMINAL = re.compile(r'\b[a-z]{4,}(tion|tions|ment|ments|ness|ance|ances|ence|ences|ity|ities|ization|izations)\b')
_PASSIVE = re.compile(r'\b(is|are|was|were|be|been|being)\s+(\w+ly\s+)?(\w+ed|built|done|made|shown|known|given|taken|seen|chosen|drawn|written|found|run|set)\b', re.I)
_WE = re.compile(r'\b(we|our|us)\b', re.I)
_CITATION = re.compile(r'\[\d+(?:[,–-]\s*\d+)*\]|et al\.,? \(?\d{4}|\(\w[^()]{0,60}\d{4}[a-z]?\)')
_CONTRIB_LIST = re.compile(
    r'(contributions?|in summary|to summarize|specifically, we|our main)[^.]{0,80}(:|as follows)'
    r'|•|(\(1\)|\(i\)|1\))[^.]{5,200}(\(2\)|\(ii\)|2\))', re.I)
_FINDING_VERBS = re.compile(r'\bwe (show|find|demonstrate|prove|observe|reveal|establish|identify)\b', re.I)


def sentences(text: str) -> list[str]:
    protected = _ABBR.sub(lambda m: m.group(0).replace('.', '§'), text)
    return [s.replace('§', '.') for s in _SENT.split(protected) if len(s.split()) >= 3]


def words(text: str) -> list[str]:
    return _WORD.findall(text)


def _rate(count: int, n: int, per: int = 1000) -> float:
    return per * count / n if n else float('nan')


def lexical(text: str) -> dict:
    tokens = [w.lower() for w in words(text)]
    n = len(tokens)
    counts = collections.Counter(tokens)
    sents = sentences(text)
    lengths = [len(words(s)) for s in sents]
    low = text.lower()
    return {
        'n_words': n,
        'sent_len_mean': statistics.mean(lengths) if lengths else float('nan'),
        'sent_len_sd': statistics.pstdev(lengths) if len(lengths) > 1 else float('nan'),
        'hedge_per_k': _rate(sum(counts[h] for h in HEDGES), n),
        'booster_per_k': _rate(sum(counts[b] for b in BOOSTERS), n),
        'promo_per_k': _rate(sum(counts[p] for p in PROMO) + low.count('state-of-the-art'), n),
        'we_per_k': _rate(len(_WE.findall(text)), n),
        'passive_per_sent': len(_PASSIVE.findall(text)) / len(sents) if sents else float('nan'),
        'nominal_per_k': _rate(len(_NOMINAL.findall(low)), n),
        'question_per_k': _rate(text.count('?'), n),
        'novel_per_k': _rate(counts['novel'], n),
        'number_per_k': _rate(len(re.findall(r'\b\d+(?:\.\d+)?%?', re.sub(
            r'\[[\d,\s–-]+\]|\b(19|20)\d\d[a-z]?\b', ' ', text))), n),
        'citation_per_k': _rate(len(_CITATION.findall(text)), n),
    }


def contribution_list(text: str) -> int:
    return int(bool(_CONTRIB_LIST.search(text)))


def finding_verbs_per_k(text: str) -> float:
    return _rate(len(_FINDING_VERBS.findall(text)), len(words(text)))


# ── LaTeX → prose ────────────────────────────────────────────────────────

_DROP_ARG = re.compile(
    r'\\(?:cite[a-z]*|ref|eqref|autoref|cref|Cref|label|includegraphics|url|href|'
    r'input|vspace|hspace|setlength)\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})+')
_KEEP_ARG = re.compile(r'\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}')
_ENV_ITEM = re.compile(r'\\item\b')


def prose(latex: str) -> str:
    """Readable prose of a LaTeX section body, as a reader of the PDF sees it."""
    text = re.sub(r'(?<!\\)%.*$', '', latex or '', flags=re.M)
    text = re.sub(r'\\begin\{(figure|table|equation|align)\*?\}.*?\\end\{\1\*?\}', ' ', text, flags=re.S)
    text = _DROP_ARG.sub(' [1] ', text)
    text = _ENV_ITEM.sub(' • ', text)
    text = re.sub(r'\$[^$]*\$', 'x', text)
    for _ in range(3):
        text = _KEEP_ARG.sub(r'\1', text)
    text = re.sub(r'\\[a-zA-Z]+\*?', ' ', text)
    text = text.replace('~', ' ').replace('{', '').replace('}', '')
    return re.sub(r'\s+', ' ', text).strip()


# ── metric registry ──────────────────────────────────────────────────────

def _lex(key):
    return lambda text: float(lexical(text)[key])


METRICS = {
    'words': lambda text: float(len(words(text))),
    'sentences': lambda text: float(len(sentences(text))),
    'sent_len_mean': _lex('sent_len_mean'),
    'hedge_per_k': _lex('hedge_per_k'),
    'booster_per_k': _lex('booster_per_k'),
    'promo_per_k': _lex('promo_per_k'),
    'novel_per_k': _lex('novel_per_k'),
    'we_per_k': _lex('we_per_k'),
    'passive_per_sent': _lex('passive_per_sent'),
    'nominal_per_k': _lex('nominal_per_k'),
    'number_per_k': _lex('number_per_k'),
    'has_number': lambda text: float(bool(re.search(r'\d', text))),
    'contribution_list': lambda text: float(contribution_list(text)),
    'finding_verbs_per_k': finding_verbs_per_k,
}

_LABELS = {
    'words': 'words', 'sentences': 'sentences', 'sent_len_mean': 'words per sentence',
    'hedge_per_k': 'hedges per 1000 words', 'booster_per_k': 'boosters per 1000 words',
    'promo_per_k': 'promotional words per 1000 words', 'novel_per_k': '"novel" per 1000 words',
    'we_per_k': 'first-person (we/our/us) per 1000 words',
    'passive_per_sent': 'passive constructions per sentence',
    'nominal_per_k': 'nominalisations per 1000 words', 'number_per_k': 'numbers per 1000 words',
    'has_number': 'a concrete number (1 = yes)', 'contribution_list': 'an explicit contribution list (1 = yes)',
    'finding_verbs_per_k': '"we show/find/demonstrate..." per 1000 words',
}


def check(metric: dict, section_text: str, plain: bool = False) -> str | None:
    """None if the section satisfies the metric, else an actionable message."""
    name, op, value = metric['name'], metric['op'], float(metric['value'])
    if name not in METRICS or op not in ('<=', '>='):
        raise ValueError(f'unknown style metric {metric!r}')
    text = section_text if plain else prose(section_text)
    measured = METRICS[name](text)
    if measured != measured:   # NaN: empty section, nothing to measure
        return None
    ok = measured <= value if op == '<=' else measured >= value
    if ok:
        return None
    target = 'at most' if op == '<=' else 'at least'
    return (f'has {measured:.3g} {_LABELS[name]}; the evidence target is {target} '
            f'{value:g} ({metric.get("why", "award/high-citation papers vs matched controls")})')
