"""lib/oauth/ — OAuth PKCE authentication for ChatGPT Plus and Claude Pro subscriptions.

Supports:
  • Claude (Anthropic) — OAuth → standard Messages API with Bearer token
  • Codex (OpenAI ChatGPT Plus) — OAuth → Responses API with format translation

Usage::

    from lib.oauth import start_oauth_flow, get_oauth_status, get_oauth_token
"""

from lib._pkg_utils import build_facade, safe_import

__all__: list[str] = []
_import = safe_import(__name__, globals(), __all__)

from . import pkce
from .pkce import *  # noqa: F401,F403

build_facade(__all__, pkce)

from . import token_store
from .token_store import *  # noqa: F401,F403

build_facade(__all__, token_store)

from . import claude
from .claude import *  # noqa: F401,F403

build_facade(__all__, claude)

from . import codex
from .codex import *  # noqa: F401,F403

build_facade(__all__, codex)

from . import manager
from .manager import *  # noqa: F401,F403

build_facade(__all__, manager)
