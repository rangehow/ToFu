#!/usr/bin/env python3
"""A/B test: Phase B2 paired-assistant interstitial compaction.

Two modes:

1. **Local byte-hash analysis** (``--local``, default, no API calls):
   Proves H2 structurally.  For each round we compute:
     - the set of message indices that Phase B mutates (BASELINE arm),
     - the set of message indices that Phase B + B2 mutates (PAIRED arm),
     - the prefix byte hash BEFORE and AFTER compaction, per arm.
   H2 is proven iff the mutation-set of PAIRED is a **superset** of the
   mutation-set of BASELINE AND every extra index lies ADJACENT to a
   BASELINE-mutated index (i.e. inside a pair already broken by Phase B).
   If both hold, Anthropic's cache invalidation cannot differ between
   arms, because cache keys are prefix-bytes-up-to-first-mutation.

2. **Live API run** (``--live``):
   Sends the compacted messages to the API and measures actual
   cache_read / cache_write tokens.  Requires available quota.


Tests the hypothesis that compacting the ``content`` on an
``assistant(tool_calls)`` message IS cache-neutral when done in the
same round as compacting its paired cold tool result (Phase B).

ARM A (BASELINE): micro_compact with Phase B only (production behavior).
ARM B (PAIRED)  : micro_compact with Phase B + Phase B2.

Hypothesis:
    H1 — Phase B2 saves EXTRA tokens (interstitial content gone).
    H2 — Phase B2 costs ZERO extra cache writes vs BASELINE (because
         Phase B was already mutating that prefix index).

If H1 and H2 both hold → enable Phase B2 unconditionally.
If H2 fails (cache writes increase) → keep off, same verdict as Phase D.

Why this is different from the Phase D test (2026-04-06, +57% cost):
    Phase D mutates assistant messages whose NEIGHBORS are intact, so
    it introduces a brand-new cache break.  Phase B2 only mutates
    assistants whose paired tool result was ALREADY mutated by Phase B
    — the cache break at that index is already paid for.

Usage:
    python debug/test_paired_compact_ab.py                    # Live
    python debug/test_paired_compact_ab.py --dry-run           # Logic only
    python debug/test_paired_compact_ab.py --model aws.claude-sonnet-4.6
    python debug/test_paired_compact_ab.py --rounds 10 --interval 8

Output:
    debug/paired_compact_ab_<timestamp>.json with per-round metrics.
    Intermediate JSON is saved AFTER EACH ROUND so ctrl-c / timeout
    never loses data.
"""

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger
from lib.llm_dispatch import dispatch_chat

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-sonnet-4.6'  # cheaper, 1024 min cache
DEFAULT_ROUNDS = 8
DEFAULT_INTERVAL = 8  # seconds between rounds — avoid rate-limit

# We monkey-patch MICRO_HOT_TAIL to a small value so Phase B fires
# even at low round counts; this keeps the A/B cheap without changing
# the semantics under test.
TEST_HOT_TAIL = 3

SYSTEM_PROMPT = """You are a coding assistant.  When the user asks a
question, investigate the code with tool calls, then answer.  Keep
answers brief — one paragraph max."""

