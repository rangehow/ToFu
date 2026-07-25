"""lib/motion_video/_scene_author.py — Per-scene composition author (P5).

The quality jump over the zero-LLM slide template
(docs/PRODUCTION_PIPELINE_DESIGN.md §2.2 stage 5): each scene gets its OWN
bounded agent loop that writes a bespoke ``index.html`` composition, instead
of every scene rendering as "gradient background + one centred line".

Design constraints (all deliberate):

  * **Narrow toolset.** The author sees only ``write_file`` (hard-confined to
    that scene's directory), ``web_search`` / ``fetch_url`` (for real logos /
    reference material), and ``composition_check`` (the zero-LLM static gate).
    It CANNOT render — rendering stays in the engine's render stage, so an
    author loop can never burn a 35s render per iteration.
  * **Per-scene isolation.** Context is one scene's text + duration + the
    composition contract. No whole-film context, so cost is linear and a
    scene's failure is local.
  * **Never fail the film.** Any failure (no composition written, gate still
    failing, token budget exhausted, LLM error, abort) degrades that ONE scene
    to :func:`lib.motion_video._template.render_scene_html`. The caller always
    gets valid HTML back.
  * **Hard cost caps** (owner 拍板 #3): ``max_rounds`` bounds the loop and
    ``token_budget`` stops it once cumulative tokens for THIS scene exceed the
    budget. There is no money cap here — that belongs to the wallet layer.

Default OFF: the engine only calls this when the job opts in
(``task['scene_author']`` or ``TOFU_MOTION_SCENE_AUTHOR=1``), because it
spends one agent loop per scene.
"""

from __future__ import annotations

import json
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['author_scene', 'scene_author_enabled', 'SCENE_AUTHOR_TOOLS']

#: Bounded rounds per scene (tool-eligible rounds; +1 final no-tools round).
_DEFAULT_MAX_ROUNDS = 4
#: Cumulative token ceiling per scene — the cost cap (拍板 #3).
_DEFAULT_TOKEN_BUDGET = 60000
#: Max output tokens per dispatch (a composition is a few hundred lines).
_MAX_TOKENS_PER_ROUND = 8192


def scene_author_enabled(task: dict | None = None) -> bool:
    """True when per-scene authoring is switched on for this job.

    Default OFF (it spends one agent loop per scene). Opt in per-job via
    ``task['scene_author']`` or globally via ``TOFU_MOTION_SCENE_AUTHOR=1``.
    """
    if isinstance(task, dict) and task.get('scene_author') is not None:
        return bool(task['scene_author'])
    return os.environ.get('TOFU_MOTION_SCENE_AUTHOR', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')


# ── Narrow tool schemas ───────────────────────────────────

SCENE_AUTHOR_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'write_composition',
            'description': (
                'Write this scene\'s composition HTML. Overwrites any previous '
                'attempt. Call composition_check afterwards to verify it passes '
                'the static gate.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'html': {'type': 'string',
                             'description': 'The COMPLETE index.html document.'},
                },
                'required': ['html'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'composition_check',
            'description': (
                'Run the zero-LLM static gate on the composition written so far '
                '(contract fields, timeline key, determinism ban-list). Returns '
                'the error list — empty means it passes.'),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': ('Search the web for reference material — official '
                            'brand SVG logos, product screenshots, factual '
                            'detail for the on-screen text.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'fetch_url',
            'description': 'Fetch one URL (e.g. an official SVG asset page).',
            'parameters': {
                'type': 'object',
                'properties': {'url': {'type': 'string'}},
                'required': ['url'],
            },
        },
    },
]


# ── Prompt ────────────────────────────────────────────────

def _read_guide(name: str, limit: int = 12000) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'guide', name)
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()[:limit]
    except OSError as e:
        logger.warning('[SceneAuthor] cannot read guide %s: %s', name, e)
        return ''


