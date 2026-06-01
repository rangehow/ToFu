#!/usr/bin/env python3
"""A/B Benchmark: Structured Tool Calling vs. Single run_command (CLI-only mode).

Inspired by the Manus backend lead's blog post arguing for collapsing all tools
into a single `run(command="…")` CLI tool with Unix pipe composition.

This benchmark tests BOTH modes on the same set of project-understanding tasks
and measures:
  - rounds: number of LLM round-trips (fewer = better composition)
  - input_tokens / output_tokens: context cost
  - latency_s: end-to-end wall time
  - accuracy: manual or LLM-as-judge scoring (1-5)
  - error_count: tool call failures

Usage:
    python benchmarks/tool_mode_ab_test.py [--tasks all|quick] [--model MODEL] [--runs N]

Requires the server to be running (uses internal APIs directly, no HTTP).
"""

import sys, os, json, time, copy, argparse, logging
from pathlib import Path
from datetime import datetime

# ── Setup path ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.llm import build_body, stream_chat
from lib.tools.project import PROJECT_TOOLS, PROJECT_TOOL_NAMES, READ_FILES_TOOL
from lib.tools.code_exec import CODE_EXEC_TOOL

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════
#  Task Definitions
# ═════════════════════════════════════════════════════════

TASKS = [
    # ── Category 1: Information Retrieval (grep → read pattern) ──
    {
        "id": "find_imports",
        "category": "retrieval",
        "prompt": "找出项目中所有导入了 `iter_completions` 的 Python 文件，列出文件路径和具体的 import 行。",
        "expected_keywords": ["orchestrator.py", "iter_completions"],  # for auto-eval
        "difficulty": "easy",
    },
    {
        "id": "count_tools",
        "category": "retrieval",
        "prompt": "统计当前项目中定义了多少个不同的 tool（function calling schema），列出每个 tool 的名称。",
        "expected_keywords": ["list_dir", "read_files", "grep_search", "run_command", "web_search"],
        "difficulty": "medium",
    },
    {
        "id": "architecture_overview",
        "category": "comprehension",
        "prompt": "解释 lib/tasks_pkg/ 目录下 orchestrator.py、executor.py、tool_dispatch.py 这三个文件的职责分工和调用关系。请阅读代码后回答，不要猜测。",
        "expected_keywords": ["orchestrator", "executor", "tool_dispatch", "parse_tool_calls"],
        "difficulty": "medium",
    },
    # ── Category 2: Multi-step Composition ──
    {
        "id": "loc_by_language",
        "category": "composition",
        "prompt": "统计项目中 Python 文件(.py)和 JavaScript 文件(.js)各有多少个文件、总共多少行代码。给出精确数字。",
        "expected_keywords": [],  # numeric, hard to keyword-match
        "difficulty": "easy_for_cli",  # one `find | wc` vs multiple tool calls
    },
    {
        "id": "find_largest_file",
        "category": "composition",
        "prompt": "找出项目中最大的 3 个 Python 文件（按行数），报告文件名和行数。",
        "expected_keywords": [],
        "difficulty": "easy_for_cli",
    },
    {
        "id": "dead_import_check",
        "category": "composition",
        "prompt": "检查 lib/tasks_pkg/orchestrator.py 中是否有未使用的 import（导入了但代码中没有引用的名字）。列出所有未使用的 import。",
        "expected_keywords": [],
        "difficulty": "medium",
    },
    # ── Category 3: Precision Editing ──
    {
        "id": "precise_read",
        "category": "precision",
        "prompt": "读取 lib/tasks_pkg/orchestrator.py 的第 1-10 行和第 300-310 行，以及 lib/tasks_pkg/executor.py 的第 1-10 行。同时返回这三段内容。",
        "expected_keywords": [],
        "difficulty": "easy_for_structured",  # read_files batching shines
    },
    {
        "id": "multi_grep",
        "category": "precision",
        "prompt": "在整个 lib/ 目录中搜索所有包含 'HOT_PATH' 注释的文件，并列出每个文件中 HOT_PATH 出现的行号。",
        "expected_keywords": ["HOT_PATH"],
        "difficulty": "easy",
    },
]

