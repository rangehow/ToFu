"""Owner-scoped router home: the Git repository the Global Router coordinates.

Responsibility: locate and idempotently create the owner's router home under
Tofu's canonical writable data directory (``lib.runtime.runtime_paths``). The
home is an ordinary local Git repository with at least one commit, so the
existing project guards accept it as a project root and Workers of the Global
Router run in isolated worktrees of it, exactly like project Workers.

Entry points: ``router_home_location(user_id)`` (pure path, no effects),
``router_home_path(user_id)`` (creates and returns the home),
``is_router_home(user_id, path)`` and ``owner_resources(user_id)`` (the
owner's shared resource registry, docs/ROUTER.md R13).

Creation writes README.md, .gitignore and the write-once ``RESOURCES.md``
seed (documented sources only, never secrets), runs ``git init`` (which makes the
home its own repository top level even when the data directory lies inside a
source checkout), then reuses ``prepare_managed_repository`` for the baseline
commit, generated AGENTS.md guidance and shared-data policy. Existing files and
history are never modified. Dependencies: runtime_paths, git_runtime,
git_checkpoint_policy, integration_control (per-root lock),
project_mod.managed_repository.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

ROUTER_HOME_DIRECTORY = 'router'
_README = '''# Global Router home

Tofu created this repository for one owner's Global Router (docs/ROUTER.md).
Workers that the router delegates for non-project work, such as summaries,
drafts and investigations, run in isolated worktrees of this repository, and
their reviewed results are published here. Projects keep their own
repositories; the router reaches them through their Project Routers.

Do not store credentials here.

## Owner resource registry

`RESOURCES.md` is the owner-wide registry of GPUs, queues, keys and storage
the owner can lend a project (docs/ROUTER.md R13). Leads read it through
`list_project_leads`; a Router-home Worker reviews and publishes each update.
It names resources and how to obtain them — never a secret value.
'''
_GITIGNORE = '''# Tofu Global Router home. Runtime, dependency and cache trees are never versioned.
.tofu*/
node_modules/
__pycache__/
.venv/
'''


_RESOURCES = '''# Owner resource registry

GPUs, queues, keys and storage the owner can lend a project. Leads read this
file through `list_project_leads` (R13); a Router-home Worker reviews and
publishes every update, including this seed. **Never store a secret here** —
name the resource and how to obtain it, never its value.

Every row names its source, when it was read and how. Re-read a source before
planning work: queue capacity, strategy and authorization change without
notice, and a dated observation is not a grant.

## Hope GPU and CPU queues

Source: `scripts/hope_patrol/queues.json`, read 2026-11-16. Those are the
queues the owner's Hope account used on 2026-11-13 (HOPE-STUCK-RUN-1 audit,
HOPE_SUBMIT.md CPU lanes); each answered `get_yarn_apps` on that date. To
refresh, read the live queue and resource-code strategy through the Hope MCP
(`$yourprovider-hope-platform`); `scripts/hope_patrol/queues.json` records which
queues the compute patrol scans and grows when a lane uses a new one.

| Queue | Kind |
| --- | --- |
| `root.hldy_training_cluster.your-user.h910c_training_data` | 910C training; forced 16-GPU worker allocation, see below |
| `root.hldy_training_cluster.your-user.h910c_elastic` | 910C elastic |
| `root.hldy_training_cluster.your-user.h910c_elastic_2` | 910C elastic |
| `root.hldy_training_cluster.your-user.h910c_m17_public` | 910C public m17 |
| `root.hldy_training_cluster.hadoop-scalellm.910c_exp` | 910C experiment |
| `root.hldy_training_cluster.your-user.longcat_pro_v` | GPU |
| `root.hldy_training_cluster.your-user.efficient_910c` | 910C |
| `root.hldy_training_cluster.your-user.h910_pretrain_sp` | 910 pretrain |
| `root.hldy_training_cluster.your-user.h800_pretrain_sp` | H800 pretrain |
| `root.hldy_training_cluster.your-user.h800_code_sp` | H800 code |
| `root.hldy_training_cluster.your-user.p_exp` | GPU experiment |
| `root.hldy_training_cluster.your-user.cpu_weight_convert` | CPU weight conversion |
| `root.shxs_training_cluster.your-user.h800_lite_pretrain` | H800 lite pretrain |
| `root.shxs_training_cluster.your-user.h800_elastic` | H800 elastic |
| `root.zw05_training_cluster.your-user.llm_second` | GPU |
| `root.zw05_training_cluster.your-user.llm_debug` | Debug; overstep force-kill |
| `root.zw05_training_cluster.your-user.cpu_job` | CPU; pod admission limit |
| `root.zw05_training_cluster.hadoop-omni.debug` | Debug |
| `root.zw06_training_cluster.your-user.hal_post_train` | Post-train |
| `root.hlsc_training_cluster.your-user.omni_production` | Production |

Also observed for this account, in
`bundles/yourprovider/skills/yourprovider-hope-platform/references/queues.md` (read
2026-11-16; capacities measured 2026-09-09, historical only):

| Queue | Detail |
| --- | --- |
| `root.shxs_training_cluster.your-user.autoresearch_agent` | H200 eval queue; the only queue resource code `longcat_harness` binds to |
| `root.shxs_training_cluster.your-user.h800_eval_agent` | H200 eval; submit needs orchestration priority P2 |
| `root.shxs_training_cluster.your-user.autoresearch_cpu` | CPU, small footprints |
| `root.zw06_training_cluster.your-user.cpu_job` | CPU; free memory was too small for a DataLoader pod |

RSI-team observations (B300 `b300_rsi_eval`, GPU sandbox
`gpu_sandbox_h800_roce`, `h800_m17_public` / `h800_m17_eval`, public pool) are
in `references/rsi-queues-2026q4.md` (2026-10-30). A queue named there is an
observation, not an allocation; confirm before submitting.

## Resource codes and off-limits queues

`longcat_harness` (P0, granted by the owner 2026-09-03) and `auto_harness`
(default plan, experiment concurrency quota 128) are documented with their
bindings in `references/queues.md`. Read the current strategy — never reuse a
historical cap, and never switch codes to evade a restriction. Queue bans are
machine-readable in `HOPE_QUEUE_POLICY.json` beside this file (R16); every
submission path enforces them, and the m17 entry is global.

## Keys and credentials

No key value belongs in this file. Names and where to get one:

| Key | How to obtain |
| --- | --- |
| `dev_example_corp_token` | dev.internal.example.com Personal Access Token, in the owner's Tofu credential vault |
| `github_token` | GitHub PAT, in the owner's Tofu credential vault |
| `pypi_token` | PyPI token, in the owner's Tofu credential vault |
| Hope / MLP access | Browser SSO session of the owner, through the configured Hope MCP |
| Eval API keys | The evaluation project's own launchers under `evaluations/`; ask its lead |

## Storage

| Storage | How to obtain |
| --- | --- |
| Shared workspace filesystem | `abtest_workdir`, `output`, `uploads` and `.eval-runs` roots of the owner's Tofu deployment |
| S3Plus | For job assets and weights; see `references/rsi-queues-2026q4.md`. CPU queue region must match storage region |

## Refresh

Re-read each source named above before relying on a row. To change the
registry, delegate a Router-home Worker: it edits this file with the new
source and read date, and its reviewed publication lands here. Until then
`list_project_leads` reports `available=true` for this seed's facts only.
'''


class RouterHomeUnavailable(RuntimeError):
    """The router home could not be prepared; the message states the remedy."""


class RouterGitUnavailable(RouterHomeUnavailable):
    """Git or its storage is temporarily unavailable; the ensure may be retried."""


def router_home_root() -> str:
    """Directory that holds every owner's router home."""
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), ROUTER_HOME_DIRECTORY)


