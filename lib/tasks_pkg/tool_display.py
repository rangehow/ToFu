"""Tool-call display helpers — build tool-round entries and tool_start events.

Extracted from ``orchestrator.py`` to keep the main run-loop module focused on
orchestration logic.  The public entry-point is :func:`_build_tool_round_entry`;
the per-tool ``_tool_display_*`` helpers are internal to this module.
"""

from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.scheduler import SCHEDULER_TOOL_NAMES
from lib.memory import MEMORY_TOOL_NAMES
from lib.tasks_pkg.executor import SWARM_TOOL_NAMES
from lib.tools import (
    BROWSER_TOOL_NAMES,
    CODE_EXEC_TOOL_NAMES,
    CONV_REF_TOOL_NAMES,
    EMIT_TO_USER_TOOL_NAMES,
    IMAGE_GEN_TOOL_NAMES,
    PROJECT_TOOL_NAMES,
)

# ── Tool round entry dispatch ─────────────────────────────────────────
#  Instead of a massive if/elif chain, we use a dispatch dict pattern.
#  Each handler returns (display_str, extra_fields_dict).


def _tool_display_web_search(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for web_search tool calls.

    For batch mode (``queries`` array), every candidate search term is
    rendered IN FULL with a newline between terms — the frontend renders
    line breaks so long queries wrap naturally instead of being elided.
    The structured list is also exposed via ``_batchQueries`` for any UI
    that wants to render it differently.
    """
    queries = fn_args.get('queries')
    if queries and isinstance(queries, list):
        n = len(queries)
        full_list = []
        for s in queries:
            if isinstance(s, dict):
                q = s.get('query', '?') or '?'
            elif isinstance(s, str):
                q = s.strip() or '?'
            else:
                q = '?'
            full_list.append(q)
        # One query per line so the frontend wraps long terms instead of
        # squashing them onto one elided line. Indent each line with "• "
        # so the count header reads naturally.
        lines = '\n'.join(f'• {q}' for q in full_list)
        display = f'{n} searches:\n{lines}'
        return display, {
            'toolName': 'web_search',
            '_display_query': display,
            '_batchQueries': full_list,
        }
    query = fn_args.get('query', '')
    return query, {'toolName': 'web_search'}


def _short_url(url, max_len=60):
    """Return a human-friendly short URL: hostname + path (truncated).

    For URLs like ``https://github.com/org/repo``, the hostname alone
    (``github.com``) loses important context.  This helper keeps the
    path prefix so users can distinguish different pages on the same host.

    Args:
        url: Full URL string.
        max_len: Maximum character length for the result.

    Returns:
        Shortened URL string, e.g. ``github.com/org/repo``.
    """
    try:
        p = urlparse(url)
    except Exception:
        return url[:max_len]
    host = p.netloc or ''
    path = (p.path or '').rstrip('/')
    # Drop trivial index paths
    if path in ('', '/'):
        return host
    short = host + path
    if len(short) <= max_len:
        return short
    # Truncate path, keeping the beginning which is most informative
    avail = max_len - len(host) - 1  # 1 for the ellipsis '…'
    if avail > 5:
        return host + path[:avail] + '…'
    # Fallback: just hostname
    return host


def _tool_display_fetch_url(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for fetch_url tool calls.

    For batch mode (``urls`` array), every URL is rendered IN FULL on its
    own line so users can see exactly what the model is fetching — long
    URLs wrap rather than being elided.  The full list is also exposed via
    ``_batchUrls`` for structured rendering.
    """
    urls = fn_args.get('urls')
    if urls and isinstance(urls, list):
        n = len(urls)
        full_list = []
        for s in urls:
            if isinstance(s, dict):
                u = s.get('url', '?') or '?'
            elif isinstance(s, str):
                u = s.strip() or '?'
            else:
                u = '?'
            full_list.append(u)
        lines = '\n'.join(f'• {u}' for u in full_list)
        display = f'📄 {n} URLs:\n{lines}'
        return display, {
            'toolName': 'fetch_url',
            '_display_query': display,
            '_batchUrls': full_list,
        }
    target_url = fn_args.get('url', '')
    is_pdf_hint = target_url.lower().rstrip('/').endswith('.pdf')
    short = _short_url(target_url)
    display_query = f'{"📑 PDF" if is_pdf_hint else "🌐"} {short}'
    return f'📄 {target_url}', {'toolName': 'fetch_url', '_display_query': display_query}


def _tool_display_code_exec(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for standalone code execution tool calls."""
    from lib.project_mod import project_tool_display
    display = project_tool_display(fn_name, fn_args)
    return display, {'toolName': 'code_exec'}


def _tool_display_project(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for project tool calls."""
    from lib.project_mod import project_tool_display
    display = project_tool_display(fn_name, fn_args)
    return display, {'toolName': fn_name}


def _tool_display_browser(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for browser tool calls (basic + advanced)."""
    from lib.browser import browser_tool_display
    display = browser_tool_display(fn_name, fn_args)
    return display, {'toolName': fn_name}


def _tool_display_memory(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for memory management tool calls."""
    if fn_name == 'create_memory':
        display = f"💡 Saving memory: {fn_args.get('name', '?')}"
    elif fn_name == 'update_memory':
        display = f"✏️ Updating memory: {fn_args.get('memory_id', '?')}"
    elif fn_name == 'delete_memory':
        display = f"🗑️ Deleting memory: {fn_args.get('memory_id', '?')}"
    elif fn_name == 'merge_memories':
        ids = fn_args.get('memory_ids', [])
        display = f"🔀 Merging {len(ids)} memories → {fn_args.get('name', '?')}"
    elif fn_name == 'search_memories':
        query = fn_args.get('query', '')
        display = f"🔍 Searching memories: {query[:80]}" if query else "🔍 Searching memories"
    else:
        display = f"💡 {fn_name}"
    return display, {'toolName': fn_name}


def _tool_display_conv_ref(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for conversation reference tool calls."""
    icon = '📋' if fn_name == 'list_conversations' else '💬'
    kw = fn_args.get('keyword', 'all') if fn_name == 'list_conversations' else fn_args.get('conversation_id', '?')[:8]
    display = f"{icon} {fn_name}: {kw}"
    return display, {'toolName': fn_name}


def _tool_display_scheduler(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for scheduler tool calls."""
    return f"⏰ {fn_name}", {'toolName': fn_name}


def _tool_display_desktop(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for desktop tool calls."""
    return f"🖥️ {fn_name}", {'toolName': fn_name}


def _tool_display_swarm(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for swarm tool calls."""
    if fn_name == 'spawn_agents':
        n_agents = len(fn_args.get('agents', [])) if isinstance(fn_args, dict) else 0
        display = f"Spawning {n_agents} agent{'s' if n_agents != 1 else ''}…" if n_agents else "Spawning agents…"
    else:
        display = fn_name.replace('_', ' ').title()
    return display, {'toolName': fn_name, '_swarm': True}


def _tool_display_compact(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for context_compact tool calls."""
    return '🗜️ Compacting context…', {'toolName': fn_name}


def _tool_display_image_gen(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for image generation tool calls."""
    prompt = fn_args.get('prompt', '…')[:80]
    return f'🎨 Generating: {prompt}', {'toolName': 'generate_image'}




def _tool_display_human_guidance(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for ask_human tool calls.

    ★ No hard 80-char cap on the question text — the frontend word-wraps
    and users explicitly requested "incomplete displays are not allowed".
    A very generous soft cap (2000 chars) still protects against a
    pathological 100 KB prompt bloating every SSE event.
    """
    _FULL_LIMIT = 2000
    question = fn_args.get('question', '…') or '…'
    if len(question) > _FULL_LIMIT:
        question = question[:_FULL_LIMIT - 1] + '…'
    response_type = fn_args.get('response_type', 'free_text')
    icon = '🗳️' if response_type == 'choice' else '🙋'
    return f'{icon} {question}', {'toolName': 'ask_human'}


def _tool_display_emit_to_user(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for emit_to_user tool calls."""
    comment = fn_args.get('comment', '…')[:80]
    return f'📤 Emit: {comment}', {'toolName': 'emit_to_user'}


def _tool_display_tool_search(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for tool_search (deferred tool discovery) calls."""
    query = fn_args.get('query', '…')[:80]
    return f'🔍 Searching tools: {query}', {'toolName': 'tool_search'}


# Keys from fn_args that identify the *resource* the call is operating on
# (the inner "what" — a file, a section, a new name, an issue, …).
# Ordered by priority; the first match wins as the resource label.
_MCP_RESOURCE_KEYS = (
    'file_path',      # overleaf/github — which file
    'path',           # github/hope_dfs_ls — which file/dir
    'name',           # create_project / create_branch — new-resource name
    'title',          # issue/PR title
    'issue_number',   # github — which issue
    'pull_number',    # github — which PR
    'query',          # search tools
    'q',              # github search_* q=
    'keyword',        # xuecheng_search — search keyword
    'url',            # fetch_url-like tools
    'branch',         # github — branch name
    'doc',            # xuecheng_* — doc id or full collabpage URL
    'app_id',         # hope_fetch_source_code / hope_get_status (appid variant)
    'appid',          # hope_get_status / hope_change_priority
    'job_id',         # hope_mlp_job_* — psx… job id
    'job_ids',        # hope_mlp_job_info — comma-separated
    'run_id',         # hope_mlp_run_* — MLP run id
    'runid',          # hope_get_status / hope_stop_job (legacy spelling)
    'session_id',     # hope_stop_session
    'pod_name',       # hope_mlp_log_files / hope_mlp_log_get
    'queue_name',     # hope_mlp_queue_*
    'queue',          # hope_list_resource / hope_mlp_run_jobs
    'key',            # hope_get_lion_config
    'template_id',    # xuecheng_get_template_markdown / create_doc
    'space_id',       # xuecheng_get_space_root_docs
    'memory_id',      # memory tools (defensive — also in _tool_display_memory)
)

# Keys that identify the *container* the call is scoped to (the outer
# "where" — a project, a repo, an owner/repo pair). Rendered after
# the resource label with a ``@`` separator so the title line reads
# e.g. ``acl.sty @ 69f21…cca7`` or ``create_issue #123 @ torvalds/linux``.
_MCP_CONTAINER_KEYS = (
    'project_id',     # overleaf — 24-hex ID (shortened)
    'repo',           # github — paired with owner below
    'cluster',        # hope_mlp_log_get — paired with namespace below
    'namespace',      # hope_mlp_log_files (fallback when cluster missing)
)

# Per-segment length cap for resource + container in the title line.
_MCP_SEG_MAX = 40


# ── Overleaf project-name cache ──────────────────────────────────────
# Maps 24-hex project_id → human-readable project name. Populated
# opportunistically by inspecting the results of ``list_projects`` /
# ``status_summary`` / ``create_project`` calls (see lib.mcp.project_names).
# Consulted at title-line render time so users see
# ``… @ [EMNLP Demo] Tofu`` instead of ``… @ 69f21…cca7``.

def _resolve_project_name(pid: str) -> str:
    """Look up a cached human-readable Overleaf project name by its ID.

    Returns '' when no cached name is available — callers should fall back
    to the short ID form. The cache is filled by
    :mod:`lib.mcp.project_names` as MCP tool calls complete.
    """
    try:
        from lib.mcp.project_names import get_project_name
        return get_project_name(pid) or ''
    except Exception:
        return ''


def _short_project_id(pid: str) -> str:
    """Format a 24-hex Overleaf project_id for display.

    Prefers the cached human-readable name (``[EMNLP Demo] Tofu``) when
    available; falls back to the compact ``prefix…suffix`` form so users
    can still tell two unknown projects apart.
    """
    s = str(pid).strip()
    if len(s) == 24 and all(c in '0123456789abcdef' for c in s.lower()):
        name = _resolve_project_name(s)
        if name:
            # Cap the project name to keep the title line readable.
            if len(name) > _MCP_SEG_MAX:
                name = name[:_MCP_SEG_MAX - 1] + '…'
            return name
        return f'{s[:5]}…{s[-4:]}'
    return s


_KM_DOC_RE = __import__('re').compile(r'/(?:collabpage|page)/(\d+)')


def _resolve_doc_title(content_id: str) -> str:
    """Look up a cached Xuecheng doc title; return '' when absent."""
    try:
        from lib.mcp.project_names import get_doc_title
        return get_doc_title(content_id) or ''
    except Exception:
        return ''


def _short_doc_id(val) -> str:
    """Format a Xuecheng ``doc`` arg for display.

    Prefers the cached human-readable title (harvested from prior
    ``xuecheng_read_doc`` / ``xuecheng_search`` / ``xuecheng_create_document``
    calls) over the bare content id, falling back to the numeric id when
    no title is known. Accepts either a bare id or a full collabpage URL.
    """
    if val is None:
        return ''
    s = str(val).strip()
    if not s:
        return ''
    cid = ''
    m = _KM_DOC_RE.search(s)
    if m:
        cid = m.group(1)
    elif s.isdigit():
        cid = s
    if cid:
        title = _resolve_doc_title(cid)
        if title:
            if len(title) > _MCP_SEG_MAX:
                title = title[:_MCP_SEG_MAX - 1] + '…'
            return title
        return cid
    return s


def _short_job_id(val: str) -> str:
    """Format a long Hope MLP job id for display.

    Hope's ``psx``-prefixed instance ids are ~32 chars and dominate the
    title line. Keep prefix + suffix so users can still distinguish two
    concurrent jobs.
    """
    s = str(val).strip()
    if len(s) > 24 and (s.startswith('psx') or '-' in s):
        return f'{s[:8]}…{s[-6:]}'
    return s


def _render_mcp_arg(key: str, val) -> str:
    """Render a single MCP fn_args value as a short display string.

    Applies per-key formatting: 24-hex project_ids are shortened, issue/PR
    numbers get a leading ``#``, ``doc`` URLs are reduced to their numeric
    id, long Hope job ids are shortened, strings are stripped + truncated.
    """
    if val is None:
        return ''
    s = str(val).strip()
    if not s:
        return ''
    if key == 'project_id':
        s = _short_project_id(s)
    elif key == 'doc':
        s = _short_doc_id(s)
    elif key in ('job_id', 'app_id', 'appid'):
        s = _short_job_id(s)
    elif key == 'job_ids':
        # comma-separated list: shorten each, cap to first 2
        parts = [_short_job_id(p) for p in s.split(',') if p.strip()]
        if len(parts) > 2:
            s = f'{parts[0]}, {parts[1]} +{len(parts) - 2} more'
        else:
            s = ', '.join(parts) or s
    if key in ('issue_number', 'pull_number'):
        s = f'#{s}'
    if len(s) > _MCP_SEG_MAX:
        s = s[:_MCP_SEG_MAX - 1] + '…'
    return s


def _mcp_arg_suffix(fn_args):
    """Compose a title-line suffix that surfaces BOTH the resource being
    operated on AND the container it lives in, when both apply.

    Examples:
      ``acl.sty @ 69f21…cca7``          (overleaf/create_file)
      ``main.tex @ 69f21…cca7``         (overleaf/edit_file)
      ``My Paper``                      (overleaf/create_project)
      ``@ 69f21…cca7``                  (overleaf/list_files — container only)
      ``torvalds/linux#123``            (github issue-specific call)
      ``issue title @ torvalds/linux``  (github/create_issue)

    Falls back to empty string when no informative arg is present.
    """
    if not isinstance(fn_args, dict):
        return ''

    # ── Container label ────────────────────────────────────────────────
    container = ''
    # github owner+repo pair takes priority over bare ``repo``
    if 'owner' in fn_args and 'repo' in fn_args:
        container = f"{fn_args.get('owner', '?')}/{fn_args.get('repo', '?')}"
        if len(container) > _MCP_SEG_MAX:
            container = container[:_MCP_SEG_MAX - 1] + '…'
    elif fn_args.get('cluster') and fn_args.get('namespace'):
        # Hope log endpoints scope a pod by cluster + namespace
        container = f"{fn_args['cluster']}/{fn_args['namespace']}"
        if len(container) > _MCP_SEG_MAX:
            container = container[:_MCP_SEG_MAX - 1] + '…'
    else:
        for key in _MCP_CONTAINER_KEYS:
            if key in fn_args:
                rendered = _render_mcp_arg(key, fn_args[key])
                if rendered:
                    container = rendered
                    break

    # ── Resource label ─────────────────────────────────────────────────
    resource = ''
    for key in _MCP_RESOURCE_KEYS:
        if key in fn_args:
            rendered = _render_mcp_arg(key, fn_args[key])
            if rendered:
                resource = rendered
                break

    # Special enrichment: overleaf update_section/get_section_content pass
    # both ``file_path`` + ``section_title`` — show both as ``file › section``
    # so users see the exact path being touched.
    sect = fn_args.get('section_title')
    if resource and sect and isinstance(sect, str) and sect.strip():
        sect_short = sect.strip()
        if len(sect_short) > _MCP_SEG_MAX:
            sect_short = sect_short[:_MCP_SEG_MAX - 1] + '…'
        resource = f'{resource} › {sect_short}'

    # ── Compose ────────────────────────────────────────────────────────
    # issue/PR number: if the resource label is a #number, and we have an
    # owner/repo container, render as ``owner/repo#N`` (no "@") — that's
    # the conventional form and more compact.
    if resource.startswith('#') and container and '/' in container:
        return f'{container}{resource}'

    if resource and container:
        return f'{resource} @ {container}'
    if resource:
        return resource
    if container:
        # Bare container (no per-call item) — used by list_files /
        # list_history / compile_project / status_summary. Prefix with
        # a tiny scope glyph so it reads naturally without the ``—``.
        return container
    return ''


def _tool_display_mcp(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for MCP bridge tool calls (mcp__server__tool).

    Surfaces the most informative arg (file_path, name, section_title, short
    project_id, owner/repo, …) after the tool name so users can tell at a
    glance which file / project / resource the call is operating on —
    instead of seeing a uniform ``🔌 overleaf/create_file`` for every write.
    """
    from lib.mcp.types import parse_namespaced_name
    parsed = parse_namespaced_name(fn_name)
    if parsed:
        server_name, tool_name = parsed
        head = f'🔌 {server_name}/{tool_name}'
    else:
        head = f'🔌 {fn_name}'
    suffix = _mcp_arg_suffix(fn_args)
    display = f'{head} — {suffix}' if suffix else head
    return display, {'toolName': fn_name}


def _tool_display_generic(fn_name, fn_args, tc_id, tc_args_str):
    """Catch-all display info for unknown/future tools."""
    # Check if this is an MCP tool before falling through to generic
    from lib.mcp.types import MCP_TOOL_PREFIX
    if fn_name.startswith(MCP_TOOL_PREFIX):
        return _tool_display_mcp(fn_name, fn_args, tc_id, tc_args_str)
    logger.warning('[Orchestrator] Unregistered tool %s — using generic round_entry. This tool may need a dedicated display handler.', fn_name)
    return f"🔧 {fn_name}", {'toolName': fn_name}


# ══════════════════════════════════════════════════════════════════════
#  Module-level dispatch table (hoisted from _build_tool_round_entry)
# ══════════════════════════════════════════════════════════════════════
# This dict is built once at module load time instead of being rebuilt on
# every call.  The only runtime-dynamic part is CODE_EXEC_TOOL_NAMES
# which depends on the ``project_enabled`` flag — that is handled inside
# _build_tool_round_entry with a cheap conditional override.

def _build_display_dispatch_table():
    """Build the static tool-name → handler dispatch table.

    Called once at module load time.  Returns the dict.
    """
    table = {}

    # Direct name matches
    table['web_search'] = _tool_display_web_search
    table['fetch_url'] = _tool_display_fetch_url
    table['context_compact'] = _tool_display_compact

    # Code exec tools — default to project handler (overridden at call
    # time when project is disabled).
    for name in CODE_EXEC_TOOL_NAMES:
        table.setdefault(name, _tool_display_project)

    # Project tools
    for name in PROJECT_TOOL_NAMES:
        table.setdefault(name, _tool_display_project)

    # ★ read_files — global tool (not in PROJECT_TOOL_NAMES), uses same
    #   project-style display rendering (🔍 / 📂 / 📄 + path + lines).
    table.setdefault('read_files', _tool_display_project)

    # Browser tools (basic + advanced)
    for name in BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser
    for name in ADVANCED_BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser

    # Memory tools
    for name in MEMORY_TOOL_NAMES:
        table[name] = _tool_display_memory

    # Conversation reference tools
    for name in CONV_REF_TOOL_NAMES:
        table[name] = _tool_display_conv_ref

    # Scheduler tools
    for name in SCHEDULER_TOOL_NAMES:
        table[name] = _tool_display_scheduler

    # Desktop tools
    for name in DESKTOP_TOOL_NAMES:
        table[name] = _tool_display_desktop

    # Swarm tools
    for name in SWARM_TOOL_NAMES:
        table[name] = _tool_display_swarm

    # Image generation tools
    for name in IMAGE_GEN_TOOL_NAMES:
        table[name] = _tool_display_image_gen

    # Human guidance tool
    table['ask_human'] = _tool_display_human_guidance

    # Emit-to-user terminal tool
    for name in EMIT_TO_USER_TOOL_NAMES:
        table[name] = _tool_display_emit_to_user

    # Deferred tool discovery
    table['tool_search'] = _tool_display_tool_search

    return table


# Hoisted constant — built once at import time.
_TOOL_DISPLAY_DISPATCH = _build_display_dispatch_table()


def _build_tool_round_entry(fn_name, fn_args, tc_id, tc_args_str, tool_round_num,
                             project_enabled):
    """Build a tool-round entry and tool_start event payload for a tool call.

    Uses a module-level dispatch table (``_TOOL_DISPLAY_DISPATCH``) instead of
    rebuilding a dict on every call.  The only runtime override is for
    CODE_EXEC_TOOL_NAMES when ``project_enabled`` is False — those get
    redirected to ``_tool_display_code_exec``.

    Returns (new_tool_round_num, round_entry, event_payload).
    """
    # ── Runtime override: code-exec tools display differently when project
    #    mode is off (standalone code execution vs. project tool).
    if not project_enabled and fn_name in CODE_EXEC_TOOL_NAMES:
        handler = _tool_display_code_exec
    else:
        handler = _TOOL_DISPLAY_DISPATCH.get(fn_name, _tool_display_generic)

    try:
        display_query, extra = handler(fn_name, fn_args, tc_id, tc_args_str)
    except Exception as e:
        logger.warning('[ToolDisplay] handler for %s raised: %s', fn_name, e)
        display_query = f'🔧 {fn_name}'
        extra = {'toolName': fn_name}

    tool_round_num += 1
    rn = tool_round_num

    # Build round_entry
    round_entry = {
        'roundNum': rn,
        'query': display_query,
        'results': None,
        'status': 'searching',
        'toolCallId': tc_id,
        'toolArgs': tc_args_str,
    }
    round_entry.update(extra)

    # Build tool_start event — same fields + type
    event = {
        'type': 'tool_start',
        'roundNum': rn,
        'query': extra.get('_display_query', display_query),
        'toolCallId': tc_id,
        'toolArgs': tc_args_str,
    }
    # Copy relevant extra fields into event (toolName, _swarm, etc.)
    for k, v in extra.items():
        if not k.startswith('_display_'):
            event[k] = v

    return tool_round_num, round_entry, event