QUICK_TASKS = ["find_imports", "loc_by_language", "precise_read"]
MID_TASKS = ["find_largest_file", "dead_import_check", "multi_grep", "architecture_overview", "count_tools"]

# ═════════════════════════════════════════════════════════
#  Mode Definitions
# ═════════════════════════════════════════════════════════

# Mode A: Full structured tools (current architecture)
MODE_A_SYSTEM = (
    "You are a project co-pilot. You have structured tools to explore and modify code. "
    "Use them to answer the user's question accurately. The project root is: {project_root}\n"
    "Available tools will be provided via function calling."
)

# Mode B: Single run_command (blog author's approach)
MODE_B_SYSTEM = (
    "You are a project co-pilot. You have ONE tool: `run_command` which executes any "
    "shell command in the project directory and returns stdout+stderr.\n\n"
    "The project root is: {project_root}\n\n"
    "You can use ANY Unix command: cat, grep, find, wc, head, tail, sed, awk, sort, etc.\n"
    "Compose them with pipes (|), && , || for efficiency.\n"
    "Examples:\n"
    "  - Find files: find . -name '*.py' | head -20\n"
    "  - Search code: grep -rn 'pattern' lib/ --include='*.py'\n"
    "  - Read file range: sed -n '10,30p' lib/foo.py\n"
    "  - Count lines: find . -name '*.py' | xargs wc -l | sort -n | tail -10\n"
    "  - Multi-command: grep -rn 'import X' lib/ && echo '---' && wc -l lib/X.py\n\n"
    "Be efficient: combine multiple operations into one command when possible.\n"
    "Always check stderr for errors and correct your approach if needed."
)


def get_mode_a_tools():
    """Full structured project tools (incl. read_files which is global)."""
    return copy.deepcopy([READ_FILES_TOOL] + list(PROJECT_TOOLS))


def get_mode_b_tools():
    """Single run_command tool only."""
    return [copy.deepcopy(CODE_EXEC_TOOL)]


# ═════════════════════════════════════════════════════════
#  Execution Engine
# ═════════════════════════════════════════════════════════

def execute_tool_call(fn_name, fn_args_str, project_root):
    """Execute a single tool call and return the result string."""
    try:
        fn_args = json.loads(fn_args_str) if fn_args_str else {}
    except json.JSONDecodeError:
        return f"❌ Invalid JSON arguments: {fn_args_str[:200]}"

    from lib.project_mod.tools import execute_tool
    try:
        result = execute_tool(fn_name, fn_args, project_root)
        if result is None:
            result = "(no output)"
        return str(result)
    except Exception as e:
        return f"❌ Tool error: {e}"


