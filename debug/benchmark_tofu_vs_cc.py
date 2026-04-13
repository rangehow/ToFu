#!/usr/bin/env python3
"""benchmark_tofu_vs_cc.py — Compare Tofu (ChatUI) vs Claude Code CLI on project-level tasks.

Measures accuracy, speed, and cost across complex coding tasks that require
reading files, understanding project structure, writing multi-file changes,
running commands, and debugging — the kind of real work these tools are for.

Both tools use the same underlying model (aws.claude-opus-4.6) via the example-corp
gateway, which provides:
  - Automatic server-side KV cache
  - Explicit cache_control breakpoint support (Anthropic-style)

Both tools get cache benefits — Tofu via native 4-breakpoint mixed-TTL strategy,
Claude Code via its built-in cache_control markers passed through the proxy.
Differences reflect **orchestration quality**, **system prompt design**, **tool
efficiency**, and **caching strategy**.

Usage:
    # Run all cases
    python debug/benchmark_tofu_vs_cc.py

    # Run specific cases
    python debug/benchmark_tofu_vs_cc.py --cases 1,3

    # Run only one tool
    python debug/benchmark_tofu_vs_cc.py --tool tofu
    python debug/benchmark_tofu_vs_cc.py --tool cc

    # Repeat each case N times for statistical significance
    python debug/benchmark_tofu_vs_cc.py --repeat 3

    # Dry-run: show cases without executing
    python debug/benchmark_tofu_vs_cc.py --dry-run

Prerequisites:
    - Tofu server running on http://127.0.0.1:15000
    - Claude Code proxy running on http://127.0.0.1:8082 (for CC tests)
    - claude CLI installed and configured (via ~/.claude/settings.json)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

# ─── Configuration ───────────────────────────────────────────────────────────

TOFU_BASE_URL = os.environ.get('TOFU_BASE_URL', 'http://127.0.0.1:15000')
TOFU_MODEL = os.environ.get('TOFU_MODEL', 'aws.claude-opus-4.6')
CC_MODEL = os.environ.get('CC_MODEL', 'opus')
DEFAULT_TIMEOUT = 300  # 5 minutes per case — complex tasks need time

# ─── Pricing (USD per 1K tokens — Opus 4 via example-corp) ────────────────────────
PRICE_INPUT_PER_1K = float(os.environ.get('PRICE_INPUT_PER_1K', '0.015'))
PRICE_OUTPUT_PER_1K = float(os.environ.get('PRICE_OUTPUT_PER_1K', '0.075'))
PRICE_CACHE_READ_PER_1K = float(os.environ.get('PRICE_CACHE_READ_PER_1K', '0.0015'))
PRICE_CACHE_WRITE_PER_1K = float(os.environ.get('PRICE_CACHE_WRITE_PER_1K', '0.01875'))


# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class TestCase:
    """A single benchmark case."""
    id: int
    name: str
    description: str
    prompt: str
    category: str  # understand | debug | feature | refactor | test
    setup: callable  # function(workspace: Path) -> None — creates project files
    validate: callable  # function(workspace: Path) -> (bool, str, dict)
    difficulty: str = 'medium'  # easy | medium | hard


@dataclass
class RunResult:
    """Result of running one case with one tool."""
    case_id: int
    tool: str  # 'tofu' or 'cc'
    run_idx: int = 0
    success: bool = False
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    response_text: str = ''
    validations: dict = field(default_factory=dict)
    error: str = ''


# ─── Project Scaffolding Helpers ─────────────────────────────────────────────

def _write_files(ws: Path, files: dict):
    """Write multiple files to workspace. files: {relative_path: content}"""
    for rel_path, content in files.items():
        p = ws / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content).lstrip('\n'))


def _run_in(ws: Path, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command in the workspace."""
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        timeout=timeout, cwd=str(ws),
    )


def _file_has(ws: Path, path: str, *needles: str) -> tuple[bool, str]:
    """Check file exists and contains all needles."""
    p = ws / path
    if not p.exists():
        return False, f'{path} not found'
    content = p.read_text(errors='replace')
    for needle in needles:
        if needle not in content:
            return False, f'{path} missing: {needle!r}'
    return True, f'{path} OK ({len(content)} chars)'


def _python_ok(ws: Path, path: str, expected: str = None) -> tuple[bool, str]:
    """Run a python file and optionally check output."""
    p = ws / path
    if not p.exists():
        return False, f'{path} not found'
    try:
        r = _run_in(ws, f'{sys.executable} {path}', timeout=15)
        if r.returncode != 0:
            return False, f'{path} exit={r.returncode}: {r.stderr[:300]}'
        if expected and expected not in r.stdout:
            return False, f'{path} output mismatch: got {r.stdout[:300]!r}'
        return True, f'{path} runs OK'
    except subprocess.TimeoutExpired:
        return False, f'{path} timeout'


def _pytest_ok(ws: Path, path: str) -> tuple[bool, str]:
    """Run pytest on a file."""
    p = ws / path
    if not p.exists():
        return False, f'{path} not found'
    try:
        r = _run_in(ws, f'{sys.executable} -m pytest {path} -v --tb=short', timeout=30)
        passed = 'passed' in r.stdout and 'failed' not in r.stdout
        lines = [l for l in r.stdout.split('\n') if 'passed' in l or 'failed' in l]
        return passed, (lines[0].strip() if lines else r.stdout[-300:])
    except subprocess.TimeoutExpired:
        return False, f'pytest {path} timeout'


# ═══════════════════════════════════════════════════════════════════════════════
# TEST CASES — Complex project-level tasks
# ═══════════════════════════════════════════════════════════════════════════════


# ── Case 1: Cross-file Bug Hunt ──────────────────────────────────────────────
# The model must read multiple files, trace a bug through function calls,
# and fix it — testing code comprehension and multi-file navigation.

