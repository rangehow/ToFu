"""Real-browser guard: Reading-mode report generation rides the CHAT inline
tool timeline (``renderSegmentTimelineHTML``) — reasoning sits adjacent to the
most recent tool calls, exactly like a chat agent bubble.

WHY THIS GUARD (owner acceptance criteria, 2026-07-28)
──────────────────────────────────────────────────────
The report progress UI used to be a fixed three-zone layout (tool cards / one
aggregated thinking block / body). The objective: reuse the agent-bubble
inline tool timeline so thinking + narration appear next to the tool calls
that produced them. This suite pins the RESULT in a real browser against the
real bundle, with ``/api/v1/paper/report/start`` + ``/api/v1/paper/report/poll``
stubbed to replay a recorded event sequence (no LLM cost, deterministic
timing):

  1. PRE-TOOL PHASE — before the first tool call the thinking content is
     still visible (lightweight strip); the area never goes blank.
  2. INLINE TIMELINE — once a tool starts, thinking renders inside the
     ``.ptool-panel.seg-timeline`` panel ADJACENT to the tool rows, and a
     later round's thinking lands right after the most recent tool group.
  3. delta_reset — a tool round's discarded draft never lingers in the
     timeline (its narration segment is removed; thinking segments are not).
  4. DONE STATE — the timeline stays visible ABOVE the final report body
     (chat parity: settled bubbles keep the tool panel over the deliverable).

This file extends the reading-mode browser coverage started in
``test_visual_surfaces.py`` (which opens the shell only) to the report
generation render chain itself.

NEUTER DISCIPLINE (charter: a guard that never bites is worse than none)
────────────────────────────────────────────────────────────────────────
Two negative controls neuter the production seams IN THE LIVE PAGE (function
overrides — no filesystem mutation, nothing to restore):

  * ``_reportSegmentsForRender → []``: the paint must fall back to a
    thinking-LESS grouped panel — the main "thinking adjacent" assertion
    would fail, proving it depends on the segments being passed.
  * ``_segApplyDeltaReset → no-op``: the discarded draft's narration segment
    survives and RENDERS — proving the "draft is gone" assertion depends on
    the removal.

Skips cleanly when Playwright/Chromium is unavailable (standard visual mark).
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

_THINK_R0 = 'I should verify the follow-up work first. '
_DRAFT_R0 = 'This interim draft must vanish. '
_THINK_R1 = 'Now I can write the final report. '
_FINAL_BODY = '# Demo Report\n\nFinal report body with the verified facts.'
_SEARCH_QUERY = 'LLaDA2.2 Levenshtein editing'


def _poll_payload(cursor: int) -> dict:
    """The recorded event sequence a real report task emits, paged by cursor.

    Round 0: thinking → draft prose (discarded via delta_reset) → web_search.
    Round 1 (terminal): thinking → final report body (no tools) → done.
    """
    if cursor == 0:
        return {
            'events': [
                {'type': 'status', 'status': 'running'},
                {'type': 'thinking', 'delta': _THINK_R0, 'llmRound': 0},
            ],
            'next_cursor': 1, 'status': 'running',
        }
    if cursor == 1:
        return {
            'events': [
                {'type': 'delta', 'delta': _DRAFT_R0, 'llmRound': 0},
                {'type': 'delta_reset', 'llmRound': 0},
                {'type': 'tool_start', 'roundNum': 1, 'llmRound': 0,
                 'toolName': 'web_search', 'query': _SEARCH_QUERY,
                 'toolCallId': 'tc-1',
                 'toolArgs': '{"queries": ["LLaDA2.2"]}'},
            ],
            'next_cursor': 2, 'status': 'running',
        }
    if cursor == 2:
        return {
            'events': [
                {'type': 'tool_done', 'roundNum': 1, 'llmRound': 0,
                 'toolName': 'web_search', 'toolCallId': 'tc-1', 'elapsed': 1.2,
                 'toolContent': 'search result bytes the model saw',
                 'results': [{'title': 'LLaDA2.2', 'url': 'https://example.org/x',
                              'snippet': 'follow-up work'}]},
                {'type': 'thinking', 'delta': _THINK_R1, 'llmRound': 1},
                {'type': 'delta', 'delta': _FINAL_BODY, 'llmRound': 1},
            ],
            'next_cursor': 3, 'status': 'running',
        }
    return {
        'events': [
            {'type': 'done', 'report': _FINAL_BODY, 'paperHash': 'h-tl',
             'meta': {'model': 'guard-model'}},
        ],
        'next_cursor': cursor + 1, 'status': 'done',
        'report': _FINAL_BODY, 'meta': {'model': 'guard-model'},
    }


def _install_report_task_routes(page) -> None:
    """Stub the two report-task endpoints; everything else hits the real app."""

    def _fulfill(route, payload: dict) -> None:
        route.fulfill(status=200, content_type='application/json',
                      body=json.dumps(payload))

    def on_start(route) -> None:
        _fulfill(route, {'ok': True, 'task_id': 'task-tl-1', 'paper_hash': 'h-tl'})

    def on_poll(route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        cursor = int(query.get('cursor', ['0'])[0])
        payload = _poll_payload(cursor)
        payload['ok'] = True
        _fulfill(route, payload)

    page.route('**/api/v1/paper/report/start', on_start)
    # Trailing '**' swallows the ?task_id=…&cursor=N query string — without it
    # the glob must match the WHOLE url and the real (404ing) server answers.
    page.route('**/api/v1/paper/report/poll**', on_poll)


def _start_generation(page) -> None:
    """Enter reading mode and drive the REAL production generation flow."""
    page.evaluate("typeof togglePaperMode === 'function' && togglePaperMode()")
    page.wait_for_timeout(800)
    page.evaluate(
        "() => { window._activePaperId = 'p-tl';"
        " window._paperParsedText = 'parsed paper text';"
        " window._paperHash = 'h-tl';"
        " _generatePaperReport(); }")


def _panel_order(page):
    """Classify the timeline panel's top-level children in DOM order."""
    return page.evaluate(
        "() => {"
        " const body = document.querySelector("
        "   '#paperReportContent .ptool-panel.seg-timeline .ptool-panel-body');"
        " if (!body) return null;"
        " return [...body.children].map(el =>"
        "   el.classList.contains('thinking-block') ? 'thinking'"
        "   : el.classList.contains('seg-narration') ? 'narration'"
        "   : 'tools');"
        "}")


