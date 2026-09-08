"""tests/test_work_unit_projection.py — WorkUnit projection pins.

Executable specification for docs/API_CONTRACT.md §2.18: the pure
``lib.production.work_unit`` projector maps one production task dict (+ its
retained stage events) onto the single cross-surface lifecycle enum
``queued|running|retrying|done|degraded|failed|aborted`` and the registered
stage-event grammar (docs/EVENTS.md §1).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from lib.production.work_unit import (STAGE_EVENT_TYPES, WORK_UNIT_STATUSES,
                                      project_stages, project_work_unit)


def _task(**over):
    base = {'id': 'slides_abc123', 'kind': 'slides-deck', 'status': 'running',
            'meta': {'topic': 'ByteDance 大模型历程'}, 'topic': 'ByteDance 大模型历程'}
    base.update(over)
    return base


# ── status enum mapping (total, one-way) ─────────────────────────────

@pytest.mark.parametrize('runtime_status,expected', [
    ('', 'queued'),
    ('pending', 'queued'),
    ('running', 'running'),
    ('done', 'done'),
    ('error', 'failed'),
    ('aborted', 'aborted'),
    ('some-future-status', 'failed'),  # unknown IS a failure mode, not a relabel
])
def test_status_mapping(runtime_status, expected):
    unit = project_work_unit(_task(status=runtime_status))
    assert unit['status'] == expected
    assert unit['status'] in WORK_UNIT_STATUSES


def test_degraded_product_axis_outranks_done():
    unit = project_work_unit(_task(
        status='done',
        artifact_quality={'degraded': True, 'reason': 'visual_qa wiped'}))
    assert unit['status'] == 'degraded'
    assert unit['quality'] == {'degraded': True, 'reason': 'visual_qa wiped'}


def test_non_work_unit_kinds_project_to_none():
    assert project_work_unit(_task(kind='chat')) is None
    assert project_work_unit(_task(kind='')) is None
    assert project_work_unit(None) is None


# ── stage grammar ────────────────────────────────────────────────────

def test_stage_event_grammar_is_the_registered_five():
    assert STAGE_EVENT_TYPES == frozenset((
        'stage_started', 'stage_done', 'stage_retry', 'stage_skipped',
        'stage_failed'))


def test_stages_reconstructed_in_index_order():
    stages, total = project_stages([
        {'type': 'stage_started', 'stage': 'outline', 'index': 1,
         'total': 3, 'attempt': 1},
        {'type': 'stage_done', 'stage': 'outline', 'index': 1, 'total': 3,
         'elapsed_s': 4.236},
        {'type': 'stage_started', 'stage': 'author', 'index': 2, 'total': 3,
         'attempt': 1},
        {'type': 'stage_retry', 'stage': 'author', 'attempt': 1,
         'errors': ['gate rejected the artifact']},
        {'type': 'stage_skipped', 'stage': 'export', 'index': 3, 'total': 3,
         'resumed': True},
        {'type': 'page_authored', 'page': 4},  # private extra: ignored
    ])
    assert total == 3
    assert [s['name'] for s in stages] == ['outline', 'author', 'export']
    outline, author, export = stages
    assert outline['state'] == 'done' and outline['elapsedS'] == 4.24
    assert author['state'] == 'active' and author['attempt'] == 2
    assert author['lastErrors'] == ['gate rejected the artifact']
    assert export['state'] == 'skipped' and export['resumed'] is True


def test_stage_failed_event_marks_the_stage():
    stages, _ = project_stages([
        {'type': 'stage_started', 'stage': 'render', 'index': 6, 'total': 9},
        {'type': 'stage_failed', 'stage': 'render', 'index': 6, 'total': 9,
         'detail': 'RuntimeError: boom', 'errors': ['e1']},
    ])
    assert stages[0]['state'] == 'failed'
    assert stages[0]['detail'] == 'RuntimeError: boom'


def test_leftover_active_stage_settles_honestly_on_terminal_units():
    events = [{'type': 'stage_started', 'stage': 'render', 'index': 6,
               'total': 9}]
    failed = project_work_unit(_task(status='error'), events=events)
    assert failed['stages'][0]['state'] == 'failed'
    aborted = project_work_unit(_task(status='aborted'), events=events)
    assert aborted['stages'][0]['state'] == 'skipped'


def test_running_unit_with_retried_stage_reads_retrying():
    events = [
        {'type': 'stage_started', 'stage': 'author', 'index': 4, 'total': 9},
        {'type': 'stage_retry', 'stage': 'author', 'attempt': 1,
         'errors': ['x']},
    ]
    unit = project_work_unit(_task(status='running'), events=events)
    assert unit['status'] == 'retrying'


# ── terminal artifacts / failure envelope ────────────────────────────

def test_done_slides_deck_projects_download_artifact():
    unit = project_work_unit(_task(
        status='done', result={'bytes': 2_450_000, 'pages': 12}))
    assert unit['status'] == 'done'
    assert unit['artifacts'] == [{
        'label': 'pptx', 'href': '/api/v1/slides/slides_abc123/file',
        'bytes': 2_450_000, 'pages': 12,
    }]
    assert unit['links']['events'] == '/api/v1/tasks/slides_abc123/events'
    assert unit['links']['abort'] == '/api/v1/tasks/slides_abc123/abort'


def test_unknown_download_kind_omits_artifacts_instead_of_inventing_url():
    unit = project_work_unit(_task(kind='research', status='done'))
    assert unit['artifacts'] == []


def test_failed_unit_carries_error_envelope():
    unit = project_work_unit(_task(
        status='error',
        error={'kind': 'generic', 'message': '质量门禁未通过',
               'detail': 'render: gate', 'hint': '稍后重试'}))
    assert unit['status'] == 'failed'
    assert unit['failureKind'] == 'generic'
    assert unit['failure']['message'] == '质量门禁未通过'
    assert unit['failure']['hint'] == '稍后重试'


def test_compact_shape_has_no_stage_or_link_keys():
    unit = project_work_unit(_task(), compact=True)
    assert unit['status'] == 'running'
    for key in ('stages', 'stageTotal', 'links', 'artifacts'):
        assert key not in unit


def test_title_and_conv_id_passthrough():
    unit = project_work_unit(_task(conv_id='mt123'))
    assert unit['title'] == 'ByteDance 大模型历程'
    assert unit['convId'] == 'mt123'


# ── kind vocabulary drift guard ──────────────────────────────────────

def test_handler_kind_literals_match_runtime_definitions():
    from lib.longform.runtime import _longform_runtime
    from lib.motion_video.runtime import _motion_runtime
    from lib.research.runtime import _research_runtime
    from lib.slides.runtime import _slides_runtime
    from lib.tasks_pkg.handlers.motion_video import _PRODUCE_WORK_UNIT_KINDS
    assert _PRODUCE_WORK_UNIT_KINDS == {
        'produce_video': _motion_runtime.kind,
        'produce_report': _longform_runtime.kind,
        'produce_slides': _slides_runtime.kind,
        'produce_research': _research_runtime.kind,
    }
