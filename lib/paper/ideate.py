"""lib/paper/ideate.py — breakthrough-idea discovery + anti-"A+B" gate (R3).

The auto-research recipe's stage 4 (docs/AUTO_RESEARCH_SYSTEM_DESIGN.md §3 阶段 4)
and its intellectual core: turn R2's library-verified open-gap map into
**genuinely novel** research ideas, and — the hard part — KILL the "A+B stitching"
ideas that a plain LLM produces by the dozen.

The gate is what makes this not a toy. It runs three passes on every idea, in
cost order (free structural checks first, LLM judging last):

  Gate ① — zero-LLM structural gate (:func:`_structural_gate`)
    Required fields non-empty (linked_gap_id / why_not_AB / core_mechanism /
    falsifiable_prediction / kind), and — the load-bearing rule — the idea's
    ``linked_gap_id`` MUST hit a real ``open_gaps[].id`` from R2. An idea that
    solves no library-verified gap is invalid: it invented its own problem, and
    a problem nobody has is exactly where A+B stitching hides. (Owner pin #2.)

  Gate ② — forced-neighbor-retrieval novelty (:func:`_novelty_prior_set`)
    Owner pin #1: **novelty is measured against a RETRIEVED neighbor set, not
    against what the model chose to cite.** Before judging, we ALWAYS run
    ``search_arxiv(title + core_mechanism)`` and pull the top-K unconditionally
    into the prior set (merged with the model's self-reported prior_art). The
    judge compares the mechanism against the CLOSEST retrieved paper and must
    label the delta mechanism-level vs parameter-level; a parameter-level delta
    (new dataset / new backbone / two modules glued) caps the novelty axis. A
    model can hand-pick three old papers and claim it beats them all while the
    actually-closest arXiv:XXXX it never mentioned sinks it — this pass is what
    catches that.

  Gate ③ — four-axis rubric (:func:`_score_idea`, mirrors insight `_rubric`)
    novelty / falsifiability / mechanism_depth / value, each 1-5, mean must
    clear ``IDEATE_GATE_THRESHOLD`` (a single tunable constant, initial 4.0 to
    match insight — 宁缺毋滥). Grounding (recommend engine) strips ideas whose
    novelty rests on a hallucinated paper.

Every rejected idea — with its four-axis scores and the reason from EACH gate —
is preserved in the result's ``rejected`` list (and persisted under the
``ideate:<lang>`` report key) so the threshold can be calibrated from real
rejection distributions rather than guessed. (Owner requirement.)

Seams (facade discipline, same as survey/insight): ``dispatch_stream``,
``search_arxiv``, ``fetch_arxiv_title`` are resolved THROUGH this module so a
test patches ``ideate.search_arxiv`` etc. and it bites every gate — the whole
thing is testable with zero network and zero real LLM.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'generate_ideas', 'ideate_lang_key', 'IDEATE_GATE_THRESHOLD',
    'IDEATE_NOVELTY_RETRIEVAL_K', 'RUBRIC_AXES',
    '_structural_gate', '_novelty_prior_set', '_ground_idea_prior_art',
]

# ── Tunables (all single named constants — calibrate, never hardcode in logic) ──

# Four-axis mean an idea must clear to survive. 4.0 matches insight's headroom
# gate (宁缺毋滥). A SINGLE constant so the owner can calibrate from the real
# rejection distribution without touching gate logic; tests must NOT hardcode
# this value (they drive scores relative to it).
IDEATE_GATE_THRESHOLD = 4.0

# How many neighbors search_arxiv MUST contribute to the novelty prior set,
# unconditionally, regardless of what the model self-reported. Owner pin #1:
# novelty = f(retrieved set). K>=5.
IDEATE_NOVELTY_RETRIEVAL_K = 5

# Generation divergence — mirror insight's 0.45 (divergent but JSON-safe).
_IDEATE_TEMPERATURE = 0.45
_IDEATE_MAX_TOKENS = 8000
_MAX_IDEATE_TOOL_ROUNDS = 6

# Rubric judging is a judgement, not creative — deterministic.
_RUBRIC_TEMPERATURE = 0.0
_RUBRIC_MAX_TOKENS = 3000

_IDEATE_LANG_PREFIX = 'ideate'

RUBRIC_AXES = ('novelty', 'falsifiability', 'mechanism_depth', 'value')

# Required non-empty fields on every idea (gate ①).
_REQUIRED_FIELDS = ('title', 'kind', 'linked_gap_id', 'core_mechanism',
                    'novelty_claim', 'falsifiable_prediction', 'why_not_AB')
_VALID_KINDS = ('methodology', 'analysis')


def ideate_lang_key(lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted ideate pass.

    ``ideate:<lang>`` — a separate row from survey/report/insight, mirroring the
    established composite-key convention."""
    return f'{_IDEATE_LANG_PREFIX}:{lang or "en"}'


