"""lib/skills — User-installed skill packages (Anthropic AgentSkills format).

Skills are USER-curated instruction bundles — a separate noun from
memories (model-authored experience notes). This package owns the skills
channel:

  • ``registry``   — enumerate installed packages (which trees, which ids)
  • ``injection``  — the always-visible ``<available_skills>`` index block
  • ``activate``   — ``activate_skill`` progressive-disclosure loader
  • ``tools``      — tool schema(s) for the agent loop

Public API::

    from lib.skills import list_skills, get_skill
"""

from lib.skills.registry import (
    get_skill,
    list_skills,
)

__all__ = [
    'list_skills', 'get_skill',
]
