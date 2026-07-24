"""lib/skills — User-installed skill packages (Anthropic AgentSkills format).

Skills are USER-curated instruction bundles — a separate noun from
memories (model-authored experience notes). This package owns the skills
channel:

  • ``registry``   — enumerate installed packages (which trees, which ids)
  • ``injection``  — the always-visible ``<available_skills>`` index block
  • ``activate``   — ``activate_skill`` progressive-disclosure loader
  • ``tools``      — tool schema(s) for the agent loop

Public API::

    from lib.skills import list_skills, get_skill, build_skills_index
    from lib.skills import activate_skill, list_skill_files
    from lib.skills import ACTIVATE_SKILL_TOOL, ALL_SKILL_TOOLS, SKILL_TOOL_NAMES
"""

from lib.skills.registry import (
    get_skill,
    list_skills,
)
from lib.skills.injection import (
    build_skills_index,
)
from lib.skills.activate import (
    activate_skill,
    list_skill_files,
)
from lib.skills.tools import (
    ACTIVATE_SKILL_TOOL,
    ALL_SKILL_TOOLS,
    SKILL_TOOL_NAMES,
)

__all__ = [
    'list_skills', 'get_skill',
    'build_skills_index',
    'activate_skill', 'list_skill_files',
    'ACTIVATE_SKILL_TOOL', 'ALL_SKILL_TOOLS', 'SKILL_TOOL_NAMES',
]