def router_home_location(user_id: int) -> str:
    """Canonical (real, normalized) home path of one owner; creates nothing."""
    owner = require_user_id(user_id, context='router home')
    return normalize_project_path(os.path.realpath(os.path.join(router_home_root(), str(owner))))


def is_router_home(user_id: int, path: str) -> bool:
    """Whether ``path`` is exactly this owner's router home identity."""
    if not isinstance(path, str) or not path.strip():
        return False
    return normalize_project_path(path.strip()) == router_home_location(user_id)


def owner_resources(user_id: int, *, content: bool) -> dict:
    """The owner-wide resource registry: ``RESOURCES.md`` at the router home root.

    Returns ``{path, available}`` plus ``bytes`` (and the text with
    ``content``), or ``error`` when a file is there but unreadable. Read
    without following a link out of the home (a link or non-regular file is
    refused); creates nothing.
    """
    from lib.conversations.project_messages import CROSS_PROJECT
    path = os.path.join(router_home_location(user_id), CROSS_PROJECT['resources']['file'])
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    except FileNotFoundError:
        return {'path': path, 'available': False}
    except OSError as error:
        return {'path': path, 'available': False,
                'error': f'{type(error).__name__}: {error.strerror or error} (a link is never followed)'}
    with os.fdopen(descriptor, 'rb') as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode):
            return {'path': path, 'available': False, 'error': 'Not a regular file'}
        view = {'path': path, 'available': True, 'bytes': status.st_size}
        if content:
            view['content'] = stream.read().decode('utf-8', errors='replace')
    return view


