# HOT_PATH
"""Anthropic prompt caching — cache breakpoint annotation.

Public API:
  - add_cache_breakpoints(body, log_prefix='')
"""

from lib.env_compat import getenv_compat
from lib.log import get_logger
from lib.model_info import is_claude

logger = get_logger(__name__)

# ── Cache-marker capability matrix ──
# Empirically probed on the sankuai gateway (2026-05-03).
#   1. Needs markers       → claude, glm-5, qwen, deepseek
#   2. Auto-caches         → minimax, doubao (markers harmful)
#   3. Unknown             → default NO markers
_CACHE_MARKERS_HELP = ('glm-5', 'qwen', 'deepseek')

# Anthropic's hard ceiling: at most 4 ``cache_control`` markers per request.
_MAX_CACHE_BP = 4

# The conversation TAIL, the last TOOL definition, and (in a long agent loop)
# a MID-HISTORY stepping-stone each get a RESERVED marker the system phase can
# never consume. The tail marker covers the growing prefix and is the single
# highest-value segment — without it cache_creation is 0 every round and
# cache_read stays pinned at the static system+tools prefix, so the whole
# conversation body is re-billed uncached on every turn (conv mqj7x0t8:
# 138/151 rounds with cache_w=0).
#
# The system prompt is static and CONTIGUOUS (static prompt + optional
# memory-accumulation + optional swarm/parallel-execution blocks), so ONE
# marker on its LAST block caches the entire system prefix — spending 2-3
# markers on separate system blocks is pure waste that starves the mid anchor.
# So the system phase gets only the budget left after the tool/tail/mid
# reservations (min 1), and places its marker(s) on the LAST stable block(s).
_TAIL_RESERVED_BP = 1   # conversation tail (volatile, short TTL)
_TOOL_RESERVED_BP = 1   # last tool definition (stable)
_MID_RESERVED_BP = 1    # mid-history stepping-stone (long loops — see below)

# Anthropic's cache lookup only searches ~20 content blocks BACKWARD from a
# breakpoint for a prior cache entry to extend. In a tool loop emitting ~3
# blocks/turn, a lone rolling tail breakpoint loses sight of the previously
# written cache segment once the tail advances >20 blocks past it, after which
# the whole prefix is re-written every round even though the bytes are
# identical. A stepping-stone breakpoint that TRAILS the tail by a bounded,
# quantized distance keeps a prior cache entry permanently within the tail's
# 20-block lookback. Only armed once the TAIL is genuinely farther than the
# ~20-block window from the head — below that the head is still directly
# reachable from the rolling tail, so a mid anchor is wasted budget (and, if
# forced, would collapse onto the early user turn). See the arming gate below.
_MID_LOOKBACK = 20
# The stepping-stone trails the tail by ~_MID_TRAIL messages but is snapped to
# a COARSE-QUANTIZED absolute index (multiple of _MID_STEP) so it only ADVANCES
# IN JUMPS, not every round. A breakpoint that moved every round would always
# be a fresh write, never a read. Quantizing keeps message[mid_idx] byte-
# identical for a run of rounds (messages are append-only within a task) → it
# caches once, is read back for the next _MID_STEP rounds, then jumps forward.
# _MID_TRAIL < 20 so the stone stays inside the rolling tail's lookback window.
_MID_STEP = 8
_MID_TRAIL = 12


def _gateway_honors_cache_markers(model: str) -> bool:
    """Return True if attaching cache_control markers helps this model."""
    if is_claude(model):
        return True
    enabled = getenv_compat('TOFU_CACHE_MARKERS_NONCLAUDE', default='1')
    if enabled.strip().lower() in ('0', 'false', 'no', 'off', ''):
        return False
    lowered = (model or '').lower()
    return any(k in lowered for k in _CACHE_MARKERS_HELP)


