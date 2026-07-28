"""lib/motion_video/_scene_author.py — Per-scene composition author (P5).

The quality jump over the zero-LLM slide template
(docs/PRODUCTION_PIPELINE_DESIGN.md §2.2 stage 5): each scene gets its OWN
bounded agent loop that writes a bespoke ``index.html`` composition, instead
of every scene rendering as "gradient background + one centred line".

Design constraints (all deliberate):

  * **Narrow toolset.** The author sees only ``write_composition`` (the HTML
    is returned as an argument — there is deliberately no filesystem write
    tool, so a composition can never reference a local asset path),
    ``web_search`` / ``fetch_url`` (for real logos / reference material, which
    are pasted in as INLINE SVG markup), and ``composition_check``.
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

Default ON (owner 2026-07-27). The zero-LLM template cannot pass the
renderer's own lint and reads as "text flying around with no formatting", so
it is the FALLBACK, never the default deliverable. A job opts OUT per-job
(``task['scene_author'] = False``) or globally via
``TOFU_MOTION_SCENE_AUTHOR=0`` (the emergency kill switch — authoring spends
one agent loop per scene).
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


#: Values of ``TOFU_MOTION_SCENE_AUTHOR`` that force authoring OFF fleet-wide.
_ENV_OFF = ('0', 'false', 'no', 'off')
#: …and the ones that force it ON (redundant with the default, kept so the
#: variable reads in BOTH directions rather than being a one-way switch).
_ENV_ON = ('1', 'true', 'yes', 'on')


def scene_author_enabled(task: dict | None = None) -> bool:
    """True when per-scene authoring is switched on for this job.

    **This is the single source of the default** — every entry point that
    spawns a motion job (the ``produce_video`` tool, ``POST /api/v1/motion/
    videos``, the paper Video-studio panel, the crash-resume scanner) reads
    the answer from here. A caller that wants the fast template path says so
    explicitly; a caller that says nothing gets an authored film.

    Resolution order (mirrors ``rows_write_enabled()``'s convention):

      1. ``task['scene_author']`` when the job stated a preference;
      2. ``TOFU_MOTION_SCENE_AUTHOR`` — honoured in BOTH directions, so ``0``
         is an emergency fleet-wide kill switch (authoring costs one agent
         loop per scene) and ``1`` pins it on;
      3. **ON.** Owner 2026-07-27: the zero-LLM template does not pass the
         renderer's own lint, so it must not be what a user gets by default.

    Deliberately NOT done: defaulting per call site. Four construction sites
    exist and the two that nobody thought about (paper panel, bare REST POST)
    are exactly the ones a user actually reaches — a per-caller default is a
    copy that silently stops matching.
    """
    if isinstance(task, dict) and task.get('scene_author') is not None:
        return bool(task['scene_author'])
    env = os.environ.get('TOFU_MOTION_SCENE_AUTHOR', '').strip().lower()
    if env in _ENV_OFF:
        return False
    if env in _ENV_ON:
        return True
    return True


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
                'Run the full static gate on the composition written so far: '
                'the contract/determinism checks PLUS the real renderer gates '
                '(lint = fonts + contract, validate = headless-Chrome runtime '
                'errors + WCAG contrast, inspect = text overflowing its '
                'container across the timeline). Returns the error list — '
                'empty means it passes.'),
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


def _full_gate(html: str, scene_dir: str, *, abort_event=None) -> list[str]:
    """Regex gate + the THREE REAL gates (lint / validate / inspect).

    The regex gate (:func:`~lib.motion_video._gates.check_composition_html`)
    only sees the contract fields and the determinism ban-list — it cannot see
    an unresolvable font, a WCAG contrast failure, a runtime console error, or
    text spilling its container. Those are exactly the defects that read as
    "no formatting", so the author must be judged on them BEFORE a render is
    spent. Findings are returned as plain strings for the repair prompt.

    When the CLI is unavailable the real gates report ``env_missing``; that is
    NOT a composition defect, so it degrades to the regex verdict rather than
    failing the scene.
    """
    from lib.motion_video._gates import check_composition_html

    errors = list(check_composition_html(html))
    if errors:
        return errors  # contract broken — no point booting Chrome
    try:
        from lib.motion_video._render import check_project
        with open(os.path.join(scene_dir, 'index.html'), 'w',
                  encoding='utf-8') as f:
            f.write(html)
        res = check_project(scene_dir, abort_event=abort_event)
    except Exception as e:
        logger.warning('[SceneAuthor] real gates unavailable: %s', e)
        return []
    if res.get('category') in ('env_missing', 'aborted', 'timeout', 'chrome'):
        logger.info('[SceneAuthor] real gates skipped (%s)', res.get('category'))
        return []
    out = [str(e) for e in res.get('errors', [])]
    out += [f'{h}' for h in res.get('fix_hints', []) if h]
    return out


def _build_prompt(scene: dict, *, width: int, height: int, duration: float,
                  scene_index: int, total_scenes: int) -> str:
    contract = _read_guide('COMPOSITION_CONTRACT.md')
    craft = _read_guide('MOTION_CRAFT.md')
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
        '4. Visual quality is the point. Pick ONE archetype from the craft '
        'guide below that fits this beat, then build it with a real type '
        'hierarchy (eyebrow / headline / caption at clearly different sizes), '
        'STAGGERED entrances, and at least one supporting graphic (rule, bar, '
        'number, icon or divider). A single centred line on a gradient is the '
        'fallback we are replacing and is not acceptable output.\n'
        '5. Self-contained: the only external reference may be the GSAP CDN '
        'script tag. Do NOT write or link local asset files — instead, when '
        'you fetch a real brand/product SVG, PASTE ITS <svg> MARKUP INLINE '
        'into the document (inline SVG is self-contained, deterministic and '
        'strongly preferred over a generic icon or a text-only card).\n'
        '6. Text must be HTML-escaped and fit inside the frame at the chosen '
        'font size.\n\n'
        f'## Composition contract\n{contract}\n\n'
        f'## Motion craft — how to make this LOOK designed\n{craft}\n\n'
        f'## Reference skeleton (a STARTING POINT, not the target quality)\n'
        f'```html\n{skeleton}\n```\n'
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
            errors = _full_gate(html, scene_dir, abort_event=abort_event)
            state['gate_ok'] = not errors
            _reply(tc_id, {'written_chars': len(html),
                           'gate_errors': errors[:8],
                           'passes_gate': not errors})
        elif name == 'composition_check':
            if not state['html']:
                _reply(tc_id, 'Nothing written yet — call write_composition first.')
                return
            errors = _full_gate(state['html'], scene_dir,
                                abort_event=abort_event)
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
    errors = _full_gate(state['html'], scene_dir, abort_event=abort_event)
    if errors:
        return _fallback('authored composition still fails the static gate: '
                         + '; '.join(str(e) for e in errors[:3]),
                         rounds=outcome.rounds, tokens=state['tokens'])

    logger.info('[SceneAuthor] %s authored in %d round(s), %d tokens',
                scene.get('id'), outcome.rounds, state['tokens'])
    return {'ok': True, 'mode': 'authored', 'html': state['html'],
            'rounds': outcome.rounds, 'tokens': state['tokens'], 'detail': ''}
