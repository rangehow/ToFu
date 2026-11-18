"""CLI: ``python -m evaluations.terminal_bench run|summarize|invalidate``.

``run`` freezes its configuration in ``<work-dir>/run.json`` on first use; a
rerun with the same work dir resumes from that file (only ``--concurrency``,
``--max-attempts``, ``--stop-file`` and ``--relay-key-file`` may change; ``--finish-by``,
``--start-deadline`` and ``--start-allowance-sec`` apply to one launch and are never frozen) and never
recomputes measured trials.
New runs carry the benchmark-source guard (``source_guard`` true in
``run.json``); a run frozen before it existed resumes without it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from evaluations.terminal_bench import relay_ingress, third_party
from evaluations.terminal_bench.harness import (ANTHROPIC_RUN_MODELS, MATRIX_MODELS, PROTOCOLS,
                                                TOFU_THINKING_DEPTHS, arm_face, list_tasks, load_artifacts,
                                                require_system_ca_bundle)

# Ingress branch carrying the native exec channel (nlp/harbor b35af6f). A
# deployment declares it; with none declared, a new run needs ``run --branch``.
INGRESS_BRANCH = os.environ.get("TOFU_TB_INGRESS_BRANCH", "").strip()
# Context window both arms are told: the value the 2026-11-01/02 Tofu runs routed
# with (Harbor TofuKernel envelope), so both harnesses share one model fact.
GLM53_CONTEXT_TOKENS = 262_144
# Context windows per run model, from the production model-routing offering the
# run replicates. Opus 5.5's offering and Pi's built-in catalog both say 1 M.
# Kimi K3's window is the catalog's (lib/model_info/_context.py), which Tofu routes with.
RUN_MODEL_CONTEXT_TOKENS = {"claude-opus-5.5": 1_000_000, "kimi-k3": 1_000_000}
TB21_TASK_CHECKSUMS = Path(__file__).resolve().parents[1] / "terminal_bench_21_task_checksums.json"


def verify_task_identity(tasks_dir: Path, names: list[str], pins_path: Path = TB21_TASK_CHECKSUMS) -> dict:
    """Harbor ``Task.checksum`` (dirhash sha256 of the task directory) against the pinned checksums.

    The default pins are TB2.1; another task set (e.g. FrontierHarness Eval v1)
    passes its own pins file in the same ``{dataset_commit, task_checksums}`` shape.
    """
    from dirhash import dirhash

    pins = json.loads(Path(pins_path).read_text(encoding="utf-8"))
    mismatched = [name for name in names
                  if dirhash(tasks_dir / name, "sha256") != pins["task_checksums"].get(name)]
    return {"dataset_commit": pins["dataset_commit"], "checked": len(names), "mismatched": mismatched}


def local_epoch(value: str) -> float:
    """``YYYY-MM-DDTHH:MM:SS`` in the host's local time as an epoch."""
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%S"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DDTHH:MM:SS local time, got {value!r}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evaluations.terminal_bench")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run or resume a native Terminal-Bench evaluation")
    run.add_argument("--work-dir", type=Path, required=True)
    run.add_argument("--concurrency", type=int, required=True, help="simultaneous sandbox sessions")
    run.add_argument("--max-attempts", type=int, default=3, help="attempts per trial for unmeasured outcomes")
    run.add_argument("--tasks-dir", type=Path)
    run.add_argument("--tasks", help="comma-separated task names (default: every task in --tasks-dir)")
    run.add_argument("--arms", default="tofu,opencode",
                     help="comma-separated arms: tofu, opencode, pi, claude-code, codex, "
                          + ", ".join(third_party.ARMS))
    run.add_argument("--task-checksums", type=Path, default=TB21_TASK_CHECKSUMS,
                     help="task identity pins {dataset_commit, task_checksums} (default: TB2.1)")
    run.add_argument("--egress-allowlist", type=Path,
                     help="JSON list of hosts (*.suffix wildcards) the trial egress proxy allows, "
                          "for agent and verifier alike; frozen in run.json")
    run.add_argument("--no-source-guard", action="store_true",
                     help="omit the benchmark-source guard projections (an allowlist run's policy is the allowlist)")
    run.add_argument("--protocol", choices=sorted(PROTOCOLS), default="vendor-6h")
    run.add_argument("--model", default="glm-5.3-flash", help="wire model name the agents send")
    run.add_argument("--relay-url", help="model relay base URL reachable from sandbox containers")
    run.add_argument("--anthropic-url",
                     help="Anthropic bridge base URL reachable from sandbox containers (claude-code arm)")
    run.add_argument("--files-url", help="artifact base URL (default: <relay-url>/files)")
    run.add_argument("--artifacts-manifest", type=Path)
    run.add_argument("--endpoint", help="sandboxGateway endpoint .../system/<cluster>")
    run.add_argument("--image", help="sandbox VM image, e.g. rootfs://harbor-pipeline-dind-v5")
    run.add_argument("--branch", default=INGRESS_BRANCH or None,
                     help="ingress branch with the native exec channel (default: TOFU_TB_INGRESS_BRANCH); "
                          "a new run needs one, a resume keeps run.json's")
    run.add_argument("--proxy", help="upstream egress proxy for the VM and the trial egress filter")
    run.add_argument("--max-alive-min", type=int, default=540)
    run.add_argument("--context-tokens", type=int,
                     help="context window every arm is told (default: the run model's production offering)")
    run.add_argument("--poll-interval-sec", type=int, default=30)
    run.add_argument("--run-label", help="relay accounting label (default: work dir name)")
    run.add_argument("--stop-file", type=Path,
                     help="while this file exists no new attempt starts (the relay's --stop-file: first 429)")
    run.add_argument("--tofu-thinking", choices=TOFU_THINKING_DEPTHS,
                     help="the Tofu arm's --thinking instead of the run model's effort (other arms unchanged); "
                          "frozen in run.json")
    run.add_argument("--finish-by", type=local_epoch,
                     help="YYYY-MM-DDTHH:MM:SS local: start only attempts whose maximum wall time (start + submit "
                          "timeouts + trial budget) ends by then; this launch only, never frozen")
    run.add_argument("--start-deadline",
                     help="ISO 8601 time, e.g. 2026-11-13T21:55:00+08:00: no attempt starts whose sandbox start "
                          "allowance plus trial budget would end after it (it is held for a later run); this "
                          "invocation only, never frozen in run.json")
    run.add_argument("--start-allowance-sec", type=float,
                     help="with --start-deadline: seconds allowed before the agent starts (e.g. an observed p95 of "
                          "session start to agent start), replacing the gateway start budget plus the runner's fixed "
                          "trial overhead; the attempt must still fit its agent, grace and verifier budgets")
    run.add_argument("--relay-key-file", type=Path,
                     help="the run's relay key, created at mode 0600 when absent (the relay's --ingress-key-file): "
                          "agents get it in place of their API-key placeholder and artifact downloads send it; "
                          "run.json keeps only the path, and a resume may name another file")
    relay_key = commands.add_parser("relay-key", help="create a run's relay key file (mode 0600) unless it exists")
    relay_key.add_argument("path", type=Path)
    summarize = commands.add_parser("summarize", help="rewrite summary.json from trial states")
    summarize.add_argument("--work-dir", type=Path, required=True)
    invalidate = commands.add_parser(
        "invalidate", help="record that a finished attempt measured an outage, not the harness (it is re-run)")
    invalidate.add_argument("--work-dir", type=Path, required=True)
    invalidate.add_argument("--arm", required=True)
    invalidate.add_argument("--task", required=True)
    invalidate.add_argument("--attempt", type=int, required=True)
    invalidate.add_argument("--rule", required=True, help="the rule applied (e.g. a preregistered deviation)")
    invalidate.add_argument("--evidence", action="append", required=True, help="one evidence line; repeat")
    invalidate.add_argument("--decided-by", required=True)
    return parser


