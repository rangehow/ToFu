"""Held-out A/B of the shipped style checklist (PREREG_HELDOUT_AB.md).

The provenance gate counts every number the source lacks, and the registered
units exclude every unit that selected the items.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluations.paper_style_corpus.validate import draft_unit, unsourced

pytestmark = pytest.mark.unit

CORPUS = Path(__file__).resolve().parents[2] / 'evaluations/paper_style_corpus'


def test_gate_flags_range_ends_negatives_and_new_citations():
    source = r'Accuracy rises from 3 to 4 points \citep{a,b}; see Figure.1.'
    revision = r'Accuracy rises by 3-5 points (-0.4 loss) \citep{a,c}; see Figure 1.'
    assert unsourced(source, revision) == {'numbers': ['0.4', '5'], 'citations': ['c']}
    assert unsourced(source, source) == {'numbers': [], 'citations': []}


def test_draft_unit_inlines_the_introduction(tmp_path):
    (tmp_path / 'sections').mkdir()
    (tmp_path / 'sections' / 'intro.tex').write_text('\\section{Introduction}\nWe show 2.\n')
    (tmp_path / 'main.tex').write_text('\\title{T}\n\\begin{abstract}A.\\end{abstract}\n'
                                       '\\input{sections/intro}\n\\section{Method}\nM.\n')
    unit = draft_unit(tmp_path / 'main.tex')
    assert 'We show 2.' in unit and 'Method' not in unit and unit.startswith('\\title{T}')


def test_registered_units_are_fresh():
    prereg = json.loads((CORPUS / 'prereg_heldout_ab.json').read_text())
    selection = {u['id'] for u in json.loads((CORPUS / 'validation_drafts.json').read_text())['units']}
    selection |= set(json.loads((CORPUS / 'validation_rule_ab.json').read_text())['units'])
    ids = [u['id'] for u in prereg['units']]
    assert len(ids) == len(set(ids)) >= 30
    assert not selection & set(ids)
    assert selection <= set(prereg['excluded_ids'])


def test_outcome_is_applied_as_registered():
    result = json.loads((CORPUS / 'validation_heldout_ab.json').read_text())
    prereg = json.loads((CORPUS / 'prereg_heldout_ab.json').read_text())
    assert result['n_units'] == len(prereg['units'])
    assert result['style_items'] == prereg['style_items']
    outcome = result['outcome']
    assert outcome['pass'] == all(v for k, v in outcome.items() if k != 'pass')
    style = json.loads((CORPUS.parents[1] / 'lib/longform/documents/style_rules.json').read_text())
    proven = {rule['id'] for rule in style['rules'] if rule.get('proven')}
    assert proven == (set(prereg['style_items']) if outcome['pass'] else set())