def _setup_case1(ws: Path):
    _write_files(ws, {
        'app/models.py': '''
            from dataclasses import dataclass, field
            from typing import Optional
            from datetime import datetime

            @dataclass
            class User:
                id: int
                name: str
                email: str
                created_at: datetime = field(default_factory=datetime.now)
                is_active: bool = True

            @dataclass
            class Order:
                id: int
                user_id: int
                items: list = field(default_factory=list)
                total: float = 0.0
                status: str = 'pending'
                created_at: datetime = field(default_factory=datetime.now)

                def add_item(self, name: str, price: float, quantity: int = 1):
                    """Add item to order and update total."""
                    self.items.append({'name': name, 'price': price, 'quantity': quantity})
                    self.total += price * quantity

                def apply_discount(self, percent: float):
                    """Apply percentage discount to total."""
                    # BUG: divides by percent instead of (percent / 100)
                    self.total = self.total * (1 - percent)
        ''',
        'app/services.py': '''
            from app.models import User, Order
            from app.repository import OrderRepository

            class OrderService:
                def __init__(self, repo: "OrderRepository"):
                    self.repo = repo

                def create_order(self, user: User, items: list) -> Order:
                    """Create a new order for user with given items."""
                    order = Order(id=self.repo.next_id(), user_id=user.id)
                    for item in items:
                        order.add_item(item['name'], item['price'], item.get('quantity', 1))
                    self.repo.save(order)
                    return order

                def apply_promo(self, order_id: int, promo_code: str) -> Order:
                    """Apply a promotion code to an order."""
                    order = self.repo.get(order_id)
                    if order is None:
                        raise ValueError(f"Order {order_id} not found")
                    discount = self._get_discount(promo_code)
                    order.apply_discount(discount)
                    self.repo.save(order)
                    return order

                def _get_discount(self, code: str) -> float:
                    """Look up discount percentage for a promo code."""
                    promos = {
                        'SAVE10': 10,
                        'SAVE20': 20,
                        'HALF': 50,
                    }
                    # Returns percentage as integer (e.g. 10 for 10%)
                    return promos.get(code, 0)
        ''',
        'app/repository.py': '''
            from app.models import Order
            from typing import Optional

            class OrderRepository:
                def __init__(self):
                    self._orders = {}
                    self._counter = 0

                def next_id(self) -> int:
                    self._counter += 1
                    return self._counter

                def save(self, order: Order):
                    self._orders[order.id] = order

                def get(self, order_id: int) -> Optional[Order]:
                    return self._orders.get(order_id)

                def list_all(self) -> list:
                    return list(self._orders.values())
        ''',
        'app/__init__.py': '',
        'test_orders.py': '''
            """Test that demonstrates the bug."""
            from app.models import User
            from app.services import OrderService
            from app.repository import OrderRepository

            def test_promo_discount():
                repo = OrderRepository()
                service = OrderService(repo)
                user = User(id=1, name='Alice', email='alice@example.com')

                order = service.create_order(user, [
                    {'name': 'Widget', 'price': 100.0, 'quantity': 1},
                ])
                assert order.total == 100.0, f"Expected 100.0, got {order.total}"

                # Apply 20% discount → should be 80.0
                order = service.apply_promo(order.id, 'SAVE20')
                assert order.total == 80.0, f"Expected 80.0 after 20% discount, got {order.total}"

            if __name__ == '__main__':
                test_promo_discount()
                print("All tests passed!")
        ''',
    })

def _validate_case1(ws: Path) -> tuple[bool, str, dict]:
    checks = {}
    # The test must pass after the fix
    ok, msg = _pytest_ok(ws, 'test_orders.py')
    checks['test_passes'] = (ok, msg)
    # The fix should be in models.py (dividing percent by 100)
    ok2, msg2 = _file_has(ws, 'app/models.py', '100')
    checks['fix_in_models'] = (ok2, msg2)
    all_ok = all(v[0] for v in checks.values())
    return all_ok, 'PASS' if all_ok else 'FAIL', checks


# ── Case 2: Feature Implementation with API Design ───────────────────────────
# Build a REST-like module with middleware pattern — tests architecture skills.

def _setup_case2(ws: Path):
    _write_files(ws, {
        'README.md': '''
            # Task Queue Library

            Build a simple in-memory task queue system with the following components:

            ## Requirements

            1. `task_queue/queue.py` — `TaskQueue` class:
               - `submit(func, *args, **kwargs) -> task_id` — submit a callable, return UUID
               - `get_status(task_id) -> dict` with keys: id, status (pending/running/done/failed), result, error
               - `get_results() -> list[dict]` — all completed/failed tasks
               - `wait(task_id, timeout=None) -> result` — block until task completes

            2. `task_queue/worker.py` — `Worker` class:
               - Takes a `TaskQueue` instance
               - `start()` / `stop()` — runs tasks from the queue in a background thread
               - Handles exceptions gracefully (task status → 'failed', error captured)

            3. `task_queue/middleware.py` — composable middleware:
               - `RetryMiddleware(max_retries=3)` — retry failed tasks
               - `TimeoutMiddleware(seconds=5)` — kill tasks exceeding time limit
               - `LoggingMiddleware()` — print task lifecycle events

            4. `main.py` — demo script that:
               - Creates a queue with retry + logging middleware
               - Submits 3 tasks (one succeeds, one fails once then succeeds, one always fails)
               - Prints final status of all tasks
               - Output must contain "Tasks completed: 3"
        ''',
    })

def _validate_case2(ws: Path) -> tuple[bool, str, dict]:
    checks = {}

    # All required files exist
    for f in ['task_queue/queue.py', 'task_queue/worker.py', 'task_queue/middleware.py', 'main.py']:
        ok, msg = _file_has(ws, f)
        checks[f'exists_{f.replace("/", "_")}'] = (ok, msg)

    # main.py runs and produces expected output
    ok, msg = _python_ok(ws, 'main.py', 'Tasks completed: 3')
    checks['main_runs'] = (ok, msg)

    # Check for actual class implementations
    ok_q, msg_q = _file_has(ws, 'task_queue/queue.py', 'class TaskQueue', 'submit', 'get_status')
    checks['queue_has_api'] = (ok_q, msg_q)
    ok_w, msg_w = _file_has(ws, 'task_queue/worker.py', 'class Worker', 'start', 'stop')
    checks['worker_has_api'] = (ok_w, msg_w)
    ok_m, msg_m = _file_has(ws, 'task_queue/middleware.py', 'RetryMiddleware', 'TimeoutMiddleware')
    checks['middleware_has_classes'] = (ok_m, msg_m)

    all_ok = all(v[0] for v in checks.values())
    return all_ok, 'PASS' if all_ok else 'FAIL', checks


# ── Case 3: Legacy Code Refactor ─────────────────────────────────────────────
# Refactor a messy single-file script into clean modular code — tests
# understanding of existing code and ability to preserve behavior.

