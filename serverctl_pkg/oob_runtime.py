"""Out-of-band repair runtime adapter (SELF-REPAIR O4).

Responsibility: run the repair session as Tofu's own storage-free headless
runtime (``tofu-agent run``) from a pinned installed copy of the last
gate-passed commit, not from the live checkout that may carry the bug. Its
model route is the project's gateway through the watcher's Unix-socket relay
with a placeholder secret. opencode is the fallback when the pinned runtime
fails ``tofu-agent doctor``.

The routing envelope reuses the Terminal-Bench harness's shape
(``evaluations/terminal_bench/harness.py:tofu_model_routing``): a complete
v2 access envelope with the credential's plaintext replaced by a placeholder,
because the relay holds the real key. It never reads the live catalog's
credentials and never copies one.

Entry points: ``PinnedRuntime``, ``OpenCodeRuntime``, ``select_runtime``,
``routing_envelope``. Dependencies: the standard library; ``oob_relay`` for the
sandbox's base URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any

from serverctl_pkg import oob_relay

# Creator identities of the gateway models the repair session may use, from
# lib/model_info/model_inference_defaults.json (read once, names only).
CREATORS = {
    'kimi-k3': 'moonshot',
    'kimi-k3.1-pro': 'moonshot',
    'kimi-k3.1-pro-m17': 'moonshot',
    'glm-5.3': 'zhipu',
    'glm-5.3-flash': 'zhipu',
    'deepseek-v4.1-flash': 'deepseek',
    'deepseek-v4.1-flash-m17': 'deepseek',
}
DEFAULT_MODEL = 'kimi-k3.1-pro-m17'
# The registry groups a repair session may use (lib/tools/registry): read,
# edit and test its lane, run commands and track its plan.
REPAIR_TOOL_SPECS = ('project', 'read_files', 'background', 'todo')


def routing_envelope(*, base_url: str = oob_relay.SANDBOX_BASE_URL, model: str = DEFAULT_MODEL,
                     key_hint: str = 'configured', protocol: str = 'openai') -> dict[str, Any]:
    """A complete ``tofu.model-routing/v2`` envelope for the relay endpoint.

    Shape mirrors the TB harness's ``tofu_model_routing``: request-scoped
    catalog facts, the wire model name, the OpenAI-compatible chat face, and
    a placeholder credential because the relay holds the real key.
    """
    creator_id = CREATORS.get(model, 'moonshot')
    model_ref = {'creator_id': creator_id, 'model_id': model}
    provider_id = 'example-corp'
    access_id, connection_id = f'{provider_id}-access', f'{provider_id}-connection'
    secret_reference, offering_id = f'{provider_id}-secret', f'{provider_id}-offering'
    capabilities = ['text', 'thinking', 'tools']
    return {
        'model_routing': {
            'contract_version': 'tofu.model-routing/v2',
            'revision': 0,
            'creators': [{'creator_id': creator_id, 'name': creator_id}],
            'models': [{**model_ref, 'display_name': model, 'capabilities': capabilities,
                        'context_window': 262144, 'quality_rank': 10}],
            'providers': [{'provider_id': provider_id, 'name': provider_id, 'scope': 'owner'}],
            'provider_accesses': [{'provider_access_id': access_id, 'provider_id': provider_id,
                                   'enabled': True, 'quota_policy': {}}],
            'connections': [{'connection_id': connection_id, 'provider_access_id': access_id,
                             'base_url': base_url.rstrip('/'), 'protocol': protocol, 'enabled': True,
                             'priority': 0, 'extra_headers': {},
                             'inference_profile': {'features': {
                                 'cache_affinity_mt_session_id': 'supported'}}}],
            'credentials': [{'credential_id': f'{provider_id}-credential', 'provider_access_id': access_id,
                             'kind': 'api_key', 'secret_reference': secret_reference,
                             'key_hint': key_hint, 'enabled': True,
                             'authorization': {'connection_ids': [connection_id], 'models': [dict(model_ref)]},
                             'quota_policy': {}}],
            'offerings': [{'offering_id': offering_id, 'provider_access_id': access_id,
                           'identity_state': 'confirmed', 'model': dict(model_ref), 'enabled': True,
                           'capabilities': capabilities, 'context_window': 262144, 'priority': 0}],
            'deployments': [{'deployment_id': f'{provider_id}-deployment', 'offering_id': offering_id,
                             'connection_id': connection_id, 'wire_model_id': model, 'enabled': True,
                             'identity_confidence': 'high', 'probe_status': 'passed', 'priority': 0}],
        },
        'model': dict(model_ref),
        'routing': {'preferred_provider_id': provider_id},
        'credential_secrets': {secret_reference: 'relay-held'},
    }


@dataclass
class PinnedRuntime:
    """The pinned installed Tofu runtime (a wheel/venv from a gate-passed commit)."""

    root: Path
    commit: str

    @property
    def python(self) -> Path:
        return self.root / 'venv' / 'bin' / 'python3'

    def argv(self, *, prompt: Path, output: Path, workspace: Path, routing: Path) -> list[str]:
        # --env-file is a top-level option of the CLI: after 'run' argparse refuses it.
        return [str(self.python), '-I', '-m', 'tofu_agent', '--env-file', '/dev/null', 'run',
                '--task-file', str(prompt), '--output', str(output),
                '--cwd', str(workspace),
                # The key-less route to the watcher's relay (oob_repair writes it per session); never a .env.
                '--model-routing-file', str(routing),
                # Empty selection: no optional tools; run_contract resolves
                # every capability explicitly off (TOOLS_NONE).
                '--tools', '',
                # Only the lane-editing groups: no swarm (sub-agents,
                # approval delegation), skills, skill installs, page preview
                # or media inspection.
                '--tool-specs', ','.join(REPAIR_TOOL_SPECS),
                # No --timeout-s: omitted means no deadline; the watcher's
                # incident-close stop is the bound.
                '--trajectory', 'atif']

    def doctor(self) -> tuple[bool, str]:
        if not self.python.exists():
            return False, f'pinned runtime missing: {self.python}'
        result = subprocess.run([str(self.python), '-I', '-m', 'tofu_agent', 'doctor'],
                                capture_output=True, text=True, timeout=60)
        return result.returncode == 0, (result.stdout + result.stderr).strip()[:200]


@dataclass
class OpenCodeRuntime:
    """The installed opencode harness with the fixed recovery agent."""

    binary: Path

    def argv(self, *, prompt: Path, output: Path, workspace: Path) -> list[str]:
        return [str(self.binary), 'run', '--standalone', '--archived', '--format', 'json',
                '--agent', 'tofu-recovery', '--title', output.parent.name,
                '--file', str(prompt)]

    def doctor(self) -> tuple[bool, str]:
        if not self.binary.exists():
            return False, f'opencode missing: {self.binary}'
        result = subprocess.run([str(self.binary), '--version'], capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout.strip()[:200]


def select_runtime(primary, fallback):
    """The pinned Tofu runtime when its doctor passes; otherwise opencode."""
    ok, _detail = primary.doctor()
    return primary if ok else fallback


__all__ = ['CREATORS', 'DEFAULT_MODEL', 'OpenCodeRuntime', 'PinnedRuntime',
           'routing_envelope', 'select_runtime']
