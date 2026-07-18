"""tests/test_vu_prompt_single_source.py — the virtual-user persona has ONE home.

The VU role prompt used to be an inline ~2000-char constant in
``lib/tasks_pkg/autopilot.py`` that TWO other places hand-copied and then
DRIFTED from:
  * ``lib/swarm/registry.AGENT_ROLES['virtual_user']['system_prompt_suffix']``
    had shrunk to a 3-sentence paraphrase (comment still claimed it "Mirrors
    ... _VU_ROLE_PROMPT");
  * ``lib/orchestration/_build.build_autopilot_definition``'s VU node
    ``objective`` was a different 3-sentence paraphrase.
Both had lost the verification discipline AND the mandatory
``[PROGRESS: resolved=X remaining=Y]`` hard-signal line — the exact
"hand-copied, began to diverge" anti-pattern ``lib/agent_verdict`` exists to
kill.

This suite pins that all three now resolve to the SINGLE source
``lib.agent_verdict.VU_ROLE_PROMPT`` (by object identity where possible, by
substring where the prompt is embedded in a larger brief), so a future
paraphrase cannot silently re-fork them.

@pytest.mark.unit — pure imports, no I/O, no live LLM.
"""

import pytest

pytestmark = pytest.mark.unit


def test_shared_constant_carries_the_full_persona():
    """The single source must still be the FULL driver persona, not a stub —
    identity is only useful if the thing everyone points at is the good one."""
    from lib.agent_verdict import VU_DONE_SENTINEL, VU_ROLE_PROMPT as p
    low = p.lower()
    # Driver identity + verification discipline + creativity + provenance.
    assert 'project owner' in low
    assert 'verify' in low
    assert 'creativ' in low
    assert 'provenance' in low
    # The mandatory hard-signal line the diminishing-returns guard consumes.
    assert '[progress: resolved=x remaining=y]' in low
    # Routes completion through the shared sentinel (defined in the same module).
    assert VU_DONE_SENTINEL in p
    # It is a substantial persona, not a 3-sentence paraphrase.
    assert len(p) > 1500


def test_autopilot_uses_the_shared_object_identically():
    """The LIVE standalone autopilot loop's ``_VU_ROLE_PROMPT`` IS the shared
    object (not a copy) — this is the production path."""
    from lib.agent_verdict import VU_ROLE_PROMPT
    from lib.tasks_pkg.autopilot import _VU_ROLE_PROMPT
    assert _VU_ROLE_PROMPT is VU_ROLE_PROMPT


def test_registry_virtual_user_suffix_is_the_shared_object():
    """The FlowExecutor engine path injects the VU persona as the sub-agent's
    system prompt via ``AGENT_ROLES['virtual_user']['system_prompt_suffix']``.
    It must BE the shared object — the drifted 3-sentence paraphrase is gone."""
    from lib.agent_verdict import VU_ROLE_PROMPT
    from lib.swarm.registry import AGENT_ROLES, get_role_system_suffix
    assert AGENT_ROLES['virtual_user']['system_prompt_suffix'] is VU_ROLE_PROMPT
    # The public accessor the SubAgent actually calls resolves to it too.
    assert get_role_system_suffix('virtual_user') is VU_ROLE_PROMPT


def test_build_autopilot_definition_vu_node_embeds_the_shared_prompt():
    """The canonical autopilot graph's virtual_user node carries the shared
    persona as its objective (so ``render_role_brief`` folds the FULL persona
    — incl. the [PROGRESS] contract — into the VU sub-agent's delegation
    brief), not a paraphrase."""
    from lib.agent_verdict import VU_ROLE_PROMPT
    from lib.orchestration import build_autopilot_definition, render_role_brief
    defn = build_autopilot_definition()
    vu = next(n for n in defn['nodes'] if n.get('role') == 'virtual_user')
    assert vu['params']['objective'] is VU_ROLE_PROMPT
    # And it survives brief rendering (objective renders as the lead paragraph).
    brief = render_role_brief(vu)
    assert '[PROGRESS: resolved=X remaining=Y]' in brief


def test_no_drifted_paraphrase_survives_in_consumers():
    """Guard against a re-fork: the old paraphrase fingerprints ("Reply in 1-3
    sentences" / "standing in for the human") must not reappear as a
    HARDCODED string in either consumer's module source."""
    import inspect

    import lib.swarm.registry as reg
    import lib.orchestration._build as build

    for mod in (reg, build):
        src = inspect.getsource(mod)
        assert 'Reply in 1-3' not in src, (
            f'{mod.__name__} still hardcodes the drifted VU paraphrase')


def test_prompt_version_hash_unchanged_by_relocation():
    """Relocating the constant must not change its bytes: ``VU_PROMPT_VERSION``
    still equals the sha256[:8] of the shared prompt (the standalone loop
    stamps this into every VU directive; a byte change would silently
    invalidate the stale-vs-live marker)."""
    import hashlib
    from lib.agent_verdict import VU_ROLE_PROMPT
    from lib.tasks_pkg.autopilot import VU_PROMPT_VERSION
    expected = hashlib.sha256(VU_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]
    assert VU_PROMPT_VERSION == expected
    assert len(VU_PROMPT_VERSION) == 8