def _setup_case3(ws: Path):
    _write_files(ws, {
        'analyzer.py': '''
            """Log analyzer — messy single-file script that needs refactoring."""
            import re
            import json
            from datetime import datetime
            from collections import Counter, defaultdict

            # Global state — bad practice
            _stats = {'total': 0, 'errors': 0, 'warnings': 0}
            _error_patterns = Counter()
            _hourly_counts = defaultdict(int)
            _slow_requests = []

            LOG_PATTERN = re.compile(
                r'\\[(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})\\] (\\w+) (.+)'
            )
            SLOW_THRESHOLD_MS = 1000

            def parse_line(line):
                """Parse a single log line. Returns (timestamp, level, message) or None."""
                m = LOG_PATTERN.match(line.strip())
                if not m:
                    return None
                ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                return ts, m.group(2), m.group(3)

            def process_line(line):
                """Process a single log line — updates global state."""
                parsed = parse_line(line)
                if not parsed:
                    return
                ts, level, msg = parsed
                _stats['total'] += 1
                _hourly_counts[ts.hour] += 1
                if level == 'ERROR':
                    _stats['errors'] += 1
                    # Extract error type
                    err_type = msg.split(':')[0].strip() if ':' in msg else msg[:50]
                    _error_patterns[err_type] += 1
                elif level == 'WARNING':
                    _stats['warnings'] += 1
                # Check for slow requests
                duration_match = re.search(r'duration=(\\d+)ms', msg)
                if duration_match:
                    duration = int(duration_match.group(1))
                    if duration > SLOW_THRESHOLD_MS:
                        _slow_requests.append({
                            'timestamp': ts.isoformat(),
                            'duration_ms': duration,
                            'message': msg[:100],
                        })

            def analyze_file(filepath):
                """Analyze a log file."""
                global _stats, _error_patterns, _hourly_counts, _slow_requests
                _stats = {'total': 0, 'errors': 0, 'warnings': 0}
                _error_patterns = Counter()
                _hourly_counts = defaultdict(int)
                _slow_requests = []

                with open(filepath) as f:
                    for line in f:
                        process_line(line)

                return generate_report()

            def generate_report():
                """Generate analysis report from global state."""
                report = {
                    'summary': dict(_stats),
                    'error_rate': round(_stats['errors'] / max(_stats['total'], 1) * 100, 2),
                    'top_errors': _error_patterns.most_common(5),
                    'peak_hour': max(_hourly_counts, key=_hourly_counts.get) if _hourly_counts else None,
                    'hourly_distribution': dict(sorted(_hourly_counts.items())),
                    'slow_requests': sorted(_slow_requests, key=lambda x: -x['duration_ms'])[:10],
                }
                return report

            def print_report(report):
                """Pretty-print the report."""
                print(f"=== Log Analysis Report ===")
                print(f"Total entries: {report['summary']['total']}")
                print(f"Errors: {report['summary']['errors']} ({report['error_rate']}%)")
                print(f"Warnings: {report['summary']['warnings']}")
                if report['peak_hour'] is not None:
                    print(f"Peak hour: {report['peak_hour']}:00")
                if report['top_errors']:
                    print(f"Top errors:")
                    for err, count in report['top_errors']:
                        print(f"  {count}x {err}")
                if report['slow_requests']:
                    print(f"Slow requests ({len(report['slow_requests'])}):")
                    for req in report['slow_requests'][:3]:
                        print(f"  {req['duration_ms']}ms at {req['timestamp']}")

            if __name__ == '__main__':
                import sys
                if len(sys.argv) > 1:
                    report = analyze_file(sys.argv[1])
                    print_report(report)
                else:
                    print("Usage: python analyzer.py <logfile>")
        ''',
        'test_data.log': '''
            [2024-01-15 10:00:01] INFO Request /api/users duration=50ms
            [2024-01-15 10:00:02] INFO Request /api/orders duration=120ms
            [2024-01-15 10:00:05] ERROR DatabaseError: Connection timeout
            [2024-01-15 10:00:06] WARNING High memory usage: 85%
            [2024-01-15 10:01:01] INFO Request /api/users duration=1500ms
            [2024-01-15 10:01:02] ERROR DatabaseError: Connection timeout
            [2024-01-15 10:01:03] INFO Request /api/products duration=30ms
            [2024-01-15 11:00:01] INFO Request /api/checkout duration=2000ms
            [2024-01-15 11:00:02] ERROR AuthenticationError: Invalid token
            [2024-01-15 11:00:03] WARNING Disk space low: 90%
            [2024-01-15 11:00:04] INFO Request /api/users duration=80ms
            [2024-01-15 12:00:01] ERROR DatabaseError: Connection timeout
            [2024-01-15 12:00:02] INFO Request /api/orders duration=200ms
        ''',
        'test_analyzer.py': '''
            """Tests that MUST pass after refactoring — verifies behavior is preserved."""
            import json
            import sys
            import os
            sys.path.insert(0, os.path.dirname(__file__))

            def test_analyze_produces_correct_report():
                """The refactored code must produce identical results to the original."""
                # Import the refactored module — it should expose analyze_file and print_report
                # Try new modular structure first, fall back to original
                try:
                    from log_analyzer import analyze_file, print_report
                except ImportError:
                    from analyzer import analyze_file, print_report

                report = analyze_file('test_data.log')

                # Verify summary counts
                assert report['summary']['total'] == 13, f"Expected 13 total, got {report['summary']['total']}"
                assert report['summary']['errors'] == 4, f"Expected 4 errors, got {report['summary']['errors']}"
                assert report['summary']['warnings'] == 2, f"Expected 2 warnings, got {report['summary']['warnings']}"

                # Verify error rate
                assert abs(report['error_rate'] - 30.77) < 0.1, f"Expected ~30.77% error rate, got {report['error_rate']}"

                # Verify peak hour
                assert report['peak_hour'] == 10, f"Expected peak hour 10, got {report['peak_hour']}"

                # Verify slow requests found
                assert len(report['slow_requests']) == 2, f"Expected 2 slow requests, got {len(report['slow_requests'])}"
                assert report['slow_requests'][0]['duration_ms'] == 2000

                # Verify top errors
                top_error_names = [e[0] for e in report['top_errors']]
                assert 'DatabaseError' in top_error_names, f"Expected DatabaseError in top errors, got {top_error_names}"

                print("All tests passed!")

            if __name__ == '__main__':
                test_analyze_produces_correct_report()
        ''',
    })

def _validate_case3(ws: Path) -> tuple[bool, str, dict]:
    checks = {}

    # Tests must pass (behavior preserved)
    ok, msg = _python_ok(ws, 'test_analyzer.py', 'All tests passed!')
    checks['behavior_preserved'] = (ok, msg)

    # Should have created modular structure (at least 2 new files)
    new_py_files = list(ws.glob('log_analyzer/**/*.py')) + list(ws.glob('log_analyzer.py'))
    if not new_py_files:
        # Maybe they refactored in-place — check analyzer.py was restructured
        new_py_files = [ws / 'analyzer.py']
    checks['has_modules'] = (
        len(list(ws.glob('**/*.py'))) > 3,
        f'Found {len(list(ws.glob("**/*.py")))} .py files',
    )

    # Original global state pattern should be removed from refactored code
    # (the original analyzer.py may still exist as a compat wrapper, so skip it)
    has_global = False
    for pyfile in ws.glob('**/*.py'):
        if pyfile.name.startswith('test_') or pyfile.name == 'analyzer.py':
            continue
        content = pyfile.read_text(errors='replace')
        if 'global _stats' in content or 'global _error_patterns' in content:
            has_global = True
    checks['no_global_state'] = (not has_global, 'Global state removed in refactored code' if not has_global else 'Still has global state in refactored code')

    all_ok = all(v[0] for v in checks.values())
    return all_ok, 'PASS' if all_ok else 'FAIL', checks