def _wait_ready(page, timeout=20000):
    page.wait_for_selector('#userInput', state='visible', timeout=timeout)
    page.wait_for_function("typeof togglePaperMode === 'function'", timeout=timeout)


def test_report_stream_uses_chat_inline_tool_timeline(page, assert_no_js_errors):
    _wait_ready(page)
    _install_report_task_routes(page)
    _start_generation(page)

    # ── Criterion 1: pre-tool phase — thinking visible before any tool call.
    page.wait_for_function(
        "() => { const b = document.getElementById('reportThinkingBlock');"
        f" return b && b.style.display !== 'none'"
        f"   && b.textContent.includes({json.dumps(_THINK_R0.strip())}); }}",
        timeout=15000)

    # ── Criterion 2: once the tool starts, thinking lives INSIDE the chat
    #    timeline panel, adjacent to the tool rows; the standalone strip
    #    yields (no double-showing the same reasoning).
    page.wait_for_function(
        "() => !!document.querySelector("
        "  '#paperReportContent .ptool-panel.seg-timeline')",
        timeout=15000)
    page.wait_for_function(
        "() => { const p = document.querySelector("
        "   '#paperReportContent .ptool-panel.seg-timeline');"
        f" return p && p.textContent.includes({json.dumps(_THINK_R0.strip())}); }}",
        timeout=15000)

    order = _panel_order(page)
    assert order is not None, 'timeline panel body never appeared'
    assert order == ['thinking', 'tools'], (
        f'expected round-0 thinking batch adjacent BEFORE its tool rows, '
        f'got child order: {order}')

    # The standalone strip must be hidden now (the panel carries the thinking).
    strip_display = page.evaluate(
        "() => document.getElementById('reportThinkingBlock').style.display")
    assert strip_display == 'none', (
        'standalone thinking strip still visible alongside the timeline — '
        'the same reasoning renders twice')

    # ── Criterion 3: the discarded round-0 draft NEVER renders in the panel.
    panel_text = page.evaluate(
        "() => document.querySelector("
        "  '#paperReportContent .ptool-panel.seg-timeline').textContent")
    assert _DRAFT_R0.strip() not in panel_text, (
        'delta_reset draft leaked into the timeline — the segment removal '
        'did not bite')

    # ── Round-1 thinking lands right AFTER the most recent tool group.
    page.wait_for_function(
        "() => { const p = document.querySelector("
        "   '#paperReportContent .ptool-panel.seg-timeline');"
        f" return p && p.textContent.includes({json.dumps(_THINK_R1.strip())}); }}",
        timeout=15000)
    order = _panel_order(page)
    assert order == ['thinking', 'tools', 'thinking'], (
        f'round-1 thinking must sit adjacent to the MOST RECENT tool rows, '
        f'got child order: {order}')

    # ── Criterion 4: done — timeline stays ABOVE the final report body.
    page.wait_for_function(
        "() => !!document.querySelector("
        "  '#paperReportContent .paper-report-article')",
        timeout=15000)
    layout = page.evaluate(
        "() => {"
        " const p = document.querySelector('#paperReportContent .ptool-panel');"
        " const a = document.querySelector("
        "   '#paperReportContent .paper-report-article');"
        " if (!p || !a) return null;"
        " return { before: !!(p.compareDocumentPosition(a)"
        "           & Node.DOCUMENT_POSITION_FOLLOWING),"
        f"  thinking: p.textContent.includes({json.dumps(_THINK_R0.strip())}),"
        f"  body: a.textContent.includes('Final report body'),"
        " };"
        "}")
    assert layout is not None, (
        'done state lost the timeline — the chat-parity rule is "tool panel '
        'above the deliverable"')
    assert layout['before'], 'timeline does not precede the report article'
    assert layout['thinking'], 'done-state timeline dropped the thinking'
    assert layout['body'], 'final report body did not render'

    # The discarded draft is nowhere in the whole container either.
    container_text = page.evaluate(
        "() => document.getElementById('paperReportContent').textContent")
    assert _DRAFT_R0.strip() not in container_text


