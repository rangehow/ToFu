#!/usr/bin/env python3
"""Re-run ONE SWE-bench instance with FULL conversation capture for analysis.

Bypasses the 50KB raw_output cap by polling the task and saving all rounds
including their full message content & tool args/results.

Usage:
    python3 debug/swebench_capture_conversation.py \
        --instance django__django-13112 \
        --tool tofu-minimax \
        --output /tmp/django-13112-tofu-minimax-trace.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debug.swebench_runner import (
    MODEL_PRESETS, build_agent_prompt, get_repo_path, load_swebench_instances,
    setup_workspace,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--instance', required=True)
    ap.add_argument('--tool', required=True, choices=list(MODEL_PRESETS))
    ap.add_argument('--server', default=os.environ.get('TOFU_BASE_URL', 'http://127.0.0.1:15001'))
    ap.add_argument('--workdir', default=str(
        Path(__file__).resolve().parent.parent.parent / 'chatui_swebench2/swebench_rerun_workdir'))
    ap.add_argument('--output', required=True)
    ap.add_argument('--max-poll-s', type=int, default=900)
    args = ap.parse_args()

    import requests

    instances = load_swebench_instances(load_all=True)
    inst = next((i for i in instances if i.instance_id == args.instance), None)
    if not inst:
        sys.exit(f'Instance {args.instance} not found')

    workdir = Path(args.workdir).resolve()
    cfg = MODEL_PRESETS[args.tool]
    print(f'instance: {inst.instance_id}  tool: {args.tool}  model: {cfg.model_id}')

    # Set up workspace
    ws = setup_workspace(inst, args.tool + '__capture', workdir)
    print(f'workspace: {ws}')

    prompt = build_agent_prompt(inst)

    # Submit task
    body = {
        'messages': [{'role': 'user', 'content': prompt}],
        'config': {
            'model': cfg.model_id,
            'projectEnabled': True,
            'projectPath': str(ws),
            **(cfg.config_overrides or {}),
        },
    }
    r = requests.post(f'{args.server}/api/chat/start',
                      json=body, timeout=30)
    r.raise_for_status()
    task_id = r.json()['taskId']
    print(f'task_id: {task_id}')

    # Poll until done
    t0 = time.time()
    last_round = -1
    while True:
        if time.time() - t0 > args.max_poll_s:
            print(f'  TIMEOUT after {args.max_poll_s}s, aborting')
            requests.post(f'{args.server}/api/chat/abort/{task_id}', timeout=5)
            break
        rr = requests.get(f'{args.server}/api/chat/poll/{task_id}', timeout=15)
        if rr.status_code != 200:
            time.sleep(2)
            continue
        data = rr.json()
        status = data.get('status')
        rounds = data.get('apiRounds') or []
        if len(rounds) > last_round:
            last_round = len(rounds)
            print(f'  [+{int(time.time()-t0):4d}s] status={status} rounds={len(rounds)}', flush=True)
        if status in ('done', 'error'):
            break
        time.sleep(2)

    # Final fetch
    rr = requests.get(f'{args.server}/api/chat/poll/{task_id}', timeout=30)
    final = rr.json()
    final['__instance_id'] = inst.instance_id
    final['__tool'] = args.tool
    final['__workspace'] = str(ws)

    # Get the final patch
    import subprocess
    patch_proc = subprocess.run(['git', 'diff'],
                                 capture_output=True, text=True,
                                 cwd=str(ws), timeout=60)
    final['__patch'] = patch_proc.stdout
    print(f'\nfinal status: {final.get("status")}  rounds: {len(final.get("apiRounds") or [])}')
    print(f'patch length: {len(final["__patch"])}')

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(final, indent=2, default=str))
    print(f'\nSaved full trace to {out_path}')


if __name__ == '__main__':
    main()
