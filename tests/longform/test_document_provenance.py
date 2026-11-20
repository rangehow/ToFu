"""Provenance gate: every number and citation in an output traces to a source.

Specification for ``lib/longform/documents/provenance.py`` and its two uses in
the document pipeline (the section/edit gate and the deliver-stage hard gate).
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from lib.longform.documents import pipeline as pl
from lib.longform.documents.provenance import check_texts
from lib.longform.documents.types import document_type

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / 'fixtures'


def _style_study_case():
    case = json.loads((FIXTURES / 'provenance' / 'style_study_C003cf5e3.json').read_text())
    return ' '.join(case['source_sentences']), '\n'.join(case['revision_paragraphs'])


def test_style_study_revision_that_was_counted_as_inventing_a_number():
    """The style study (94a280f9) counted one shipped-style revision (held-out
    unit C003cf5e3) as adding a number. The number is the 1 of "Figure 1": the
    source writes "Figure.1", which the study's own extractor skipped. The
    gate traces it; the same revision with one unsourced value is refused,
    naming the value and where it is."""
    source, revision = _style_study_case()
    study_extractor = lambda text: set(re.findall(r'(?<![\w.])\d+(?:\.\d+)?', text))  # noqa: E731
    assert study_extractor(revision) - study_extractor(source) == {'1'}
    assert 'Figure.1' in source and 'Figure 1' in revision

    assert check_texts({'revision': revision}, {'source': source}, latex=False)['unsourced'] == []

    invented = revision.replace('Figure 1', 'Figure 1, Dice 0.873', 1)
    found = check_texts({'revision': invented}, {'source': source}, latex=False)['unsourced']
    assert [(row['kind'], row['value'], row['location']) for row in found] == [
        ('number', '0.873', 'revision:1')]
    assert 'Dice 0.873' in found[0]['context']


def test_rounding_and_percent_restatement_are_sourced_and_recorded():
    source = {'results.json': '{"acc": 0.8734, "ppl": 12.456, "n": 1024}'}
    report = check_texts({'paper': 'Accuracy 87.3\\% (0.873), perplexity 12.5 over 1{,}024 runs.'},
                         source)
    assert report['unsourced'] == []
    how = {row['value']: (row['how'], row['source_value']) for row in report['rounded']}
    assert how == {'87.3': ('rescaled percent', '0.8734'), '0.873': ('rounded', '0.8734'),
                   '12.5': ('rounded', '12.456')}
    off = check_texts({'paper': 'Accuracy 87.4\\%.'}, source)['unsourced']
    assert [row['value'] for row in off] == ['87.4']
    # A fraction sources a percentage only where the output writes a % sign.
    count = check_texts({'paper': 'We ran 87 seeds.'}, {'r': 'acc 0.87'})['unsourced']
    assert [row['value'] for row in count] == ['87']
    back = check_texts({'paper': 'acc 0.873'}, {'r': 'acc 87.3%'})
    assert back['unsourced'] == [] and back['rounded'][0]['how'] == 'rescaled fraction'


def test_every_number_kind_is_checked_and_layout_is_not_content():
    output = (
        '\\begin{table}[t]\\centering\\resizebox{0.9\\linewidth}{!}{%\n'
        '\\begin{tabular}{@{}lcc@{}}\n'
        '\\multicolumn{2}{c}{Method} & Acc \\\\[2pt]\n'
        'Ours & 91.2 & 3 \\\\\n'
        '\\end{tabular}}\n'
        '\\vspace{-3mm}\\includegraphics[width=0.48\\textwidth]{figs/fig7.pdf}\\label{tab:t9}\n'
        'Gains hold on BF16 and GPT-2 (CI [0.81, 0.92], p < 0.001, n = 5) \\citep{known,made2024}.\n'
        '\\end{table}\n')
    found = check_texts({'main.tex': output}, {'draft': 'We ran 3 seeds.'},
                        bib_keys=['known'])['unsourced']
    assert [(row['kind'], row['value']) for row in found] == [
        ('number', '91.2'), ('number', '0.81'), ('number', '0.92'), ('number', '0.001'),
        ('number', '5'), ('citation', 'made2024')]
    assert {row['location'] for row in found} == {'main.tex:4', 'main.tex:7'}


# ── pipeline ────────────────────────────────────────────────────────────────

def _ctx(tmp_path: Path) -> dict:
    paper = tmp_path / 'paper'
    shutil.copytree(FIXTURES / 'sample_paper', paper)
    brief = json.loads((paper / pl.BRIEF_FILE).read_text())
    ctx = {'paper_dir': str(paper), 'workdir': str(tmp_path / 'work'), 'brief': brief,
           'doc_type': document_type(brief['type']), 'run_started': 100.0, 'artifacts': {}}
    ctx['artifacts']['dossier'] = pl._run_dossier(ctx)
    return ctx


def test_deliver_refuses_and_names_every_unsourced_item_before_writing(tmp_path):
    ctx = _ctx(tmp_path)
    ctx['artifacts']['render'] = {'sections': {
        'abstract': 'Stochastic rounding everywhere reaches 3.124.',
        'experiments': 'The flagged 8.7\\% reach 3.125; a random 8.7\\% reach 3.171.\n'
                       'Memory falls by 52.9\\% \\citep{kalamkar2019study,invented2025}.'}}
    art = pl._run_deliver(ctx)
    errors = pl._gate_deliver(ctx, art)
    assert errors == [
        "deliver.provenance: unsourced number '52.9' at experiments:2: Memory falls by "
        "52.9\\% \\citep{kalamkar2019study,invented2025}.",
        "deliver.provenance: unsourced citation 'invented2025' at experiments:2: Memory falls "
        "by 52.9\\% \\citep{kalamkar2019study,invented2025}."]
    assert not (Path(ctx['paper_dir']) / ctx['brief']['output_dir']).exists()
    report = (Path(ctx['workdir']) / 'artifacts' / '11-deliver.md').read_text()
    assert '52.9' in report and 'invented2025' in report


def test_section_and_edit_gate_use_the_same_provenance(tmp_path):
    ctx = _ctx(tmp_path)
    errors = pl._section_check(ctx, 'experiments', 'We train on 37 tasks.')
    assert any('no-invented-numbers' in e and "'37'" in e for e in errors)
    assert not any('no-invented-numbers' in e for e in
                   pl._section_check(ctx, 'experiments', 'Loss reaches 3.12.'))


def test_existing_draft_delivers_only_its_changed_files_for_checking(tmp_path):
    ctx = _ctx(tmp_path)
    ctx['brief'] = {**ctx['brief'], 'draft': {'main': 'main.tex'}}
    ctx['artifacts']['render'] = {'files': {'main.tex': 'x'}, 'sections': {'a': 'y'}}
    assert pl._delivered_text(ctx) == {'main.tex': 'x'}
