"""Promote references to this task's verified file outputs, never arbitrary paths."""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from lib.log import get_logger

logger = get_logger(__name__)


def _candidates(content, extensions):
    from markdown_it import MarkdownIt

    # Reject a tail where the delivery card would be swallowed by an unfinished
    # Markdown block. The empty probe stands in for the final native card.
    tokens = MarkdownIt('commonmark').parse(content + '\n\n```tofu-ui\n```\n')
    fence_line = content.replace('\r\n', '\n').replace('\r', '\n').count('\n') + 2
    tail = tokens[-1]
    if (tail.type != 'fence' or tail.level != 0 or tail.info != 'tofu-ui'
            or tail.map != [fence_line, fence_line + 2]):
        return {}
    paths = {}
    for token in tokens[:-1]:
        for child in token.children or ():
            if child.type != 'link_open':
                continue
            try:
                url = urlsplit(child.attrGet('href') or '')
                path = unquote(url.path, errors='strict')
            except (ValueError, UnicodeError):
                continue
            if (url.scheme or url.netloc or url.query or url.fragment or not path
                    or path.startswith('/') or '\\' in path or ':' in path
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)):
                continue
            parts = PurePosixPath(path).parts
            if (parts and not any(part.startswith('.') for part in parts)
                    and PurePosixPath(path).suffix.lower() in extensions):
                paths.setdefault('/'.join(parts), None)
    return paths


def append_workspace_output_links(task, *, finish_reason):
    """Supplement final presentation; failures never change the answer verdict."""
    cfg = task.get('config') or {}
    content = task.get('content') or ''
    if (finish_reason not in ('stop', 'end_turn') or not task.get('_attemptId')
            or not task.get('convId') or not task.get('id') or task.get('error')
            or task.get('aborted') or task.get('_transientRuntime')
            or task.get('_parentTaskId') or task.get('_flow_managed') or task.get('_vu_subtask')
            or task.get('_invocationEventSink') or cfg.get('planMode') is True
            or '[' not in content):
        return
    try:
        import lib
        from lib.desktop.remote import remote_worktree_binding
        if not getattr(lib, 'ARTIFACTS_ENABLED', True) or remote_worktree_binding(cfg):
            return
        policy = task.get('_workspaceRootPolicy') or {}
        root = policy.get('primaryPath')
        if not root or not any(row.get('path') == root and row.get('access') == 'rw'
                               for row in policy.get('roots', {}).values()):
            return
        from lib.artifacts.response_files import contract, publish_file
        from lib.project_mod.modifications import task_output_fingerprints
        from lib.tasks_pkg.manager._registry import task_user_id
        paths = _candidates(content, contract()['x-policy']['extensions'])
        if not paths:
            return
        fingerprints = task_output_fingerprints(root, task['id'], task['convId'], paths)
        owner = task_user_id(task)
        blocks = []
        abort_event = task.get('abort_event')

        def cancelled():
            return bool(task.get('aborted') or (abort_event and abort_event.is_set()))

        for path, fingerprint in fingerprints.items():
            if cancelled():
                return
            try:
                receipt = publish_file(path, conv_id=task['convId'], user_id=owner,
                                       roots=[root], task_id=task['id'], cancelled=cancelled,
                                       expected_output_digest=fingerprint)
            except (OSError, ValueError):
                continue  # Missing, changed, cancelled, or not a supported file.
            blocks.append({'type': 'action_panel', 'id': f'output-{len(blocks)}',
                           'title': receipt['filename'], 'actions': [
                               {'kind': 'open_artifact', 'label': 'Open', 'targetId': receipt['id']},
                               {'kind': 'download_artifact', 'label': 'Download', 'targetId': receipt['id']},
                           ]})
        if blocks and not cancelled():
            # JSON is a single line, so path/title text cannot terminate the fence.
            payload = json.dumps({'version': 1, 'title': 'Workspace files', 'blocks': blocks},
                                 ensure_ascii=True, separators=(',', ':'))
            if task.get('content') == content:
                task['content'] = content + '\n\n```tofu-ui\n' + payload + '\n```\n'
    except Exception as error:
        logger.debug('Workspace output delivery unavailable: %s', type(error).__name__)