# ── Case 4: Implement a CLI Tool with Subcommands ────────────────────────────
# Tests ability to build a complete feature with argument parsing,
# file I/O, and formatted output.

def _setup_case4(ws: Path):
    _write_files(ws, {
        'data/contacts.json': '''
            [
                {"id": 1, "name": "Alice Johnson", "email": "alice@example.com", "phone": "555-0101", "tags": ["friend", "work"]},
                {"id": 2, "name": "Bob Smith", "email": "bob@example.com", "phone": "555-0102", "tags": ["work"]},
                {"id": 3, "name": "Carol White", "email": "carol@example.com", "phone": "555-0103", "tags": ["family"]},
                {"id": 4, "name": "David Brown", "email": "david@example.com", "phone": "555-0104", "tags": ["friend"]},
                {"id": 5, "name": "Eve Davis", "email": "eve@example.com", "phone": "555-0105", "tags": ["work", "friend"]}
            ]
        ''',
        'README.md': '''
            # Contact Manager CLI

            Build a CLI tool `contacts.py` using argparse with these subcommands:

            1. `python contacts.py list` — Show all contacts in a table format
            2. `python contacts.py search <query>` — Search by name or email (case-insensitive)
            3. `python contacts.py tag <tag>` — Filter contacts by tag
            4. `python contacts.py add --name NAME --email EMAIL [--phone PHONE] [--tags tag1,tag2]` — Add a contact
            5. `python contacts.py stats` — Show summary statistics

            Data file: `data/contacts.json`

            ## Expected output examples:

            `python contacts.py search alice` should print a line containing "Alice Johnson"
            `python contacts.py tag work` should show 3 contacts (Alice, Bob, Eve)
            `python contacts.py stats` should print "Total contacts: 5"

            After `python contacts.py add --name "Frank Lee" --email frank@example.com --tags work`:
            `python contacts.py stats` should print "Total contacts: 6"
        ''',
    })

def _validate_case4(ws: Path) -> tuple[bool, str, dict]:
    checks = {}

    ok, msg = _file_has(ws, 'contacts.py', 'argparse', 'def ')
    checks['file_exists'] = (ok, msg)

    # Reset data file first (tool may have added contacts during its run)
    try:
        orig_data = json.loads((ws / 'data' / 'contacts.json').read_text())
        orig_5 = [c for c in orig_data if c.get('id', 0) <= 5][:5]
        if len(orig_5) == 5:
            (ws / 'data' / 'contacts.json').write_text(json.dumps(orig_5, indent=2))
    except Exception:
        pass

    # Test search (use subprocess directly, not _python_ok which expects a file path)
    r = _run_in(ws, f'{sys.executable} contacts.py search alice')
    search_ok = r.returncode == 0 and 'Alice' in r.stdout
    checks['search_works'] = (search_ok, f'search: {"found Alice" if search_ok else r.stdout[:200] + r.stderr[:100]}')

    # Test tag filter
    r = _run_in(ws, f'{sys.executable} contacts.py tag work')
    tag_ok = r.returncode == 0 and ('Alice' in r.stdout or 'alice' in r.stdout.lower())
    checks['tag_filter'] = (tag_ok, f'tag work: {r.stdout[:200]}' if not tag_ok else 'tag filter works')

    # Test stats (should show 5 after reset)
    r = _run_in(ws, f'{sys.executable} contacts.py stats')
    stats_ok = r.returncode == 0 and 'Total contacts: 5' in r.stdout
    checks['stats_works'] = (stats_ok, f'stats: {r.stdout[:200]}' if not stats_ok else 'stats works')

    # Test add then stats
    _run_in(ws, f'{sys.executable} contacts.py add --name "Frank Lee" --email frank@example.com --tags work')
    r = _run_in(ws, f'{sys.executable} contacts.py stats')
    add_ok = r.returncode == 0 and 'Total contacts: 6' in r.stdout
    checks['add_works'] = (add_ok, f'add+stats: {r.stdout[:200]}' if not add_ok else 'add + stats works')

    all_ok = all(v[0] for v in checks.values())
    return all_ok, 'PASS' if all_ok else 'FAIL', checks


# ── Case 5: Debug a Concurrency Issue ────────────────────────────────────────
# The model must identify and fix a race condition — tests deep understanding
# of threading and debugging skills.

def _setup_case5(ws: Path):
    _write_files(ws, {
        'bank.py': '''
            """Simple bank with a thread-safety bug."""
            import threading
            import time

            class BankAccount:
                def __init__(self, balance: float = 0):
                    self.balance = balance
                    # Note: no lock!

                def deposit(self, amount: float):
                    current = self.balance
                    time.sleep(0.001)  # Simulate processing delay
                    self.balance = current + amount

                def withdraw(self, amount: float) -> bool:
                    current = self.balance
                    time.sleep(0.001)  # Simulate processing delay
                    if current >= amount:
                        self.balance = current - amount
                        return True
                    return False

                def transfer(self, other: "BankAccount", amount: float) -> bool:
                    """Transfer amount from self to other."""
                    if self.withdraw(amount):
                        other.deposit(amount)
                        return True
                    return False

            def run_test():
                """Test that exposes the race condition."""
                account = BankAccount(1000)
                results = {'deposits': 0, 'withdrawals': 0}

                def do_deposits():
                    for _ in range(100):
                        account.deposit(10)
                        results['deposits'] += 1

                def do_withdrawals():
                    for _ in range(100):
                        if account.withdraw(10):
                            results['withdrawals'] += 1

                threads = []
                for _ in range(5):
                    threads.append(threading.Thread(target=do_deposits))
                    threads.append(threading.Thread(target=do_withdrawals))

                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                expected = 1000 + (results['deposits'] * 10) - (results['withdrawals'] * 10)
                actual = account.balance
                if abs(actual - expected) < 0.01:
                    print(f"PASS: balance={actual:.2f} expected={expected:.2f}")
                    return True
                else:
                    print(f"FAIL: balance={actual:.2f} expected={expected:.2f} diff={actual-expected:.2f}")
                    return False

            if __name__ == '__main__':
                # Run multiple times to increase chance of seeing the bug
                passes = 0
                total = 5
                for i in range(total):
                    if run_test():
                        passes += 1
                print(f"Results: {passes}/{total} passed")
                if passes == total:
                    print("All consistent!")
                else:
                    print("Race condition detected!")
        ''',
    })

