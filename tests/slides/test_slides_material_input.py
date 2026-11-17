"""Caller-supplied material drives a deck instead of an over-length topic."""
from __future__ import annotations

import pytest

from lib.slides.contracts import (
    normalise_slide_material,
    normalise_slide_topic,
)
from lib.slides.recipe import _build_outline_prompt, _run_research
from lib.error_envelope import BadRequest

pytestmark = pytest.mark.unit


def test_material_is_taken_whole_however_long():
    # A 65k-character paper keeps its later experiments and appendix tables.
    paper = '§3 key-value retrieval: worst case 45.6%. ' * 2000
    assert normalise_slide_material(paper) == paper.strip()


def test_material_is_optional_and_defaults_to_empty():
    assert normalise_slide_material(None) == ''
    assert normalise_slide_material('  ') == ''


def test_topic_limit_is_unchanged():
    with pytest.raises(BadRequest):
        normalise_slide_topic('x' * 4001)


def test_research_stage_attaches_supplied_material(monkeypatch):
    called = []
    monkeypatch.setattr(
        'lib.production.research.research_topic',
        lambda topic: called.append(topic))
    material = '## 真实证据\nCARD 困惑度 34.40 vs AR 38.68。'
    result = _run_research({'topic': 'CARD 汇报', 'material': material})
    assert called == []
    assert result['supplied_material'] == material
    assert result['material_cards'][0]['point'] == 'CARD 困惑度 34.40 vs AR 38.68。'


def test_research_stage_without_material_changes_nothing(monkeypatch):
    monkeypatch.setattr(
        'lib.production.research.research_topic',
        lambda topic: {'topic': topic, 'cards': []})
    result = _run_research({'topic': 'plain', 'material': ''})
    assert 'supplied_material' not in result


def test_outline_prompt_marks_material_as_authoritative():
    prompt = _build_outline_prompt(
        'CARD 汇报', lang='zh', max_pages=12, scenarios_doc='tech(技术)',
        style_hint='', research_doc='', research_as_of='2026-11',
        material='真实证据：困惑度 34.40。')
    assert 'AUTHORITATIVE' in prompt
    assert '真实证据：困惑度 34.40。' in prompt
    assert 'Never infer personal achievements' in prompt
    # Material must not be silently dropped when research is absent.
    assert prompt.index('AUTHORITATIVE') < prompt.index('Audience and evidence')