# ── id normalization (shared discipline) ───────────────────────────────────

def _norm_id(arxiv_id) -> str:
    """Strip a version suffix so ``2502.09992v3`` and ``2502.09992`` compare equal."""
    return (arxiv_id or '').split('v')[0].strip()


# ── Facade seams (monkeypatchable) ─────────────────────────────────────────

def dispatch_stream(*args, **kwargs):
    """Facade → real dispatcher. Patched by tests as ``ideate.dispatch_stream``."""
    from lib.llm_dispatch import dispatch_stream as _ds
    return _ds(*args, **kwargs)


def search_arxiv(query, max_results=10):
    """Facade → arXiv search. Patched by tests as ``ideate.search_arxiv``.
    This is the neighbor-retrieval seam gate ② depends on."""
    from lib.paper.arxiv import search_arxiv as _sa
    return _sa(query, max_results=max_results)


def fetch_arxiv_title(arxiv_id):
    """Facade → arXiv title verify. Patched by tests as ``ideate.fetch_arxiv_title``."""
    from lib.paper.arxiv import fetch_arxiv_title as _ft
    return _ft(arxiv_id)


# ── Gate ①: zero-LLM structural gate ───────────────────────────────────────

def _structural_gate(idea: dict, valid_gap_ids: set) -> Optional[str]:
    """Return a rejection reason string if the idea fails the structural gate,
    else None (passes).

    Owner pin #2 — the load-bearing rule is ``linked_gap_id ∈ valid_gap_ids``:
    an idea must attach to a real, library-verified open gap from R2. An idea
    that invents its own problem is invalid (that is where A+B stitching hides).
    All checks are pure — no LLM, no network — so this runs first and free.
    """
    if not isinstance(idea, dict):
        return 'not a dict'
    for f in _REQUIRED_FIELDS:
        v = idea.get(f)
        if not (isinstance(v, str) and v.strip()):
            return f'missing/empty required field: {f}'
    if idea.get('kind') not in _VALID_KINDS:
        return f'invalid kind: {idea.get("kind")!r} (must be one of {_VALID_KINDS})'
    gid = (idea.get('linked_gap_id') or '').strip()
    if gid not in valid_gap_ids:
        return (f'linked_gap_id {gid!r} does not match any library-verified '
                f'open_gap (invented problem — no evidence it solves a real gap)')
    # novelty_claim must cite at least one concrete arxiv-shaped id (not "nobody did this")
    import re
    if not re.search(r'\d{4}\.\d{4,5}|[a-z-]+/\d{7}', idea.get('novelty_claim') or ''):
        return 'novelty_claim cites no concrete arxiv id (unfalsifiable "nobody did this")'
    return None


# ── Gate ②: forced-neighbor-retrieval novelty prior set ────────────────────