def _validate_case5(ws: Path) -> tuple[bool, str, dict]:
    checks = {}

    # Must have a lock in the code
    ok_lock, msg_lock = _file_has(ws, 'bank.py', 'Lock')
    checks['has_lock'] = (ok_lock, msg_lock)

    # Run the test 3 times — all should pass consistently
    all_passed = True
    for i in range(3):
        r = _run_in(ws, f'{sys.executable} bank.py', timeout=30)
        if r.returncode != 0:
            all_passed = False
            checks[f'run_{i}'] = (False, f'exit={r.returncode}: {r.stderr[:200]}')
        elif 'All consistent!' not in r.stdout:
            all_passed = False
            checks[f'run_{i}'] = (False, f'Not consistent: {r.stdout[:200]}')
        else:
            checks[f'run_{i}'] = (True, 'All consistent')

    checks['consistently_passes'] = (all_passed, 'All 3 runs consistent' if all_passed else 'Inconsistent results')

    all_ok = all(v[0] for v in checks.values())
    return all_ok, 'PASS' if all_ok else 'FAIL', checks


# ── Case 6: Add Comprehensive Tests + CI Config for Existing Code ────────────
# Tests ability to understand undocumented code, write thorough tests,
# and set up infrastructure.

def _setup_case6(ws: Path):
    _write_files(ws, {
        'markdown_parser.py': '''
            """A simple Markdown-to-HTML converter."""
            import re

            class MarkdownParser:
                """Convert a subset of Markdown to HTML."""

                def __init__(self):
                    self._rules = [
                        # Headers
                        (re.compile(r'^######\\s+(.+)$', re.M), r'<h6>\\1</h6>'),
                        (re.compile(r'^#####\\s+(.+)$', re.M), r'<h5>\\1</h5>'),
                        (re.compile(r'^####\\s+(.+)$', re.M), r'<h4>\\1</h4>'),
                        (re.compile(r'^###\\s+(.+)$', re.M), r'<h3>\\1</h3>'),
                        (re.compile(r'^##\\s+(.+)$', re.M), r'<h2>\\1</h2>'),
                        (re.compile(r'^#\\s+(.+)$', re.M), r'<h1>\\1</h1>'),
                        # Bold and italic
                        (re.compile(r'\\*\\*\\*(.+?)\\*\\*\\*'), r'<strong><em>\\1</em></strong>'),
                        (re.compile(r'\\*\\*(.+?)\\*\\*'), r'<strong>\\1</strong>'),
                        (re.compile(r'\\*(.+?)\\*'), r'<em>\\1</em>'),
                        # Code
                        (re.compile(r'`(.+?)`'), r'<code>\\1</code>'),
                        # Links
                        (re.compile(r'\\[(.+?)\\]\\((.+?)\\)'), r'<a href="\\2">\\1</a>'),
                        # Horizontal rule
                        (re.compile(r'^---+$', re.M), r'<hr>'),
                    ]

                def parse(self, text: str) -> str:
                    """Convert markdown text to HTML."""
                    result = text
                    for pattern, replacement in self._rules:
                        result = pattern.sub(replacement, result)
                    # Handle paragraphs: split by double newlines
                    paragraphs = result.split('\\n\\n')
                    processed = []
                    for p in paragraphs:
                        p = p.strip()
                        if not p:
                            continue
                        # Don't wrap block elements in <p>
                        if p.startswith('<h') or p.startswith('<hr'):
                            processed.append(p)
                        else:
                            # Replace single newlines with <br>
                            p = p.replace('\\n', '<br>')
                            processed.append(f'<p>{p}</p>')
                    return '\\n'.join(processed)

                def parse_inline(self, text: str) -> str:
                    """Parse only inline elements (bold, italic, code, links)."""
                    result = text
                    for pattern, replacement in self._rules[6:]:  # Skip headers and HR
                        result = pattern.sub(replacement, result)
                    return result

                def extract_links(self, text: str) -> list:
                    """Extract all [text](url) links from markdown."""
                    return re.findall(r'\\[(.+?)\\]\\((.+?)\\)', text)

                def extract_headers(self, text: str) -> list:
                    """Extract all headers with their levels."""
                    headers = []
                    for line in text.split('\\n'):
                        m = re.match(r'^(#{1,6})\\s+(.+)$', line)
                        if m:
                            headers.append({'level': len(m.group(1)), 'text': m.group(2)})
                    return headers
        ''',
    })

def _validate_case6(ws: Path) -> tuple[bool, str, dict]:
    checks = {}

    # Test file must exist and pass
    test_files = list(ws.glob('test_*.py')) + list(ws.glob('tests/test_*.py'))
    has_tests = len(test_files) > 0
    checks['test_file_exists'] = (has_tests, f'Found {len(test_files)} test files' if has_tests else 'No test files')

    if has_tests:
        # Run pytest on all test files
        r = _run_in(ws, f'{sys.executable} -m pytest -v --tb=short', timeout=30)
        passed = 'passed' in r.stdout and 'failed' not in r.stdout
        # Count test functions
        test_count = r.stdout.count(' PASSED')
        checks['tests_pass'] = (passed, f'{test_count} tests passed' if passed else r.stdout[-300:])
        checks['enough_tests'] = (test_count >= 10, f'{test_count} tests (need ≥10)')
    else:
        checks['tests_pass'] = (False, 'No test files to run')
        checks['enough_tests'] = (False, 'No tests')

    # Should test edge cases
    if test_files:
        all_test_content = '\n'.join(f.read_text(errors='replace') for f in test_files)
        has_edge = (
            'empty' in all_test_content.lower() or
            '""' in all_test_content or
            "'')" in all_test_content or
            'edge' in all_test_content.lower()
        )
        checks['has_edge_cases'] = (has_edge, 'Tests include edge cases' if has_edge else 'Missing edge case tests')

    all_ok = all(v[0] for v in checks.values())
    return all_ok, 'PASS' if all_ok else 'FAIL', checks


# ═══════════════════════════════════════════════════════════════════════════════
# CASE REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