# Realistic INTERSTITIAL commentary — what an assistant says right
# before each tool call.  This is what Phase B2 compacts.
INTERSTITIAL_COMMENTARIES = [
    (
        "Let me start by examining the project structure. I'll list the "
        "root directory to see what files exist, then read the main "
        "entry point to understand the architecture. This should give me "
        "a clear picture of how the application is organized before I "
        "dive deeper into any specific module."
    ),
    (
        "Now I need to search for the error handling patterns used "
        "throughout this codebase. I'm specifically looking for how "
        "exceptions propagate from the low-level request handlers up to "
        "the user-facing error messages, because this is often where "
        "subtle bugs hide and where refactoring needs to be cautious."
    ),
    (
        "Next I'll read the test suite for this module. Tests often "
        "document the intended behavior better than the implementation "
        "itself, and they'll tell me which edge cases the author "
        "consciously considered. Let me look at the full test file so I "
        "don't miss any corner cases."
    ),
    (
        "I want to check how this function is called elsewhere. If it "
        "has many callers with varying expectations, changing its "
        "signature is risky — I'd rather wrap the new behavior in a "
        "separate function and migrate callers gradually. Let me grep "
        "for all usages first."
    ),
    (
        "Let me examine the configuration file for any tunable "
        "parameters that might affect this behavior. Sometimes what "
        "looks like a code bug is actually a deployment misconfig, and "
        "I want to rule that out before proposing code changes."
    ),
    (
        "I should verify the database schema matches what the code "
        "expects. ORM-based codebases sometimes drift out of sync "
        "between the migration files and the live schema, especially "
        "after hot-fixes applied directly in production."
    ),
    (
        "Looking at the logging output for this module to understand "
        "what state transitions happen during a typical request. The "
        "log prefixes and levels will tell me which paths are hot and "
        "which are rare edge cases."
    ),
    (
        "Let me trace through one end-to-end request to confirm my "
        "understanding. I'll start from the HTTP entry point and follow "
        "the call chain down to the data layer, noting any caching, "
        "validation, or authorization steps along the way."
    ),
    (
        "Time to run the existing tests to establish a baseline before "
        "I propose any changes. If any tests are currently failing "
        "that's important context — I want to know what's already "
        "broken vs what I might break."
    ),
    (
        "Now I'll cross-reference with the external documentation. "
        "Sometimes the code implements a subset of a specification and "
        "the behavior that feels like a bug is actually documented as "
        "intentional behavior elsewhere."
    ),
]


# Tool definitions for the test
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_files",
            "description": "Read one or more files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                },
                "required": ["reads"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Pattern search across files.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]

# Realistic tool results (~1.5-2KB each so they exceed MICRO_COMPACT_THRESHOLD)
TOOL_RESULTS = {
    'list_dir': (
        "Directory: /project\n\n"
        + "Files:\n"
        + "".join(
            f"  module_{i:03d}.py ({120 + i * 7}L, {3.2 + i * 0.2:.1f}KB)\n"
            for i in range(30)
        )
        + "\nSubdirectories:\n"
        + "".join(f"  subdir_{i}/\n" for i in range(15))
    ),
    'read_files': (
        "File: lib/core/module.py (340 lines, 12.1KB)\n"
        + "─" * 40
        + "\n"
        + "".join(
            f"{i:3d} | # line {i}: do some work with value "
            f"{i * 37 % 1000}, trace={'x' * (i % 20)}\n"
            for i in range(1, 60)
        )
    ),
    'grep_search': (
        'grep "handle_request" across files — 15 matches:\n\n'
        + "".join(
            f"lib/routes/handler_{i:02d}.py:{10 + i * 4}:    def "
            f"handle_request(self, req):  # handler variant {i}\n"
            for i in range(15)
        )
        + "\n"
        + "Additional context lines:\n"
        + "".join(
            f"    {10 + i * 4 - 1}: # preceding logic\n"
            f"    {10 + i * 4 + 1}:     return process(req)\n"
            for i in range(15)
        )
    ),
}

TOOL_NAMES_PER_ROUND = ['list_dir', 'grep_search', 'read_files',
                        'grep_search', 'list_dir', 'read_files',
                        'grep_search', 'list_dir', 'read_files',
                        'grep_search']


# ═══════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class RoundResult:
    round_num: int
    arm: str
    msg_count: int = 0
    est_tokens_before_compact: int = 0
    est_tokens_after_compact: int = 0
    tokens_saved_by_compact: int = 0
    prompt_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    elapsed: float = 0.0
    status: str = ''
    error: str = ''


@dataclass
class ArmResult:
    label: str
    model: str
    rounds: list = field(default_factory=list)

    @property
    def valid(self):
        return [r for r in self.rounds if not r.error]

    def total(self, field_):
        return sum(getattr(r, field_) for r in self.valid)


# ═══════════════════════════════════════════════════════════════════════
#  Build realistic conversation
# ═══════════════════════════════════════════════════════════════════════