def _write_once(path: str, content: str) -> None:
    if os.path.lexists(path):
        return
    directory = os.path.dirname(path)
    handle, temporary = tempfile.mkstemp(prefix='.tofu_router_', dir=directory)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(content)
        # A concurrent creator writes identical bytes; whichever lands first wins.
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _git(home: str, *args: str) -> subprocess.CompletedProcess:
    """Local metadata commands only; the baseline commit runs in the managed owner."""
    from lib.git_checkpoint_policy import git_policy_environment
    from lib.git_runtime import GIT_STALL_SECONDS, GitExecutableUnavailable, git_executable
    from lib.project_mod.activity_supervised_process import (
        ProcessRetirementUnproven, ProcessStalled, run_activity_supervised)
    try:
        return run_activity_supervised(
            [git_executable(), '-c', 'core.fsmonitor=false', '-C', home, *args],
            cwd=home, env=git_policy_environment(), stall_seconds=GIT_STALL_SECONDS)
    except (GitExecutableUnavailable, OSError, ProcessStalled, ProcessRetirementUnproven) as error:
        logger.warning('[RouterHome] Git command unavailable', exc_info=True)
        raise RouterGitUnavailable(
            'Git or its filesystem is temporarily unavailable. Retry opening the main coordinator.') from error


def _has_commit(home: str) -> bool:
    return _git(home, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}').returncode == 0


def router_home_path(user_id: int) -> str:
    """Create (idempotently) and return the owner's router home repository."""
    home = router_home_location(user_id)
    os.makedirs(home, exist_ok=True)
    if os.path.isdir(os.path.join(home, '.git')) and _has_commit(home):
        # R13: a home created before the registry seed must not point leads at a
        # missing RESOURCES.md. Write-once; the owner or a reviewed Worker may
        # edit or delete it afterwards and nothing here restores or overwrites it.
        _write_once(os.path.join(home, 'RESOURCES.md'), _RESOURCES)
        return home
    # The same cross-process per-root lock as managed repository preparation:
    # concurrent `git init` runs race on template and config lock files.
    from lib.integration_control import _repo_lock
    with _repo_lock(home):
        _write_once(os.path.join(home, 'README.md'), _README)
        _write_once(os.path.join(home, '.gitignore'), _GITIGNORE)
        _write_once(os.path.join(home, 'RESOURCES.md'), _RESOURCES)
        if not os.path.lexists(os.path.join(home, '.git')):
            initialized = _git(home, 'init', '-q')
            if initialized.returncode:
                raise RouterHomeUnavailable(
                    f'Cannot initialize the router home {home}: {initialized.stderr.strip()}')
            _git(home, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    from lib.project_mod.managed_repository import (
        ManagedRepositoryUnavailable, prepare_managed_repository,
    )
    try:
        prepare_managed_repository(home, refresh=False)
    except ManagedRepositoryUnavailable as error:
        if error.code == 'volume_stalled':
            logger.warning('[RouterHome] Git preparation stalled', exc_info=True)
            raise RouterGitUnavailable(
                'The main coordinator workspace filesystem stopped responding. Retry shortly.') from error
        raise RouterHomeUnavailable(f'{error.reason} {error.remedy}') from error
    if not _has_commit(home):
        raise RouterHomeUnavailable(f'The router home {home} has no baseline commit.')
    return home


def router_home_owners() -> list[int]:
    """Owners that have a router home, ascending; creates nothing.

    A deployment-wide fault such as a storage fence concerns every owner with
    a Global Router, and their homes are where those owners are recorded.
    """
    root = router_home_root()
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    return sorted(int(name) for name in names
                  if name.isdigit() and int(name) > 0 and os.path.isdir(os.path.join(root, name)))


__all__ = ['ROUTER_HOME_DIRECTORY', 'RouterGitUnavailable', 'RouterHomeUnavailable', 'is_router_home',
           'owner_resources', 'router_home_location', 'router_home_owners', 'router_home_path',
           'router_home_root']