def _novelty_prior_set(idea: dict, *, k: int = IDEATE_NOVELTY_RETRIEVAL_K) -> dict:
    """Build the neighbor set the novelty judge MUST use (owner pin #1).

    ALWAYS runs ``search_arxiv(title + core_mechanism)`` and takes the top-k
    hits unconditionally — this is the retrieved evidence the judge compares
    against, NOT the model's self-reported prior_art. The two are merged (so
    the model's picks still count) but the retrieved set can never be empty just
    because the model chose to cite nothing damaging.

    Returns::

        {
          'retrieved': [ {arxiv_id, title, summary}, ... ],   # from search_arxiv
          'self_reported': ['id', ...],                        # idea.prior_art (normalized)
          'merged_ids': ['id', ...],                           # union, normalized
          'retrieval_query': str,
        }
    """
    import lib.paper.ideate as _self
    title = (idea.get('title') or '').strip()
    mech = (idea.get('core_mechanism') or '').strip()
    query = f'{title} {mech}'.strip()
    retrieved = []
    try:
        hits = _self.search_arxiv(query, max_results=max(k, IDEATE_NOVELTY_RETRIEVAL_K)) or []
        for h in hits[:k]:
            aid = _norm_id(h.get('arxiv_id'))
            if aid:
                retrieved.append({'arxiv_id': aid, 'title': h.get('title') or '',
                                  'summary': (h.get('summary') or '')[:400]})
    except Exception as e:
        logger.warning('[Paper:Ideate] novelty retrieval failed for %.60s: %s', title, e)
    self_reported = [_norm_id(x) for x in (idea.get('prior_art') or []) if _norm_id(x)]
    merged = list(dict.fromkeys([r['arxiv_id'] for r in retrieved] + self_reported))
    logger.info('[Paper:Ideate] novelty prior set for %.50s — retrieved=%d self=%d merged=%d',
                title, len(retrieved), len(self_reported), len(merged))
    return {'retrieved': retrieved, 'self_reported': self_reported,
            'merged_ids': merged, 'retrieval_query': query}


# ── Gate ②/③: the LLM judge (novelty + four-axis rubric in one dispatch) ────

def _parse_llm_json(content):
    from lib.llm.json_extract import extract_first_json_object
    return extract_first_json_object(content, log_prefix='[Paper:Ideate]', log=logger)