def _build_conversation_through_round(round_idx: int, arm_seed: str) -> list:
    """Build a conversation through `round_idx` rounds, ready to be
    sent as the (round_idx)-th API request.

    Structure:
        [system]
        [user: initial question]
        Round 0: assistant(interstitial + tool_call) → tool_result
                 assistant(interstitial + tool_call) → tool_result
        [user: follow-up]
        Round 1: ...
    """
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'I need to understand the error handling in this project. '
            'Please investigate and give me a brief summary.'
        )},
    ]

    tc_counter = 0
    for r in range(round_idx):
        # Two assistant → tool pairs per round to exercise multi-tool fan-out
        interstitial = INTERSTITIAL_COMMENTARIES[r % len(INTERSTITIAL_COMMENTARIES)]
        tool_name = TOOL_NAMES_PER_ROUND[r % len(TOOL_NAMES_PER_ROUND)]

        tc_id = f'call_{tc_counter:04d}'
        tc_counter += 1
        messages.append({
            'role': 'assistant',
            'content': interstitial,
            'tool_calls': [{
                'id': tc_id,
                'type': 'function',
                'function': {'name': tool_name, 'arguments': '{"path":"."}'},
            }],
        })
        messages.append({
            'role': 'tool',
            'tool_call_id': tc_id,
            'name': tool_name,
            'content': TOOL_RESULTS[tool_name],
        })

        # Follow-up user prompt
        if r < round_idx - 1:
            messages.append({
                'role': 'user',
                'content': 'Good, please continue investigating.',
            })

    return messages


def _estimate_tokens(messages: list) -> int:
    total = 0
    for msg in messages:
        for field_ in ('content', 'reasoning_content'):
            v = msg.get(field_)
            if isinstance(v, str):
                total += len(v)
            elif isinstance(v, list):
                for b in v:
                    if isinstance(b, dict) and b.get('type') == 'text':
                        total += len(b.get('text', ''))
        for tc in msg.get('tool_calls', []) or []:
            total += len(tc.get('function', {}).get('arguments', ''))
    return total // 4


# ═══════════════════════════════════════════════════════════════════════
#  Run one round
# ═══════════════════════════════════════════════════════════════════════

def _run_round(
    *,
    round_idx: int,
    num_rounds: int,
    arm_label: str,
    enable_paired: bool,
    model: str,
    arm_seed: str,
    dry_run: bool = False,
) -> RoundResult:
    from lib.tasks_pkg.compaction import micro_compact

    messages = _build_conversation_through_round(round_idx + 1, arm_seed)
    est_before = _estimate_tokens(messages)

    # Apply Phase B (and optionally B2) — this mutates `messages`.
    saved = micro_compact(
        messages,
        conv_id=f'ab_{arm_label}',
        enable_paired_assistant_compact=enable_paired,
    )
    est_after = _estimate_tokens(messages)

    rr = RoundResult(
        round_num=round_idx + 1,
        arm=arm_label,
        msg_count=len(messages),
        est_tokens_before_compact=est_before,
        est_tokens_after_compact=est_after,
        tokens_saved_by_compact=est_before - est_after,
    )

    print(f"    [R{round_idx+1}/{num_rounds}] {arm_label} msgs={len(messages)} "
          f"est={est_before}→{est_after} (saved ~{est_before - est_after})",
          flush=True)

    if dry_run:
        rr.prompt_tokens = max(1, est_after - 2000)
        rr.cache_read = max(0, est_after - 2000 - round_idx * 500)
        rr.cache_write = max(500, 1500 - round_idx * 100)
        rr.output_tokens = 150
        rr.status = 'SIM'
        return rr

    # ── Live API call via dispatcher (strict: don't rotate models) ────
    # ★ max_retries=1 + timeout=90: fail fast on 429 rather than burning
    #   100 cycles on exhausted keys. With key_1 explicitly disabled via
    #   set_key_override, the dispatcher only has key_0 to try, so a 429
    #   there means "wait for per-minute recovery" — we surface the error
    #   rather than looping.
    t0 = time.time()
    try:
        _content, usage = dispatch_chat(
            messages,
            max_tokens=256,
            temperature=1.0,
            thinking_enabled=False,
            tools=TOOLS,
            prefer_model=model,
            strict_model=True,
            max_retries=1,
            timeout=90,
            log_prefix=f'[{arm_label} R{round_idx+1}]',
        )
    except Exception as e:
        logger.warning('[AB] dispatch error R%d %s: %s',
                       round_idx + 1, arm_label, e)
        rr.error = str(e)[:200]
        return rr

    rr.elapsed = time.time() - t0
    u = usage or {}
    rr.cache_read = (u.get('cache_read_tokens')
                     or u.get('cache_read_input_tokens') or 0)
    rr.cache_write = (u.get('cache_creation_input_tokens')
                      or u.get('cache_write_tokens') or 0)
    rr.prompt_tokens = u.get('prompt_tokens', 0)
    rr.output_tokens = u.get('completion_tokens', 0)

    if rr.cache_write > 500 and rr.cache_read > 500:
        rr.status = 'HIT+WRITE'
    elif rr.cache_read > 500:
        rr.status = 'HIT'
    elif rr.cache_write > 500:
        rr.status = 'WRITE'
    else:
        rr.status = 'MISS'

    print(f"      ⏱ {rr.elapsed:.1f}s | {rr.status}  pt={rr.prompt_tokens:,}  "
          f"cr={rr.cache_read:,}  cw={rr.cache_write:,}  out={rr.output_tokens}",
          flush=True)
    return rr


