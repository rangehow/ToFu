"""CLI: ``python -m evaluations.frontier_harness prepare-tasks|export``.

``prepare-tasks`` writes the v1 Terminal-Bench task directories, their identity
pins (``task_checksums.json`` for ``python -m evaluations.terminal_bench run
--task-checksums``) and the FHE allowlist (``allowlist.json`` for
``--egress-allowlist``). ``export`` turns a finished native run into FHE runs
and scores each arm with the FHE repository's own normalizer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evaluations.frontier_harness")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-tasks", help="materialize and pin the v1 Terminal-Bench tasks")
    prepare.add_argument("--fhe-repo", type=Path, required=True, help="checkout of the FHE repository at the pin")
    prepare.add_argument("--tb2-repo", type=Path, required=True,
                         help="checkout of laude-institute/terminal-bench-2 at the registry pin")
    prepare.add_argument("--out-dir", type=Path, required=True, help="new or empty directory")
    prepare.add_argument("--deep-swe-repo", type=Path,
                         help="checkout of datacurve-ai/deep-swe at the FHE corpus pin (needed for DeepSWE tasks)")
    prepare.add_argument("--pyknotid-repo", type=Path,
                         help="pyknotid clone with tag 0.5.3: build-cython-ext's offline /app/pyknotid.bundle")
    prepare.add_argument("--tasks", help="comma-separated task names (default: all 30)")
    export = commands.add_parser("export", help="native run -> FHE trial.json runs, scored by the FHE normalizer")
    export.add_argument("--native-dir", type=Path, required=True, help="native runner work dir")
    export.add_argument("--relay-log", type=Path, required=True, help="relay request log of that run")
    export.add_argument("--fhe-repo", type=Path, required=True)
    export.add_argument("--out-root", type=Path, required=True, help="one FHE run directory per arm is written here")
    export.add_argument("--label", required=True, help="FHE label; smoke* labels stay out of canonical selection")
    export.add_argument("--run-id", required=True)
    export.add_argument("--provider", required=True, help="model serving route, recorded in run.json")
    export.add_argument("--no-normalizer", action="store_true", help="write trial.json files only")
    export.add_argument("--exclude-throttled", action="store_true",
                        help="PREREG sensitivity variant: omit trials that saw an upstream 429")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare-tasks":
        from evaluations.frontier_harness.tasks import prepare_tasks

        manifest = prepare_tasks(args.fhe_repo, args.tb2_repo, args.out_dir,
                                 args.tasks.split(",") if args.tasks else None, deep_swe_repo=args.deep_swe_repo,
                                 pyknotid_repo=args.pyknotid_repo)
        print(json.dumps({"dataset_commit": manifest["dataset_commit"], "tasks": sorted(manifest["tasks"]),
                          "pending_repairs": manifest["pending_repairs"]}, indent=2))
        return 0
    from evaluations.frontier_harness.export import export

    summary = export(native_dir=args.native_dir, relay_log=args.relay_log, fhe_repo=args.fhe_repo,
                     out_root=args.out_root, label=args.label, run_id=args.run_id, provider=args.provider,
                     run_normalizer=not args.no_normalizer, exclude_throttled=args.exclude_throttled)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