def _coerce_score(v):
    """Clamp a rubric score to an int in [1,5]; None on garbage (mirrors insight)."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(1, min(5, n))


def _judge_prompt(idea: dict, prior_set: dict, gap: dict, lang: str) -> str:
    """Prompt for the combined novelty + four-axis judgement.

    Feeds the RETRIEVED neighbor set (not the model's self-report) as the
    comparison basis, and demands an explicit mechanism-level vs
    parameter-level verdict against the closest retrieved paper."""
    zh = (lang or 'en').startswith('zh')
    neigh_lines = []
    for r in prior_set.get('retrieved', []):
        neigh_lines.append(f"- arXiv:{r['arxiv_id']}  {r['title']}\n    {r['summary']}")
    neighbors = '\n'.join(neigh_lines) or '(retrieval returned nothing)'
    gap_txt = f"{gap.get('id')}: {gap.get('gap','')}" if gap else '(no linked gap)'
    idea_txt = (f"title: {idea.get('title')}\nkind: {idea.get('kind')}\n"
                f"core_mechanism: {idea.get('core_mechanism')}\n"
                f"novelty_claim: {idea.get('novelty_claim')}\n"
                f"falsifiable_prediction: {idea.get('falsifiable_prediction')}\n"
                f"why_not_AB: {idea.get('why_not_AB')}")
    if zh:
        head = (
            '你是一位严格的审稿人,正在判定一个研究 idea 是否真正新颖,还是「A+B 缝合」。\n'
            '**新颖性只能相对下面【检索到的近邻集合】判定,不许用 idea 自报的 prior_art 糊弄。**\n'
            '尤其要对比 idea 的 core_mechanism 与近邻里【最接近的那一篇】:差异是\n'
            '**机理级(mechanism-level,新的原理/机制)还是参数级(parameter-level,换数据集/\n'
            '换 backbone/两个已有模块拼接)**?parameter-level 增量的 novelty 轴最高只能给 2。\n')
        axes = ('四轴打分(1-5 整数):novelty(相对检索近邻的新颖度)、falsifiability(预测可否证伪)、'
                'mechanism_depth(是否解释了为什么而非只描述做什么)、value(若成立是否真解决 linked gap)。')
        fmt = ('只返回一个 JSON:{"mechanism_delta":"mechanism-level|parameter-level",'
               '"closest_neighbor":"arxiv_id","scores":{"novelty":n,"falsifiability":n,'
               '"mechanism_depth":n,"value":n},"justifications":{轴:一句话},"verdict":"一句话"}')
    else:
        head = (
            'You are a strict reviewer judging whether a research idea is genuinely novel '
            'or an "A+B stitch".\n'
            '**Judge novelty ONLY against the RETRIEVED NEIGHBORS below — do NOT let the '
            "idea's self-reported prior_art off the hook.**\n"
            "Compare the idea's core_mechanism against the CLOSEST retrieved paper: is the "
            'delta **mechanism-level (a new principle/mechanism) or parameter-level (new '
            'dataset / new backbone / two existing modules glued together)**? A '
            'parameter-level delta caps the novelty axis at 2.\n')
        axes = ('Score four axes (integers 1-5): novelty (vs retrieved neighbors), '
                'falsifiability, mechanism_depth (explains WHY not just WHAT), value '
                '(does it truly solve the linked gap).')
        fmt = ('Return ONLY one JSON: {"mechanism_delta":"mechanism-level|parameter-level",'
               '"closest_neighbor":"arxiv_id","scores":{"novelty":n,"falsifiability":n,'
               '"mechanism_depth":n,"value":n},"justifications":{axis:one-line},"verdict":"one-line"}')
    return (f'{head}\n{axes}\n\n## LINKED GAP\n{gap_txt}\n\n## RETRIEVED NEIGHBORS '
            f'(the ONLY novelty basis)\n{neighbors}\n\n## IDEA\n{idea_txt}\n\n{fmt}')


def _score_idea(idea: dict, prior_set: dict, gap: dict, lang: str, *,
                model=None, abort=None) -> Optional[dict]:
    """Judge one idea: novelty-vs-retrieved + four-axis rubric, one dispatch.

    Returns::
        {'scores': {axis: 1-5}, 'overall': float, 'mechanism_delta': str,
         'closest_neighbor': str, 'justifications': {...}, 'verdict': str,
         'novelty_capped': bool}
    or None on dispatch/parse failure. ``overall`` is recomputed here — never
    trust the model's arithmetic. A parameter-level mechanism_delta forces the
    novelty axis to min(score, 2) (the cap is applied HERE, deterministically,
    not left to the model).
    """
    import lib.paper.ideate as _self
    from lib.agent_loop import AbortSignal
    from lib.llm_errors import AbortedError
    _dispatch_stream = _self.dispatch_stream

    abort_signal = AbortSignal.from_callback(abort)
    buf = {'content': ''}

    def _on_content(t):
        buf['content'] += t

    try:
        msg, _finish, _usage = _dispatch_stream(
            [{'role': 'user', 'content': _judge_prompt(idea, prior_set, gap, lang)}],
            on_content=_on_content, abort_check=abort_signal.is_set,
            prefer_model=model or None, strict_model=bool(model), capability='text',
            max_tokens=_RUBRIC_MAX_TOKENS, temperature=_RUBRIC_TEMPERATURE,
            thinking_enabled=False, log_prefix='[Paper:Ideate:Judge]')
    except AbortedError:
        raise
    except Exception as e:
        logger.error('[Paper:Ideate] judge dispatch failed: %s', e, exc_info=True)
        return None

    content = buf['content'] or (msg.get('content') if isinstance(msg, dict) else '') or ''
    parsed = _parse_llm_json(content)
    if not isinstance(parsed, dict):
        logger.warning('[Paper:Ideate] unparseable judge reply for %.50s', idea.get('title'))
        return None

    raw = parsed.get('scores') if isinstance(parsed.get('scores'), dict) else {}
    scores = {}
    for axis in RUBRIC_AXES:
        s = _coerce_score(raw.get(axis))
        if s is not None:
            scores[axis] = s
    if not scores:
        logger.warning('[Paper:Ideate] no valid axis scores for %.50s', idea.get('title'))
        return None

    delta = (parsed.get('mechanism_delta') or '').strip().lower()
    novelty_capped = False
    if delta == 'parameter-level' and scores.get('novelty', 0) > 2:
        # Deterministic cap — do not let a parameter-level stitch score high novelty.
        scores['novelty'] = 2
        novelty_capped = True
        logger.info('[Paper:Ideate] novelty capped to 2 (parameter-level delta) for %.50s',
                    idea.get('title'))

    overall = round(sum(scores.values()) / len(scores), 2)
    justifs = parsed.get('justifications') if isinstance(parsed.get('justifications'), dict) else {}
    return {
        'scores': scores, 'overall': overall, 'mechanism_delta': delta,
        'closest_neighbor': _norm_id(parsed.get('closest_neighbor')),
        'justifications': {k: str(v) for k, v in justifs.items()},
        'verdict': (parsed.get('verdict') or '').strip(),
        'novelty_capped': novelty_capped,
    }


# ── Grounding (recommend engine, facade-resolved) ──────────────────────────

def _ground_idea_prior_art(idea: dict) -> tuple:
    """Ground every arXiv id in the idea's prior_art; return (grounded, dropped).

    Reuses the arXiv seam. An id that cannot be grounded is removed from
    prior_art and counted — an idea whose novelty rests on hallucinated papers
    should not survive on their strength. Mutates ``idea['prior_art']`` in place.
    """
    import lib.paper.ideate as _self
    grounded, dropped = [], 0
    for raw in (idea.get('prior_art') or []):
        aid = _norm_id(raw)
        if not aid:
            continue
        try:
            title = _self.fetch_arxiv_title(aid)
        except Exception as e:
            logger.debug('[Paper:Ideate] grounding lookup failed for %s: %s', aid, e)
            title = ''
        if title:
            grounded.append(aid)
        else:
            dropped += 1
            logger.debug('[Paper:Ideate] dropped ungrounded prior_art id %s', aid)
    idea['prior_art'] = grounded
    return grounded, dropped


# ── Generation (facade-resolved agentic loop, mirrors survey) ──────────────

def _generate_raw_ideas(direction, open_gaps, reader_context, lang, *,
                        n_ideas=6, model=None, abort=None, on_tool_event=None) -> list:
    """Generate raw (un-gated) ideas anchored to R2's open_gaps.

    Single seam tests monkeypatch (``ideate._generate_raw_ideas``) to drive the
    gate pipeline offline. Mirrors insight/survey synthesis: run_agent_loop +
    _REPORT_TOOLS, facade-resolved dispatch."""
    import lib.paper.ideate as _self
    from lib.agent_loop import AbortSignal, run_agent_loop
    from lib.paper.prompts import _REPORT_TOOLS, date_anchor_clause
    from lib.paper.tools import make_research_tool_executor

    system = date_anchor_clause(lang) + _ideate_system_prompt(lang, n_ideas)
    gaps_json = _compact_gaps(open_gaps)
    parts = [f'## RESEARCH DIRECTION\n{direction}']
    if reader_context:
        parts.append(reader_context)
    parts.append('## OPEN GAPS (from the survey — every idea MUST link to one of these ids)\n'
                 + gaps_json)
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': '\n\n---\n\n'.join(parts)}]
    abort_signal = AbortSignal.from_callback(abort)
    _round = {'content': ''}
    _last = {'msg': None}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(t):
            _round['content'] += t
        return _self.dispatch_stream(
            messages, on_content=_on_content, abort_check=abort_signal.is_set,
            prefer_model=model or None, strict_model=bool(model), capability='text',
            tools=tools, max_tokens=_IDEATE_MAX_TOKENS, temperature=_IDEATE_TEMPERATURE,
            thinking_enabled=False, log_prefix='[Paper:Ideate]')

    def _on_round_result(rnd, msg, finish, usage):
        _last['msg'] = msg

    def _begin_tool_round(rnd, msg):
        _round['content'] = ''
        messages.append(msg)

    from lib.paper.report_engine import _execute_report_tool
    _execute_tool = make_research_tool_executor(
        messages, user_question=direction[:300], abort_signal=abort_signal,
        execute_report_tool=_execute_report_tool, on_tool_event=on_tool_event,
        log_prefix='[Paper:Ideate]')

    run_agent_loop(
        abort=abort_signal, max_tool_rounds=_MAX_IDEATE_TOOL_ROUNDS,
        round_tools=_REPORT_TOOLS, dispatch=_dispatch, execute_tool=_execute_tool,
        on_round_result=_on_round_result, on_tool_round=_begin_tool_round)

    content = _round['content']
    if not content and isinstance(_last['msg'], dict):
        content = _last['msg'].get('content') or ''
    parsed = _parse_llm_json(content)
    if isinstance(parsed, dict) and isinstance(parsed.get('ideas'), list):
        return parsed['ideas']
    if isinstance(parsed, list):
        return parsed
    logger.warning('[Paper:Ideate] generation produced no idea list')
    return []


def _compact_gaps(open_gaps: dict) -> str:
    """Compact the open_gaps map to just the fields the generator needs."""
    import json
    gaps = []
    for g in (open_gaps or {}).get('open_gaps', []):
        if isinstance(g, dict) and g.get('id'):
            gaps.append({'id': g['id'], 'gap': g.get('gap', ''),
                         'why_open': g.get('why_open', ''),
                         'kind_hint': g.get('kind_hint', '')})
    return json.dumps({'open_gaps': gaps}, ensure_ascii=False)


def _valid_gap_ids(open_gaps: dict) -> set:
    """The set of real open_gap ids an idea may link to (gate ① basis)."""
    ids = set()
    for g in (open_gaps or {}).get('open_gaps', []):
        if isinstance(g, dict) and (g.get('id') or '').strip():
            ids.add(g['id'].strip())
    return ids


def _gap_by_id(open_gaps: dict, gid: str) -> Optional[dict]:
    for g in (open_gaps or {}).get('open_gaps', []):
        if isinstance(g, dict) and g.get('id') == gid:
            return g
    return None


def _ideate_system_prompt(lang: str, n_ideas: int) -> str:
    zh = (lang or 'en').startswith('zh')
    if zh:
        return (
            f'你是一位有品位的资深研究员,正在为一个方向提出 {n_ideas} 个**真正新颖**的研究 idea。\n'
            '铁律:每个 idea 必须 `linked_gap_id` 指向给定 open_gaps 里的一个真实 id —— '
            '**不许凭空发明问题**。core_mechanism 要讲清「为什么 work」的机理,不是操作步骤;'
            'why_not_AB 要正面回答「为什么这不是两个已有件的拼接」;novelty_claim 要引用具体 arxiv_id。'
            f'只返回 JSON:{{"ideas":[{{...}} × {n_ideas}]}},每个 idea 含 title/kind/linked_gap_id/'
            'core_mechanism/novelty_claim/prior_art/falsifiable_prediction/why_not_AB。')
    return (
        f'You are a tasteful senior researcher proposing {n_ideas} GENUINELY NOVEL ideas '
        'for a direction.\n'
        'Iron rule: every idea MUST set linked_gap_id to a real id from the given open_gaps '
        '— do NOT invent your own problem. core_mechanism must explain WHY it works (a '
        'mechanism, not steps); why_not_AB must directly answer why this is not two existing '
        'pieces glued; novelty_claim must cite a concrete arxiv_id. '
        f'Return ONLY JSON: {{"ideas":[{{...}} × {n_ideas}]}}, each idea with title/kind/'
        'linked_gap_id/core_mechanism/novelty_claim/prior_art/falsifiable_prediction/why_not_AB.')


# ── Public entry ───────────────────────────────────────────────────────────

def generate_ideas(direction: str, open_gaps: dict, *, lang: str = 'en',
                   reader_context: str = '', n_ideas: int = 6,
                   threshold: Optional[float] = None, model: Optional[str] = None,
                   abort: Optional[Callable[[], bool]] = None,
                   on_tool_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Generate ideas from R2's open-gap map and run the anti-A+B gate.

    Args:
        direction: the research direction.
        open_gaps: the library-verified gap map from R2 (survey.build_survey's
            ``open_gaps`` — schema_version 1). Its ``open_gaps[].id`` set is the
            basis for the structural gate.
        lang / reader_context / n_ideas / model / abort / on_tool_event: as usual.
        threshold: override IDEATE_GATE_THRESHOLD (calibration only; defaults to
            the module constant).

    Returns:
        {
          'ok': bool,
          'direction': str, 'lang': str,
          'accepted': [ {idea..., 'scores', 'overall', 'mechanism_delta',
                         'closest_neighbor', 'prior_set_ids', ...}, ... ],
          'rejected': [ {idea..., 'reject_stage': 'structural'|'grounding'|'rubric',
                         'reject_reason', 'scores'?, 'overall'?}, ... ],
          'threshold': float,
          'error': str,
        }
    Every rejected idea keeps its scores + the gate that killed it, so the
    threshold can be calibrated from real data.
    """
    import lib.paper.ideate as _self
    direction = (direction or '').strip()
    thr = IDEATE_GATE_THRESHOLD if threshold is None else float(threshold)
    valid_gaps = _valid_gap_ids(open_gaps)
    if not direction:
        return {'ok': False, 'error': 'empty direction', 'direction': '', 'lang': lang,
                'accepted': [], 'rejected': [], 'threshold': thr}
    if not valid_gaps:
        return {'ok': False, 'error': 'open_gaps has no verified gap ids (run survey first)',
                'direction': direction, 'lang': lang, 'accepted': [], 'rejected': [],
                'threshold': thr}

    try:
        raw = _self._generate_raw_ideas(direction, open_gaps, reader_context, lang,
                                        n_ideas=n_ideas, model=model, abort=abort,
                                        on_tool_event=on_tool_event)
    except Exception as e:
        from lib.llm_errors import AbortedError
        if isinstance(e, AbortedError):
            return {'ok': False, 'error': 'aborted', 'direction': direction, 'lang': lang,
                    'accepted': [], 'rejected': [], 'threshold': thr}
        logger.error('[Paper:Ideate] generation failed: %s', e, exc_info=True)
        return {'ok': False, 'error': f'generation failed: {e}', 'direction': direction,
                'lang': lang, 'accepted': [], 'rejected': [], 'threshold': thr}

    accepted, rejected = [], []
    for idea in raw:
        if not isinstance(idea, dict):
            continue
        # Gate ① — structural (free, first)
        reason = _structural_gate(idea, valid_gaps)
        if reason:
            rejected.append({**idea, 'reject_stage': 'structural', 'reject_reason': reason})
            logger.info('[Paper:Ideate] REJECT(structural) %.50s — %s', idea.get('title'), reason)
            continue
        # Grounding — strip hallucinated prior_art
        _grounded, dropped = _ground_idea_prior_art(idea)
        # Gate ② — forced-neighbor retrieval prior set
        prior_set = _novelty_prior_set(idea)
        # Gate ③ — four-axis rubric judged against the RETRIEVED set
        gap = _gap_by_id(open_gaps, idea.get('linked_gap_id'))
        verdict = _score_idea(idea, prior_set, gap, lang, model=model, abort=abort)
        if verdict is None:
            rejected.append({**idea, 'reject_stage': 'rubric',
                             'reject_reason': 'judge failed/unparseable',
                             'prior_set_ids': prior_set['merged_ids']})
            continue
        record = {**idea, **verdict, 'prior_set_ids': prior_set['merged_ids'],
                  'retrieval_query': prior_set['retrieval_query'],
                  'prior_art_dropped': dropped}
        if verdict['overall'] >= thr:
            accepted.append(record)
            logger.info('[Paper:Ideate] ACCEPT %.50s overall=%.2f', idea.get('title'),
                        verdict['overall'])
        else:
            rejected.append({**record, 'reject_stage': 'rubric',
                             'reject_reason': f'overall {verdict["overall"]:.2f} < threshold {thr}'})
            logger.info('[Paper:Ideate] REJECT(rubric) %.50s overall=%.2f < %.2f',
                        idea.get('title'), verdict['overall'], thr)

    logger.info('[Paper:Ideate] done — direction=%.60s generated=%d accepted=%d rejected=%d thr=%.2f',
                direction, len(raw), len(accepted), len(rejected), thr)
    return {'ok': True, 'direction': direction, 'lang': lang, 'accepted': accepted,
            'rejected': rejected, 'threshold': thr, 'error': ''}