# ═══════════════════════════════════════════════════════════════════════
#  Cost + reporting
# ═══════════════════════════════════════════════════════════════════════

def _pricing(model: str):
    m = model.lower()
    if 'opus' in m:
        return 15.0, 75.0
    if 'sonnet' in m:
        return 3.0, 15.0
    return 3.0, 15.0


def _cost(arm: ArmResult, *, cw_mul=1.25, cr_mul=0.10):
    ip, op = _pricing(arm.model)
    tp = arm.total('prompt_tokens')
    tr = arm.total('cache_read')
    tw = arm.total('cache_write')
    to = arm.total('output_tokens')
    ti = tp + tr + tw
    return {
        'total_prompt': tp, 'total_read': tr, 'total_write': tw,
        'total_output': to, 'total_input': ti,
        'cost_prompt': tp * ip / 1e6,
        'cost_read': tr * ip * cr_mul / 1e6,
        'cost_write': tw * ip * cw_mul / 1e6,
        'cost_output': to * op / 1e6,
        'total': ((tp + tr * cr_mul + tw * cw_mul) * ip + to * op) / 1e6,
    }


def _delta(a, b, lower_better=True):
    if a == 0:
        return 'N/A'
    pct = (b - a) / a * 100
    better = pct < 0 if lower_better else pct > 0
    sym = "✅" if better else ("⚠️" if abs(pct) > 3 else "➖")
    return f"{pct:+.1f}% {sym}"


