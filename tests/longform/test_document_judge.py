"""One judge for every quality A/B of the document pipeline.

The style-blind ``pipeline._judge_pair`` (41246f17, commit 58fea3fd) is the
only judge of any quality comparison: the pipeline's adoption and delivery
A/Bs, the style-rule studies and the figure/table before-after study. A
second judge with its own prompt could reward something the delivery judge
does not (the style study found a rule-aware judge favoured rule followers
21-1, a blind one 13-10), so every evaluation reuses this entry point.
"""
from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest

from lib.longform.documents import pipeline as pl
from lib.longform.documents.types import document_type

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
#: Every tree that compares document quality: the pipeline, its CLI and the
#: evaluations that A/B its outputs (the figure study once it lands).
QUALITY_AB_TREES = ('lib/longform', 'scripts/document_pipeline.py',
                    'evaluations/paper_style_corpus', 'evaluations/figure_style_ab')
JUDGE_HOME = ROOT / 'lib/longform/documents/pipeline.py'


def _sources():
    for rel in QUALITY_AB_TREES:
        path = ROOT / rel
        yield from ([path] if path.is_file() else sorted(path.rglob('*.py')) if path.is_dir() else [])


def _function_of(tree: ast.AST, node: ast.AST) -> str:
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                func.lineno <= node.lineno <= (func.end_lineno or func.lineno):
            return func.name
    return '<module>'


def test_every_quality_ab_routes_through_the_one_style_blind_judge():
    """No other prompt asks a model to pick a version, and every both-orders
    comparison gets its judge from ``_judge_pair`` (directly or through
    ``validate.pipeline_judges``, which returns ``_judge_pair``)."""
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
                if name == 'critic_prompt' and not (
                        path == JUDGE_HOME and _function_of(tree, node) == '_judge_pair'):
                    offenders.append(f'{rel}:{node.lineno} builds a pairwise judge prompt')
                if name == 'blind_pairwise':
                    judge = ast.unparse(node.args[0]) if node.args else ''
                    if not ('_judge_pair(' in judge or 'judges[' in judge or judge == 'judge'):
                        offenders.append(f'{rel}:{node.lineno} blind_pairwise judge {judge!r}')
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(
                    marker in node.value for marker in ('"winner"', '"better"', "'winner'",
                                                        'Which one would you rather', 'Which one would make')):
                if not (path == JUDGE_HOME and _function_of(tree, node) == '_judge_pair'):
                    offenders.append(f'{rel}:{node.lineno} asks a model to pick a version')
        source = path.read_text(encoding='utf-8')
        if 'blind_pairwise(' in source and path != JUDGE_HOME:
            assert 'pipeline_judges(' in source or '_judge_pair(' in source, rel
    assert offenders == []


def test_pipeline_judges_is_the_pipeline_judge(monkeypatch, tmp_path):
    from evaluations.paper_style_corpus import validate
    seen = []
    original = pl._judge_pair
    monkeypatch.setattr(pl, '_judge_pair', lambda ctx, label: (seen.append(label),
                                                               original(ctx, label))[1])
    judges = validate.pipeline_judges(['u1'], 'gpt-5.5', tmp_path)
    assert seen == ['style-ab'] and list(judges['u1']) == [validate.PIPELINE_JUDGE]


def test_rendered_images_are_judged_by_the_same_judge(tmp_path, monkeypatch):
    """A table or figure before/after is a pair of renders: ``_judge_pair``
    sends them as images after the same style-blind prompt."""
    paper = tmp_path / 'paper'
    shutil.copytree(Path(__file__).parent / 'fixtures' / 'sample_paper', paper)
    brief = json.loads((paper / pl.BRIEF_FILE).read_text())
    ctx = {'paper_dir': str(paper), 'workdir': str(tmp_path / 'work'), 'brief': brief,
           'doc_type': document_type('paper'), 'artifacts': {}}
    before, after = tmp_path / 'before.png', tmp_path / 'after.png'
    before.write_bytes(b'\x89PNG before')
    after.write_bytes(b'\x89PNG after')
    sent = []
    monkeypatch.setattr(pl, '_chat', lambda c, role, messages, label: (
        sent.append((role, label, messages[-1]['content'])),
        json.dumps({'winner': 2, 'reason': 'r', 'scores': [{'clarity': 4}, {'clarity': 7}]}))[1])
    winner, _reason, _scores = pl._judge_pair(ctx, 'figure-ab')(
        [{'part': 'Table 2', 'images': [str(before)]}, {'part': 'Table 2', 'images': [str(after)]}])
    assert winner == 1
    role, label, content = sent[0]
    assert (role, label) == ('judge', 'figure-ab')
    assert content[0]['type'] == 'text' and ctx['doc_type'].reviewer in content[0]['text']
    assert 'Candidate 1' in content[0]['text'] and 'rule' not in content[0]['text'].lower()
    images = [part['image_url']['url'] for part in content if part['type'] == 'image_url']
    assert len(images) == 2 and all(url.startswith('data:image/png;base64,') for url in images)