def test_neuter_drop_segments_passing_removes_inline_thinking(page, assert_no_js_errors):
    """NEUTER 1: if the paint stops passing segments to the timeline renderer,
    the main assertion (thinking adjacent to tools) must collapse — the panel
    falls back to a thinking-LESS grouped render. Proves the guard depends on
    the segments seam, not on incidental markup."""
    _wait_ready(page)
    _install_report_task_routes(page)
    # Precondition: the production seam must exist in the shipped bundle —
    # an absent global means the override neuters NOTHING (silent no-op).
    seam = page.evaluate("() => typeof window._reportSegmentsForRender")
    assert seam == 'function', (
        'window._reportSegmentsForRender is not a function in the shipped '
        'bundle — the neuter would be a no-op; check bundling first')
    page.evaluate("() => { window._reportSegmentsForRender = () => []; }")
    _start_generation(page)

    page.wait_for_function(
        "() => !!document.querySelector('#paperReportContent .ptool-panel')",
        timeout=15000)
    state = page.evaluate(
        "() => { const p = document.querySelector("
        "  '#paperReportContent .ptool-panel');"
        f" return {{ hasThinking: p.textContent.includes({json.dumps(_THINK_R0.strip())}),"
        f"  hasQuery: p.textContent.includes({json.dumps(_SEARCH_QUERY)}) }}; }}")
    assert state['hasQuery'], (
        'neutered render lost the tool rows too — the override broke more '
        'than the segments seam; the control says nothing')
    assert not state['hasThinking'], (
        'thinking STILL rendered adjacent to the tools with segments passing '
        'neutered — the main assertion does not depend on the seam it claims '
        'to guard')


def test_neuter_drop_delta_reset_segment_removal_leaks_draft(page, assert_no_js_errors):
    """NEUTER 2: if delta_reset stops removing the round's narration segment,
    the discarded draft RENDERS in the timeline (its round did call tools).
    Proves the draft-absence assertion depends on the removal."""
    _wait_ready(page)
    _install_report_task_routes(page)
    seam = page.evaluate("() => typeof window._segApplyDeltaReset")
    assert seam == 'function', (
        'window._segApplyDeltaReset is not a function in the shipped bundle — '
        'the neuter would be a no-op; check bundling first')
    page.evaluate("() => { window._segApplyDeltaReset = () => {}; }")
    _start_generation(page)

    page.wait_for_function(
        "() => !!document.querySelector("
        "  '#paperReportContent .ptool-panel.seg-timeline')",
        timeout=15000)
    page.wait_for_function(
        "() => { const p = document.querySelector("
        "   '#paperReportContent .ptool-panel.seg-timeline');"
        f" return p && p.textContent.includes({json.dumps(_DRAFT_R0.strip())}); }}",
        timeout=15000)