def _print_report(arm_a: ArmResult, arm_b: ArmResult):
    print()
    print("  " + "▓" * 60)
    print("  PAIRED-COMPACT (PHASE B2) — A/B RESULTS")
    print("  " + "▓" * 60)

    for arm in (arm_a, arm_b):
        print(f"\n  ── {arm.label} ──")
        print(f"  {'Rnd':>3} │ {'Msgs':>4} │ {'EstSvd':>7} │ {'Prompt':>7} │ "
              f"{'CacheR':>7} │ {'CacheW':>7} │ {'Out':>5} │ {'Status':>10}")
        print(f"  {'─' * 3}─┼─{'─' * 4}─┼─{'─' * 7}─┼─{'─' * 7}─┼─"
              f"{'─' * 7}─┼─{'─' * 7}─┼─{'─' * 5}─┼─{'─' * 10}")
        for r in arm.rounds:
            if r.error:
                print(f"  {r.round_num:>3} │  ERR │ {r.error[:60]}")
                continue
            print(f"  {r.round_num:>3} │ {r.msg_count:>4} │ "
                  f"{r.tokens_saved_by_compact:>7,} │ {r.prompt_tokens:>7,} │ "
                  f"{r.cache_read:>7,} │ {r.cache_write:>7,} │ "
                  f"{r.output_tokens:>5} │ {r.status:>10}")

    ca = _cost(arm_a)
    cb = _cost(arm_b)

    print(f"\n  {'Metric':<32} │ {'BASELINE':>12} │ {'PAIRED':>12} │ {'Δ':>12}")
    print(f"  {'─' * 32}─┼─{'─' * 12}─┼─{'─' * 12}─┼─{'─' * 12}")
    for name, a, b, lb in [
        ("Est tokens saved by compact",
         arm_a.total('tokens_saved_by_compact'),
         arm_b.total('tokens_saved_by_compact'), False),
        ("Uncached prompt (tokens)", ca['total_prompt'], cb['total_prompt'], True),
        ("Cache reads (tokens)",     ca['total_read'],   cb['total_read'],   False),
        ("Cache writes (tokens)",    ca['total_write'],  cb['total_write'],  True),
        ("Output (tokens)",          ca['total_output'], cb['total_output'], True),
    ]:
        print(f"  {name:<32} │ {a:>12,} │ {b:>12,} │ {_delta(a, b, lb):>12}")
    print(f"  {'─' * 32}─┼─{'─' * 12}─┼─{'─' * 12}─┼─{'─' * 12}")
    print(f"  {'TOTAL COST':<32} │ ${ca['total']:>11.4f} │ "
          f"${cb['total']:>11.4f} │ {_delta(ca['total'], cb['total'], True):>12}")

    # Verdict
    diff = ca['total'] - cb['total']
    print()
    print(f"  HYPOTHESIS 1 — Phase B2 saves more tokens:", end=' ')
    if arm_b.total('tokens_saved_by_compact') > arm_a.total('tokens_saved_by_compact'):
        extra = arm_b.total('tokens_saved_by_compact') - arm_a.total('tokens_saved_by_compact')
        print(f"✅ YES ({extra:,} extra tokens saved)")
    else:
        print(f"❌ NO (no extra savings — maybe no cold pairs this run)")

    print(f"  HYPOTHESIS 2 — Phase B2 is cache-neutral:",  end=' ')
    dw = cb['total_write'] - ca['total_write']
    if dw <= max(500, ca['total_write'] * 0.05):
        print(f"✅ YES (cache writes Δ={dw:+,} within 5% noise band)")
    else:
        print(f"❌ NO (cache writes up {dw:+,} tokens — mutation broke cache)")

    if abs(diff) < 0.0005:
        verdict = "NEUTRAL"
    elif diff > 0:
        verdict = f"PAIRED saves ${diff:.4f} ({diff / max(ca['total'], 1e-5) * 100:.1f}%)"
    else:
        verdict = f"BASELINE is ${-diff:.4f} ({-diff / max(cb['total'], 1e-5) * 100:.1f}%) cheaper"
    print(f"\n  💰 {verdict}")