def run_single_task(task, mode, model, project_root, max_rounds=20):
    """Run one task in one mode and collect metrics.

    Returns dict with: mode, task_id, rounds, input_tokens, output_tokens,
                       latency_s, error_count, final_answer, tool_calls_log
    """
    if mode == 'A':
        system_msg = MODE_A_SYSTEM.format(project_root=project_root)
        tools = get_mode_a_tools()
    else:
        system_msg = MODE_B_SYSTEM.format(project_root=project_root)
        tools = get_mode_b_tools()

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": task["prompt"]},
    ]

    metrics = {
        "mode": mode,
        "task_id": task["id"],
        "category": task["category"],
        "difficulty": task.get("difficulty", ""),
        "rounds": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "latency_s": 0.0,
        "error_count": 0,
        "tool_calls_log": [],
        "final_answer": "",
    }

    t0 = time.time()

    for round_num in range(max_rounds):
        metrics["rounds"] = round_num + 1

        # ── Build request ──
        body = build_body(
            model, messages,
            max_tokens=16000,
            temperature=0.3,
            tools=tools,
        )

        # ── Call LLM via stream_chat (with retry on 429) ──
        assistant_msg = finish_reason = usage = None
        for _attempt in range(5):
            try:
                assistant_msg, finish_reason, usage = stream_chat(
                    body, log_prefix=f'[bench-{mode}]'
                )
                break
            except Exception as e:
                err_str = str(e)
                if '429' in err_str:
                    wait = 15 * (_attempt + 1)
                    logger.warning("Rate limited (429), waiting %ds before retry %d/5…", wait, _attempt + 2)
                    time.sleep(wait)
                    continue
                logger.error("LLM call failed in round %d: %s", round_num, e, exc_info=True)
                metrics["error_count"] += 1
                break
        else:
            logger.error("Exhausted 5 retries on 429 in round %d", round_num)
            metrics["error_count"] += 1
            break
        if assistant_msg is None:
            break

        if usage:
            metrics["input_tokens"] += usage.get('prompt_tokens', 0)
            metrics["output_tokens"] += usage.get('completion_tokens', 0)
            metrics["thinking_tokens"] += usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)

        content = assistant_msg.get('content', '') or ''
        tool_calls = assistant_msg.get('tool_calls', []) or []

        # ── Append assistant message to conversation ──
        messages.append(assistant_msg)

        # ── If no tool calls, we're done ──
        if not tool_calls or finish_reason == 'stop':
            metrics["final_answer"] = content
            break

        # ── Execute tool calls ──
        for tc in tool_calls:
            fn_name = tc.get('function', {}).get('name', '')
            fn_args_str = tc.get('function', {}).get('arguments', '')
            tc_id = tc.get('id', f'call_{round_num}')

            t_tool_start = time.time()
            result = execute_tool_call(fn_name, fn_args_str, project_root)
            t_tool_end = time.time()

            is_error = result.startswith('❌')
            if is_error:
                metrics["error_count"] += 1

            metrics["tool_calls_log"].append({
                "round": round_num,
                "fn_name": fn_name,
                "fn_args": fn_args_str[:500],
                "result_len": len(result),
                "result_preview": result[:300],
                "error": is_error,
                "exec_time_s": round(t_tool_end - t_tool_start, 3),
            })

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result,
            })

        logger.info("  [%s] Round %d: %d tool calls, content=%d chars",
                     mode, round_num + 1, len(tool_calls), len(content))

    metrics["latency_s"] = round(time.time() - t0, 2)

    # If we exhausted rounds without a final answer
    if not metrics["final_answer"] and messages:
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                metrics["final_answer"] = msg["content"]
                break

    return metrics


# ═════════════════════════════════════════════════════════
#  LLM-as-Judge Evaluation
# ═════════════════════════════════════════════════════════

JUDGE_SYSTEM = (
    "You are an expert evaluator. You will be given a coding task prompt and two "
    "answers (Answer A and Answer B). Rate each answer on accuracy (1-5) and "
    "completeness (1-5). Respond ONLY with JSON:\n"
    '{"a_accuracy": N, "a_completeness": N, "b_accuracy": N, "b_completeness": N, '
    '"winner": "A"|"B"|"tie", "reasoning": "brief explanation"}'
)


def judge_answers(task, answer_a, answer_b, model):
    """Use LLM-as-judge to compare two answers."""
    judge_prompt = (
        f"## Task\n{task['prompt']}\n\n"
        f"## Answer A (Structured Tools)\n{answer_a[:3000]}\n\n"
        f"## Answer B (CLI-only)\n{answer_b[:3000]}\n\n"
        f"Rate both answers. Expected keywords to check: {task.get('expected_keywords', [])}"
    )

    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": judge_prompt},
    ]

    body = build_body(model, messages, max_tokens=1000, temperature=0.1)
    assistant_msg, _fr, _usage = stream_chat(body, log_prefix='[judge]')
    raw = (assistant_msg.get('content', '') or '').strip()
    # Try to extract JSON
    try:
        # Handle markdown code blocks
        if '```' in raw:
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception:
        logger.warning("Judge response not valid JSON: %s", raw[:200])
        return {"raw_response": raw, "winner": "unknown"}


