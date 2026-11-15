"""lib/mcp/vendored.py — registry of vendored internal MCP servers.

Deliberately TINY and dependency-free (stdlib only). Both the runtime
auto-installer (``lib/mcp/client.py``) and the release-time vendoring script
(``scripts/vendor_mcp.sh``) import ``VENDORED_LAUNCHERS`` from here, so the
list of internal servers lives in exactly ONE place. Keeping it in its own
module means the script can read the registry without importing the heavy
``client`` module (which pulls in asyncio, the MCP SDK, etc.).

Repository-owned entries declare ``first_party`` and resolve only their
``packages/`` source. They share the isolated runner; external vendoring
validates their presence without copying or replacing them.

Internal MCP servers (declared by an overlay bundle) are private and not on PyPI, so a fresh
Tofu checkout cannot obtain them — the user just sees "launcher X is not on
PATH". We ship the source next to the repo and launch it ISOLATED via
``uv run --no-project --with-editable <source>``: each server resolves its own
dependency tree (including its own ``mcp``) into its OWN environment, never
Tofu's interpreter. That decoupling is what lets the Tofu client and any
individual server move SDK versions independently (measured 2026-07-31: a v1
client and a v2 server interoperate on the wire, so the only thing that ever
made SDK versions couple was the shared interpreter).

``--with-editable`` (not ``uvx --from``) is deliberate: uv caches local-dir
wheel builds aggressively, and even ``--refresh`` / ``--reinstall`` were
measured to serve a STALE build (a file created in the source was absent from
the installed package). Editable links the source tree, so dev edits and
re-vendored snapshots are live on the next connect.

Registry shape — command → spec. ``sources`` is an ordered list of candidate
dirs (relative paths resolved lazily against :func:`repo_root`), tried in
order: a sibling dev checkout (``../<name>``, live edits — the developer
box), then an export-bundled copy (``vendor/<name>``), then the in-repo
vendored snapshot (``tools/<name>``, hermetic fallback). The first dir with a
``pyproject.toml`` wins.

To add a server: add one row here (and make sure either a sibling checkout or
a ``tools/<name>`` snapshot exists; ``make vendor-mcp`` populates the latter).
Company-internal servers are declared by their overlay bundle's
``mcp_launchers/*.json`` instead and merged below (``tofu_extensions``).
"""
from __future__ import annotations

import os
from typing import NotRequired, TypedDict

# Repo root = two dirs up from this file (lib/mcp/vendored.py → repo).
def repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class SourceLauncher(TypedDict):
    sources: list[str]
    first_party: NotRequired[bool]
    extra_sources: NotRequired[list[str]]


VENDORED_LAUNCHERS: dict[str, SourceLauncher] = {
    # Repository-owned source uses the same isolated runner. The vendoring
    # script must never replace it with a sibling checkout or a tools/ copy.
    'tofu-overleaf-mcp': {
        'sources': ['packages/tofu-overleaf-mcp'],
        'first_party': True,
    },
    # Operator checkout (tools/) on the dev box; vendor/ on internal exports.
    'github-batch-mcp': {
        'sources': ['tools/github-batch-mcp', 'vendor/github-batch-mcp'],
    },
}

MCP_LAUNCHERS_CONTRACT = 'tofu.mcp-launchers/v1'


def _bundle_launchers() -> dict[str, SourceLauncher]:
    """Rows installed bundles declare in ``mcp_launchers/*.json`` (docs/PUBLIC_CORE.md).

    Company-internal servers live in their overlay's bundle, never in this
    table. A malformed file is skipped; core rows always win by name.
    """
    import json

    from tofu_extensions import bundle_directories
    rows: dict[str, SourceLauncher] = {}
    for directory in bundle_directories('mcp_launchers'):
        for path in sorted(directory.glob('*.json')):
            try:
                document = json.loads(path.read_text(encoding='utf-8'))
                if document.get('contract') != MCP_LAUNCHERS_CONTRACT:
                    raise ValueError('not a ' + MCP_LAUNCHERS_CONTRACT + ' document')
                for name, row in document['launchers'].items():
                    sources = row.get('sources')
                    if (not isinstance(sources, list) or not sources
                            or not all(isinstance(s, str) and s for s in sources)):
                        raise ValueError(f'{name}: sources must be non-empty strings')
                    extra = row.get('extra_sources', [])
                    if not isinstance(extra, list) or not all(isinstance(s, str) and s for s in extra):
                        raise ValueError(f'{name}: extra_sources must be strings')
                    rows[name] = {'sources': list(sources),
                                  **({'extra_sources': list(extra)} if extra else {})}
            except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
                import logging
                logging.getLogger(__name__).warning(
                    '[MCP:Vendored] skipping launcher file %s: %s', path, exc)
    return rows


VENDORED_LAUNCHERS.update({name: row for name, row in _bundle_launchers().items()
                           if name not in VENDORED_LAUNCHERS})