def _save(arm_a: ArmResult, arm_b: ArmResult, path: str):
    out = {
        'arm_a': {'label': arm_a.label, 'model': arm_a.model,
                  'rounds': [asdict(r) for r in arm_a.rounds]},
        'arm_b': {'label': arm_b.label, 'model': arm_b.model,
                  'rounds': [asdict(r) for r in arm_b.rounds]},
    }
    with open(path, 'w') as f:
        json.dump(out, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════

def _hash_msg(msg: dict) -> str:
    """Stable byte hash of ONE message's cache-relevant content."""
    import hashlib
    # Use sort_keys so dict order doesn't affect the hash — cache keys
    # in all mainstream providers are order-insensitive within a message.
    payload = json.dumps(msg, sort_keys=True, ensure_ascii=False,
                         default=str).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()[:16]


def _diff_mutations(before: list, after: list) -> list[int]:
    """Return indices where a message's content hash changed."""
    mutated = []
    for i, (b, a) in enumerate(zip(before, after)):
        if _hash_msg(b) != _hash_msg(a):
            mutated.append(i)
    return mutated


def run_local_analysis(num_rounds: int):
    """Byte-hash analysis — proves H2 (cache-neutrality) without API calls.

    For each round we:
      1. Build the conversation (through round `r`).
      2. Run BASELINE compaction (Phase B only); record per-index
         before/after byte hashes.
      3. Run PAIRED compaction (Phase B + B2) on a FRESH deep-copy;
         record per-index before/after byte hashes.
      4. Compare the two mutation sets.

    Cache-neutrality (H2) holds iff:
      (a) PAIRED_mutated ⊇ BASELINE_mutated  (we don't skip anything)
      (b) Every extra index in PAIRED is the paired assistant of a
          tool message ALREADY mutated by BASELINE.  Because Anthropic
          invalidates the cache starting at the FIRST mutation, a
          co-located mutation at an adjacent index cannot introduce
          an extra cache break.

    Metric to eyeball:
      min_mutation_idx_BASELINE  vs  min_mutation_idx_PAIRED
    If they're identical on every round, cache prefix reuse is identical.
    """
    from lib.tasks_pkg.compaction import micro_compact

    print()
    print("█" * 70)
    print("  PAIRED-COMPACT (PHASE B2) — LOCAL BYTE-HASH ANALYSIS")
    print(f"  Rounds: {num_rounds}")
    print(f"  Hypothesis H2: Phase B2 introduces NO extra cache break "
          f"beyond Phase B.")
    print("█" * 70)

    all_pass = True
    extra_tokens_total = 0
    for r in range(num_rounds):
        msgs = _build_conversation_through_round(r + 1, arm_seed='')
        baseline = copy.deepcopy(msgs)
        paired = copy.deepcopy(msgs)

        # Run BASELINE compaction
        before_b = copy.deepcopy(baseline)
        micro_compact(baseline, conv_id='local_baseline',
                      enable_paired_assistant_compact=False)
        mutated_b = _diff_mutations(before_b, baseline)

        # Run PAIRED compaction
        before_p = copy.deepcopy(paired)
        micro_compact(paired, conv_id='local_paired',
                      enable_paired_assistant_compact=True)
        mutated_p = _diff_mutations(before_p, paired)

        # H2 tests:
        #   (a) superset
        superset = set(mutated_b).issubset(set(mutated_p))
        #   (b) every extra index is adjacent to a BASELINE mutation
        extra = sorted(set(mutated_p) - set(mutated_b))
        adjacent = all(
            (idx - 1) in mutated_b or (idx + 1) in mutated_b
            for idx in extra
        )
        #   min mutation index — this is the "first break" from cache POV
        first_break_b = min(mutated_b) if mutated_b else None
        first_break_p = min(mutated_p) if mutated_p else None
        first_break_ok = (first_break_b == first_break_p)

        # Token counts
        est_baseline = _estimate_tokens(baseline)
        est_paired = _estimate_tokens(paired)
        extra_tokens = est_baseline - est_paired
        extra_tokens_total += extra_tokens

        round_pass = superset and adjacent and first_break_ok
        all_pass = all_pass and round_pass
        status = '✅' if round_pass else '❌'
        print(f"\n  R{r+1} {status}  msgs={len(msgs)}")
        print(f"    BASELINE mutated {len(mutated_b)} msgs at {mutated_b}")
        print(f"    PAIRED   mutated {len(mutated_p)} msgs at {mutated_p}")
        print(f"    Extra in PAIRED: {extra}  (all adjacent to B mutation: {adjacent})")
        print(f"    First break:  BASELINE@{first_break_b}  PAIRED@{first_break_p}  "
              f"{'SAME' if first_break_ok else 'DIFFERENT'}")
        print(f"    Tokens:  BASELINE={est_baseline}  PAIRED={est_paired}  "
              f"Δ={extra_tokens:+} (extra saved by B2)")

    print()
    print("  " + "─" * 66)
    if all_pass:
        print(f"  ✅ ALL ROUNDS PASS  — H2 (cache-neutrality) proven structurally.")
        print(f"     Phase B2 only mutates messages adjacent to Phase B mutations;")
        print(f"     first cache-break index is IDENTICAL in both arms every round.")
    else:
        print(f"  ❌ ONE OR MORE ROUNDS FAILED  — Phase B2 breaks cache-neutrality.")
    print(f"     Extra tokens saved by Phase B2 across {num_rounds} rounds: "
          f"{extra_tokens_total:,}")
    print("  " + "─" * 66)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)
    ap.add_argument('--interval', type=int, default=DEFAULT_INTERVAL,
                    help='Seconds between rounds to avoid rate-limit')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--local', action='store_true',
                    help='Run byte-hash analysis only, no API calls')
    ap.add_argument('--live', action='store_true',
                    help='Run live API calls (requires quota)')
    args = ap.parse_args()

    # Monkey-patch compaction params FIRST so both paths see the same config.
    import lib.tasks_pkg.compaction as _comp
    _orig_hot_tail = _comp.MICRO_HOT_TAIL
    _comp.MICRO_HOT_TAIL = TEST_HOT_TAIL
    _orig_thresh = _comp.MICRO_COMPACT_THRESHOLD
    _comp.MICRO_COMPACT_THRESHOLD = 500

    try:
        # Default: local analysis unless --live explicitly passed
        if args.local or not args.live:
            run_local_analysis(args.rounds)
            if not args.live:
                return
    finally:
        if not args.live:
            _comp.MICRO_HOT_TAIL = _orig_hot_tail
            _comp.MICRO_COMPACT_THRESHOLD = _orig_thresh

    # Monkey-patch MICRO_HOT_TAIL small so Phase B fires quickly.
    import lib.tasks_pkg.compaction as _comp
    _orig_hot_tail = _comp.MICRO_HOT_TAIL
    _comp.MICRO_HOT_TAIL = TEST_HOT_TAIL
    _orig_thresh = _comp.MICRO_COMPACT_THRESHOLD
    _comp.MICRO_COMPACT_THRESHOLD = 500  # lower threshold so our tool results get compacted

    # Also we need Phase B to actually mutate — normally it's cache-aware
    # and skips messages in the cache prefix.  In a test, cache_tracking
    # has no state so get_cache_prefix_count returns 0 → all indices
    # outside the hot tail are eligible.  Good.

    print()
    print("█" * 70)
    print("  PAIRED-COMPACT (PHASE B2) — A/B TEST")
    print(f"  Model: {args.model}")
    print(f"  Rounds: {args.rounds}  Interval: {args.interval}s  "
          f"Dry: {args.dry_run}  HOT_TAIL: {TEST_HOT_TAIL}")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("█" * 70)

    arm_a = ArmResult(label='BASELINE', model=args.model)
    arm_b = ArmResult(label='PAIRED', model=args.model)

    # Different seed per arm keeps cache independent between them
    seed_a = f'\n<!-- arm=A seed={time.time():.0f} -->'
    seed_b = f'\n<!-- arm=B seed={time.time() + 1:.0f} -->'

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    result_path = f'debug/paired_compact_ab_{timestamp}.json'

    try:
        # Interleave arms per round so they see similar API conditions
        for r in range(args.rounds):
            # ARM A
            rr_a = _run_round(
                round_idx=r, num_rounds=args.rounds,
                arm_label='BASELINE', enable_paired=False,
                model=args.model, arm_seed=seed_a, dry_run=args.dry_run,
            )
            arm_a.rounds.append(rr_a)
            if not args.dry_run:
                time.sleep(args.interval)

            # ARM B
            rr_b = _run_round(
                round_idx=r, num_rounds=args.rounds,
                arm_label='PAIRED', enable_paired=True,
                model=args.model, arm_seed=seed_b, dry_run=args.dry_run,
            )
            arm_b.rounds.append(rr_b)

            # Save intermediate after each round so ctrl-c / timeout
            # never loses data
            _save(arm_a, arm_b, result_path)

            if not args.dry_run and r < args.rounds - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n[aborted by user — partial results saved]', flush=True)
    finally:
        _comp.MICRO_HOT_TAIL = _orig_hot_tail
        _comp.MICRO_COMPACT_THRESHOLD = _orig_thresh

    _print_report(arm_a, arm_b)
    print(f"\n📁 Saved: {result_path}")
    print("█" * 70)


if __name__ == '__main__':
    main()