def _build_prompt(scene: dict, *, width: int, height: int, duration: float,
                  scene_index: int, total_scenes: int) -> str:
    contract = _read_guide('COMPOSITION_CONTRACT.md')
    skeleton = _read_guide('skeleton.html', limit=6000)
    text = str(scene.get('text') or '').strip()
    visual = str(scene.get('visual') or '').strip()
    return (
        f'You are authoring ONE scene of a {total_scenes}-scene motion-graphics '
        f'video. Write a single self-contained HTML composition for it.\n\n'
        f'## This scene\n'
        f'- id: {scene.get("id")}\n'
        f'- index: {scene_index} of {total_scenes}\n'
        f'- EXACT duration: {duration} seconds (data-duration MUST be this)\n'
        f'- frame: {width}x{height} px\n'
        f'- narration (spoken over this scene): {text or "(none)"}\n'
        + (f'- visual direction: {visual}\n' if visual else '')
        + '\n## Hard requirements\n'
        '1. Call write_composition with the COMPLETE document, then '
        'composition_check. Iterate until the check returns no errors.\n'
        '2. The composition must satisfy the contract below exactly '
        '(data-composition-id / data-start / data-duration / data-width / '
        'data-height, ONE paused GSAP timeline registered on '
        'window.__timelines under the SAME key as data-composition-id).\n'
        '3. DETERMINISM: no Date.now(), Math.random(), performance.now(), '
        'requestAnimationFrame, setInterval, or infinite repeats.\n'
        '4. Visual quality is the point: typography hierarchy, staged reveals, '
        'motion that supports the narration. Do NOT just centre one line of '
        'text on a gradient — that is the fallback we are replacing.\n'
        '5. Everything must be inline/self-contained except the GSAP CDN '
        'script tag. No local asset files.\n'
        '6. Text must be HTML-escaped and fit inside the frame at the chosen '
        'font size.\n\n'
        f'## Composition contract\n{contract}\n\n'
        f'## Reference skeleton\n```html\n{skeleton}\n```\n'
    )


# ── Author one scene ──────────────────────────────────────