def frozen_config(args) -> dict:
    run_file = args.work_dir / "run.json"
    if run_file.is_file():
        config = json.loads(run_file.read_text(encoding="utf-8"))
        # A resume never switches relays: every new attempt keeps the frozen URL, so silently ignoring a different
        # --relay-url would send it to the old relay (possibly a stopped one) and end it unmeasured.
        if args.relay_url and args.relay_url.rstrip("/") != config["relay_url"]:
            raise SystemExit(f"{run_file} is frozen on relay {config['relay_url']}; a resume cannot switch it to "
                             f"{args.relay_url.rstrip('/')}. Edit relay_url and files_url in run.json (and record "
                             "why), or start a new run.")
        config["max_attempts"] = args.max_attempts
        if args.stop_file:
            config["stop_file"] = str(args.stop_file.resolve())
        config["finish_by"] = args.finish_by
        if args.relay_key_file:
            config["relay_key_file"] = str(args.relay_key_file.resolve())
        return config
    if not args.tasks_dir or not args.relay_url or not args.artifacts_manifest or not args.endpoint \
            or not args.image or not args.proxy or not args.branch:
        raise SystemExit("a new run needs --tasks-dir --relay-url --artifacts-manifest --endpoint --image --proxy "
                         "and --branch (or TOFU_TB_INGRESS_BRANCH)")
    if PROTOCOLS[args.protocol].per_model:
        bridged = arm_face("claude-code", args.model) == "bridge"
    else:
        bridged = args.model not in ANTHROPIC_RUN_MODELS
    if "claude-code" in args.arms.split(",") and bridged and not args.anthropic_url:
        raise SystemExit("the claude-code arm needs --anthropic-url (python -m evaluations.terminal_bench.anthropic_bridge)")
    tasks_dir = args.tasks_dir.resolve()
    names = [task.name for task in list_tasks(tasks_dir, args.tasks.split(",") if args.tasks else None)]
    identity = verify_task_identity(tasks_dir, names, args.task_checksums)
    if identity["mismatched"]:
        raise SystemExit(f"tasks differ from the pinned checksums in {args.task_checksums} "
                         f"({identity['dataset_commit']}): {identity['mismatched'][:10]}")
    allow_hosts = None
    if args.egress_allowlist:
        allow_hosts = json.loads(args.egress_allowlist.read_text(encoding="utf-8"))
        if not isinstance(allow_hosts, list) or not all(isinstance(host, str) and host for host in allow_hosts):
            raise SystemExit(f"{args.egress_allowlist} must hold a JSON list of host names")
        allow_hosts = sorted(set(allow_hosts))
    if "codex" in args.arms.split(",") and not args.no_source_guard:
        raise SystemExit("the codex arm has no source-guard projection: run it with --no-source-guard "
                         "and --egress-allowlist")
    unguarded = [arm for arm in args.arms.split(",") if arm in third_party.ARMS]
    if unguarded and not (args.no_source_guard and args.egress_allowlist):
        raise SystemExit(f"{', '.join(unguarded)} have no source-guard projection: run with --no-source-guard "
                         "and --egress-allowlist")
    config = {
        "tasks_dir": str(tasks_dir),
        "tasks": names,
        "task_identity": identity,
        "arms": args.arms.split(","),
        "protocol": args.protocol,
        "model": args.model,
        "relay_url": args.relay_url.rstrip("/"),
        "files_url": (args.files_url or args.relay_url.rstrip("/") + "/files").rstrip("/"),
        "artifacts_manifest": str(args.artifacts_manifest.resolve()),
        "endpoint": args.endpoint.rstrip("/"),
        "image": args.image,
        "branch": args.branch,
        "proxy": args.proxy,
        "max_alive_min": args.max_alive_min,
        "max_attempts": args.max_attempts,
        "context_tokens": (args.context_tokens if args.context_tokens is not None
                           else MATRIX_MODELS[args.model]["context_tokens"] if PROTOCOLS[args.protocol].per_model
                           else RUN_MODEL_CONTEXT_TOKENS.get(args.model, GLM53_CONTEXT_TOKENS)),
        "poll_interval_sec": args.poll_interval_sec,
        "run_label": args.run_label or args.work_dir.name,
        "anthropic_url": args.anthropic_url.rstrip("/") if args.anthropic_url else None,
        "source_guard": not args.no_source_guard,
        "allow_hosts": allow_hosts,
        "stop_file": str(args.stop_file.resolve()) if args.stop_file else None,
        "tofu_thinking": args.tofu_thinking,
        "relay_key_file": str(args.relay_key_file.resolve()) if args.relay_key_file else None,
    }
    args.work_dir.mkdir(parents=True, exist_ok=True)
    run_file.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    return dict(config, finish_by=args.finish_by)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from evaluations.terminal_bench.runner import RunConfig, Runner, invalidate, summarize

    if args.command == "summarize":
        print(json.dumps(summarize(args.work_dir)["arms"], indent=2))
        return 0
    if args.command == "invalidate":
        directory = args.work_dir / "trials" / args.arm / args.task / f"attempt-{args.attempt}"
        verdict = invalidate(directory, rule=args.rule, evidence=args.evidence, decided_by=args.decided_by)
        print(json.dumps(verdict, indent=2, ensure_ascii=False))
        return 0
    if args.command == "relay-key":
        print(relay_ingress.create_key_file(args.path))
        return 0
    config = frozen_config(args)
    if config.get("relay_key_file"):
        relay_ingress.create_key_file(config["relay_key_file"])
    start_deadline = datetime.fromisoformat(args.start_deadline).timestamp() if args.start_deadline else None
    if args.start_allowance_sec is not None and start_deadline is None:
        raise SystemExit("--start-allowance-sec needs --start-deadline")
    require_system_ca_bundle(load_artifacts(Path(config["artifacts_manifest"])))
    run_config = RunConfig(
        work_dir=args.work_dir.resolve(), tasks_dir=Path(config["tasks_dir"]), tasks=config["tasks"],
        arms=config["arms"], protocol=config["protocol"], model=config["model"],
        relay_url=config["relay_url"], files_url=config["files_url"],
        artifacts_manifest=Path(config["artifacts_manifest"]), endpoint=config["endpoint"],
        image=config["image"], branch=config["branch"], proxy=config["proxy"],
        max_alive_min=config["max_alive_min"], max_attempts=config["max_attempts"],
        context_tokens=config["context_tokens"], poll_interval_sec=config["poll_interval_sec"],
        run_label=config["run_label"], anthropic_url=config.get("anthropic_url"),
        source_guard=bool(config.get("source_guard")), allow_hosts=config.get("allow_hosts"),
        stop_file=config.get("stop_file"), tofu_thinking=config.get("tofu_thinking"),
        finish_by=config.get("finish_by"), start_deadline=start_deadline,
        start_allowance_sec=args.start_allowance_sec, relay_key_file=config.get("relay_key_file"))
    summary = Runner(run_config).run(args.concurrency)
    print(json.dumps(summary["arms"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
