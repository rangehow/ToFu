"""Synthetic figure/table interpretations must not imply unsupported equivalence."""
import csv
import json
import pytest
from decimal import Decimal
from pathlib import Path


pytestmark = pytest.mark.unit
SAMPLE = Path(__file__).parent / 'fixtures/sample_paper'


def test_table_interpretation_reports_point_estimates_without_equivalence():
    rows = {r['method']: r for r in csv.DictReader((SAMPLE / 'results/selective.csv').read_text().splitlines())}
    assert Decimal(rows['half_ulp_selective']['val_loss']) - Decimal(rows['stochastic_rounding_everywhere']['val_loss']) == Decimal('0.001')
    assert rows['half_ulp_selective']['random_bits_relative'] == '0.087'
    draft = (SAMPLE / 'draft/main.tex').read_text()
    dossier = json.loads((SAMPLE / 'dossier.json').read_text())
    selective = next(f['text'] for f in dossier['facts'] if f['id'] == 'c_selective')
    for text in (draft, selective):
        assert 'matches' not in text
        assert 'point estimates' in text
        assert 'equivalence margin' in text
        assert all(value in text for value in ('3.125', '3.124', '3.171', '0.087'))


def test_figure_support_is_descriptive_and_keeps_full_and_selective_sr_distinct():
    dossier = json.loads((SAMPLE / 'dossier.json').read_text())
    figure = next(f for f in dossier['figures'] if f['label'] == 'fig:loss')
    assert figure['supports'] == ['loss']
    facts = {f['id']: f for f in dossier['facts']}
    comparison = facts['c_match']
    assert 'fig:loss' not in comparison['evidence']
    assert 'matches' not in comparison['text']
    assert 'full-stochastic-rounding' in comparison['text']
    assert 'does not establish parity' in comparison['text']
    assert '3.124 versus 3.112' in comparison['text']
    draft = (SAMPLE / 'draft/main.tex').read_text()
    assert 'selective stochastic rounding has validation loss 3.125' in draft
    assert 'full stochastic rounding 3.124' in draft
    assert 'FP32 master weights 3.112' in draft


def test_stale_update_measurements_do_not_assert_global_stalling_causality():
    dossier = json.loads((SAMPLE / 'dossier.json').read_text())
    mechanism = next(f['text'] for f in dossier['facts'] if f['id'] == 'mechanism')
    assert all(value in mechanism for value in ('61.5%', '20000', '0.98', '0.96'))
    assert 'stalls because' not in mechanism
    assert 'do not establish global training stalling or its cause' in mechanism
    draft = (SAMPLE / 'draft/main.tex').read_text()
    assert 'seems to stall' not in draft
    assert 'do not establish global training stalling or its cause' in draft