CASES = [
    TestCase(
        id=1,
        name='cross_file_bug_hunt',
        description='Trace and fix a discount calculation bug across 3 files (models → services → repository)',
        category='debug',
        difficulty='medium',
        prompt=(
            'The test in test_orders.py is failing. The bug is somewhere in the app/ directory. '
            'Read the code, find the root cause, and fix it. Do NOT modify test_orders.py. '
            'Run the test after fixing to confirm it passes.'
        ),
        setup=_setup_case1,
        validate=_validate_case1,
    ),
    TestCase(
        id=2,
        name='task_queue_system',
        description='Build a complete task queue with workers and middleware from a README spec',
        category='feature',
        difficulty='hard',
        prompt=(
            'Read README.md and implement the task queue system as described. '
            'Create all the required files in the task_queue/ directory and main.py. '
            'Make sure main.py runs and prints "Tasks completed: 3". '
            'Run main.py to verify it works.'
        ),
        setup=_setup_case2,
        validate=_validate_case2,
    ),
    TestCase(
        id=3,
        name='legacy_refactor',
        description='Refactor a messy single-file log analyzer into clean modular code while preserving behavior',
        category='refactor',
        difficulty='hard',
        prompt=(
            'The file analyzer.py is a messy single-file script with global state. '
            'Refactor it into a clean modular structure:\n'
            '- Create a package (e.g., log_analyzer/) with separate modules for parsing, '
            'analysis, and reporting\n'
            '- Eliminate all global mutable state — use classes instead\n'
            '- Make sure the existing test_analyzer.py still passes after refactoring '
            '(it tries to import from log_analyzer first, then falls back to analyzer)\n'
            '- Run test_analyzer.py to verify behavior is preserved.'
        ),
        setup=_setup_case3,
        validate=_validate_case3,
    ),
    TestCase(
        id=4,
        name='cli_tool_with_subcommands',
        description='Build a full CLI tool with 5 subcommands, JSON persistence, and formatted output',
        category='feature',
        difficulty='medium',
        prompt=(
            'Read README.md and build the contacts.py CLI tool as specified. '
            'It should work with the existing data/contacts.json file. '
            'Test all subcommands: list, search alice, tag work, stats, '
            'and add a new contact then verify stats shows the updated count.'
        ),
        setup=_setup_case4,
        validate=_validate_case4,
    ),
    TestCase(
        id=5,
        name='fix_race_condition',
        description='Identify and fix a thread-safety bug in a bank account system',
        category='debug',
        difficulty='hard',
        prompt=(
            'The file bank.py has a concurrency bug — when run, it sometimes shows '
            '"Race condition detected!" because the BankAccount is not thread-safe. '
            'Read the code, identify the race condition, and fix it so that all 5 '
            'test runs pass consistently. The fix should use proper synchronization. '
            'Run bank.py to verify all runs pass.'
        ),
        setup=_setup_case5,
        validate=_validate_case5,
    ),
    TestCase(
        id=6,
        name='comprehensive_test_suite',
        description='Write 10+ thorough tests for an undocumented Markdown parser, covering edge cases',
        category='test',
        difficulty='medium',
        prompt=(
            'Read markdown_parser.py carefully. Write a comprehensive test suite '
            'using pytest. Requirements:\n'
            '- At least 10 test functions\n'
            '- Cover all public methods (parse, parse_inline, extract_links, extract_headers)\n'
            '- Include edge cases: empty input, nested formatting, special characters\n'
            '- Test all supported markdown elements: headers (h1-h6), bold, italic, '
            'bold+italic, code, links, horizontal rules, paragraphs\n'
            '- Run the tests to make sure they all pass.'
        ),
        setup=_setup_case6,
        validate=_validate_case6,
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNERS
# ═══════════════════════════════════════════════════════════════════════════════

def run_tofu(case: TestCase, workspace: Path, timeout: int) -> RunResult:
    """Run a case using the Tofu API."""
    result = RunResult(case_id=case.id, tool='tofu')
    t0 = time.time()

    try:
        resp = requests.post(
            f'{TOFU_BASE_URL}/api/chat/start',
            json={
                'convId': f'bench-{case.id}-tofu-{int(time.time())}',
                'messages': [{'role': 'user', 'content': case.prompt}],
                'config': {
                    'model': TOFU_MODEL,
                    'projectPath': str(workspace),
                },
            },
            timeout=10,
        )
        resp.raise_for_status()
        task_id = resp.json()['taskId']

        # Poll until done
        deadline = time.time() + timeout
        poll_interval = 1.5
        while time.time() < deadline:
            time.sleep(poll_interval)
            poll_resp = requests.get(
                f'{TOFU_BASE_URL}/api/chat/poll/{task_id}',
                timeout=10,
            )
            poll_resp.raise_for_status()
            data = poll_resp.json()

            if data.get('status') in ('done', 'error', 'interrupted'):
                result.duration_s = time.time() - t0
                result.response_text = data.get('content', '')
                result.error = data.get('error', '')

                # Parse usage from apiRounds
                api_rounds = data.get('apiRounds', [])
                if isinstance(api_rounds, list) and api_rounds:
                    result.num_turns = len(api_rounds)
                    for rd in api_rounds:
                        ru = rd.get('usage', {})
                        result.input_tokens += ru.get('prompt_tokens', 0)
                        result.output_tokens += ru.get('completion_tokens', 0)
                        result.cache_read_tokens += ru.get('cache_read_tokens', 0)
                        result.cache_write_tokens += ru.get('cache_write_tokens', 0)
                else:
                    usage = data.get('usage', {})
                    result.input_tokens = usage.get('prompt_tokens', 0)
                    result.output_tokens = usage.get('completion_tokens', 0)
                    result.cache_read_tokens = usage.get('cache_read_tokens', 0)
                    result.cache_write_tokens = usage.get('cache_write_tokens', 0)
                    result.num_turns = 1

                result.cost_usd = _compute_cost(result)

                if data.get('status') == 'error':
                    result.error = data.get('error', 'Unknown error')
                break

            poll_interval = min(poll_interval * 1.2, 4.0)
        else:
            result.duration_s = time.time() - t0
            result.error = f'Timeout after {timeout}s'
            try:
                requests.post(f'{TOFU_BASE_URL}/api/chat/abort/{task_id}', timeout=5)
            except Exception:
                pass

    except Exception as e:
        result.duration_s = time.time() - t0
        result.error = str(e)

    return result


def run_cc(case: TestCase, workspace: Path, timeout: int) -> RunResult:
    """Run a case using Claude Code CLI (--print mode)."""
    result = RunResult(case_id=case.id, tool='cc')
    t0 = time.time()

    try:
        proc = subprocess.run(
            [
                'claude', '-p',
                '--output-format', 'json',
                '--model', CC_MODEL,
                '--dangerously-skip-permissions',
                case.prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
        )

        result.duration_s = time.time() - t0

        if proc.returncode != 0:
            result.error = f'Exit code {proc.returncode}: {proc.stderr[:500]}'

        _parse_cc_output(proc.stdout, result)

        # Always compute cost consistently using our pricing
        # (CC's total_cost_usd uses its own internal pricing table)
        if result.input_tokens > 0 or result.cache_read_tokens > 0:
            result.cost_usd = _compute_cost(result)

    except subprocess.TimeoutExpired:
        result.duration_s = time.time() - t0
        result.error = f'Timeout after {timeout}s'
    except Exception as e:
        result.duration_s = time.time() - t0
        result.error = str(e)

    return result


def _parse_cc_output(stdout: str, result: RunResult):
    """Parse Claude Code JSON output into RunResult."""
    if not stdout.strip():
        return
    try:
        data = json.loads(stdout.strip())
        result.response_text = data.get('result', '')
        result.num_turns = data.get('num_turns', 1)
        result.duration_s = data.get('duration_ms', result.duration_s * 1000) / 1000.0

        usage = data.get('usage', {})
        result.input_tokens = usage.get('input_tokens', 0)
        result.output_tokens = usage.get('output_tokens', 0)
        result.cache_read_tokens = usage.get('cache_read_input_tokens', 0)
        result.cache_write_tokens = usage.get('cache_creation_input_tokens', 0)

        # Cost is computed consistently in run_cc() using _compute_cost()
    except (json.JSONDecodeError, KeyError, TypeError):
        result.response_text = stdout[:2000]


def _compute_cost(result: RunResult) -> float:
    """Compute cost from token counts.

    Both Tofu and CC use the Anthropic convention from the example-corp gateway:
      - prompt_tokens / input_tokens = uncached input tokens only
      - cache_read_tokens = tokens read from cache (10% of input price)
      - cache_write_tokens = tokens written to cache (125% of input price)
    """
    cost = (
        result.input_tokens * PRICE_INPUT_PER_1K / 1000
        + result.output_tokens * PRICE_OUTPUT_PER_1K / 1000
        + result.cache_read_tokens * PRICE_CACHE_READ_PER_1K / 1000
        + result.cache_write_tokens * PRICE_CACHE_WRITE_PER_1K / 1000
    )
    return round(cost, 6)


# ═══════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def validate_result(case: TestCase, result: RunResult, workspace: Path):
    """Run validation for a case."""
    if result.error and not result.response_text:
        result.success = False
        result.validations = {'error': (False, result.error[:300])}
        return

    try:
        ok, summary, checks = case.validate(workspace)
        result.success = ok
        result.validations = checks
    except Exception as e:
        result.success = False
        result.validations = {'validation_error': (False, str(e)[:300])}


# ═══════════════════════════════════════════════════════════════════════════════
# WORKSPACE
# ═══════════════════════════════════════════════════════════════════════════════

def setup_workspace(case: TestCase, tool: str, base_dir: Path, run_idx: int = 0) -> Path:
    """Create a clean workspace for a case."""
    ws = base_dir / f'case{case.id}_{tool}_r{run_idx}'
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    case.setup(ws)
    return ws


# ═══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════════════════════

def print_case_result(case: TestCase, result: RunResult):
    """Print result for a single case + tool."""
    status = '✅ PASS' if result.success else '❌ FAIL'
    print(f'    [{result.tool.upper():5s}] {status}  '
          f'⏱ {result.duration_s:6.1f}s  '
          f'💰 ${result.cost_usd:.4f}  '
          f'🔄 {result.num_turns} turns  '
          f'📊 in={result.input_tokens:,} out={result.output_tokens:,} '
          f'cache_r={result.cache_read_tokens:,} cache_w={result.cache_write_tokens:,}')

    for vname, (passed, msg) in result.validations.items():
        icon = '  ✓' if passed else '  ✗'
        print(f'      {icon} [{vname}] {msg}')

    if result.error:
        print(f'      ⚠ Error: {result.error[:300]}')


def print_summary(all_results: list[RunResult], cases_map: dict):
    """Print aggregate comparison."""
    print('\n' + '═' * 100)
    print('                              BENCHMARK SUMMARY')
    print('═' * 100)

    # Per-case comparison table
    print(f'\n{"Case":<28s} │ {"Tofu":^30s} │ {"Claude Code":^30s} │ Winner')
    print(f'{"":28s} │ {"Pass   Time     Cost":^30s} │ {"Pass   Time     Cost":^30s} │')
    print('─' * 100)

    tofu_by = {}
    cc_by = {}
    for r in all_results:
        bucket = tofu_by if r.tool == 'tofu' else cc_by
        bucket.setdefault(r.case_id, []).append(r)

    case_winners = {'tofu': 0, 'cc': 0, 'tie': 0}

    for cid in sorted(set(r.case_id for r in all_results)):
        case = cases_map[cid]
        t_list = tofu_by.get(cid, [])
        c_list = cc_by.get(cid, [])

        # Average across runs
        def _avg(lst, attr):
            if not lst: return 0
            return sum(getattr(r, attr) for r in lst) / len(lst)

        def _any_pass(lst):
            return any(r.success for r in lst) if lst else False

        def _pass_rate(lst):
            if not lst: return '—'
            p = sum(1 for r in lst if r.success)
            return f'{p}/{len(lst)}'

        t_str = f'{_pass_rate(t_list):>5s}  {_avg(t_list, "duration_s"):5.0f}s  ${_avg(t_list, "cost_usd"):.4f}' if t_list else '         — skipped —'
        c_str = f'{_pass_rate(c_list):>5s}  {_avg(c_list, "duration_s"):5.0f}s  ${_avg(c_list, "cost_usd"):.4f}' if c_list else '         — skipped —'

        # Determine winner
        winner = '—'
        if t_list and c_list:
            t_pass = sum(1 for r in t_list if r.success)
            c_pass = sum(1 for r in c_list if r.success)
            if t_pass > c_pass:
                winner = '🏆 Tofu'
                case_winners['tofu'] += 1
            elif c_pass > t_pass:
                winner = '🏆 CC'
                case_winners['cc'] += 1
            else:
                # Same pass rate → compare cost
                t_cost = _avg(t_list, 'cost_usd')
                c_cost = _avg(c_list, 'cost_usd')
                if t_cost < c_cost * 0.9:
                    winner = '💰 Tofu'
                    case_winners['tofu'] += 1
                elif c_cost < t_cost * 0.9:
                    winner = '💰 CC'
                    case_winners['cc'] += 1
                else:
                    winner = 'Tie'
                    case_winners['tie'] += 1

        print(f'{case.name:<28s} │ {t_str:>30s} │ {c_str:>30s} │ {winner}')

    print('─' * 100)

    # Aggregates
    for label, tool_key in [('Tofu', 'tofu'), ('Claude Code', 'cc')]:
        rs = [r for r in all_results if r.tool == tool_key]
        if not rs:
            continue
        passed = sum(1 for r in rs if r.success)
        total = len(rs)
        avg_time = sum(r.duration_s for r in rs) / total
        total_cost = sum(r.cost_usd for r in rs)
        total_in = sum(r.input_tokens for r in rs)
        total_out = sum(r.output_tokens for r in rs)
        total_cr = sum(r.cache_read_tokens for r in rs)
        total_cw = sum(r.cache_write_tokens for r in rs)
        avg_turns = sum(r.num_turns for r in rs) / total
        cache_rate = total_cr / max(total_in + total_cr + total_cw, 1) * 100

        print(f'\n  {label}:')
        print(f'    Accuracy:       {passed}/{total} ({100*passed/total:.0f}%)')
        print(f'    Avg time:       {avg_time:.1f}s')
        print(f'    Total cost:     ${total_cost:.4f}')
        print(f'    Avg turns:      {avg_turns:.1f}')
        print(f'    Tokens:         {total_in:,} in + {total_out:,} out')
        print(f'    Cache:          {total_cr:,} read ({cache_rate:.0f}%), {total_cw:,} write')

    # Overall winner
    if case_winners['tofu'] + case_winners['cc'] > 0:
        print(f'\n  Case wins: Tofu={case_winners["tofu"]}, CC={case_winners["cc"]}, Tie={case_winners["tie"]}')
        if case_winners['tofu'] > case_winners['cc']:
            print('  🏆 Overall: Tofu wins!')
        elif case_winners['cc'] > case_winners['tofu']:
            print('  🏆 Overall: Claude Code wins!')
        else:
            print('  🤝 Overall: It\'s a tie!')

    print('\n' + '═' * 100)

    return {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'config': {
            'tofu_model': TOFU_MODEL,
            'cc_model': CC_MODEL,
            'timeout': DEFAULT_TIMEOUT,
        },
        'results': [
            {
                'case_id': r.case_id,
                'case_name': cases_map[r.case_id].name,
                'category': cases_map[r.case_id].category,
                'difficulty': cases_map[r.case_id].difficulty,
                'tool': r.tool,
                'run_idx': r.run_idx,
                'success': r.success,
                'duration_s': round(r.duration_s, 2),
                'cost_usd': round(r.cost_usd, 6),
                'input_tokens': r.input_tokens,
                'output_tokens': r.output_tokens,
                'cache_read_tokens': r.cache_read_tokens,
                'cache_write_tokens': r.cache_write_tokens,
                'num_turns': r.num_turns,
                'error': r.error,
                'validations': {k: {'pass': v[0], 'msg': v[1]} for k, v in r.validations.items()},
            }
            for r in all_results
        ],
        'case_winners': case_winners,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PREFLIGHT CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

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
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Benchmark Tofu vs Claude Code CLI — Complex Project Tasks',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent('''\
            Cases:
              1. cross_file_bug_hunt     — Trace & fix a bug across 3 files
              2. task_queue_system       — Build complete system from README spec
              3. legacy_refactor         — Refactor messy code, preserve behavior
              4. cli_tool_subcommands    — Build CLI with 5 subcommands
              5. fix_race_condition      — Debug and fix thread-safety bug
              6. comprehensive_tests     — Write 10+ tests for undocumented code
        '''),
    )
    parser.add_argument('--cases', type=str, default='',
                        help='Comma-separated case IDs (default: all)')
    parser.add_argument('--tool', choices=['tofu', 'cc', 'both'], default='both')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help=f'Timeout per case in seconds (default: {DEFAULT_TIMEOUT})')
    parser.add_argument('--repeat', type=int, default=1,
                        help='Run each case N times (for statistical significance)')
    parser.add_argument('--delay', type=int, default=10,
                        help='Delay between runs (seconds, to avoid rate limits)')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output', type=str, default='')
    args = parser.parse_args()

    # Select cases
    if args.cases:
        case_ids = set(int(x) for x in args.cases.split(','))
        selected = [c for c in CASES if c.id in case_ids]
    else:
        selected = CASES

    cases_map = {c.id: c for c in CASES}

    if args.dry_run:
        print('📋 Test Cases:')
        for c in selected:
            print(f'  [{c.id}] {c.name} ({c.category}, {c.difficulty})')
            print(f'      {c.description}')
        print(f'\n  Tools: {args.tool} | Repeat: {args.repeat}x | Timeout: {args.timeout}s')
        return

    # Preflight
    tools = []
    if args.tool in ('tofu', 'both'):
        if check_tofu():
            tools.append('tofu')
            print(f'✅ Tofu server: {TOFU_BASE_URL}')
        else:
            print(f'❌ Tofu server NOT reachable at {TOFU_BASE_URL}')

    if args.tool in ('cc', 'both'):
        if check_cc():
            tools.append('cc')
            print('✅ Claude Code CLI + proxy ready')
        else:
            print('❌ Claude Code CLI or proxy NOT available')

    if not tools:
        print('\n❌ No tools available.')
        sys.exit(1)

    total_runs = len(selected) * len(tools) * args.repeat
    print(f'\n🏃 {len(selected)} cases × {len(tools)} tools × {args.repeat} repeats = {total_runs} runs')
    print(f'   Timeout: {args.timeout}s | Delay: {args.delay}s\n')

    # Use project-local data/tmp/ instead of system /tmp (may not be accessible on all machines)
    _project_tmp = Path(__file__).resolve().parent.parent / 'data' / 'tmp'
    _project_tmp.mkdir(parents=True, exist_ok=True)
    base_dir = Path(tempfile.mkdtemp(prefix='bench_', dir=str(_project_tmp)))
    print(f'📁 Workspace: {base_dir}\n')

    all_results: list[RunResult] = []
    run_count = 0

    for case in selected:
        print(f'━━━ Case {case.id}: {case.name} ({case.category}, {case.difficulty}) ━━━')
        print(f'    {case.description}')

        for run_idx in range(args.repeat):
            if args.repeat > 1:
                print(f'\n  ── Run {run_idx + 1}/{args.repeat} ──')

            for tool in tools:
                workspace = setup_workspace(case, tool, base_dir, run_idx)
                print(f'\n    [{tool.upper()}] Running in {workspace.name}...')

                if tool == 'tofu':
                    result = run_tofu(case, workspace, args.timeout)
                else:
                    result = run_cc(case, workspace, args.timeout)
                result.run_idx = run_idx

                validate_result(case, result, workspace)
                print_case_result(case, result)
                all_results.append(result)

                run_count += 1
                if run_count < total_runs and args.delay > 0:
                    print(f'    ⏳ {args.delay}s cooldown...')
                    time.sleep(args.delay)

        print()

    # Summary
    summary = print_summary(all_results, cases_map)

    output_path = args.output or str(base_dir / 'results.json')
    with open(output_path, 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f'\n📄 Results: {output_path}')
    print(f'📁 Workspaces: {base_dir}')


if __name__ == '__main__':
    main()
