"""Specification of the shared style measurements and the metric lint check."""
from __future__ import annotations

import math

import pytest

from lib.longform.documents import style_metrics as sm

pytestmark = pytest.mark.unit


def test_prose_drops_latex_identifiers_and_keeps_text():
    latex = (r'We show \textbf{strong} gains~\citep{smith2020} (Fig.~\ref{fig:a}). '
             r'% a comment with may and might' '\n'
             r'\begin{figure}\caption{may}\end{figure}Accuracy is $x^2$.')
    text = sm.prose(latex)
    assert 'strong' in text and 'smith2020' not in text and 'fig:a' not in text
    assert 'comment' not in text and 'caption' not in text and 'may' not in text


def test_lexical_rates_are_per_thousand_words():
    text = 'We may improve results. ' * 10   # 40 words, 10 hedges, 10 "we"
    lex = sm.lexical(text)
    assert lex['n_words'] == 40
    assert lex['hedge_per_k'] == pytest.approx(250.0)
    assert lex['we_per_k'] == pytest.approx(250.0)
    assert lex['sent_len_mean'] == pytest.approx(4.0)


def test_sentences_keep_abbreviations_together():
    assert len(sm.sentences('We follow Smith et al. in this setup. Results are good here.')) == 2


def test_contribution_list_detection():
    assert sm.contribution_list('Our contributions are as follows: (1) a method; (2) a benchmark.')
    assert sm.contribution_list('In summary, we make three contributions: a, b and c.')
    assert not sm.contribution_list('We propose a method and evaluate it on two benchmarks.')


def test_check_passes_and_explains_failures():
    metric = {'name': 'words', 'section': 'abstract', 'op': '<=', 'value': 5}
    assert sm.check(metric, 'One two three four.') is None
    message = sm.check(metric, 'One two three four five six seven.')
    assert message.startswith('has 7 words; the evidence target is at most 5')
    at_least = {'name': 'has_number', 'section': 'abstract', 'op': '>=', 'value': 1}
    assert sm.check(at_least, r'We gain 3.2 points \cite{x}.') is None
    assert 'at least 1' in sm.check(at_least, 'We gain points.')


def test_check_ignores_empty_sections_and_rejects_unknown_metrics():
    assert sm.check({'name': 'sent_len_mean', 'section': 'abstract', 'op': '<=', 'value': 20}, '') is None
    with pytest.raises(ValueError):
        sm.check({'name': 'nope', 'section': 'abstract', 'op': '<=', 'value': 1}, 'text')
    with pytest.raises(ValueError):
        sm.check({'name': 'words', 'section': 'abstract', 'op': '<', 'value': 1}, 'text')


def test_every_metric_returns_a_number():
    text = 'We show that our novel method clearly outperforms baselines by 3.1 points. It may help.'
    for name, fn in sm.METRICS.items():
        value = fn(text)
        assert isinstance(value, float) and not math.isnan(value), name
