"""Writer instructions respect registry enforcement roles; no live model calls."""
import pytest

from lib.longform.documents import pipeline as pl
from lib.longform.documents.rules import rules_for
from lib.longform.documents.types import document_type

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['paper', 'resume', 'longform'])
def test_writer_excludes_reviewer_only_checklist(monkeypatch, kind):
    monkeypatch.setattr(pl, '_usable_facts', lambda ctx: [])
    monkeypatch.setattr(pl, '_strengths', lambda ctx: {})
    monkeypatch.setattr(pl, '_bib_keys', lambda ctx: [])
    ctx = {'doc_type': document_type(kind),
           'brief': {'title': 'T', 'audience': 'A', 'goal': 'G', 'voice': 'V', 'lang': 'en'},
           'artifacts': {'dossier': {'figures': []}}}
    prompt = pl._writer_context(ctx)
    reviewer = pl._checklist(ctx)
    for rule in rules_for(kind):
        marker = f'[{rule.id}]'
        if rule.kind == 'checklist':
            assert marker not in prompt
            assert rule.text not in prompt
            assert rule in reviewer
        else:
            assert marker in prompt
            assert rule.text in prompt
    if kind == 'paper':
        style = [rule for rule in rules_for(kind) if rule.id.startswith('paper.style.')]
        assert len(style) == 5
        assert all(rule in reviewer and rule.text not in prompt for rule in style)
    assert all(rule.kind == 'checklist' for rule in reviewer)
