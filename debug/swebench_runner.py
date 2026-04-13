#!/usr/bin/env python3
"""swebench_runner.py — Full SWE-bench Verified evaluation: Tofu vs Claude Code CLI.

Runs all 500 SWE-bench Verified instances using official test specs from the
swebench package. Uses conda environments per repo+version for proper isolation.
No Docker required.

Key design choices:
  - NO artificial inference timeout — agents work as long as they need
  - Conda environments from official MAP_REPO_VERSION_TO_SPECS
  - Save-after-each-instance for robust resume
  - Official test commands (runtests.py for Django, bin/test for sympy, pytest for rest)
  - Parallel inference support (Tofu + CC on different instances)

Usage:
    # Run full SWE-bench Verified (500 instances)
    python debug/swebench_runner.py --num 500

    # Run all instances, no limit
    python debug/swebench_runner.py --all

    # Run specific instances
    python debug/swebench_runner.py --instances django__django-13128,sympy__sympy-16886

    # Run only one tool
    python debug/swebench_runner.py --tool tofu --all

    # Filter by repo or difficulty
    python debug/swebench_runner.py --repo django/django --all
    python debug/swebench_runner.py --difficulty "<15 min fix" --all

    # Resume a previous run
    python debug/swebench_runner.py --all --resume --output /path/to/results.json

    # Set up conda environments only (no inference)
    python debug/swebench_runner.py --setup-envs-only

    # Dry-run: show selected instances
    python debug/swebench_runner.py --dry-run --all

    # Custom output directory
    python debug/swebench_runner.py --all --workdir /tmp/swebench_full

Prerequisites:
    - Tofu server running on http://127.0.0.1:15000
    - Claude Code proxy running on http://127.0.0.1:8082 (for CC tests)
    - claude CLI installed and configured
    - conda available for environment management
    - git available
    - pip install datasets swebench
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import requests

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('swebench')

# ─── Configuration ────────────────────────────────────────────────────────────

TOFU_BASE_URL = os.environ.get('TOFU_BASE_URL', 'http://127.0.0.1:15000')
TOFU_MODEL = os.environ.get('TOFU_MODEL', 'aws.claude-opus-4.6')
CC_MODEL = os.environ.get('CC_MODEL', 'opus')

# NO artificial inference timeout — agents run as long as they need.
# Only a generous safety net to catch truly stuck processes (4 hours).
INFERENCE_SAFETY_TIMEOUT = int(os.environ.get('INFERENCE_SAFETY_TIMEOUT', '14400'))
# Test execution timeout (per test, in seconds)
TEST_TIMEOUT = int(os.environ.get('TEST_TIMEOUT', '1800'))  # 30 min — large test suites need time
# Cooldown between API calls to avoid rate limits
COOLDOWN_SECONDS = int(os.environ.get('COOLDOWN_SECONDS', '5'))

# Directories
DEFAULT_WORKDIR = Path(os.environ.get('SWEBENCH_WORKDIR', '/tmp/swebench_full'))
REPO_CACHE_DIR = Path(os.environ.get('REPO_CACHE_DIR', '/tmp/swebench_repos'))
CONDA_ENV_PREFIX = Path(os.environ.get(
    'CONDA_ENV_DIR',
    '/tmp/swebench_conda_envs',
))

# Default pricing (Opus via example-corp gateway — Anthropic convention)
PRICE_INPUT_PER_1K = 0.015
PRICE_OUTPUT_PER_1K = 0.075
PRICE_CACHE_READ_PER_1K = 0.0015
PRICE_CACHE_WRITE_PER_1K = 0.01875

# Max parallel eval workers (test execution is CPU-bound)
MAX_EVAL_WORKERS = int(os.environ.get('MAX_EVAL_WORKERS', '8'))


# ─── Model Configuration ─────────────────────────────────────────────────────

@dataclass
class ModelConfig:
    """Configuration for one model to benchmark."""
    name: str          # Short display name (e.g. 'opus', 'minimax', 'glm')
    backend: str       # 'tofu' or 'cc'
    model_id: str      # Model string sent to the API (e.g. 'aws.claude-opus-4.6')
    concurrency: int = 3    # Max parallel inference workers for this model
    # Per-model pricing (per 1K tokens)
    price_input: float = 0.015
    price_output: float = 0.075
    price_cache_read: float = 0.0015
    price_cache_write: float = 0.01875


# Built-in model presets (user can override via --models)
MODEL_PRESETS = {
    'opus': ModelConfig(
        name='opus', backend='tofu', model_id='aws.claude-opus-4.6',
        concurrency=3,
        price_input=0.015, price_output=0.075,
        price_cache_read=0.0015, price_cache_write=0.01875,
    ),
    'cc': ModelConfig(
        name='cc', backend='cc', model_id='opus',
        concurrency=3,
        price_input=0.015, price_output=0.075,
        price_cache_read=0.0015, price_cache_write=0.01875,
    ),
    'minimax': ModelConfig(
        name='minimax', backend='tofu', model_id='MiniMax-M2.7',
        concurrency=5,
        price_input=0.001, price_output=0.002,
        price_cache_read=0.0002, price_cache_write=0.001,
    ),
    'glm': ModelConfig(
        name='glm', backend='tofu', model_id='glm-5.1',
        concurrency=5,
        price_input=0.002, price_output=0.008,
        price_cache_read=0.0004, price_cache_write=0.002,
    ),
    'longcat': ModelConfig(
        name='longcat', backend='tofu', model_id='longcat-pro-0403',
        concurrency=5,
        price_input=0.001, price_output=0.004,
        price_cache_read=0.0002, price_cache_write=0.001,
    ),
}

# Thread-safe results management
import threading as _threading
_results_lock = _threading.Lock()

# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class SWEInstance:
    """A single SWE-bench instance."""
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    patch: str           # gold patch (the correct fix)
    test_patch: str      # test patch (tests that verify the fix)
    fail_to_pass: list   # tests that should go from FAIL → PASS
    pass_to_pass: list   # tests that should remain PASS
    version: str
    difficulty: str
    created_at: str
    environment_setup_commit: str = ''


@dataclass
class InferenceResult:
    """Result of one tool's attempt at an instance."""
    instance_id: str
    tool: str
    model_patch: str = ''
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    error: str = ''
    raw_output: str = ''   # for debugging


@dataclass
class TestOutput:
    """Captured output from a single test execution."""
    test_id: str
    passed: bool = False
    stdout: str = ''
    stderr: str = ''
    duration_s: float = 0.0
    command: str = ''
    return_code: int = -1


@dataclass
class EvalResult:
    """Result of evaluating a generated patch."""
    instance_id: str
    tool: str
    resolved: bool = False
    fail_to_pass_results: dict = field(default_factory=dict)
    pass_to_pass_results: dict = field(default_factory=dict)
    fail_to_pass_outputs: list = field(default_factory=list)   # list of TestOutput
    pass_to_pass_outputs: list = field(default_factory=list)   # list of TestOutput
    patch_applies: bool = False
    test_patch_applies: bool = False
    patch_apply_stderr: str = ''
    test_patch_apply_stderr: str = ''
    install_stdout: str = ''
    install_stderr: str = ''
    error: str = ''


@dataclass
class BenchmarkResult:
    """Combined result for one instance + tool."""
    instance_id: str
    repo: str
    difficulty: str
    tool: str
    resolved: bool = False
    duration_s: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    num_turns: int = 0
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_passed: int = 0
    pass_to_pass_total: int = 0
    patch_applies: bool = False
    error: str = ''


# ─── Dataset Loading ─────────────────────────────────────────────────────────

# Repos that require C/Cython compilation — cannot install without Docker
C_EXTENSION_REPOS = {
    'astropy/astropy',
    'scikit-learn/scikit-learn',
    'matplotlib/matplotlib',
}


def load_swebench_instances(
    num: int = None,
    instance_ids: list = None,
    repo_filter: str = None,
    difficulty_filter: str = None,
    seed: int = 42,
    load_all: bool = False,
    skip_repos: set = None,
) -> list[SWEInstance]:
    """Load SWE-bench Verified instances from HuggingFace."""
    from datasets import load_dataset
    ds = load_dataset('princeton-nlp/SWE-bench_Verified', split='test')

    instances = []
    for item in ds:
        inst = SWEInstance(
            instance_id=item['instance_id'],
            repo=item['repo'],
            base_commit=item['base_commit'],
            problem_statement=item['problem_statement'],
            hints_text=item['hints_text'] or '',
            patch=item['patch'],
            test_patch=item['test_patch'],
            fail_to_pass=json.loads(item['FAIL_TO_PASS']),
            pass_to_pass=json.loads(item['PASS_TO_PASS']),
            version=item['version'],
            difficulty=item['difficulty'],
            created_at=item['created_at'],
            environment_setup_commit=item.get('environment_setup_commit', ''),
        )
        instances.append(inst)

    # Apply filters
    if skip_repos:
        before = len(instances)
        instances = [i for i in instances if i.repo not in skip_repos]
        if len(instances) < before:
            log.info('Skipped %d instances from repos: %s', before - len(instances), skip_repos)
    if instance_ids:
        id_set = set(instance_ids)
        instances = [i for i in instances if i.instance_id in id_set]
    if repo_filter:
        instances = [i for i in instances if i.repo == repo_filter]
    if difficulty_filter:
        instances = [i for i in instances if i.difficulty == difficulty_filter]

    if load_all or (num and num >= len(instances)):
        # Return all (sorted for reproducibility)
        instances.sort(key=lambda x: x.instance_id)
        return instances

    # Sample if num specified
    if num and num < len(instances):
        import random
        rng = random.Random(seed)
        # Stratified sampling by repo (ensures diversity)
        by_repo = {}
        for inst in instances:
            by_repo.setdefault(inst.repo, []).append(inst)
        for k in by_repo:
            by_repo[k].sort(key=lambda x: x.instance_id)
            rng.shuffle(by_repo[k])

        selected = []
        remaining = num
        repos = sorted(by_repo.keys())
        for i, repo in enumerate(repos):
            if i == len(repos) - 1:
                take = remaining
            else:
                take = max(1, round(num * len(by_repo[repo]) / len(instances)))
                take = min(take, remaining, len(by_repo[repo]))
            selected.extend(by_repo[repo][:take])
            remaining -= take
            if remaining <= 0:
                break
        instances = selected

    instances.sort(key=lambda x: x.instance_id)
    return instances


