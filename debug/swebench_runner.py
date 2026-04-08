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
TEST_TIMEOUT = int(os.environ.get('TEST_TIMEOUT', '300'))
# Cooldown between API calls to avoid rate limits
COOLDOWN_SECONDS = int(os.environ.get('COOLDOWN_SECONDS', '5'))

# Directories
DEFAULT_WORKDIR = Path(os.environ.get('SWEBENCH_WORKDIR', '/tmp/swebench_full'))
REPO_CACHE_DIR = Path(os.environ.get('REPO_CACHE_DIR', '/tmp/swebench_repos'))
CONDA_ENV_PREFIX = Path(os.environ.get(
    'CONDA_ENV_DIR',
    '/tmp/swebench_conda_envs',
))

# Pricing (Opus via example-corp gateway — Anthropic convention)
PRICE_INPUT_PER_1K = 0.015
PRICE_OUTPUT_PER_1K = 0.075
PRICE_CACHE_READ_PER_1K = 0.0015
PRICE_CACHE_WRITE_PER_1K = 0.01875

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

def load_swebench_instances(
    num: int = None,
    instance_ids: list = None,
    repo_filter: str = None,
    difficulty_filter: str = None,
    seed: int = 42,
    load_all: bool = False,
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
        ['git', 'clone', '--quiet', '--no-local', str(repo_path), str(ws)],
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

def run_tofu_inference(inst: SWEInstance, workspace: Path) -> InferenceResult:
    """Run Tofu on an instance. No artificial timeout."""
    result = InferenceResult(instance_id=inst.instance_id, tool='tofu')
    prompt = build_agent_prompt(inst)
    t0 = time.time()

    try:
        resp = requests.post(
            f'{TOFU_BASE_URL}/api/chat/start',
            json={
                'convId': f'swebench-{inst.instance_id}-tofu-{int(time.time())}',
                'messages': [{'role': 'user', 'content': prompt}],
                'config': {
                    'model': TOFU_MODEL,
                    'projectPath': str(workspace),
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        task_id = resp.json()['taskId']

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

                result.cost_usd = _compute_cost(result)

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


# ─── Inference: Claude Code ───────────────────────────────────────────────────

def run_cc_inference(inst: SWEInstance, workspace: Path) -> InferenceResult:
    """Run Claude Code CLI on an instance. No artificial timeout."""
    result = InferenceResult(instance_id=inst.instance_id, tool='cc')
    prompt = build_agent_prompt(inst)
    t0 = time.time()

    try:
        proc = subprocess.run(
            [
                'claude', '-p',
                '--output-format', 'json',
                '--model', CC_MODEL,
                '--dangerously-skip-permissions',
                prompt,
            ],
            capture_output=True, text=True,
            timeout=INFERENCE_SAFETY_TIMEOUT,  # safety net only
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
        )

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

        result.cost_usd = _compute_cost(result)

    except subprocess.TimeoutExpired:
        result.duration_s = time.time() - t0
        result.error = f'Safety timeout after {INFERENCE_SAFETY_TIMEOUT}s'
    except Exception as e:
        result.duration_s = time.time() - t0
        result.error = str(e)

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

def evaluate_patch(
    inst: SWEInstance,
    model_patch: str,
    tool: str,
    base_dir: Path,
    env_map: dict[str, Path],
) -> EvalResult:
    """Evaluate a model-generated patch using official SWE-bench test specs.

    1. Create fresh workspace at base_commit
    2. Apply model_patch
    3. Apply gold test_patch
    4. Install package in conda env
    5. Run FAIL_TO_PASS tests using official test_cmd
    6. Run PASS_TO_PASS tests (sampled)
    """
    result = EvalResult(instance_id=inst.instance_id, tool=tool)

    if not model_patch:
        result.error = 'Empty patch'
        return result

    # Get conda env
    env_key = f'{inst.repo}/{inst.version}'
    env_path = env_map.get(env_key)

    # Create eval workspace
    eval_ws = base_dir / 'eval' / f'{inst.instance_id}__{tool}'
    if eval_ws.exists():
        shutil.rmtree(eval_ws)

    try:
        repo_path = get_repo_path(inst.repo)
        subprocess.run(
            ['git', 'clone', '--quiet', '--no-local', str(repo_path), str(eval_ws)],
            capture_output=True, text=True, timeout=120, check=True,
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

    # Apply model patch
    patch_file = eval_ws / '__model_patch.diff'
    patch_file.write_text(model_patch)
    r = subprocess.run(
        ['git', 'apply', '--whitespace=fix', str(patch_file)],
        capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
    )
    result.patch_applies = (r.returncode == 0)
    result.patch_apply_stderr = r.stderr[:2000] if r.stderr else ''
    if not result.patch_applies:
        # Try with reduced context
        r2 = subprocess.run(
            ['git', 'apply', '--whitespace=fix', '-C1', str(patch_file)],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
        )
        result.patch_applies = (r2.returncode == 0)
        if result.patch_applies:
            result.patch_apply_stderr = '(applied with -C1)'
        else:
            result.patch_apply_stderr += '\n--- fallback -C1 ---\n' + (r2.stderr[:1000] or '')
            # Last resort: --reject
            subprocess.run(
                ['git', 'apply', '--whitespace=fix', '--reject', str(patch_file)],
                capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
            )
            result.error = f'Patch apply failed: {r.stderr[:300]}'

    # Apply gold test patch
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

    # Install package in conda env
    if env_path:
        install_r = _install_in_conda(inst, eval_ws, env_path)
        if install_r:
            result.install_stdout = (install_r.stdout or '')[-5000:]
            result.install_stderr = (install_r.stderr or '')[-5000:]

    # Run FAIL_TO_PASS tests
    for test_id in inst.fail_to_pass:
        test_out = _run_test_captured(inst, eval_ws, test_id, env_path)
        result.fail_to_pass_results[test_id] = test_out.passed
        result.fail_to_pass_outputs.append(test_out)

    # Run PASS_TO_PASS tests (sample up to 20 for speed)
    for test_id in inst.pass_to_pass[:20]:
        test_out = _run_test_captured(inst, eval_ws, test_id, env_path)
        result.pass_to_pass_results[test_id] = test_out.passed
        result.pass_to_pass_outputs.append(test_out)

    # Resolved = all F2P tests now pass
    result.resolved = (
        len(result.fail_to_pass_results) > 0
        and all(result.fail_to_pass_results.values())
    )

    return result


def _install_in_conda(inst: SWEInstance, workspace: Path, env_path: Path):
    """Install the package in the conda env for testing."""
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

    specs = {}
    if inst.repo in MAP_REPO_VERSION_TO_SPECS:
        specs = MAP_REPO_VERSION_TO_SPECS[inst.repo].get(inst.version, {})

    # Run pre_install (skip apt commands)
    for cmd in specs.get('pre_install', []):
        if 'apt-get' in cmd or 'apt ' in cmd or 'DEBIAN_FRONTEND' in cmd:
            continue
        _conda_run(env_path, f'bash -c "{cmd}"', cwd=str(workspace), timeout=120)

    # Run eval_commands (environment setup)
    eval_cmds = specs.get('eval_commands', [])

    # Install
    install_cmd = specs.get('install', 'python -m pip install -e .')
    log.debug('  Installing: %s', install_cmd[:100])
    r = _conda_run(env_path, install_cmd, cwd=str(workspace), timeout=600)
    if r.returncode != 0:
        log.warning('  Install failed: %s', r.stderr[:200])
    return r


def _run_test_captured(inst: SWEInstance, workspace: Path, test_id: str,
                       env_path: Path = None) -> TestOutput:
    """Run a single test and return full captured output (stdout/stderr/timing).

    This is the primary test entry point for the evaluation pipeline.
    """
    out = TestOutput(test_id=test_id)
    t0 = time.time()

    try:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        specs = {}
        if inst.repo in MAP_REPO_VERSION_TO_SPECS:
            specs = MAP_REPO_VERSION_TO_SPECS[inst.repo].get(inst.version, {})

        test_cmd_template = specs.get('test_cmd', 'pytest -rA')

        if 'django' in inst.repo:
            r, passed = _run_django_test(workspace, test_id, inst, env_path, test_cmd_template)
        elif 'sympy' in inst.repo and 'bin/test' in test_cmd_template:
            r, passed = _run_sympy_test(workspace, test_id, inst, env_path, test_cmd_template)
        elif '::' in test_id or '/' in test_id:
            r, passed = _run_pytest_test(workspace, test_id, env_path)
        else:
            r, passed = _run_pytest_bare(workspace, test_id, inst, env_path)

        out.passed = passed
        if r is not None:
            out.stdout = (r.stdout or '')[-10000:]
            out.stderr = (r.stderr or '')[-5000:]
            out.return_code = r.returncode
            out.command = getattr(r, '_command', '')

    except subprocess.TimeoutExpired as e:
        out.passed = False
        out.stderr = f'TIMEOUT after {TEST_TIMEOUT}s'
        raw = getattr(e, 'stdout', None)
        if isinstance(raw, bytes):
            out.stdout = raw.decode('utf-8', errors='replace')[-5000:]
        elif raw:
            out.stdout = str(raw)[-5000:]
    except Exception as e:
        out.passed = False
        out.stderr = f'Exception: {e}'
        log.debug('Test execution error for %s: %s', test_id, e)

    out.duration_s = round(time.time() - t0, 2)
    return out


def _run_test(inst: SWEInstance, workspace: Path, test_id: str, env_path: Path = None) -> bool:
    """Run a single test — backward-compat wrapper."""
    return _run_test_captured(inst, workspace, test_id, env_path).passed


def _run_django_test(workspace: Path, test_id: str, inst: SWEInstance,
                     env_path: Path, test_cmd: str) -> tuple:
    """Run Django test using official runtests.py.

    Returns (subprocess_result, passed_bool).
    """
    try:
        # Parse test ID: "test_method (module.TestClass)" or "module.TestClass.test_method"
        match = re.match(r'(\w+)\s+\(([^)]+)\)', test_id)
        if match:
            method_name = match.group(1)
            class_path = match.group(2)
            django_test_id = f'{class_path}.{method_name}'
        else:
            django_test_id = test_id

        # Extract test label for runtests.py
        # e.g., "auth_tests.test_validators.UsernameValidatorsTests.test_ascii" → "auth_tests.test_validators"
        parts = django_test_id.split('.')
        # Find the test module (before the TestClass)
        test_label = '.'.join(parts[:2]) if len(parts) >= 3 else parts[0]

        # Use official test command
        # test_cmd is like: ./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1
        cmd = f'{test_cmd} {test_label}'

        env_setup = 'export LANG=en_US.UTF-8; export LC_ALL=en_US.UTF-8; '
        full_cmd = f'{env_setup} cd tests && python runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 {test_label}'

        if env_path:
            r = _conda_run(env_path, f'bash -c "{full_cmd}"',
                          cwd=str(workspace), timeout=TEST_TIMEOUT)
        else:
            r = subprocess.run(
                [sys.executable, 'tests/runtests.py', '--verbosity=2',
                 '--settings=test_sqlite', '--parallel=1', test_label],
                capture_output=True, text=True, timeout=TEST_TIMEOUT,
                cwd=str(workspace),
                env={**os.environ, 'PYTHONPATH': str(workspace)},
            )

        r._command = full_cmd  # attach for logging

        # Check for our specific test passing
        passed = False
        if match:
            if re.search(rf'{method_name}\b.*\bok\b', r.stdout):
                passed = True
        if not passed:
            passed = (r.returncode == 0)
        return r, passed

    except subprocess.TimeoutExpired:
        return None, False
    except Exception as e:
        log.debug('Django test error: %s', e)
        return None, False


def _run_sympy_test(workspace: Path, test_id: str, inst: SWEInstance,
                    env_path: Path, test_cmd: str) -> tuple:
    """Run sympy test using bin/test.

    Returns (subprocess_result, passed_bool).
    """
    try:
        test_file = _find_test_file(inst, workspace, test_id)
        if not test_file:
            return None, False

        cmd = f"python bin/test -C --verbose {test_file}"

        if env_path:
            r = _conda_run(env_path, cmd, cwd=str(workspace), timeout=TEST_TIMEOUT)
        else:
            r = subprocess.run(
                cmd, shell=True,
                capture_output=True, text=True, timeout=TEST_TIMEOUT,
                cwd=str(workspace),
                env={**os.environ, 'PYTHONPATH': str(workspace)},
            )

        r._command = cmd

        passed = False
        if r.returncode == 0:
            passed = True
        elif 'passed' in r.stdout and '0 failed' in r.stdout:
            passed = True
        return r, passed

    except subprocess.TimeoutExpired:
        return None, False
    except Exception:
        return None, False


def _run_pytest_test(workspace: Path, test_id: str, env_path: Path = None) -> tuple:
    """Run a pytest-style test.

    Returns (subprocess_result, passed_bool).
    """
    try:
        cmd = f'python -m pytest {test_id} -xvs --no-header -rN'
        if env_path:
            r = _conda_run(env_path, cmd,
                          cwd=str(workspace), timeout=TEST_TIMEOUT)
        else:
            r = subprocess.run(
                [sys.executable, '-m', 'pytest', test_id, '-xvs', '--no-header', '-rN'],
                capture_output=True, text=True, timeout=TEST_TIMEOUT,
                cwd=str(workspace),
                env={**os.environ, 'PYTHONPATH': str(workspace), 'PYTHONDONTWRITEBYTECODE': '1'},
            )
        r._command = cmd
        return r, (r.returncode == 0)
    except subprocess.TimeoutExpired:
        return None, False
    except Exception:
        return None, False


def _run_pytest_bare(workspace: Path, test_name: str, inst: SWEInstance,
                     env_path: Path = None) -> tuple:
    """Run a bare test name by finding the file from the test_patch.

    Returns (subprocess_result, passed_bool).
    """
    test_file = _find_test_file(inst, workspace, test_name)
    if test_file:
        return _run_pytest_test(workspace, f'{test_file}::{test_name}', env_path)

    # Fallback: pytest -k
    try:
        cmd = f'python -m pytest -k {test_name} -x'
        if env_path:
            r = _conda_run(env_path, cmd,
                          cwd=str(workspace), timeout=TEST_TIMEOUT)
        else:
            r = subprocess.run(
                [sys.executable, '-m', 'pytest', '-k', test_name, '-x'],
                capture_output=True, text=True, timeout=TEST_TIMEOUT,
                cwd=str(workspace),
                env={**os.environ, 'PYTHONPATH': str(workspace)},
            )
        r._command = cmd
        return r, (r.returncode == 0 and 'passed' in r.stdout)
    except Exception:
        return None, False


def _find_test_file(inst: SWEInstance, workspace: Path, test_name: str) -> Optional[str]:
    """Find the test file for a bare test name from the test_patch."""
    for line in inst.test_patch.split('\n'):
        if line.startswith('+++ b/'):
            candidate = line[6:].strip()
            if (workspace / candidate).exists():
                return candidate
        elif line.startswith('diff --git'):
            parts = line.split()
            if len(parts) >= 4:
                candidate = parts[3][2:]  # strip 'b/'
                if (workspace / candidate).exists():
                    return candidate

    # Search
    try:
        r = subprocess.run(
            ['grep', '-rl', f'def {test_name}', '--include=*.py', '.'],
            capture_output=True, text=True, timeout=10, cwd=str(workspace),
        )
        for line in r.stdout.strip().split('\n'):
            f = line.strip().lstrip('./')
            if f and 'test' in f.lower():
                return f
    except Exception:
        pass

    return None


# ─── Cost Computation ─────────────────────────────────────────────────────────

def _compute_cost(result: InferenceResult) -> float:
    """Compute cost from token counts (Anthropic convention)."""
    cost = (
        result.input_tokens * PRICE_INPUT_PER_1K / 1000
        + result.output_tokens * PRICE_OUTPUT_PER_1K / 1000
        + result.cache_read_tokens * PRICE_CACHE_READ_PER_1K / 1000
        + result.cache_write_tokens * PRICE_CACHE_WRITE_PER_1K / 1000
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
        test_outputs_f2p = []
        for to in getattr(eval_result, 'fail_to_pass_outputs', []):
            test_outputs_f2p.append({
                'test_id': to.test_id,
                'passed': to.passed,
                'duration_s': to.duration_s,
                'command': to.command,
                'return_code': to.return_code,
                'stdout': to.stdout[-5000:] if to.stdout else '',  # cap at 5KB per test
                'stderr': to.stderr[-3000:] if to.stderr else '',
            })
        test_outputs_p2p = []
        for to in getattr(eval_result, 'pass_to_pass_outputs', []):
            test_outputs_p2p.append({
                'test_id': to.test_id,
                'passed': to.passed,
                'duration_s': to.duration_s,
                'command': to.command,
                'return_code': to.return_code,
                'stdout': to.stdout[-3000:] if to.stdout else '',
                'stderr': to.stderr[-2000:] if to.stderr else '',
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
            'fail_to_pass_outputs': test_outputs_f2p,
            'pass_to_pass_outputs': test_outputs_p2p,
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
    instances = load_swebench_instances(
        num=num,
        instance_ids=instance_ids,
        repo_filter=args.repo or None,
        difficulty_filter=args.difficulty or None,
        seed=args.seed,
        load_all=args.all,
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