# ═════════════════════════════════════════════════════════
#  Main Runner
# ═════════════════════════════════════════════════════════

def print_comparison_table(results_a, results_b, judgments):
    """Pretty-print the comparison table."""
    print("\n" + "=" * 100)
    print("  A/B BENCHMARK RESULTS: Structured Tools (A) vs. CLI-only (B)")
    print("=" * 100)

    header = f"{'Task':<25} {'Mode':>5} {'Rounds':>7} {'InTok':>8} {'OutTok':>8} {'ThinkTok':>9} {'Errors':>7} {'Time(s)':>8}"
    print(header)
    print("-" * 100)

    for ra, rb in zip(results_a, results_b):
        tid = ra['task_id']
        print(f"{tid:<25} {'A':>5} {ra['rounds']:>7} {ra['input_tokens']:>8} {ra['output_tokens']:>8} {ra['thinking_tokens']:>9} {ra['error_count']:>7} {ra['latency_s']:>8.1f}")
        print(f"{'':25} {'B':>5} {rb['rounds']:>7} {rb['input_tokens']:>8} {rb['output_tokens']:>8} {rb['thinking_tokens']:>9} {rb['error_count']:>7} {rb['latency_s']:>8.1f}")

        # Winner
        j = judgments.get(tid, {})
        winner = j.get('winner', '?')
        reasoning = j.get('reasoning', '')[:60]
        print(f"{'':25} {'→':>5} Winner: {winner}  {reasoning}")
        print()

    # ── Summary ──
    print("=" * 100)
    sum_a = {k: sum(r[k] for r in results_a) for k in ['rounds', 'input_tokens', 'output_tokens', 'thinking_tokens', 'error_count']}
    sum_b = {k: sum(r[k] for r in results_b) for k in ['rounds', 'input_tokens', 'output_tokens', 'thinking_tokens', 'error_count']}
    sum_a['latency_s'] = sum(r['latency_s'] for r in results_a)
    sum_b['latency_s'] = sum(r['latency_s'] for r in results_b)

    print(f"{'TOTAL':<25} {'A':>5} {sum_a['rounds']:>7} {sum_a['input_tokens']:>8} {sum_a['output_tokens']:>8} {sum_a['thinking_tokens']:>9} {sum_a['error_count']:>7} {sum_a['latency_s']:>8.1f}")
    print(f"{'':25} {'B':>5} {sum_b['rounds']:>7} {sum_b['input_tokens']:>8} {sum_b['output_tokens']:>8} {sum_b['thinking_tokens']:>9} {sum_b['error_count']:>7} {sum_b['latency_s']:>8.1f}")

    # Token savings / cost
    token_diff = sum_a['input_tokens'] - sum_b['input_tokens']
    round_diff = sum_a['rounds'] - sum_b['rounds']
    print(f"\n  → Input token diff (A-B): {token_diff:+d}  ({'A cheaper' if token_diff < 0 else 'B cheaper'})")
    print(f"  → Round diff (A-B):       {round_diff:+d}  ({'A fewer' if round_diff < 0 else 'B fewer'})")

    wins = {'A': 0, 'B': 0, 'tie': 0, 'unknown': 0}
    for j in judgments.values():
        w = j.get('winner', 'unknown').upper()
        if w in wins:
            wins[w] += 1
        else:
            wins['unknown'] += 1
    print(f"  → Quality wins:           A={wins['A']}, B={wins['B']}, tie={wins.get('TIE', 0) + wins.get('tie', 0)}")
    print("=" * 100)


