"""lib/agent_backends/ — Multi-backend agent architecture.

Registry for coding agent backends (built-in Tofu, Claude Code, Codex).
Each backend implements the ``AgentBackend`` protocol and is auto-registered
on import.

Usage::

    from lib.agent_backends import get_backend, list_backends

    # Get a specific backend instance
    backend = get_backend('claude-code')
    if backend and backend.is_available():
        for event in backend.start_turn(task, message, project_path=path):
            ...

    # List all backends with status
    backends = list_backends()
    # [{'name': 'builtin', 'available': True, ...}, ...]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib.log import get_logger

if TYPE_CHECKING:
    from lib.agent_backends.protocol import AgentBackend

logger = get_logger(__name__)

__all__ = [
    'get_backend',
    'list_backends',
    'register_backend',
    'BACKEND_CLASSES',
]


# ═══════════════════════════════════════════════════════════
#  Registry
# ═══════════════════════════════════════════════════════════

# Map: backend name → class (lazy instantiation)
BACKEND_CLASSES: dict[str, type[AgentBackend]] = {}

# Instance cache — one instance per backend
_instances: dict[str, AgentBackend] = {}


def register_backend(name: str, cls: type[AgentBackend]) -> None:
    """Register a backend class in the registry."""
    BACKEND_CLASSES[name] = cls
    logger.debug('[Backends] Registered backend class: %s', name)


def get_backend(name: str) -> AgentBackend | None:
    """Get a backend instance by name (cached).

    Args:
        name: Backend name (e.g. 'builtin', 'claude-code', 'codex').

    Returns:
        AgentBackend instance, or None if not registered.
    """
    if name in _instances:
        return _instances[name]

    cls = BACKEND_CLASSES.get(name)
    if cls is None:
        logger.warning('[Backends] Unknown backend: %s', name)
        return None

    try:
        instance = cls()
        _instances[name] = instance
        return instance
    except Exception as e:
        logger.error('[Backends] Failed to instantiate backend %s: %s', name, e, exc_info=True)
        return None


def list_backends() -> list[dict]:
    """List all registered backends with their status.

    Returns:
        List of dicts with name, displayName, available, authenticated,
        version, and capabilities for each backend.
    """
    results = []
    for name in BACKEND_CLASSES:
        backend = get_backend(name)
        if backend is None:
            results.append({
                'name': name,
                'displayName': name,
                'available': False,
                'authenticated': False,
                'version': None,
                'capabilities': {},
            })
            continue

        try:
            results.append({
                'name': backend.name,
                'displayName': backend.display_name,
                'available': backend.is_available(),
                'authenticated': backend.is_authenticated(),
                'version': backend.get_version(),
                'capabilities': backend.get_capabilities().to_dict(),
            })
        except Exception as e:
            logger.warning('[Backends] Error querying backend %s: %s', name, e)
            results.append({
                'name': name,
                'displayName': name,
                'available': False,
                'authenticated': False,
                'version': None,
                'capabilities': {},
                'error': str(e),
            })

    return results


# ═══════════════════════════════════════════════════════════
#  Auto-registration — register all known backends
# ═══════════════════════════════════════════════════════════

def _auto_register():
    """Register all known backend classes.

    Uses lazy imports so detection/subprocess code is not loaded
    until a backend is actually instantiated.
    """
    # Built-in is always available
    from lib.agent_backends.builtin import BuiltinBackend
    register_backend('builtin', BuiltinBackend)

    # External backends — import the class but don't instantiate
    from lib.agent_backends.claude_code import ClaudeCodeBackend
    register_backend('claude-code', ClaudeCodeBackend)

    from lib.agent_backends.codex import CodexBackend
    register_backend('codex', CodexBackend)


_auto_register()