def add_cache_breakpoints(body, log_prefix=''):
    """Add Anthropic-style ephemeral cache breakpoints with mixed TTL.

    Annotates up to ``_MAX_CACHE_BP`` (4) content blocks with cache_control.
    The last tool definition and the conversation tail each get a RESERVED
    marker (see ``_TOOL_RESERVED_BP`` / ``_TAIL_RESERVED_BP``); the system
    message gets only the markers left over after those reservations. This
    ordering guarantees the highest-value segments — the growing-prefix tail
    above all — can never be starved no matter how many cache-stable blocks
    the system message accumulates:
      1. System messages (static / memory / swarm blocks) — leftover budget
      2. Last tool definition — reserved
      3. Last message with content — the conversation tail — reserved

    Mixed TTL strategy (when CACHE_EXTENDED_TTL is enabled):
      - System + tools: ttl="1h" — stable content
      - Conversation tail: ttl="5m" (default) — changes every round
    """
    model = body.get('model', '')
    if not _gateway_honors_cache_markers(model):
        return

    _task_id = body.pop('_task_id', '')
    if _task_id:
        from lib.tasks_pkg.cache_tracking import latch_extended_ttl
        use_extended_ttl = latch_extended_ttl(_task_id)
    else:
        import lib as _lib
        use_extended_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)

    if not is_claude(model):
        use_extended_ttl = False

    if use_extended_ttl:
        _cc_stable = {'type': 'ephemeral', 'ttl': '1h'}
        _cc_tail   = {'type': 'ephemeral'}
    else:
        _cc_stable = {'type': 'ephemeral'}
        _cc_tail   = {'type': 'ephemeral'}

    messages = body.get('messages', [])

    # Phase 0: Strip ALL existing cache_control
    for i, msg in enumerate(messages):
        content = msg.get('content')
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and 'cache_control' in block:
                    content[j] = {k: v for k, v in block.items() if k != 'cache_control'}
    tools = body.get('tools')
    if tools:
        for t_idx, tool in enumerate(tools):
            fn = tool.get('function')
            if fn and 'cache_control' in fn:
                tools[t_idx] = {**tool,
                                'function': {k: v for k, v in fn.items() if k != 'cache_control'}}

    bp = 0

    # Reserve markers for the tool + tail (+ mid-anchor) phases up front, then
    # give the system phase only what's left. This guarantees those high-value
    # breakpoints can never be starved — adding another system block just drops
    # the extra system marker, it cannot steal the tool / tail / mid slot.
    # Reservations are conditional on those phases actually having something to
    # mark (no tools → the tool marker reverts to the system phase; short conv
    # → no mid anchor; this also matters for the system-only edge case).
    _nonsys_msgs = sum(1 for m in messages if m.get('role') != 'system')
    _n_sys = len(messages) - _nonsys_msgs
    # Arm the mid anchor only once the tail is genuinely FARTHER than the
    # lookback window from the head — a stone trailing the tail by _MID_TRAIL
    # must land safely past the head region (system + first user), else it
    # collapses onto the early user turn (which the anti-oscillation regression
    # forbids). Require the trail target to sit at least _MID_LOOKBACK messages
    # past the head.
    _head_floor = _n_sys + 1  # system block(s) + the first user turn
    _mid_target = len(messages) - _MID_TRAIL
    _mid_armed = _mid_target >= _head_floor + _MID_LOOKBACK
    _reserve = 0
    if body.get('tools'):
        _reserve += _TOOL_RESERVED_BP
    if len(messages) >= 2:
        _reserve += _TAIL_RESERVED_BP
    if _mid_armed:
        _reserve += _MID_RESERVED_BP
    _system_bp_budget = max(0, _MAX_CACHE_BP - _reserve)

    # Cache the system prefix, capped at the budget left after the reservations
    # above. The system prompt is CONTIGUOUS, so a marker on its LAST stable
    # block already caches everything before it — we place on the LAST N blocks
    # (N = budget), not the first, so a single leftover marker still covers the
    # whole system prefix rather than only the first block.
    for i, msg in enumerate(messages):
        if msg.get('role') != 'system' or bp >= _system_bp_budget:
            continue
        content = msg.get('content', '')
        if isinstance(content, str) and content.strip():
            messages[i] = {**msg, 'content': [
                {'type': 'text', 'text': content,
                 'cache_control': dict(_cc_stable)}
            ]}
            bp += 1
        elif isinstance(content, list) and content:
            _text_idxs = [j for j, blk in enumerate(content)
                          if isinstance(blk, dict) and blk.get('type') == 'text']
            _slots = _system_bp_budget - bp
            for blk_idx in _text_idxs[-_slots:] if _slots > 0 else []:
                content[blk_idx] = {**content[blk_idx],
                                    'cache_control': dict(_cc_stable)}
                bp += 1

    # Cache last tool definition (stable, extended TTL)
    tools = body.get('tools')
    if tools and bp < _MAX_CACHE_BP:
        fn = tools[-1].get('function')
        if fn:
            tools[-1] = {**tools[-1],
                         'function': {**fn, 'cache_control': dict(_cc_stable)}}
            bp += 1

    # Cache a stepping-stone breakpoint that TRAILS the tail by ~_MID_TRAIL
    # messages (quantized to _MID_STEP), keeping a prior cache entry inside the
    # rolling tail's 20-block lookback window (the 20-block-lookback fix). The
    # quantized absolute index advances in jumps, not every round — a marker
    # that moved every round would always be a fresh write, never a read.
    # Within a task messages are append-only, so message[mid_idx] stays
    # byte-identical for a run of rounds → caches once, then reads back.
    if _mid_armed and bp < _MAX_CACHE_BP:
        # Target index trails the tail; snap DOWN to a _MID_STEP multiple so it
        # only moves every _MID_STEP rounds. Floor it past the head region so it
        # can never collapse onto the system/first-user turn, and keep it before
        # the reserved tail (last ~3 msgs).
        _mid_idx = max(_head_floor, (_mid_target // _MID_STEP) * _MID_STEP)
        _placed_mid = False
        # Scan forward from the quantized index to the first markable message,
        # never entering the reserved tail zone (last 3 msgs) or the system head.
        for i in range(_mid_idx, len(messages) - 3):
            msg = messages[i]
            if msg.get('role') == 'system':
                continue
            content = msg.get('content', '')
            if isinstance(content, str) and content:
                messages[i] = {**msg, 'content': [
                    {'type': 'text', 'text': content,
                     'cache_control': dict(_cc_stable)}
                ]}
                bp += 1
                _placed_mid = True
                break
            elif isinstance(content, list) and content and isinstance(content[-1], dict):
                content[-1] = {**content[-1], 'cache_control': dict(_cc_stable)}
                bp += 1
                _placed_mid = True
                break
        if not _placed_mid and log_prefix:
            logger.debug('%s Cache: mid-history anchor not placed '
                         '(no markable msg near trail index %d)', log_prefix, _mid_idx)

    # Cache conversation tail (volatile, short TTL)
    if len(messages) >= 2 and bp < _MAX_CACHE_BP:
        _bp4_placed = False
        for _bp4_offset in range(1, min(6, len(messages))):
            idx = len(messages) - _bp4_offset
            if idx <= 0:
                break
            msg = messages[idx]
            if msg.get('role') == 'system':
                break
            content = msg.get('content', '')
            if isinstance(content, str) and content:
                messages[idx] = {**msg, 'content': [
                    {'type': 'text', 'text': content,
                     'cache_control': dict(_cc_tail)}
                ]}
                bp += 1
                _bp4_placed = True
                break
            elif isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    content[-1] = {**last, 'cache_control': dict(_cc_tail)}
                    bp += 1
                    _bp4_placed = True
                    break
        if not _bp4_placed and log_prefix:
            logger.debug('%s Cache: BP4 tail breakpoint could not be placed '
                         '(no message with content near tail)', log_prefix)

    if bp > 0 and log_prefix:
        _ttl_info = ' (mixed TTL: BP1-3=1h, BP4=5m)' if use_extended_ttl else ''
        logger.debug('%s Cache: %d breakpoint(s)%s', log_prefix, bp, _ttl_info)
