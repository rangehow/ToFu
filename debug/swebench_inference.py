#!/usr/bin/env python3
"""swebench_inference.py — SWE-bench Verified inference: Tofu vs Claude Code CLI.

Inference ONLY. Generates predictions.jsonl for each tool.
Evaluation is done separately via the official SWE-bench Docker harness:

    python -m swebench.harness.run_evaluation \
        --predictions_path results/tofu/predictions.jsonl \
        --max_workers 4 --run_id tofu

    # Or via sb-cli (cloud, no Docker needed):
    sb-cli submit swe-bench_verified test \
        --predictions_path results/tofu/predictions.jsonl \
        --run_id tofu

Usage:
    # Run all 500 instances with both tools
    python debug/swebench_inference.py --all

    # Run specific number / instances / tool
    python debug/swebench_inference.py --num 50
    python debug/swebench_inference.py --instances django__django-13128,sympy__sympy-16886
    python debug/swebench_inference.py --tool tofu --all

    # Resume after interruption
    python debug/swebench_inference.py --all --resume

    # Dry-run
    python debug/swebench_inference.py --dry-run --all

Output structure:
    {workdir}/
        tofu/
            predictions.jsonl          # Official SWE-bench format (1 JSON per line)
            patches/{instance_id}.diff # Individual patch files
            details/{instance_id}.json # Full inference metadata (tokens, cost, raw output)
            inference.log              # Structured log
        cc/
            predictions.jsonl
            patches/{instance_id}.diff
            details/{instance_id}.json
            inference.log
        summary.json                   # Aggregate stats for both tools
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
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import requests

# ─── Constants ────────────────────────────────────────────────────────────────

TOFU_BASE_URL = os.environ.get('TOFU_BASE_URL', 'http://127.0.0.1:15001')
TOFU_MODEL = os.environ.get('TOFU_MODEL', 'aws.claude-opus-4.6')
CC_MODEL = os.environ.get('CC_MODEL', 'opus')

# Safety timeout for truly stuck agents (4 hours)
SAFETY_TIMEOUT = int(os.environ.get('SAFETY_TIMEOUT', '14400'))
COOLDOWN_SECONDS = int(os.environ.get('COOLDOWN_SECONDS', '3'))

DEFAULT_WORKDIR = Path(os.environ.get('SWEBENCH_WORKDIR', '/tmp/swebench_infer'))
REPO_CACHE_DIR = Path(os.environ.get('REPO_CACHE_DIR', '/tmp/swebench_repos'))

# Pricing (Opus via example-corp gateway)
PRICE_INPUT_PER_1K = 0.015
PRICE_OUTPUT_PER_1K = 0.075
PRICE_CACHE_READ_PER_1K = 0.0015
PRICE_CACHE_WRITE_PER_1K = 0.01875

# Repos needing C compilation — skip by default (no Docker)
C_EXTENSION_REPOS = {
    'astropy/astropy',
    'scikit-learn/scikit-learn',
    'matplotlib/matplotlib',
}

# Files/dirs created by agents that should be excluded from patches
_EXCLUDE_PREFIXES = (
    '.chatui/', '__pycache__/', '.project_sessions/',
    '.claude/', '.git/',
)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging(tool_dir: Path) -> logging.Logger:
    """Create a logger that writes to both console and tool-specific log file."""
    logger = logging.getLogger(f'swebench.{tool_dir.name}')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter('%(asctime)s %(levelname)-5s %(message)s', datefmt='%H:%M:%S')

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(tool_dir / 'inference.log')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-5s [%(funcName)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    logger.addHandler(fh)

    return logger


# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class Instance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    version: str
    difficulty: str


@dataclass
class InferenceResult:
    instance_id: str
    tool: str
    model_patch: str = ''
    duration_s: float = 0.0
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ''
    raw_output: str = ''


# ─── Dataset ──────────────────────────────────────────────────────────────────

def load_instances(
    num: int = None,
    instance_ids: list = None,
    repo_filter: str = None,
    skip_c_repos: bool = True,
    load_all: bool = False,
    seed: int = 42,
) -> list[Instance]:
    """Load SWE-bench Verified instances."""
    from datasets import load_dataset
    ds = load_dataset('princeton-nlp/SWE-bench_Verified', split='test')

    instances = []
    for item in ds:
        inst = Instance(
            instance_id=item['instance_id'],
            repo=item['repo'],
            base_commit=item['base_commit'],
            problem_statement=item['problem_statement'],
            hints_text=item['hints_text'] or '',
            version=item['version'],
            difficulty=item.get('difficulty', ''),
        )
        instances.append(inst)

    # Filters
    if skip_c_repos:
        instances = [i for i in instances if i.repo not in C_EXTENSION_REPOS]
    if instance_ids:
        ids = set(instance_ids)
        instances = [i for i in instances if i.instance_id in ids]
    if repo_filter:
        instances = [i for i in instances if i.repo == repo_filter]

    instances.sort(key=lambda x: x.instance_id)

    if load_all or not num:
        return instances
    if num >= len(instances):
        return instances

    # Stratified sampling
    import random
    rng = random.Random(seed)
    by_repo = {}
    for inst in instances:
        by_repo.setdefault(inst.repo, []).append(inst)
    for v in by_repo.values():
        rng.shuffle(v)

    selected = []
    repos = sorted(by_repo.keys())
    remaining = num
    for i, repo in enumerate(repos):
        take = remaining if i == len(repos) - 1 else max(1, round(num * len(by_repo[repo]) / len(instances)))
        take = min(take, remaining, len(by_repo[repo]))
        selected.extend(by_repo[repo][:take])
        remaining -= take
        if remaining <= 0:
            break

    selected.sort(key=lambda x: x.instance_id)
    return selected


# ─── Repo & Workspace ────────────────────────────────────────────────────────

def _clone_repo(repo: str) -> Path:
    """Clone repo to cache if not already there."""
    repo_dir = REPO_CACHE_DIR / repo.replace('/', '__')
    if repo_dir.exists():
        return repo_dir
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['git', 'clone', '--quiet', f'https://github.com/{repo}.git', str(repo_dir)],
        capture_output=True, text=True, timeout=600, check=True,
    )
    return repo_dir


def setup_workspace(inst: Instance, tool: str, workdir: Path) -> Path:
    """Create workspace: clone repo at base_commit."""
    ws = workdir / tool / 'workspaces' / inst.instance_id
    if ws.exists():
        shutil.rmtree(ws)

    repo_path = _clone_repo(inst.repo)
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


def extract_patch(workspace: Path) -> str:
    """Extract git diff, excluding agent artifacts."""
    try:
        subprocess.run(
            ['git', 'add', '-A'],
            capture_output=True, text=True, timeout=10, cwd=str(workspace),
        )
        r = subprocess.run(
            ['git', 'diff', '--cached', '--name-only'],
            capture_output=True, text=True, timeout=10, cwd=str(workspace),
        )
        if r.returncode != 0:
            return ''

        files = [
            f.strip() for f in r.stdout.strip().split('\n')
            if f.strip()
            and not any(f.strip().startswith(p) for p in _EXCLUDE_PREFIXES)
            and not f.strip().endswith('.pyc')
        ]
        if not files:
            return ''

        r = subprocess.run(
            ['git', 'diff', '--cached', '--'] + files,
            capture_output=True, text=True, timeout=30, cwd=str(workspace),
        )
        diff = r.stdout.rstrip('\r') if r.returncode == 0 else ''
        if diff and not diff.endswith('\n'):
            diff += '\n'
        return diff
    except Exception:
        return ''


# ─── Cost ─────────────────────────────────────────────────────────────────────

def compute_cost(r: InferenceResult) -> float:
    return (
        r.input_tokens * PRICE_INPUT_PER_1K / 1000
        + r.output_tokens * PRICE_OUTPUT_PER_1K / 1000
        + r.cache_read_tokens * PRICE_CACHE_READ_PER_1K / 1000
        + r.cache_write_tokens * PRICE_CACHE_WRITE_PER_1K / 1000
    )


# ─── Agent Prompt ─────────────────────────────────────────────────────────────

def build_prompt(inst: Instance) -> str:
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

def run_tofu(inst: Instance, workspace: Path, log: logging.Logger) -> InferenceResult:
    """Run Tofu inference. Returns InferenceResult with model_patch."""
    result = InferenceResult(instance_id=inst.instance_id, tool='tofu')
    prompt = build_prompt(inst)
    t0 = time.time()

    try:
        # Start task
        resp = requests.post(
            f'{TOFU_BASE_URL}/api/chat/start',
            json={
                'convId': f'swe-{inst.instance_id}-tofu-{int(t0)}',
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
        log.info('Tofu task started: %s', task_id)

        # Poll
        poll_interval = 2.0
        while True:
            elapsed = time.time() - t0
            if elapsed > SAFETY_TIMEOUT:
                result.error = f'Safety timeout after {elapsed:.0f}s'
                requests.post(f'{TOFU_BASE_URL}/api/chat/abort/{task_id}', timeout=5)
                break

            time.sleep(poll_interval)
            try:
                data = requests.get(
                    f'{TOFU_BASE_URL}/api/chat/poll/{task_id}', timeout=10
                ).json()
            except Exception as e:
                log.debug('Poll error: %s', e)
                continue

            status = data.get('status', '')
            if status in ('done', 'error', 'interrupted'):
                result.duration_s = time.time() - t0

                # Parse usage
                for rd in (data.get('apiRounds') or []):
                    u = rd.get('usage', {})
                    result.input_tokens += u.get('prompt_tokens', 0)
                    result.output_tokens += u.get('completion_tokens', 0)
                    result.cache_read_tokens += u.get('cache_read_tokens', 0)
                    result.cache_write_tokens += u.get('cache_write_tokens', 0)
                result.num_turns = len(data.get('apiRounds') or [])
                result.cost_usd = compute_cost(result)

                try:
                    result.raw_output = json.dumps(data, ensure_ascii=False)[:50_000]
                except Exception:
                    result.raw_output = str(data)[:20_000]

                if status == 'error':
                    result.error = data.get('error', 'Unknown')
                break

            poll_interval = min(poll_interval * 1.1, 10.0)

    except Exception as e:
        result.duration_s = time.time() - t0
        result.error = str(e)
        log.error('Tofu inference failed: %s', e, exc_info=True)

    result.model_patch = extract_patch(workspace)
    return result


# ─── Inference: Claude Code ───────────────────────────────────────────────────

def run_cc(inst: Instance, workspace: Path, log: logging.Logger) -> InferenceResult:
    """Run Claude Code CLI inference. Returns InferenceResult with model_patch."""
    result = InferenceResult(instance_id=inst.instance_id, tool='cc')
    prompt = build_prompt(inst)
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
            timeout=SAFETY_TIMEOUT,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
        )
        result.duration_s = time.time() - t0

        if proc.returncode != 0 and not proc.stdout.strip():
            result.error = f'Exit code {proc.returncode}: {proc.stderr[:500]}'

        if proc.stdout.strip():
            try:
                data = json.loads(proc.stdout.strip())
                result.num_turns = data.get('num_turns', 1)
                u = data.get('usage', {})
                result.input_tokens = u.get('input_tokens', 0)
                result.output_tokens = u.get('output_tokens', 0)
                result.cache_read_tokens = u.get('cache_read_input_tokens', 0)
                result.cache_write_tokens = u.get('cache_creation_input_tokens', 0)
                result.raw_output = proc.stdout[:50_000]
            except (json.JSONDecodeError, KeyError, TypeError):
                result.raw_output = proc.stdout[:20_000]

        result.cost_usd = compute_cost(result)

    except subprocess.TimeoutExpired:
        result.duration_s = time.time() - t0
        result.error = f'Safety timeout after {SAFETY_TIMEOUT}s'
    except Exception as e:
        result.duration_s = time.time() - t0
        result.error = str(e)
        log.error('CC inference failed: %s', e, exc_info=True)

    result.model_patch = extract_patch(workspace)
    return result


# ─── Persistence ──────────────────────────────────────────────────────────────

def save_prediction(tool_dir: Path, inst_id: str, model_patch: str, model_name: str):
    """Append one prediction to predictions.jsonl in official SWE-bench format."""
    pred_file = tool_dir / 'predictions.jsonl'
    entry = {
        'instance_id': inst_id,
        'model_name_or_path': model_name,
        'model_patch': model_patch,
    }
    with open(pred_file, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def save_detail(tool_dir: Path, result: InferenceResult):
    """Save full inference detail to a JSON file."""
    detail_dir = tool_dir / 'details'
    detail_dir.mkdir(parents=True, exist_ok=True)
    detail_file = detail_dir / f'{result.instance_id}.json'
    with open(detail_file, 'w') as f:
        json.dump(asdict(result), f, ensure_ascii=False, indent=2)


def save_patch(tool_dir: Path, inst_id: str, patch: str):
    """Save patch to a .diff file."""
    patch_dir = tool_dir / 'patches'
    patch_dir.mkdir(parents=True, exist_ok=True)
    with open(patch_dir / f'{inst_id}.diff', 'w') as f:
        f.write(patch)


def load_completed(tool_dir: Path) -> set[str]:
    """Load set of already-completed instance IDs from predictions.jsonl."""
    pred_file = tool_dir / 'predictions.jsonl'
    completed = set()
    if pred_file.exists():
        with open(pred_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        completed.add(json.loads(line)['instance_id'])
                    except (json.JSONDecodeError, KeyError):
                        pass
    return completed


def save_summary(workdir: Path, tools: list[str]):
    """Generate summary.json from all detail files."""
    summary = {'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'tools': {}}

    for tool in tools:
        tool_dir = workdir / tool
        detail_dir = tool_dir / 'details'
        if not detail_dir.exists():
            continue

        details = []
        for f in sorted(detail_dir.glob('*.json')):
            with open(f) as fh:
                details.append(json.load(fh))

        total = len(details)
        has_patch = sum(1 for d in details if d.get('model_patch', '').strip())
        errors = sum(1 for d in details if d.get('error'))
        total_cost = sum(d.get('cost_usd', 0) for d in details)
        total_time = sum(d.get('duration_s', 0) for d in details)
        total_turns = sum(d.get('num_turns', 0) for d in details)
        total_input = sum(d.get('input_tokens', 0) for d in details)
        total_output = sum(d.get('output_tokens', 0) for d in details)
        total_cache_read = sum(d.get('cache_read_tokens', 0) for d in details)
        total_cache_write = sum(d.get('cache_write_tokens', 0) for d in details)

        summary['tools'][tool] = {
            'total_instances': total,
            'has_patch': has_patch,
            'empty_patch': total - has_patch,
            'errors': errors,
            'total_cost_usd': round(total_cost, 2),
            'avg_cost_usd': round(total_cost / max(total, 1), 2),
            'total_time_s': round(total_time, 1),
            'avg_time_s': round(total_time / max(total, 1), 1),
            'avg_turns': round(total_turns / max(total, 1), 1),
            'total_input_tokens': total_input,
            'total_output_tokens': total_output,
            'total_cache_read_tokens': total_cache_read,
            'total_cache_write_tokens': total_cache_write,
            'cache_hit_rate': round(
                total_cache_read / max(total_cache_read + total_input, 1) * 100, 1
            ),
        }

    with open(workdir / 'summary.json', 'w') as f:
        json.dump(summary, f, indent=2)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def run_pipeline(
    instances: list[Instance],
    tools: list[str],
    workdir: Path,
    resume: bool = False,
):
    """Run inference for all instances × tools, saving results incrementally."""

    # Setup output dirs
    for tool in tools:
        tool_dir = workdir / tool
        for subdir in ('patches', 'details', 'workspaces'):
            (tool_dir / subdir).mkdir(parents=True, exist_ok=True)

    total_runs = len(instances) * len(tools)
    completed_count = 0
    start_time = time.time()

    for idx, inst in enumerate(instances):
        for tool in tools:
            tool_dir = workdir / tool
            log = _setup_logging(tool_dir)
            run_label = f'[{tool.upper()}] {inst.instance_id} ({idx+1}/{len(instances)})'

            # Resume: skip if already done
            if resume:
                done = load_completed(tool_dir)
                if inst.instance_id in done:
                    completed_count += 1
                    log.info('%s — SKIP (already done)', run_label)
                    continue

            # ETA
            completed_count += 1
            elapsed = time.time() - start_time
            if completed_count > 1:
                eta_s = elapsed / (completed_count - 1) * (total_runs - completed_count + 1)
                eta_str = time.strftime('%H:%M', time.localtime(time.time() + eta_s))
            else:
                eta_str = '?'

            log.info('%s — START (ETA: %s)', run_label, eta_str)

            try:
                # Setup workspace
                ws = setup_workspace(inst, tool, workdir)

                # Run inference
                if tool == 'tofu':
                    result = run_tofu(inst, ws, log)
                else:
                    result = run_cc(inst, ws, log)

                # Save everything
                save_prediction(
                    tool_dir, inst.instance_id, result.model_patch,
                    TOFU_MODEL if tool == 'tofu' else f'claude-code-{CC_MODEL}',
                )
                save_patch(tool_dir, inst.instance_id, result.model_patch)
                save_detail(tool_dir, result)

                patch_size = len(result.model_patch)
                log.info(
                    '%s — DONE in %.0fs, patch=%d bytes, cost=$%.2f, turns=%d%s',
                    run_label, result.duration_s, patch_size, result.cost_usd,
                    result.num_turns, f', error={result.error}' if result.error else '',
                )

                # Cleanup workspace to save disk
                try:
                    shutil.rmtree(ws, ignore_errors=True)
                except Exception:
                    pass

            except Exception as e:
                log.error('%s — CRASH: %s', run_label, e, exc_info=True)
                # Write an empty prediction so resume skips it
                save_prediction(
                    tool_dir, inst.instance_id, '',
                    TOFU_MODEL if tool == 'tofu' else f'claude-code-{CC_MODEL}',
                )
                save_detail(tool_dir, InferenceResult(
                    instance_id=inst.instance_id, tool=tool,
                    error=f'CRASH: {e}',
                ))

            # Cooldown between runs
            time.sleep(COOLDOWN_SECONDS)

    # Final summary
    save_summary(workdir, tools)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='SWE-bench Verified inference runner')
    p.add_argument('--all', action='store_true', help='Run all instances')
    p.add_argument('--num', type=int, help='Number of instances to run')
    p.add_argument('--instances', type=str, help='Comma-separated instance IDs')
    p.add_argument('--repo', type=str, help='Filter by repo (e.g. django/django)')
    p.add_argument('--tool', type=str, choices=['tofu', 'cc', 'both'], default='both')
    p.add_argument('--workdir', type=str, default=str(DEFAULT_WORKDIR))
    p.add_argument('--resume', action='store_true', help='Skip already-completed instances')
    p.add_argument('--include-c-repos', action='store_true', help='Include C-extension repos')
    p.add_argument('--dry-run', action='store_true', help='Show instances without running')
    args = p.parse_args()

    # Load instances
    instance_ids = args.instances.split(',') if args.instances else None
    instances = load_instances(
        num=args.num,
        instance_ids=instance_ids,
        repo_filter=args.repo,
        skip_c_repos=not args.include_c_repos,
        load_all=args.all,
    )

    if not instances:
        print('No instances selected.')
        return

    tools = ['tofu', 'cc'] if args.tool == 'both' else [args.tool]
    workdir = Path(args.workdir)

    if args.dry_run:
        from collections import Counter
        repos = Counter(i.repo for i in instances)
        print(f'\n{len(instances)} instances × {len(tools)} tools = {len(instances) * len(tools)} runs')
        print(f'Repos: {dict(repos)}')
        print(f'Tools: {tools}')
        print(f'Workdir: {workdir}')
        print(f'\nFirst 10:')
        for inst in instances[:10]:
            print(f'  {inst.instance_id} ({inst.repo}, {inst.difficulty})')
        return

    # Verify tools are available
    if 'tofu' in tools:
        try:
            r = requests.get(f'{TOFU_BASE_URL}/', timeout=5)
            if r.status_code < 500:
                print(f'✅ Tofu server at {TOFU_BASE_URL}')
            else:
                raise RuntimeError(f'HTTP {r.status_code}')
        except Exception as e:
            print(f'❌ Tofu server not reachable: {e}')
            sys.exit(1)

    if 'cc' in tools:
        r = subprocess.run(['claude', '--version'], capture_output=True, text=True)
        if r.returncode != 0:
            print('❌ claude CLI not found')
            sys.exit(1)
        print(f'✅ Claude Code: {r.stdout.strip()}')

    print(f'\n🚀 Starting: {len(instances)} instances × {len(tools)} tools = {len(instances) * len(tools)} runs')
    print(f'   Workdir: {workdir}')
    if args.resume:
        for tool in tools:
            done = load_completed(workdir / tool)
            print(f'   {tool}: {len(done)} already done')
    print()

    # Redirect stderr to log file for crash diagnostics
    workdir.mkdir(parents=True, exist_ok=True)
    stderr_log = open(workdir / 'stderr.log', 'a')
    sys.stderr = stderr_log

    try:
        run_pipeline(instances, tools, workdir, resume=args.resume)
    except KeyboardInterrupt:
        print('\n⚠️  Interrupted. Use --resume to continue.')
    except Exception as e:
        logging.getLogger('swebench').critical('Fatal: %s', e, exc_info=True)
        raise
    finally:
        sys.stderr = sys.__stderr__
        stderr_log.close()

    # Print final summary
    summary_file = workdir / 'summary.json'
    if summary_file.exists():
        summary = json.load(open(summary_file))
        print('\n' + '=' * 60)
        print('SUMMARY')
        print('=' * 60)
        for tool, stats in summary.get('tools', {}).items():
            print(f'\n  {tool.upper()}:')
            print(f'    Instances:  {stats["total_instances"]}')
            print(f'    Has patch:  {stats["has_patch"]} ({100*stats["has_patch"]/max(stats["total_instances"],1):.0f}%)')
            print(f'    Errors:     {stats["errors"]}')
            print(f'    Total cost: ${stats["total_cost_usd"]:.2f}')
            print(f'    Avg time:   {stats["avg_time_s"]:.0f}s')
            print(f'    Avg turns:  {stats["avg_turns"]}')
            print(f'    Cache hit:  {stats["cache_hit_rate"]}%')

    print(f'\n📂 Results in: {workdir}/')
    print(f'   Evaluate with:')
    for tool in tools:
        print(f'     python -m swebench.harness.run_evaluation \\')
        print(f'       --predictions_path {workdir}/{tool}/predictions.jsonl \\')
        print(f'       --max_workers 4 --run_id {tool}')


if __name__ == '__main__':
    main()