# ─── Repository & Environment Management ─────────────────────────────────────

def get_repo_path(repo: str) -> Path:
    """Get or clone a repository to the cache."""
    repo_dir = REPO_CACHE_DIR / repo.replace('/', '__')
    if not repo_dir.exists():
        log.info('Cloning %s...', repo)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ['git', 'clone', '--quiet', f'https://github.com/{repo}.git', str(repo_dir)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            raise RuntimeError(f'Failed to clone {repo}: {r.stderr[:500]}')
        log.info('Cloned %s (%s)', repo, _dir_size(repo_dir))
    return repo_dir


def _dir_size(path: Path) -> str:
    """Human-readable directory size."""
    try:
        r = subprocess.run(['du', '-sh', str(path)], capture_output=True, text=True, timeout=10)
        return r.stdout.split()[0] if r.returncode == 0 else '?'
    except Exception:
        return '?'


def setup_workspace(inst: SWEInstance, tool: str, base_dir: Path) -> Path:
    """Create a workspace for an instance by cloning the repo at base_commit."""
    ws = base_dir / 'workspaces' / f'{inst.instance_id}__{tool}'
    if ws.exists():
        shutil.rmtree(ws)

    repo_path = get_repo_path(inst.repo)
    subprocess.run(
        ['git', 'clone', '--quiet', '--shared', str(repo_path), str(ws)],
        capture_output=True, text=True, timeout=300, check=True,
    )
    subprocess.run(
        ['git', 'checkout', '--quiet', inst.base_commit],
        capture_output=True, text=True, timeout=30, cwd=str(ws), check=True,
    )
    subprocess.run(
        ['git', 'clean', '-fdx', '--quiet'],
        capture_output=True, text=True, timeout=30, cwd=str(ws),
    )
    return ws


def get_conda_env_name(repo: str, version: str) -> str:
    """Get conda environment name for a repo+version."""
    return f'swe_{repo.replace("/", "_")}_{version}'.replace('.', '_')


def get_conda_env_path(repo: str, version: str) -> Path:
    """Get conda environment prefix path."""
    return CONDA_ENV_PREFIX / get_conda_env_name(repo, version)


def _conda_run(env_path: Path, cmd: str, cwd: str = None, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a command inside a conda environment by activating it directly."""
    # Use direct PATH manipulation instead of 'conda run' which has version-specific flags
    env_bin = env_path / 'bin'
    env = {**os.environ}
    env['PATH'] = f'{env_bin}:{env.get("PATH", "")}'
    env['CONDA_PREFIX'] = str(env_path)
    env['VIRTUAL_ENV'] = str(env_path)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return subprocess.run(
        cmd, shell=True,
        capture_output=True, text=True,
        timeout=timeout, cwd=cwd, env=env,
    )


def setup_conda_env(repo: str, version: str, workspace: Path = None) -> Path:
    """Create a conda environment for a repo+version using official SWE-bench specs.

    Returns the path to the conda environment.
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

    env_path = get_conda_env_path(repo, version)

    # Check if already exists and is valid
    if env_path.exists():
        r = _conda_run(env_path, 'python --version')
        if r.returncode == 0:
            log.info('Conda env already exists: %s', env_path.name)
            return env_path
        else:
            log.warning('Conda env broken, recreating: %s', env_path.name)
            shutil.rmtree(env_path, ignore_errors=True)

    # Get specs
    if repo not in MAP_REPO_VERSION_TO_SPECS:
        log.warning('No specs for %s, using default Python 3.9', repo)
        specs = {'python': '3.9', 'install': 'python -m pip install -e .'}
    elif version not in MAP_REPO_VERSION_TO_SPECS[repo]:
        log.warning('No specs for %s v%s, using latest version specs', repo, version)
        available = sorted(MAP_REPO_VERSION_TO_SPECS[repo].keys())
        specs = MAP_REPO_VERSION_TO_SPECS[repo][available[-1]]
    else:
        specs = MAP_REPO_VERSION_TO_SPECS[repo][version]

    py_version = specs.get('python', '3.9')
    log.info('Creating conda env: %s (Python %s)', env_path.name, py_version)

    # Create environment
    env_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ['conda', 'create', '--prefix', str(env_path), f'python={py_version}',
         '-y', '--quiet'],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f'Failed to create conda env: {r.stderr[:500]}')

    # Install conda packages (if specified and not a file reference)
    packages = specs.get('packages', '')
    if packages and packages not in ('requirements.txt', 'environment.yml'):
        pkg_list = packages if isinstance(packages, str) else ' '.join(packages)
        log.info('  Installing conda packages: %s', pkg_list[:100])
        # Use conda install with --prefix directly (not through _conda_run)
        r = subprocess.run(
            f'conda install --prefix {env_path} -y --quiet {pkg_list}',
            shell=True, capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            # Fallback to pip
            log.debug('  conda install failed, trying pip: %s', r.stderr[:100])
            _conda_run(env_path, f'pip install {pkg_list}', timeout=300)

    # Install pip packages
    pip_packages = specs.get('pip_packages', [])
    if pip_packages:
        pkg_str = ' '.join(f'"{p}"' for p in pip_packages)
        log.info('  Installing pip packages: %s', pkg_str[:150])
        r = _conda_run(env_path, f'pip install {pkg_str} --quiet', timeout=300)
        if r.returncode != 0:
            log.warning('  pip install failed (non-fatal): %s', r.stderr[:200])

    # Run pre_install commands (skip apt-get as we don't have sudo)
    pre_install = specs.get('pre_install', [])
    for cmd in pre_install:
        if 'apt-get' in cmd or 'apt ' in cmd:
            log.debug('  Skipping apt command: %s', cmd[:80])
            continue
        if 'DEBIAN_FRONTEND' in cmd:
            continue
        log.info('  Pre-install: %s', cmd[:100])
        if workspace:
            _conda_run(env_path, f'bash -c "{cmd}"', cwd=str(workspace), timeout=120)

    # Install the package itself (requires workspace)
    if workspace:
        install_cmd = specs.get('install', 'python -m pip install -e .')
        log.info('  Installing package: %s', install_cmd[:100])
        r = _conda_run(env_path, install_cmd, cwd=str(workspace), timeout=600)
        if r.returncode != 0:
            log.warning('  Package install failed: %s', r.stderr[:300])

    # Install requirements.txt / environment.yml if specified
    if workspace and packages == 'requirements.txt':
        req_file = workspace / 'requirements.txt'
        if req_file.exists():
            log.info('  Installing requirements.txt')
            _conda_run(env_path, f'pip install -r requirements.txt --quiet',
                       cwd=str(workspace), timeout=300)
    elif workspace and packages == 'environment.yml':
        env_file = workspace / 'environment.yml'
        if env_file.exists():
            log.info('  Installing from environment.yml')
            # conda env update doesn't work with --prefix well, use pip for deps
            _conda_run(env_path, f'pip install -e . --quiet',
                       cwd=str(workspace), timeout=300)

    # Ensure pytest is always available
    _conda_run(env_path, 'pip install pytest --quiet', timeout=120)

    log.info('  ✅ Conda env ready: %s', env_path.name)
    return env_path


def setup_all_conda_envs(instances: list[SWEInstance]) -> dict[str, Path]:
    """Pre-build all conda environments needed for the instances.

    Returns a dict mapping 'repo/version' to env_path.
    """
    combos = set()
    for inst in instances:
        combos.add((inst.repo, inst.version))

    log.info('Setting up %d conda environments...', len(combos))
    env_map = {}
    for repo, version in sorted(combos):
        key = f'{repo}/{version}'
        try:
            env_path = setup_conda_env(repo, version)
            env_map[key] = env_path
        except Exception as e:
            log.error('Failed to create env for %s: %s', key, e)
            env_map[key] = None

    ok = sum(1 for v in env_map.values() if v)
    log.info('Conda environments: %d/%d ready', ok, len(env_map))
    return env_map


# ─── Agent Prompt ─────────────────────────────────────────────────────────────

def build_agent_prompt(inst: SWEInstance) -> str:
    """Build the prompt for the agent to solve the issue."""
    prompt = textwrap.dedent(f"""\
        You are solving a GitHub issue in the repository {inst.repo}.

        ## Issue Description

        {inst.problem_statement}

        ## Instructions

        1. Read the relevant source files to understand the codebase and the issue.
        2. Identify the root cause of the bug or the files that need to be changed.
        3. Make the minimal necessary code changes to fix the issue.
        4. Do NOT modify any test files.
        5. Do NOT add new test files.
        6. Focus on fixing the actual issue described above.

        The repository is already checked out at the correct commit. Start by exploring
        the project structure and reading the relevant files mentioned in the issue.
    """)

    if inst.hints_text:
        prompt += f"\n## Hints\n\n{inst.hints_text}\n"

    return prompt


# ─── Inference: Tofu ──────────────────────────────────────────────────────────

def run_tofu_inference(inst: SWEInstance, workspace: Path,
                      mcfg: ModelConfig = None) -> InferenceResult:
    """Run Tofu on an instance. No artificial timeout."""
    tool_name = mcfg.name if mcfg else 'tofu'
    model_id = mcfg.model_id if mcfg else TOFU_MODEL
    result = InferenceResult(instance_id=inst.instance_id, tool=tool_name)
    prompt = build_agent_prompt(inst)
    t0 = time.time()

    try:
        # Retry the initial POST up to 3 times — the server may be briefly
        # busy finishing a previous task (GIL contention, heavy streaming).
        task_id = None
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    f'{TOFU_BASE_URL}/api/chat/start',
                    json={
                        'convId': f'swebench-{inst.instance_id}-{tool_name}-{int(time.time())}',
                        'messages': [{'role': 'user', 'content': prompt}],
                        'config': {
                            'model': model_id,
                            'projectPath': str(workspace),
                        },
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                task_id = resp.json()['taskId']
                break
            except (requests.Timeout, requests.ConnectionError) as e:
                log.warning('[Tofu] POST attempt %d/3 failed: %s', attempt, e)
                if attempt < 3:
                    time.sleep(10 * attempt)  # back off: 10s, 20s
                else:
                    raise  # final attempt — let outer handler catch it
        if not task_id:
            result.error = 'Failed to get task_id after 3 attempts'
            result.duration_s = time.time() - t0
            return result

        # Poll until done — no tight timeout, generous safety net only
        poll_interval = 2.0
        while True:
            elapsed = time.time() - t0
            if elapsed > INFERENCE_SAFETY_TIMEOUT:
                result.error = f'Safety timeout after {elapsed:.0f}s'
                try:
                    requests.post(f'{TOFU_BASE_URL}/api/chat/abort/{task_id}', timeout=5)
                except Exception:
                    pass
                break

            time.sleep(poll_interval)
            try:
                poll_resp = requests.get(
                    f'{TOFU_BASE_URL}/api/chat/poll/{task_id}',
                    timeout=10,
                )
                poll_resp.raise_for_status()
                data = poll_resp.json()
            except Exception as e:
                log.warning('Poll failed: %s', e)
                continue

            status = data.get('status', '')
            if status in ('done', 'error', 'interrupted'):
                result.duration_s = time.time() - t0

                # Parse usage from apiRounds
                api_rounds = data.get('apiRounds', [])
                if isinstance(api_rounds, list):
                    result.num_turns = len(api_rounds)
                    for rd in api_rounds:
                        ru = rd.get('usage', {})
                        result.input_tokens += ru.get('prompt_tokens', 0)
                        result.output_tokens += ru.get('completion_tokens', 0)
                        result.cache_read_tokens += ru.get('cache_read_tokens', 0)
                        result.cache_write_tokens += ru.get('cache_write_tokens', 0)

                result.cost_usd = _compute_cost(result, mcfg)

                # Save full poll response for debugging
                try:
                    result.raw_output = json.dumps(data, ensure_ascii=False)[:50000]
                except Exception:
                    result.raw_output = str(data)[:20000]

                if status == 'error':
                    result.error = data.get('error', 'Unknown error')
                break

            # Adaptive polling: back off but cap at 10s
            poll_interval = min(poll_interval * 1.1, 10.0)

    except Exception as e:
        result.duration_s = time.time() - t0
        result.error = str(e)

    # Extract patch from workspace
    result.model_patch = _extract_git_diff(workspace)
    return result


# ─── CC Proxy Management ──────────────────────────────────────────────────────

CC_PROXY_URL = os.environ.get('CC_PROXY_URL', 'http://127.0.0.1:8082')
CC_PROXY_DIR = os.environ.get('CC_PROXY_DIR', '')  # auto-detected if empty
_cc_proxy_proc = None


def _find_proxy_dir() -> Optional[Path]:
    """Find the claude-code-proxy directory."""
    if CC_PROXY_DIR:
        return Path(CC_PROXY_DIR)
    # Auto-detect: look relative to chatui project
    candidates = [
        Path(__file__).resolve().parent.parent.parent / 'claude-code-workspace' / 'proxy',
        Path.home() / 'claude-code-workspace' / 'proxy',
    ]
    for c in candidates:
        if (c / 'start_proxy.py').exists():
            return c
    return None


def ensure_cc_proxy_alive() -> bool:
    """Check if CC proxy is responding; restart it if dead. Returns True if alive."""
    global _cc_proxy_proc
    try:
        resp = requests.get(f'{CC_PROXY_URL}/health', timeout=5)
        if resp.status_code == 200:
            return True
    except Exception:
        pass

    # Proxy is dead — try to restart
    log.warning('[CC Proxy] Not responding, attempting restart...')
    proxy_dir = _find_proxy_dir()
    if not proxy_dir:
        log.error('[CC Proxy] Cannot find proxy directory. Set CC_PROXY_DIR env var.')
        return False

    # Kill any lingering proxy process
    if _cc_proxy_proc and _cc_proxy_proc.poll() is None:
        _cc_proxy_proc.terminate()
        try:
            _cc_proxy_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _cc_proxy_proc.kill()

    env = os.environ.copy()
    env['HOST'] = '127.0.0.1'  # Override conda's HOST
    _cc_proxy_proc = subprocess.Popen(
        [sys.executable, 'start_proxy.py'],
        cwd=str(proxy_dir),
        env=env,
        stdout=open('/tmp/cc_proxy.log', 'a'),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    log.info('[CC Proxy] Started PID %d, waiting for it to be ready...', _cc_proxy_proc.pid)

    # Wait for proxy to become healthy
    for i in range(30):  # up to 30 seconds
        time.sleep(1)
        try:
            resp = requests.get(f'{CC_PROXY_URL}/health', timeout=3)
            if resp.status_code == 200:
                log.info('[CC Proxy] Healthy after %ds', i + 1)
                return True
        except Exception:
            pass

    log.error('[CC Proxy] Failed to start after 30s')
    return False


# ─── Inference: Claude Code ───────────────────────────────────────────────────

def _is_cc_retryable_error(stdout: str, stderr: str) -> bool:
    """Check if CC output indicates a retryable error (429, ECONNREFUSED, etc)."""
    combined = (stdout or '') + (stderr or '')
    retryable_patterns = [
        'ECONNREFUSED',
        '429',
        'rate_limit',
        'too many requests',
        'overloaded',
        'Unable to connect to API',
        'socket hang up',
        'ETIMEDOUT',
        'ECONNRESET',
    ]
    lower = combined.lower()
    for pat in retryable_patterns:
        if pat.lower() in lower:
            return True
    return False


def run_cc_inference(inst: SWEInstance, workspace: Path,
                    mcfg: ModelConfig = None) -> InferenceResult:
    """Run Claude Code CLI on an instance. Retries on 429/connection errors."""
    tool_name = mcfg.name if mcfg else 'cc'
    cc_model = mcfg.model_id if mcfg else CC_MODEL
    result = InferenceResult(instance_id=inst.instance_id, tool=tool_name)
    prompt = build_agent_prompt(inst)
    t0 = time.time()
    max_retries = 10  # generous — 429s can require many retries

    for attempt in range(1, max_retries + 1):
        # Ensure proxy is alive before each attempt
        if not ensure_cc_proxy_alive():
            log.error('[CC] Proxy dead and cannot restart, attempt %d/%d', attempt, max_retries)
            if attempt < max_retries:
                time.sleep(30)
                continue
            result.error = 'CC proxy unavailable'
            result.duration_s = time.time() - t0
            return result

        # Reset workspace for retry (undo any partial changes)
        if attempt > 1:
            subprocess.run(
                ['git', 'checkout', '.'],
                capture_output=True, text=True, timeout=30, cwd=str(workspace),
            )
            subprocess.run(
                ['git', 'clean', '-fd', '--quiet'],
                capture_output=True, text=True, timeout=30, cwd=str(workspace),
            )

        try:
            proc = subprocess.run(
                [
                    'claude', '-p',
                    '--output-format', 'json',
                    '--model', cc_model,
                    '--dangerously-skip-permissions',
                    prompt,
                ],
                capture_output=True, text=True,
                timeout=INFERENCE_SAFETY_TIMEOUT,  # safety net only
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
            )

            # Check for retryable errors in output
            if _is_cc_retryable_error(proc.stdout, proc.stderr):
                wait = min(30 * attempt, 300)  # 30s, 60s, 90s, ... up to 5min
                log.warning('[CC] Attempt %d/%d hit retryable error, waiting %ds: %.200s',
                            attempt, max_retries, wait,
                            (proc.stdout or proc.stderr or '')[:200])
                if attempt < max_retries:
                    time.sleep(wait)
                    continue
                # Last attempt — fall through to record whatever we got

            result.duration_s = time.time() - t0

            if proc.returncode != 0 and not proc.stdout.strip():
                result.error = f'Exit code {proc.returncode}: {proc.stderr[:500]}'

            # Parse CC JSON output
            if proc.stdout.strip():
                try:
                    data = json.loads(proc.stdout.strip())
                    result.num_turns = data.get('num_turns', 1)
                    usage = data.get('usage', {})
                    result.input_tokens = usage.get('input_tokens', 0)
                    result.output_tokens = usage.get('output_tokens', 0)
                    result.cache_read_tokens = usage.get('cache_read_input_tokens', 0)
                    result.cache_write_tokens = usage.get('cache_creation_input_tokens', 0)
                    result.raw_output = proc.stdout[:50000]  # save full output for detail file
                except (json.JSONDecodeError, KeyError, TypeError):
                    result.raw_output = proc.stdout[:20000]

            result.cost_usd = _compute_cost(result, mcfg)
            break  # success — exit retry loop

        except subprocess.TimeoutExpired:
            result.duration_s = time.time() - t0
            result.error = f'Safety timeout after {INFERENCE_SAFETY_TIMEOUT}s'
            break  # don't retry timeouts
        except Exception as e:
            result.duration_s = time.time() - t0
            result.error = str(e)
            if attempt < max_retries:
                log.warning('[CC] Attempt %d/%d exception, retrying: %s', attempt, max_retries, e)
                time.sleep(30 * attempt)
                continue
            break

    result.model_patch = _extract_git_diff(workspace)
    return result


# ─── Patch Extraction ─────────────────────────────────────────────────────────

_EXCLUDE_PREFIXES = [
    '.chatui/', '__pycache__/', '.project_sessions/',
    '.claude/', '.git/',
]

def _extract_git_diff(workspace: Path) -> str:
    """Extract git diff of changes made by the agent."""
    try:
        subprocess.run(
            ['git', 'add', '-A'],
            capture_output=True, text=True, timeout=10, cwd=str(workspace),
        )
        r_files = subprocess.run(
            ['git', 'diff', '--cached', '--name-only'],
            capture_output=True, text=True, timeout=10, cwd=str(workspace),
        )
        if r_files.returncode != 0:
            return ''

        source_files = []
        for f in r_files.stdout.strip().split('\n'):
            f = f.strip()
            if not f:
                continue
            if any(f.startswith(p) for p in _EXCLUDE_PREFIXES):
                continue
            if f.endswith('.pyc'):
                continue
            source_files.append(f)

        if not source_files:
            return ''

        r = subprocess.run(
            ['git', 'diff', '--cached', '--'] + source_files,
            capture_output=True, text=True, timeout=30, cwd=str(workspace),
        )
        diff = r.stdout.rstrip('\r') if r.returncode == 0 else ''
        if diff and not diff.endswith('\n'):
            diff += '\n'
        return diff
    except Exception as e:
        log.warning('Failed to extract diff: %s', e)
        return ''


# ─── Evaluation ───────────────────────────────────────────────────────────────

def _parse_django_log_fixed(log: str, test_spec) -> dict[str, str]:
    """Parse Django test output, correctly handling multi-line docstring output.

    The official swebench parse_log_django misparses tests with docstrings because
    the verbose output spans two lines:
        test_foo (module.TestClass)
        Description of the test ... ok

    The parser takes the docstring line as the key instead of the test name line.
    This version tracks prev_test to handle this correctly.
    """
    from swebench.harness.grading import TestStatus
    status_map = {}
    lines = log.split('\n')
    prev_test = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            prev_test = None
            continue

        # Check if this is a test name line: "test_foo (module.Class)" without " ... "
        test_name_match = re.match(r'^(\S+\s+\([^)]+\))\s*$', stripped)
        if test_name_match:
            prev_test = test_name_match.group(1)
            continue

        # Check for status suffixes
        status = None
        test_name = None

        if ' ... ok' in stripped or ' ... OK' in stripped or ' ...  OK' in stripped:
            status = TestStatus.PASSED.value
            test_name = re.split(r'\s+\.\.\.\s+(?:ok|OK)', stripped)[0]
        elif ' ... skipped' in stripped:
            status = TestStatus.SKIPPED.value
            test_name = stripped.split(' ... skipped')[0]
        elif stripped.endswith(' ... FAIL'):
            status = TestStatus.FAILED.value
            test_name = stripped.rsplit(' ... FAIL', 1)[0]
        elif stripped.endswith(' ... ERROR'):
            status = TestStatus.ERROR.value
            test_name = stripped.rsplit(' ... ERROR', 1)[0]
        elif stripped.startswith('FAIL:'):
            status = TestStatus.FAILED.value
            parts = stripped.split()
            test_name = parts[1].strip() if len(parts) > 1 else stripped
        elif stripped.startswith('ERROR:'):
            status = TestStatus.ERROR.value
            parts = stripped.split()
            test_name = parts[1].strip() if len(parts) > 1 else stripped

        if status:
            # If the test_name looks like a docstring and we have prev_test, use prev_test
            if prev_test and not re.match(r'^(\w+)\s+\(', test_name):
                # test_name is a docstring — use prev_test instead
                status_map[prev_test] = status
            else:
                status_map[test_name] = status
            prev_test = None
        elif not test_name_match:
            # Non-test line that doesn't reset state (could be continuation)
            pass

    return status_map


def _get_test_directives(inst: SWEInstance) -> list[str]:
    """Extract test directives from test_patch using official SWE-bench logic.

    For Django: converts file paths to module notation (tests/auth_tests/test_validators.py → auth_tests.test_validators).
    For others: returns file paths as-is (e.g. tests/test_foo.py).

    If the test_patch only modifies non-Python files (e.g. .txt, .json), extracts
    unique test modules from the F2P + P2P test IDs as a fallback.
    """
    diff_pat = r"diff --git a/.* b/(.*)"
    directives = re.findall(diff_pat, inst.test_patch)
    # Remove non-test files
    non_test_exts = ['.txt', '.md', '.rst', '.json', '.yml', '.yaml', '.cfg', '.ini', '.toml']
    directives = [d for d in directives if not any(d.endswith(ext) for ext in non_test_exts)]

    if inst.repo == 'django/django':
        transformed = []
        for d in directives:
            d = d[:-len('.py')] if d.endswith('.py') else d
            d = d[len('tests/'):] if d.startswith('tests/') else d
            d = d.replace('/', '.')
            transformed.append(d)
        directives = transformed

    # Fallback: when test_patch only modifies non-.py files (e.g. .txt fixtures),
    # extract unique test modules from F2P + P2P test IDs
    if not directives:
        modules = set()
        for test_id in inst.fail_to_pass + inst.pass_to_pass:
            if inst.repo == 'django/django':
                # Django format: "test_method (module.TestClass)"
                m = re.match(r'\S+\s+\(([^)]+)\)', test_id)
                if m:
                    parts = m.group(1).split('.')
                    mod = '.'.join(parts[:2]) if len(parts) >= 2 else parts[0]
                    modules.add(mod)
            elif '::' in test_id:
                # pytest format: "path/to/test.py::test_func"
                modules.add(test_id.split('::')[0])
            elif '/' in test_id:
                modules.add(test_id)
        directives = sorted(modules)
        if directives:
            log.info('  Extracted %d test directives from F2P/P2P (no .py in test_patch)',
                     len(directives))

    return directives


def evaluate_patch(
    inst: SWEInstance,
    model_patch: str,
    tool: str,
    base_dir: Path,
    env_map: dict[str, Path],
) -> EvalResult:
    """Evaluate a model-generated patch using official SWE-bench test methodology.

    Uses the EXACT same approach as the official SWE-bench Docker harness:
    1. Create fresh workspace at base_commit
    2. Apply model_patch + gold test_patch
    3. Install package in conda env
    4. Run the FULL test command once (with test directives from test_patch)
    5. Parse output with official repo-specific log parser
    6. Grade with official get_eval_tests_report
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.grading import (
        MAP_REPO_TO_PARSER, get_eval_tests_report, get_resolution_status,
        ResolvedStatus, TestStatus, FAIL_TO_PASS, PASS_TO_PASS,
        EvalType, FAIL_ONLY_REPOS,
    )

    result = EvalResult(instance_id=inst.instance_id, tool=tool)

    if not model_patch:
        result.error = 'Empty patch'
        return result

    # Get conda env
    env_key = f'{inst.repo}/{inst.version}'
    env_path = env_map.get(env_key)

    # Get specs
    specs = {}
    if inst.repo in MAP_REPO_VERSION_TO_SPECS:
        specs = MAP_REPO_VERSION_TO_SPECS[inst.repo].get(inst.version, {})

    # --- Workspace setup ---
    eval_ws = base_dir / 'eval' / f'{inst.instance_id}__{tool}'
    if eval_ws.exists():
        shutil.rmtree(eval_ws)

    try:
        repo_path = get_repo_path(inst.repo)
        subprocess.run(
            ['git', 'clone', '--quiet', '--shared', str(repo_path), str(eval_ws)],
            capture_output=True, text=True, timeout=300, check=True,
        )
        subprocess.run(
            ['git', 'checkout', '--quiet', inst.base_commit],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws), check=True,
        )
        subprocess.run(
            ['git', 'clean', '-fdx', '--quiet'],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
        )
    except Exception as e:
        result.error = f'Workspace setup failed: {e}'
        return result

    # --- Apply model patch ---
    patch_file = eval_ws / '__model_patch.diff'
    patch_file.write_text(model_patch)
    r = subprocess.run(
        ['git', 'apply', '--whitespace=fix', str(patch_file)],
        capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
    )
    result.patch_applies = (r.returncode == 0)
    result.patch_apply_stderr = r.stderr[:2000] if r.stderr else ''
    if not result.patch_applies:
        r2 = subprocess.run(
            ['git', 'apply', '--whitespace=fix', '-C1', str(patch_file)],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
        )
        result.patch_applies = (r2.returncode == 0)
        if result.patch_applies:
            result.patch_apply_stderr = '(applied with -C1)'
        else:
            result.patch_apply_stderr += '\n--- fallback -C1 ---\n' + (r2.stderr[:1000] or '')
            subprocess.run(
                ['git', 'apply', '--whitespace=fix', '--reject', str(patch_file)],
                capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
            )
            result.error = f'Patch apply failed: {r.stderr[:300]}'

    # --- Apply gold test patch ---
    test_patch_file = eval_ws / '__test_patch.diff'
    test_patch_file.write_text(inst.test_patch)
    r = subprocess.run(
        ['git', 'apply', '--whitespace=fix', str(test_patch_file)],
        capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
    )
    result.test_patch_applies = (r.returncode == 0)
    result.test_patch_apply_stderr = r.stderr[:2000] if r.stderr else ''
    if not result.test_patch_applies:
        r2 = subprocess.run(
            ['git', 'apply', '--whitespace=fix', '-C1', str(test_patch_file)],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
        )
        result.test_patch_applies = (r2.returncode == 0)
        if not result.test_patch_applies:
            result.test_patch_apply_stderr += '\n--- fallback ---\n' + (r2.stderr[:1000] or '')
            subprocess.run(
                ['git', 'apply', '--whitespace=fix', '--reject', str(test_patch_file)],
                capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
            )

    # --- Install package ---
    if env_path:
        install_r = _install_in_conda(inst, eval_ws, env_path, specs)
        if install_r:
            result.install_stdout = (install_r.stdout or '')[-5000:]
            result.install_stderr = (install_r.stderr or '')[-5000:]

    # --- Run full test command (official SWE-bench approach) ---
    test_directives = _get_test_directives(inst)
    test_cmd_template = specs.get('test_cmd', 'python -m pytest -rA')
    if isinstance(test_cmd_template, list):
        test_cmd_template = test_cmd_template[-1]

    # Build full test command with directives
    full_test_cmd = f'{test_cmd_template} {" ".join(test_directives)}'

    # Set up environment variables from eval_commands
    env_setup_cmds = []
    for cmd in specs.get('eval_commands', []):
        if cmd.startswith('export '):
            env_setup_cmds.append(cmd)
        elif 'locale-gen' in cmd or 'locale.gen' in cmd:
            continue  # skip locale setup (requires root)
    env_prefix = '; '.join(env_setup_cmds) + '; ' if env_setup_cmds else ''
    # Always set LANG for Django tests
    if 'django' in inst.repo:
        env_prefix = 'export LANG=en_US.UTF-8; export LC_ALL=en_US.UTF-8; ' + env_prefix

    log.info('  Running tests: %s', full_test_cmd[:200])

    # Run the full test command
    t0 = time.time()
    test_output = TestOutput(test_id='__full_test_run__')
    try:
        if env_path:
            r = _conda_run(
                env_path,
                f'bash -c "{env_prefix}{full_test_cmd}"',
                cwd=str(eval_ws),
                timeout=TEST_TIMEOUT,
            )
        else:
            r = subprocess.run(
                f'bash -c "{env_prefix}{full_test_cmd}"',
                shell=True, capture_output=True, text=True,
                timeout=TEST_TIMEOUT, cwd=str(eval_ws),
            )
        test_output.stdout = (r.stdout or '')[-50000:]  # keep more for log parsing
        test_output.stderr = (r.stderr or '')[-50000:]
        test_output.return_code = r.returncode
        test_output.command = full_test_cmd
    except subprocess.TimeoutExpired as e:
        test_output.stderr = f'TIMEOUT after {TEST_TIMEOUT}s'
        raw = getattr(e, 'stdout', None)
        if isinstance(raw, bytes):
            test_output.stdout = raw.decode('utf-8', errors='replace')[-50000:]
        result.error = f'Test timeout after {TEST_TIMEOUT}s'
    except Exception as e:
        test_output.stderr = f'Test execution error: {e}'
        result.error = str(e)
    test_output.duration_s = round(time.time() - t0, 2)

    # Store the full test output for debugging
    result.fail_to_pass_outputs = [test_output]

    # --- Parse test output with official log parser ---
    log_content = test_output.stdout + '\n' + test_output.stderr
    # Use our fixed Django parser that handles multi-line docstring output
    if inst.repo == 'django/django':
        log_parser = _parse_django_log_fixed
    else:
        log_parser = MAP_REPO_TO_PARSER.get(inst.repo)

    if log_parser:
        try:
            # Create a minimal TestSpec-like object for the parser
            class _MinimalTestSpec:
                def __init__(self, inst):
                    self.instance_id = inst.instance_id
                    self.repo = inst.repo
                    self.version = inst.version
                    self.FAIL_TO_PASS = inst.fail_to_pass
                    self.PASS_TO_PASS = inst.pass_to_pass

            test_spec = _MinimalTestSpec(inst)
            status_map = log_parser(log_content, test_spec)
            log.info('  Parsed %d test results from log output', len(status_map))

            # Grade using official logic
            eval_ref = {
                'instance_id': inst.instance_id,
                FAIL_TO_PASS: inst.fail_to_pass,
                PASS_TO_PASS: inst.pass_to_pass,
            }
            eval_type = EvalType.FAIL_ONLY if inst.repo in FAIL_ONLY_REPOS else EvalType.PASS_AND_FAIL
            report = get_eval_tests_report(status_map, eval_ref, eval_type=eval_type)

            # Extract results
            f2p_success = report.get(FAIL_TO_PASS, {}).get('success', [])
            f2p_failure = report.get(FAIL_TO_PASS, {}).get('failure', [])
            p2p_success = report.get(PASS_TO_PASS, {}).get('success', [])
            p2p_failure = report.get(PASS_TO_PASS, {}).get('failure', [])

            for t in f2p_success:
                result.fail_to_pass_results[t] = True
            for t in f2p_failure:
                result.fail_to_pass_results[t] = False
            for t in p2p_success:
                result.pass_to_pass_results[t] = True
            for t in p2p_failure:
                result.pass_to_pass_results[t] = False

            # Resolved = all F2P pass AND all P2P pass (official criteria)
            result.resolved = (
                get_resolution_status(report) == ResolvedStatus.FULL.value
            )

            log.info('  F2P: %d/%d passed, P2P: %d/%d passed, resolved=%s',
                     len(f2p_success), len(f2p_success) + len(f2p_failure),
                     len(p2p_success), len(p2p_success) + len(p2p_failure),
                     result.resolved)

        except Exception as e:
            log.warning('  Log parser failed: %s', e, exc_info=True)
            result.error = f'Log parser failed: {e}'
    else:
        log.warning('  No log parser for repo %s — using return code', inst.repo)
        # Fallback: treat return code 0 as all tests passed
        if test_output.return_code == 0:
            for t in inst.fail_to_pass:
                result.fail_to_pass_results[t] = True
            for t in inst.pass_to_pass:
                result.pass_to_pass_results[t] = True
            result.resolved = True

    return result


def _install_in_conda(inst: SWEInstance, workspace: Path, env_path: Path,
                      specs: dict = None):
    """Install the package in the conda env for testing."""
    if specs is None:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        specs = {}
        if inst.repo in MAP_REPO_VERSION_TO_SPECS:
            specs = MAP_REPO_VERSION_TO_SPECS[inst.repo].get(inst.version, {})

    # Run pre_install (skip apt commands)
    for cmd in specs.get('pre_install', []):
        if 'apt-get' in cmd or 'apt ' in cmd or 'DEBIAN_FRONTEND' in cmd:
            continue
        _conda_run(env_path, f'bash -c "{cmd}"', cwd=str(workspace), timeout=120)

    # Run eval_commands that are NOT 'export' (those are env vars, handled at test time)
    for cmd in specs.get('eval_commands', []):
        if cmd.startswith('export '):
            continue  # handled at test time
        if 'locale-gen' in cmd or 'locale.gen' in cmd:
            continue  # requires root
        if 'apt-get' in cmd or 'apt ' in cmd:
            continue
        _conda_run(env_path, f'bash -c "{cmd}"', cwd=str(workspace), timeout=120)

    # Install
    install_cmd = specs.get('install', 'python -m pip install -e .')
    log.debug('  Installing: %s', install_cmd[:100])
    r = _conda_run(env_path, install_cmd, cwd=str(workspace), timeout=600)
    if r.returncode != 0:
        log.warning('  Install failed: %s', r.stderr[:200])
    return r


    # (Old per-test runner functions removed — evaluation now uses official SWE-bench
    #  log parsers via evaluate_patch() above)


# ─── Cost Computation ─────────────────────────────────────────────────────────

def _compute_cost(result: InferenceResult, mcfg: ModelConfig = None) -> float:
    """Compute cost from token counts using per-model pricing."""
    pi = mcfg.price_input if mcfg else PRICE_INPUT_PER_1K
    po = mcfg.price_output if mcfg else PRICE_OUTPUT_PER_1K
    pcr = mcfg.price_cache_read if mcfg else PRICE_CACHE_READ_PER_1K
    pcw = mcfg.price_cache_write if mcfg else PRICE_CACHE_WRITE_PER_1K
    cost = (
        result.input_tokens * pi / 1000
        + result.output_tokens * po / 1000
        + result.cache_read_tokens * pcr / 1000
        + result.cache_write_tokens * pcw / 1000
    )
    return round(cost, 6)


# ─── Results Persistence ──────────────────────────────────────────────────────

def _save_per_run_detail(
    base_dir: Path,
    inst: SWEInstance,
    tool: str,
    inf_result: 'InferenceResult',
    eval_result: 'EvalResult | None',
    br: BenchmarkResult,
):
    """Save full per-run detail: patch, raw agent output, test outputs.

    Directory structure:
        workdir/
          patches/{instance_id}__{tool}.diff
          details/{instance_id}__{tool}.json
    """
    # Always save the model patch
    patch_dir = base_dir / 'patches'
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_file = patch_dir / f'{inst.instance_id}__{tool}.diff'
    patch_file.write_text(inf_result.model_patch or '# (empty — no patch generated)\n')

    # Build detailed record
    detail = {
        'instance_id': inst.instance_id,
        'repo': inst.repo,
        'version': inst.version,
        'difficulty': inst.difficulty,
        'tool': tool,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        # Inference details
        'inference': {
            'duration_s': inf_result.duration_s,
            'input_tokens': inf_result.input_tokens,
            'output_tokens': inf_result.output_tokens,
            'cache_read_tokens': inf_result.cache_read_tokens,
            'cache_write_tokens': inf_result.cache_write_tokens,
            'cost_usd': inf_result.cost_usd,
            'num_turns': inf_result.num_turns,
            'error': inf_result.error,
            'patch_size': len(inf_result.model_patch),
            'raw_output': inf_result.raw_output,  # full CC JSON or Tofu poll response
        },
        # Eval details
        'eval': None,
        # Summary
        'resolved': br.resolved,
        'patch_applies': br.patch_applies,
        'gold_patch_size': len(inst.patch),
    }

    if eval_result:
        # Full test run output (single run, parsed with official log parser)
        test_run_outputs = []
        for to in getattr(eval_result, 'fail_to_pass_outputs', []):
            test_run_outputs.append({
                'test_id': to.test_id,
                'duration_s': to.duration_s,
                'command': to.command,
                'return_code': to.return_code,
                'stdout': to.stdout[-30000:] if to.stdout else '',
                'stderr': to.stderr[-10000:] if to.stderr else '',
            })

        detail['eval'] = {
            'resolved': eval_result.resolved,
            'patch_applies': eval_result.patch_applies,
            'test_patch_applies': eval_result.test_patch_applies,
            'patch_apply_stderr': eval_result.patch_apply_stderr,
            'test_patch_apply_stderr': eval_result.test_patch_apply_stderr,
            'install_stdout': (eval_result.install_stdout or '')[-3000:],
            'install_stderr': (eval_result.install_stderr or '')[-3000:],
            'fail_to_pass': eval_result.fail_to_pass_results,
            'pass_to_pass': eval_result.pass_to_pass_results,
            'test_run_output': test_run_outputs,
            'error': eval_result.error,
        }

    detail_dir = base_dir / 'details'
    detail_dir.mkdir(parents=True, exist_ok=True)
    detail_file = detail_dir / f'{inst.instance_id}__{tool}.json'
    with open(detail_file, 'w') as f:
        json.dump(detail, f, indent=2, ensure_ascii=False)

    log.debug('Saved detail: %s (%d bytes)', detail_file.name, detail_file.stat().st_size)


def _save_results(results: list[BenchmarkResult], instances: list[SWEInstance],
                  output_path: Path):
    """Save results to JSON after each instance."""
    summary = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'config': {
            'tofu_model': TOFU_MODEL,
            'cc_model': CC_MODEL,
            'inference_safety_timeout': INFERENCE_SAFETY_TIMEOUT,
            'num_instances': len(instances),
        },
        'instances': [
            {
                'instance_id': inst.instance_id,
                'repo': inst.repo,
                'version': inst.version,
                'difficulty': inst.difficulty,
                'fail_to_pass_count': len(inst.fail_to_pass),
                'pass_to_pass_count': len(inst.pass_to_pass),
                'gold_patch_size': len(inst.patch),
            }
            for inst in instances
        ],
        'results': [asdict(r) for r in results],
        'summary': _compute_summary_stats(results),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def _compute_summary_stats(results: list[BenchmarkResult]) -> dict:
    """Compute summary statistics for the results."""
    stats = {}
    for tool in ['tofu', 'cc']:
        rs = [r for r in results if r.tool == tool]
        if not rs:
            continue
        resolved = sum(1 for r in rs if r.resolved)
        total = len(rs)
        total_cost = sum(r.cost_usd for r in rs)
        avg_time = sum(r.duration_s for r in rs) / max(total, 1)
        avg_turns = sum(r.num_turns for r in rs) / max(total, 1)
        total_cr = sum(r.cache_read_tokens for r in rs)
        total_in = sum(r.input_tokens for r in rs)
        total_cw = sum(r.cache_write_tokens for r in rs)
        cache_rate = total_cr / max(total_in + total_cr + total_cw, 1) * 100

        stats[tool] = {
            'resolved': resolved,
            'total': total,
            'resolve_rate': round(resolved / max(total, 1) * 100, 1),
            'avg_time_s': round(avg_time, 1),
            'total_cost_usd': round(total_cost, 2),
            'avg_turns': round(avg_turns, 1),
            'cache_hit_rate': round(cache_rate, 1),
        }
    return stats


def _load_completed(output_path: Path) -> tuple[list[BenchmarkResult], set]:
    """Load completed results from a previous run."""
    if not output_path.exists():
        return [], set()
    with open(output_path) as f:
        data = json.load(f)
    results = []
    completed = set()
    for r in data.get('results', []):
        br = BenchmarkResult(**{k: v for k, v in r.items() if k in BenchmarkResult.__dataclass_fields__})
        results.append(br)
        completed.add(f'{br.instance_id}__{br.tool}')
    return results, completed


# ─── Reporting ────────────────────────────────────────────────────────────────

def print_instance_result(br: BenchmarkResult):
    """Print result for a single instance."""
    status = '✅' if br.resolved else '❌'
    apply_str = '📎' if br.patch_applies else '💔'
    print(f'    {status} {apply_str} {br.instance_id:<45s} '
          f'⏱{br.duration_s:7.0f}s  💰${br.cost_usd:6.3f}  '
          f'🔄{br.num_turns:3d}t  '
          f'F2P={br.fail_to_pass_passed}/{br.fail_to_pass_total}  '
          f'P2P={br.pass_to_pass_passed}/{br.pass_to_pass_total}')
    if br.error:
        print(f'         ⚠ {br.error[:200]}')


def print_progress(results: list[BenchmarkResult], total_runs: int):
    """Print live progress stats."""
    done = len(results)
    if done == 0:
        return

    for tool in ['tofu', 'cc']:
        rs = [r for r in results if r.tool == tool]
        if not rs:
            continue
        resolved = sum(1 for r in rs if r.resolved)
        total = len(rs)
        cost = sum(r.cost_usd for r in rs)
        label = 'Tofu' if tool == 'tofu' else 'CC  '
        print(f'    📊 {label}: {resolved}/{total} resolved ({100*resolved/max(total,1):.0f}%)  '
              f'${cost:.2f} spent  [{done}/{total_runs} runs done]')


def print_summary(results: list[BenchmarkResult]):
    """Print final comparison summary."""
    print('\n' + '═' * 120)
    print('                              SWE-BENCH VERIFIED — BENCHMARK SUMMARY')
    print('═' * 120)

    for tool in ['tofu', 'cc']:
        rs = [r for r in results if r.tool == tool]
        if not rs:
            continue

        resolved = sum(1 for r in rs if r.resolved)
        total = len(rs)
        avg_time = sum(r.duration_s for r in rs) / max(total, 1)
        total_cost = sum(r.cost_usd for r in rs)
        avg_cost = total_cost / max(total, 1)
        avg_turns = sum(r.num_turns for r in rs) / max(total, 1)
        total_in = sum(r.input_tokens for r in rs)
        total_out = sum(r.output_tokens for r in rs)
        total_cr = sum(r.cache_read_tokens for r in rs)
        total_cw = sum(r.cache_write_tokens for r in rs)
        total_f2p_pass = sum(r.fail_to_pass_passed for r in rs)
        total_f2p = sum(r.fail_to_pass_total for r in rs)
        patch_applies = sum(1 for r in rs if r.patch_applies)
        cache_rate = total_cr / max(total_in + total_cr + total_cw, 1) * 100
        errors = sum(1 for r in rs if r.error and 'timeout' in r.error.lower())

        label = 'Tofu' if tool == 'tofu' else 'Claude Code'
        print(f'\n  {label}:')
        print(f'    ✅ Resolved:         {resolved}/{total} ({100*resolved/max(total,1):.1f}%)')
        print(f'    📎 Patch applies:    {patch_applies}/{total}')
        print(f'    🧪 F2P tests:        {total_f2p_pass}/{total_f2p} passed')
        print(f'    ⏱  Avg time:         {avg_time:.0f}s')
        print(f'    💰 Total cost:       ${total_cost:.2f}  (avg ${avg_cost:.3f}/instance)')
        print(f'    🔄 Avg turns:        {avg_turns:.1f}')
        print(f'    📊 Input tokens:     {total_in:,}')
        print(f'    📊 Output tokens:    {total_out:,}')
        print(f'    💾 Cache read:       {total_cr:,} ({cache_rate:.0f}%)')
        print(f'    💾 Cache write:      {total_cw:,}')
        if errors:
            print(f'    ⚠️  Timeouts:        {errors}')

        # Breakdown by difficulty
        print(f'\n    By difficulty:')
        for diff in ['<15 min fix', '15 min - 1 hour', '1-4 hours', '>4 hours']:
            dr = [r for r in rs if r.difficulty == diff]
            if dr:
                d_resolved = sum(1 for r in dr if r.resolved)
                print(f'      {diff:>15s}: {d_resolved}/{len(dr)} ({100*d_resolved/max(len(dr),1):.0f}%)')

        # Breakdown by repo
        print(f'\n    By repository:')
        repos = sorted(set(r.repo for r in rs))
        for repo in repos:
            rr = [r for r in rs if r.repo == repo]
            r_resolved = sum(1 for r in rr if r.resolved)
            print(f'      {repo:<30s}: {r_resolved}/{len(rr)} ({100*r_resolved/max(len(rr),1):.0f}%)')

    # Head-to-head comparison
    tofu_map = {r.instance_id: r for r in results if r.tool == 'tofu'}
    cc_map = {r.instance_id: r for r in results if r.tool == 'cc'}
    common = set(tofu_map.keys()) & set(cc_map.keys())

    if common:
        print(f'\n  Head-to-head ({len(common)} instances):')
        both = sum(1 for i in common if tofu_map[i].resolved and cc_map[i].resolved)
        tofu_only = sum(1 for i in common if tofu_map[i].resolved and not cc_map[i].resolved)
        cc_only = sum(1 for i in common if not tofu_map[i].resolved and cc_map[i].resolved)
        neither = sum(1 for i in common if not tofu_map[i].resolved and not cc_map[i].resolved)
        print(f'    Both resolved:       {both}')
        print(f'    Tofu only:           {tofu_only}')
        print(f'    CC only:             {cc_only}')
        print(f'    Neither:             {neither}')

        if tofu_only:
            print(f'\n    Tofu-only wins:')
            for i in sorted(common):
                if tofu_map[i].resolved and not cc_map[i].resolved:
                    print(f'      • {i} (Tofu: {tofu_map[i].duration_s:.0f}s/${tofu_map[i].cost_usd:.3f})')
        if cc_only:
            print(f'\n    CC-only wins:')
            for i in sorted(common):
                if not tofu_map[i].resolved and cc_map[i].resolved:
                    print(f'      • {i} (CC: {cc_map[i].duration_s:.0f}s/${cc_map[i].cost_usd:.3f})')

        # Speed comparison on jointly-resolved instances
        joint = [(i, tofu_map[i], cc_map[i]) for i in common
                 if tofu_map[i].resolved and cc_map[i].resolved]
        if joint:
            avg_tofu_t = sum(t.duration_s for _, t, _ in joint) / len(joint)
            avg_cc_t = sum(c.duration_s for _, _, c in joint) / len(joint)
            avg_tofu_c = sum(t.cost_usd for _, t, _ in joint) / len(joint)
            avg_cc_c = sum(c.cost_usd for _, _, c in joint) / len(joint)
            print(f'\n    On jointly-resolved ({len(joint)} instances):')
            print(f'      Avg time:  Tofu {avg_tofu_t:.0f}s vs CC {avg_cc_t:.0f}s  ({avg_cc_t/max(avg_tofu_t,1):.1f}×)')
            print(f'      Avg cost:  Tofu ${avg_tofu_c:.3f} vs CC ${avg_cc_c:.3f}  ({avg_cc_c/max(avg_tofu_c,0.001):.1f}×)')

    print('\n' + '═' * 120)


# ─── Preflight Checks ────────────────────────────────────────────────────────

def check_tofu() -> bool:
    try:
        r = requests.get(f'{TOFU_BASE_URL}/api/health', timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def check_cc() -> bool:
    try:
        r = subprocess.run(['claude', '--version'], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False
    except Exception:
        return False
    try:
        r = requests.get('http://127.0.0.1:8082/health', timeout=5)
        return r.status_code == 200
    except Exception:
        # CC might work without proxy (direct API)
        log.warning('CC proxy not available — CC will use direct API')
        return True


def preflight(tools: list[str]) -> list[str]:
    """Check which tools are available."""
    available = []
    if 'tofu' in tools:
        if check_tofu():
            available.append('tofu')
            log.info('✅ Tofu: %s (model: %s)', TOFU_BASE_URL, TOFU_MODEL)
        else:
            log.error('❌ Tofu NOT reachable at %s', TOFU_BASE_URL)

    if 'cc' in tools:
        if check_cc():
            available.append('cc')
            log.info('✅ Claude Code CLI ready (model: %s)', CC_MODEL)
        else:
            log.error('❌ Claude Code CLI NOT available')

    return available


# ─── Main Pipeline ────────────────────────────────────────────────────────────

def run_pipeline(
    instances: list[SWEInstance],
    tools: list[str],
    base_dir: Path,
    output_path: Path,
    skip_eval: bool = False,
    existing_results: list[BenchmarkResult] = None,
    completed: set = None,
    env_map: dict = None,
    cooldown: int = COOLDOWN_SECONDS,
):
    """Run the full inference + evaluation pipeline.

    Saves results after each instance for robust resume.
    """
    all_results = list(existing_results or [])
    completed = completed or set()
    total_runs = len(instances) * len(tools)
    done_runs = len(completed)
    start_time = time.time()

    log.info('Pipeline: %d instances × %d tools = %d total runs (%d already done)',
             len(instances), len(tools), total_runs, done_runs)

    for idx, inst in enumerate(instances, 1):
        log.info('━' * 80)
        log.info('[%d/%d] %s (%s v%s, %s)',
                 idx, len(instances), inst.instance_id,
                 inst.repo, inst.version, inst.difficulty)
        log.info('  Issue: %s', inst.problem_statement[:150].strip())
        log.info('  Gold: %d chars, F2P: %d tests, P2P: %d tests',
                 len(inst.patch), len(inst.fail_to_pass), len(inst.pass_to_pass))

        for tool in tools:
            run_key = f'{inst.instance_id}__{tool}'
            if run_key in completed:
                log.info('  [%s] Already completed, skipping', tool.upper())
                continue

            done_runs += 1
            elapsed_total = time.time() - start_time
            avg_per_run = elapsed_total / max(done_runs - len(completed or set()), 1)
            remaining = (total_runs - done_runs) * avg_per_run
            eta = time.strftime('%H:%M', time.localtime(time.time() + remaining)) if remaining > 0 else '?'

            log.info('  [%s] Running (%d/%d, ETA: %s)...',
                     tool.upper(), done_runs, total_runs, eta)

            try:
                # Phase 1: Inference
                workspace = setup_workspace(inst, tool, base_dir)

                if tool == 'tofu':
                    inf_result = run_tofu_inference(inst, workspace)
                else:
                    inf_result = run_cc_inference(inst, workspace)

                log.info('  [%s] Patch: %d chars, %.0fs, $%.3f, %d turns',
                         tool.upper(), len(inf_result.model_patch),
                         inf_result.duration_s, inf_result.cost_usd, inf_result.num_turns)
                if inf_result.error:
                    log.warning('  [%s] Error: %s', tool.upper(), inf_result.error[:200])

                # Phase 2: Evaluation
                br = BenchmarkResult(
                    instance_id=inst.instance_id,
                    repo=inst.repo,
                    difficulty=inst.difficulty,
                    tool=tool,
                    duration_s=inf_result.duration_s,
                    cost_usd=inf_result.cost_usd,
                    input_tokens=inf_result.input_tokens,
                    output_tokens=inf_result.output_tokens,
                    cache_read_tokens=inf_result.cache_read_tokens,
                    cache_write_tokens=inf_result.cache_write_tokens,
                    num_turns=inf_result.num_turns,
                )

                eval_result = None  # track for _save_per_run_detail

                if skip_eval:
                    br.error = 'Eval skipped'
                elif inf_result.model_patch:
                    log.info('  [%s] Evaluating...', tool.upper())
                    eval_result = evaluate_patch(inst, inf_result.model_patch, tool, base_dir,  # noqa
                                                env_map or {})
                    br.resolved = eval_result.resolved
                    br.patch_applies = eval_result.patch_applies
                    br.fail_to_pass_passed = sum(1 for v in eval_result.fail_to_pass_results.values() if v)
                    br.fail_to_pass_total = len(eval_result.fail_to_pass_results)
                    br.pass_to_pass_passed = sum(1 for v in eval_result.pass_to_pass_results.values() if v)
                    br.pass_to_pass_total = len(eval_result.pass_to_pass_results)
                    if eval_result.error:
                        br.error = eval_result.error
                else:
                    br.error = inf_result.error or 'No patch generated'

                print_instance_result(br)
                all_results.append(br)

                # Save per-run detail (patch, raw output, test outputs)
                _save_per_run_detail(base_dir, inst, tool, inf_result, eval_result, br)

            except Exception as e:
                # Catch ANY crash in inference/eval so we don't lose the whole run
                log.error('  [%s] CRASH on %s: %s', tool.upper(), inst.instance_id, e, exc_info=True)
                br = BenchmarkResult(
                    instance_id=inst.instance_id,
                    repo=inst.repo,
                    difficulty=inst.difficulty,
                    tool=tool,
                    error=f'CRASH: {e}',
                )
                all_results.append(br)

            # Save summary after each run (even on crash)
            try:
                _save_results(all_results, instances, output_path)
            except Exception as e:
                log.error('Failed to save results: %s', e, exc_info=True)

            # Print progress every 10 runs
            if done_runs % 10 == 0:
                print_progress(all_results, total_runs)

            # Cooldown between runs
            if done_runs < total_runs and cooldown > 0:
                time.sleep(cooldown)

    return all_results


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='SWE-bench Verified evaluation: Tofu vs Claude Code CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--num', type=int, default=None,
                        help='Number of instances (default: 5 unless --all)')
    parser.add_argument('--all', action='store_true',
                        help='Run ALL 500 instances')
    parser.add_argument('--instances', type=str, default='',
                        help='Comma-separated instance IDs')
    parser.add_argument('--repo', type=str, default='',
                        help='Filter by repository')
    parser.add_argument('--difficulty', type=str, default='',
                        help='Filter by difficulty')
    parser.add_argument('--tool', choices=['tofu', 'cc', 'both'], default='both')
    parser.add_argument('--delay', type=int, default=COOLDOWN_SECONDS,
                        help=f'Delay between runs (default: {COOLDOWN_SECONDS}s)')
    parser.add_argument('--skip-eval', action='store_true',
                        help='Inference only, save patches')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--setup-envs-only', action='store_true',
                        help='Only set up conda environments, no inference')
    parser.add_argument('--output', type=str, default='',
                        help='Output JSON path')
    parser.add_argument('--workdir', type=str, default=str(DEFAULT_WORKDIR),
                        help='Working directory for workspaces')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from existing results')
    parser.add_argument('--reeval', action='store_true',
                        help='Re-evaluate all existing patches (skip inference)')
    parser.add_argument('--skip-c-repos', action='store_true', default=True,
                        help='Skip repos requiring C compilation (astropy, sklearn, matplotlib). Default: True')
    parser.add_argument('--include-c-repos', action='store_true',
                        help='Include C-extension repos (requires Docker-like env)')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    _cooldown = args.delay

    base_dir = Path(args.workdir)
    base_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else base_dir / 'swebench_results.json'

    # ─── Set up structured log file (always persisted) ─────────────────────
    log_file = base_dir / 'swebench_runner.log'
    file_handler = logging.FileHandler(str(log_file), mode='a', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-5s [%(funcName)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    log.addHandler(file_handler)

    # Redirect stderr to the log file so tracebacks are never lost
    # (critical when running under nohup > /dev/null 2>&1)
    stderr_log = open(str(base_dir / 'swebench_stderr.log'), 'a', encoding='utf-8')
    sys.stderr = stderr_log

    log.info('═══ SWE-bench runner started — log file: %s ═══', log_file)

    # Load instances
    log.info('Loading SWE-bench Verified dataset...')
    instance_ids = [x.strip() for x in args.instances.split(',') if x.strip()] if args.instances else None
    num = args.num if args.num else (None if args.all else 5)
    skip_repos = C_EXTENSION_REPOS if (args.skip_c_repos and not args.include_c_repos) else None
    instances = load_swebench_instances(
        num=num,
        instance_ids=instance_ids,
        repo_filter=args.repo or None,
        difficulty_filter=args.difficulty or None,
        seed=args.seed,
        load_all=args.all,
        skip_repos=skip_repos,
    )
    log.info('Selected %d instances', len(instances))

    # Dry-run
    if args.dry_run:
        print(f'\n📋 Selected {len(instances)} instances:\n')
        for inst in instances:
            print(f'  [{inst.difficulty:>15s}] {inst.instance_id:<45s} ({inst.repo} v{inst.version})')
        diffs = Counter(i.difficulty for i in instances)
        repos = Counter(i.repo for i in instances)
        print(f'\n  By difficulty: {dict(sorted(diffs.items()))}')
        print(f'  By repo: {dict(sorted(repos.items(), key=lambda x: -x[1]))}')

        # Estimate cost
        avg_cost_per_instance = 0.5  # rough estimate
        est_cost = len(instances) * avg_cost_per_instance * (2 if args.tool == 'both' else 1)
        est_time_h = len(instances) * 2 * (2 if args.tool == 'both' else 1) / 60  # ~2 min avg
        print(f'\n  Estimated cost: ~${est_cost:.0f}')
        print(f'  Estimated time: ~{est_time_h:.1f} hours')
        return

    # Setup conda envs only
    if args.setup_envs_only:
        env_map = setup_all_conda_envs(instances)
        log.info('Done. %d environments ready.', sum(1 for v in env_map.values() if v))
        return

    # Preflight
    requested_tools = ['tofu', 'cc'] if args.tool == 'both' else [args.tool]
    tools = preflight(requested_tools)
    if not tools:
        log.error('No tools available. Exiting.')
        sys.exit(1)

    # Resume
    existing_results, completed = [], set()
    if args.resume and not args.reeval:
        existing_results, completed = _load_completed(output_path)
        if completed:
            log.info('Resuming: %d runs already completed', len(completed))

    # Pre-build conda environments
    log.info('Setting up conda environments...')
    env_map = setup_all_conda_envs(instances)

    # Pre-clone all repos
    log.info('Pre-cloning repositories...')
    repos = set(inst.repo for inst in instances)
    for repo in sorted(repos):
        try:
            get_repo_path(repo)
        except Exception as e:
            log.error('Failed to clone %s: %s', repo, e)

    # ─── Re-evaluate existing patches (--reeval) ──────────────────────────
    if args.reeval:
        log.info('═══ RE-EVALUATION MODE: re-running eval on existing patches ═══')
        patch_dir = base_dir / 'patches'
        inst_map = {inst.instance_id: inst for inst in instances}
        reeval_results = []

        patch_files = sorted(patch_dir.glob('*.diff')) if patch_dir.exists() else []
        log.info('Found %d patch files to re-evaluate', len(patch_files))

        for pf in patch_files:
            # Parse filename: {instance_id}__{tool}.diff
            stem = pf.stem  # e.g. "astropy__astropy-12907__tofu"
            parts = stem.rsplit('__', 1)
            if len(parts) != 2:
                log.warning('Skipping malformed patch filename: %s', pf.name)
                continue
            iid, tool = parts
            if tool not in requested_tools:
                continue
            inst = inst_map.get(iid)
            if not inst:
                log.warning('Instance %s not in selected set, skipping', iid)
                continue

            model_patch = pf.read_text()
            if not model_patch or model_patch.startswith('# (empty'):
                log.info('  [%s] %s — empty patch, skipping', tool.upper(), iid)
                continue

            log.info('  [%s] %s — re-evaluating (%d chars)', tool.upper(), iid, len(model_patch))
            eval_result = evaluate_patch(inst, model_patch, tool, base_dir, env_map or {})

            # Build result from existing detail file + new eval
            detail_file = base_dir / 'details' / f'{iid}__{tool}.json'
            inf_data = {}
            if detail_file.exists():
                with open(detail_file) as f:
                    detail = json.load(f)
                inf_data = detail.get('inference', {})

            br = BenchmarkResult(
                instance_id=iid,
                repo=inst.repo,
                difficulty=inst.difficulty,
                tool=tool,
                duration_s=inf_data.get('duration_s', 0),
                cost_usd=inf_data.get('cost_usd', 0),
                input_tokens=inf_data.get('input_tokens', 0),
                output_tokens=inf_data.get('output_tokens', 0),
                cache_read_tokens=inf_data.get('cache_read_tokens', 0),
                cache_write_tokens=inf_data.get('cache_write_tokens', 0),
                num_turns=inf_data.get('num_turns', 0),
                resolved=eval_result.resolved,
                patch_applies=eval_result.patch_applies,
                fail_to_pass_passed=sum(1 for v in eval_result.fail_to_pass_results.values() if v),
                fail_to_pass_total=len(eval_result.fail_to_pass_results),
                pass_to_pass_passed=sum(1 for v in eval_result.pass_to_pass_results.values() if v),
                pass_to_pass_total=len(eval_result.pass_to_pass_results),
                error=eval_result.error or '',
            )
            print_instance_result(br)
            reeval_results.append(br)

            # Update detail file with new eval
            dummy_inf = type('obj', (object,), {
                'model_patch': model_patch,
                'duration_s': inf_data.get('duration_s', 0),
                'cost_usd': inf_data.get('cost_usd', 0),
                'input_tokens': inf_data.get('input_tokens', 0),
                'output_tokens': inf_data.get('output_tokens', 0),
                'cache_read_tokens': inf_data.get('cache_read_tokens', 0),
                'cache_write_tokens': inf_data.get('cache_write_tokens', 0),
                'num_turns': inf_data.get('num_turns', 0),
                'error': inf_data.get('error', ''),
                'raw_output': inf_data.get('raw_output', ''),
            })()
            _save_per_run_detail(base_dir, inst, tool, dummy_inf, eval_result, br)

        # Save updated results (merge with any existing non-reevaled results)
        _save_results(reeval_results, instances, output_path)
        print_summary(reeval_results)
        log.info('Re-evaluation complete. Results saved: %s', output_path)
        return

    # Summary before starting
    new_runs = len(instances) * len(tools) - len(completed)
    log.info('')
    log.info('=' * 60)
    log.info('  SWE-bench Verified Benchmark')
    log.info('  Instances: %d | Tools: %s | New runs: %d',
             len(instances), ', '.join(tools), new_runs)
    log.info('  Workdir: %s', base_dir)
    log.info('  Output: %s', output_path)
    log.info('  Safety timeout: %ds | Cooldown: %ds',
             INFERENCE_SAFETY_TIMEOUT, _cooldown)
    log.info('=' * 60)
    log.info('')

    # Run
    results = run_pipeline(
        instances, tools, base_dir, output_path,
        skip_eval=args.skip_eval,
        existing_results=existing_results,
        completed=completed,
        env_map=env_map,
        cooldown=_cooldown,
    )

    # Final summary
    print_summary(results)
    log.info('Results saved: %s', output_path)
    log.info('Workdir: %s', base_dir)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info('Interrupted by user (Ctrl+C)')
        sys.exit(130)
    except Exception:
        log.critical('Fatal unhandled exception', exc_info=True)
        sys.exit(1)
