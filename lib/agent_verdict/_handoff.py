"""lib/agent_verdict/_handoff.py — Sentinels, tool sets, and structured-token
parsers for the agent-loop decision logic.

Carries the small pieces that classify_verdict and the autopilot loop consume:

  * the state-changing ("deliverable") tool sets;
  * the virtual-user completion sentinel (``VU_DONE_SENTINEL``);
  * the virtual-user HANDOFF sentinel + parser (``_VU_HANDOFF_RE`` /
    ``parse_vu_handoff``) — imported DIRECTLY by autopilot.py;
  * the replan kill-switch (``replan_enabled``);
  * the state-changing tool-round counter (``count_state_changing_rounds``);
  * the structured ``[PROGRESS: resolved=X remaining=Y]`` parser
    (``_PROGRESS_RE`` / ``parse_progress``) — used both by classify_verdict's
    backend-authoritative gate and by the diminishing-returns ledger.

Each regex lives WITH the function that uses it.  Pure logic — imports only
``lib.log`` and ``lib.env_compat``.
"""

from __future__ import annotations

import re

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  State-changing ("deliverable") tools
# ══════════════════════════════════════════════════════════

# Calls to these tools are what we count as real work; everything else
# (list_dir, read_files, grep_search, find_files, web_search, fetch_url, …)
# is exploration.
#
# ``code_exec`` is deliberately NOT a member here: endpoint's round counter
# special-cases it (a code_exec round carries a different toolName), so the
# membership test must NOT match it.  Callers that count from a flat list of
# tool names — and therefore have no special-casing — should use
# :data:`STATE_CHANGING_TOOLS_WITH_CODE_EXEC` instead.
STATE_CHANGING_TOOLS = frozenset({
    'write_file',
    'apply_diff',
    'apply_diffs',
    'insert_content',
    'insert_contents',
    'run_command',
    'create_project',
    'generate_image',
})

# Same set plus ``code_exec`` — for callers (e.g. the orchestration engine's
# flat-tool-name snapshot) that do not special-case code_exec separately.
STATE_CHANGING_TOOLS_WITH_CODE_EXEC = STATE_CHANGING_TOOLS | {'code_exec'}


# ══════════════════════════════════════════════════════════
#  Autopilot virtual-user completion sentinel
# ══════════════════════════════════════════════════════════

# A virtual_user emits this verbatim when it judges the task finished.
# Used by autopilot's role prompt + done check, and by the engine's
# virtual_user verdict inversion.
VU_DONE_SENTINEL = '[VU: TASK_DONE]'


# ══════════════════════════════════════════════════════════
#  Autopilot virtual-user HANDOFF (park-on-board) sentinel
# ══════════════════════════════════════════════════════════

# A virtual_user emits ``[VU: HANDOFF paths=<p1>,<p2>]`` when the objective's
# remaining acceptance criteria are BLOCKED on an EXTERNAL commit the assistant
# cannot itself resolve (a sibling conversation must land a file first). This
# is the THIRD terminal verdict — distinct from TASK_DONE (met) and keep-going
# (unmet + actionable in-conversation): the run is done in this conversation,
# but the residual is parked onto the project board's wait-on-path primitive so
# it auto-resumes when the dependency lands. See lib/tasks_pkg/autopilot.py
# ``_conclude_handoff``.
#
# The ``paths=`` value follows the SAME structured-token contract as the board's
# ``_parse_sibling_wait_paths`` (comma-separated, whitespace ends the token) so
# the two never diverge — free-text scraping is forbidden.
_VU_HANDOFF_RE = re.compile(
    r'\[VU:\s*HANDOFF(?:\s+paths?=(\S+))?\s*\]', re.IGNORECASE)


def parse_vu_handoff(text: str):
    """Parse a ``[VU: HANDOFF paths=a,b]`` sentinel from a virtual-user reply.

    Returns
    -------
    list | None
        ``None`` when NO handoff sentinel is present (distinct from an empty
        list). A list of paths when the sentinel is present — ``[]`` for a bare
        ``[VU: HANDOFF]`` with no path token (still a handoff signal). Paths are
        comma-separated; the value ends at the first whitespace run (trailing
        prose is never consumed); de-duped, order-preserving.

    Pure + side-effect-free.
    """
    m = _VU_HANDOFF_RE.search(text or '')
    if m is None:
        return None
    raw = m.group(1)
    if not raw:
        return []
    out = []
    for p in raw.split(','):
        s = p.strip()
        if s and s not in out:
            out.append(s)
    return out


# ══════════════════════════════════════════════════════════
#  Replan kill-switch
# ══════════════════════════════════════════════════════════

def replan_enabled() -> bool:
    """Replan kill-switch: ``TOFU_ENDPOINT_REPLAN=0`` disables CONTINUE_PLANNER.

    When disabled, a ``planner`` phase is downgraded to ``worker`` so the
    redesign can be hot-disabled without a code rollback.  Defaults to
    enabled (``'1'``).  Documented in CLAUDE.md §9.
    """
    return getenv_compat('TOFU_ENDPOINT_REPLAN', default='1').strip() != '0'


# ══════════════════════════════════════════════════════════
#  State-changing tool round counter
# ══════════════════════════════════════════════════════════

def count_state_changing_rounds(tool_rounds) -> tuple:
    """Count state-changing vs exploratory tool rounds in a single worker turn.

    Parameters
    ----------
    tool_rounds : list[dict] | None
        ``task['toolRounds']`` snapshot — each entry has ``toolName``.

    Returns
    -------
    (int, int, list[str])
        ``(state_changing_count, exploratory_count, state_changing_tool_names)``.
        ``state_changing_tool_names`` preserves order + duplicates so the
        deliverables snapshot can show "apply_diff×2, write_file".

    ``code_exec`` rounds (whose ``toolName`` differs — see executor.py) are
    treated as state-changing.
    """
    if not tool_rounds:
        return 0, 0, []

    state_changing_names: list[str] = []
    exploratory_count = 0

    for entry in tool_rounds:
        if not isinstance(entry, dict):
            continue
        name = entry.get('toolName') or entry.get('tool_name') or ''
        if name == 'code_exec':
            state_changing_names.append('code_exec')
            continue
        if name in STATE_CHANGING_TOOLS:
            state_changing_names.append(name)
        else:
            exploratory_count += 1

    return len(state_changing_names), exploratory_count, state_changing_names


# ══════════════════════════════════════════════════════════
#  Structured [PROGRESS: resolved=X remaining=Y] parser
# ══════════════════════════════════════════════════════════

_PROGRESS_RE = re.compile(
    r'\[PROGRESS:\s*resolved\s*=\s*(\d+)\s*(?:,|;|\s)\s*remaining\s*=\s*(\d+)\s*\]',
    re.IGNORECASE,
)


def parse_progress(text: str):
    """Extract the structured ``[PROGRESS: resolved=X remaining=Y]`` line.

    Returns ``(resolved, remaining)`` as ints, or ``(None, None)`` when no
    parseable line is present (the guard then fails open — it cannot conclude
    no-progress without the hard signal).  Uses the LAST match if the VU
    emitted more than one.
    """
    last = None
    for m in _PROGRESS_RE.finditer(text or ''):
        last = m
    if last is None:
        return None, None
    try:
        return int(last.group(1)), int(last.group(2))
    except (ValueError, TypeError) as e:
        logger.debug('[Verdict] parse_progress: non-int PROGRESS values (%s) — '
                     'failing open', e)
        return None, None