def author_scene(scene: dict, scene_dir: str, *, width: int, height: int,
                 duration: float, scene_index: int, total_scenes: int,
                 max_rounds: int = _DEFAULT_MAX_ROUNDS,
                 token_budget: int = _DEFAULT_TOKEN_BUDGET,
                 model: str | None = None,
                 abort_event=None) -> dict:
    """Author one scene's composition with a bounded agent loop.

    Returns ``{'ok', 'html', 'mode', 'rounds', 'tokens', 'detail'}`` where
    ``mode`` is ``'authored'`` (the agent's composition passed the static gate)
    or ``'template'`` (degraded — ``html`` is the zero-LLM template). NEVER
    raises: every failure path degrades, so one bad scene cannot take down the
    film.
    """
    from lib.agent_loop import AbortSignal, run_agent_loop
    from lib.motion_video._gates import check_composition_html
    from lib.motion_video._template import render_scene_html

    def _fallback(detail: str, *, rounds: int = 0, tokens: int = 0) -> dict:
        logger.info('[SceneAuthor] %s → template fallback (%s)',
                    scene.get('id'), detail)
        return {'ok': True, 'mode': 'template', 'rounds': rounds,
                'tokens': tokens, 'detail': detail,
                'html': render_scene_html(scene, width=width, height=height,
                                          duration=duration,
                                          scene_index=scene_index,
                                          total_scenes=total_scenes)}

    abort = (AbortSignal.from_event(abort_event) if abort_event is not None
             else AbortSignal.never())
    if abort.aborted:
        return _fallback('aborted before authoring')

    state = {'html': '', 'tokens': 0, 'gate_ok': False}
    messages = [{'role': 'user', 'content': _build_prompt(
        scene, width=width, height=height, duration=duration,
        scene_index=scene_index, total_scenes=total_scenes)}]

    def _dispatch(rnd, tools):
        from lib.llm_dispatch.api import dispatch_chat
        content, usage = dispatch_chat(
            messages, max_tokens=_MAX_TOKENS_PER_ROUND, temperature=0.3,
            tools=tools, prefer_model=model,
            log_prefix=f'[SceneAuthor:{scene.get("id")}:R{rnd}]')
        tool_calls = []
        if isinstance(usage, dict):
            state['tokens'] += int(usage.get('total_tokens') or 0)
            tool_calls = usage.get('_tool_calls') or []
        msg = {'role': 'assistant', 'content': content or None,
               'tool_calls': tool_calls}
        return msg, None, usage

    def _on_round(rnd, msg, finish, usage):
        # Cost cap (拍板 #3): once this scene's cumulative tokens exceed the
        # budget, stop asking for more rounds — the loop's abort seam is the
        # cheapest way to break out without a special-case return path.
        if state['tokens'] > token_budget:
            logger.warning('[SceneAuthor] %s hit the %d-token budget (%d used)'
                           ' — stopping', scene.get('id'), token_budget,
                           state['tokens'])
            state['budget_exhausted'] = True

    def _on_tool_round(rnd, msg):
        messages.append(msg)

    def _reply(tc_id: str, payload) -> None:
        messages.append({'role': 'tool', 'tool_call_id': tc_id,
                         'content': payload if isinstance(payload, str)
                         else json.dumps(payload, ensure_ascii=False)})

    def _execute(rnd, tc):
        fn = tc.get('function') or {}
        name = fn.get('name') or ''
        tc_id = tc.get('id', '')
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            _reply(tc_id, f'Invalid JSON arguments: {e}')
            return
        if not isinstance(args, dict):
            args = {}

        if name == 'write_composition':
            html = str(args.get('html') or '')
            if len(html) < 200:
                _reply(tc_id, 'Rejected: that is not a complete HTML document.')
                return
            state['html'] = html
            errors = check_composition_html(html)
            state['gate_ok'] = not errors
            _reply(tc_id, {'written_chars': len(html),
                           'gate_errors': errors[:8],
                           'passes_gate': not errors})
        elif name == 'composition_check':
            if not state['html']:
                _reply(tc_id, 'Nothing written yet — call write_composition first.')
                return
            errors = check_composition_html(state['html'])
            state['gate_ok'] = not errors
            _reply(tc_id, {'gate_errors': errors[:8], 'passes_gate': not errors})
        elif name == 'web_search':
            query = str(args.get('query') or '').strip()
            if not query:
                _reply(tc_id, 'Error: query is required')
                return
            try:
                from lib.tasks_pkg.handlers.search import _web_search_one
                results, _diag, _eb, _v = _web_search_one(
                    query, str(scene.get('text') or ''), '', vertical='off')
                from tofu_search.search import format_search_for_tool_response
                _reply(tc_id, format_search_for_tool_response(
                    results, query=query)[:12000])
            except Exception as e:
                logger.warning('[SceneAuthor] web_search failed: %s', e)
                _reply(tc_id, f'Search failed: {e}')
        elif name == 'fetch_url':
            url = str(args.get('url') or '').strip()
            if not url:
                _reply(tc_id, 'Error: url is required')
                return
            try:
                from lib.tasks_pkg.handlers.search import _fetch_url_one
                got = _fetch_url_one(url, str(scene.get('text') or ''),
                                     fetch_reason='scene asset')
                _reply(tc_id, (got.get('page_content')
                               or f'Failed to fetch {url}')[:12000])
            except Exception as e:
                logger.warning('[SceneAuthor] fetch_url failed: %s', e)
                _reply(tc_id, f'Fetch failed: {e}')
        else:
            _reply(tc_id, f'Unknown tool {name!r}')

    # The budget flag participates in the loop's abort seam so a blown budget
    # exits through the same path as a user Stop.
    combined = AbortSignal(lambda: abort.aborted or bool(state.get('budget_exhausted')))

    try:
        outcome = run_agent_loop(
            abort=combined, max_tool_rounds=max_rounds,
            round_tools=SCENE_AUTHOR_TOOLS, dispatch=_dispatch,
            execute_tool=_execute, on_round_result=_on_round,
            on_tool_round=_on_tool_round)
    except Exception as e:
        logger.error('[SceneAuthor] %s loop crashed: %s', scene.get('id'), e,
                     exc_info=True)
        return _fallback(f'author loop error: {type(e).__name__}: {e}',
                         tokens=state['tokens'])

    if not state['html']:
        return _fallback('author wrote no composition',
                         rounds=outcome.rounds, tokens=state['tokens'])
    errors = check_composition_html(state['html'])
    if errors:
        return _fallback('authored composition still fails the static gate: '
                         + '; '.join(str(e) for e in errors[:3]),
                         rounds=outcome.rounds, tokens=state['tokens'])

    logger.info('[SceneAuthor] %s authored in %d round(s), %d tokens',
                scene.get('id'), outcome.rounds, state['tokens'])
    return {'ok': True, 'mode': 'authored', 'html': state['html'],
            'rounds': outcome.rounds, 'tokens': state['tokens'], 'detail': ''}