def main():
    parser = argparse.ArgumentParser(description='A/B test: structured tools vs CLI-only')
    parser.add_argument('--tasks', default='quick', choices=['all', 'quick', 'mid'],
                        help='Task set: "quick" (3 tasks) or "all" (8 tasks)')
    parser.add_argument('--model', default=None,
                        help='Model to use (default: from env LLM_MODEL or aws.claude-sonnet-4.6)')
    parser.add_argument('--judge-model', default=None,
                        help='Model for LLM-as-judge (default: same as --model)')
    parser.add_argument('--project-root', default=str(PROJECT_ROOT),
                        help='Project root path')
    parser.add_argument('--output', default=None,
                        help='Output JSON path (default: benchmarks/results_<timestamp>.json)')
    args = parser.parse_args()

    model = args.model or os.environ.get('LLM_MODEL', 'aws.claude-sonnet-4.6')
    judge_model = args.judge_model or model
    project_root = args.project_root

    if args.tasks == 'quick':
        tasks = [t for t in TASKS if t['id'] in QUICK_TASKS]
    elif args.tasks == 'mid':
        tasks = [t for t in TASKS if t['id'] in MID_TASKS]
    else:
        tasks = TASKS

    print(f"\n🧪 A/B Benchmark: {len(tasks)} tasks, model={model}")
    print(f"   Project root: {project_root}")
    print(f"   Mode A: Structured tools ({len(get_mode_a_tools())} tools)")
    print(f"   Mode B: CLI-only ({len(get_mode_b_tools())} tool)\n")

    results_a = []
    results_b = []

    for i, task in enumerate(tasks):
        print(f"\n{'─'*60}")
        print(f"  Task {i+1}/{len(tasks)}: [{task['id']}] {task['prompt'][:60]}…")
        print(f"{'─'*60}")

        # Run Mode A (structured)
        print(f"  ▶ Running Mode A (structured tools)…")
        ra = run_single_task(task, 'A', model, project_root)
        results_a.append(ra)
        print(f"    → {ra['rounds']} rounds, {ra['input_tokens']} in_tok, {ra['output_tokens']} out_tok, {ra['latency_s']:.1f}s, {ra['error_count']} errors")

        # Cool down between modes to avoid 429
        time.sleep(10)

        # Run Mode B (CLI-only)
        print(f"  ▶ Running Mode B (CLI-only)…")
        rb = run_single_task(task, 'B', model, project_root)
        results_b.append(rb)
        print(f"    → {rb['rounds']} rounds, {rb['input_tokens']} in_tok, {rb['output_tokens']} out_tok, {rb['latency_s']:.1f}s, {rb['error_count']} errors")

    # ── LLM-as-Judge ──
    print(f"\n⚖️  Running LLM-as-Judge evaluation…")
    judgments = {}
    for ra, rb in zip(results_a, results_b):
        task = next(t for t in tasks if t['id'] == ra['task_id'])
        print(f"  Judging [{task['id']}]…")
        try:
            j = judge_answers(task, ra['final_answer'] or "(no answer)", rb['final_answer'] or "(no answer)", judge_model)
        except Exception as e:
            logger.error("Judge failed for %s: %s", task['id'], e, exc_info=True)
            j = {'winner': 'unknown', 'reasoning': str(e)[:100]}
        judgments[task['id']] = j
        print(f"    → Winner: {j.get('winner', '?')}")

    # ── Print results ──
    print_comparison_table(results_a, results_b, judgments)

    # ── Save raw results ──
    output_path = args.output or f"benchmarks/results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump({
            "model": model,
            "timestamp": datetime.now().isoformat(),
            "tasks": [t['id'] for t in tasks],
            "results_a": results_a,
            "results_b": results_b,
            "judgments": judgments,
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n📁 Raw results saved to: {output_path}")


if __name__ == '__main__':
    main()
