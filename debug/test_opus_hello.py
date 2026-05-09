"""Direct test: send "hello" to aws.claude-opus-4.7 and aws.claude-opus-4.6

Uses the exact same request path the app uses — the sankuai gateway → AWS Bedrock.
"""
import json
import os
import sys
import time

# Ensure project is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib
from lib.llm_dispatch.api import dispatch_chat

# Only test opus-4.7 (it's your default) and sonnet as control
MODELS_TO_TEST = [
    "aws.claude-opus-4.7",
    "aws.claude-sonnet-4.6",
]

# Progressive prompts — from safe to potentially triggering
PROMPTS = [
    # Test 1: Simple hello (confirmed working)
    ("simple_hello", [{"role": "user", "content": "hello"}]),
    # Test 2: Minimal coding question
    ("minimal_code", [{"role": "user", "content": "Write a Python function that adds two numbers."}]),
    # Test 3: Brief SWE-like instruction
    ("brief_swe", [{"role": "user", "content": "You are solving a GitHub issue. Fix the bug in this code: `def add(a, b): return a - b`"}]),
    # Test 4: Tool-calling with no tools (tests tool_use fallback)
    ("tool_prompt", [{"role": "user", "content": "Read the file /tmp/test.txt"}]),
    # Test 5: System prompt + user (mimicking SWE-bench task structure)
    ("system_prompt", [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Fix this bug: def multiply(a, b): return a + b"},
    ]),
    # Test 6: Long prompt with issue context (mini SWE-bench)
    ("mini_swebench", [
        {"role": "system", "content": "You are solving a GitHub issue. You must fix the code in the repository."},
        {"role": "user", "content": "The function divide() has a bug where it returns None instead of raising an error. Fix it. The file is at src/math.py. Write the complete fixed code."},
    ]),
]

for model in MODELS_TO_TEST:
    for label, messages in PROMPTS:
        tag = f"[{model}][{label}]"
        preview = messages[-1]['content'][:90].replace('\n', ' ')
        print(f"\n{'='*70}")
        print(f"{tag}")
        print(f"  msg: {preview}")
        t0 = time.time()
        try:
            content, usage = dispatch_chat(
                messages,
                prefer_model=model,
                temperature=1.0,
                thinking_enabled=False,
                preset='medium',
                max_tokens=256,
                timeout=60,
            )
            elapsed = time.time() - t0
            if not content or not content.strip():
                print(f"  ⚠️ EMPTY ({elapsed:.1f}s)")
                u = f"in={usage.get('input_tokens','?')} out={usage.get('output_tokens','?')}" if usage else "None"
                print(f"  usage: {u}")
            else:
                print(f"  ✓ OK ({elapsed:.1f}s, {len(content)} chars)")
                print(f"  → {content[:150]}")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  ✗ FAILED ({elapsed:.1f}s) {type(e).__name__}")
            print(f"  → {str(e)[:300]}")
