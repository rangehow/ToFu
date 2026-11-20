# TofuDB / optimized SQLite: local XFS correctness receipts

For the separate [formal 1,000-conversation scale comparison](scale1000.md), see its report and original archive. It completed both pairs but failed the scheduled-stream latency budget.

This is a correctness reproduction, not a capacity or latency verdict. The original [published bundle](/api/artifacts/49be2340-3416-471d-b4fb-3381e7efdd10/download) remains separate: delegated browser access was denied, so the tracked raw receipts are new executions, not copies of that bundle. The prior independent reviewer assessed the **runner and limitations only**, not either current pair. The checkpoint containing these files is the input for independent receipt review.

## Reproduce

Use a clean Git worktree at `9ad7521c75bfdb7860e33f30e545fcfa7577a52c` (tree `9d5e43b3843f98e6d4455cefb7d272612706afc1`), Python 3.12.13, and a new local XFS temp root. A bare `git archive` failed at the runner's `git ls-files` provenance inventory; it is not a substitute for a Git checkout. Use a private copy of the source-matched daemon binary, not the running process or its authority. `--build-info` reports the compiled storage fingerprint, `source_fingerprint(checkout)` independently checks the source closure, and SHA-256 identifies executable bytes. These pins do not certify a deployment or require any runtime diff.

```sh
# R is a new private local XFS directory; PY is the absolute project Python path.
# Place the verified binary at "$R/engine" before running.
mkdir "$R/home" "$R/db"
for mode in plain replay; do
    set --
    if [ "$mode" = replay ]; then set -- --carry-stream-events; fi
    rc=0
    env -i PATH="$PATH" HOME="$R/home" TMPDIR="$R" PYTHONPATH="$PWD" \
        PYTHONDONTWRITEBYTECODE=1 "$PY" benchmarks/conversation_compare.py \
        --binary "$R/engine" --directory "$R/db" --output "$R/$mode.json" \
        --conversations 4 --repeats 1 --leg-timeout 90 "$@" >"$R/$mode.log" 2>&1 || rc=$?
    printf '%s %s\n' "$mode" "$rc"
done
```

Both arms run independent application storage implementations on one XFS device with the same logical schedule (four simultaneous Attempts, four unique 8-KiB chunks, one-second arrivals), 30-second RPC deadline, and no fastpath relocation. Native daemon Strict and SQLite WAL/FULL were selected as intended acknowledgement policies. These receipts do not measure per-ACK sync behavior, stable-media ordering, or power-loss durability. The loaded coalescing VFS enables the separate PASSIVE checkpointer (4,096-frame threshold plus writer remainder); a configured checkpointer is not evidence of a pass.

The `raw.tar.gz` archive retains pair and leg JSON, console logs, exit records, and environment/source proof. Machine-specific temporary and checkout paths are normalized; workload/failure values are not. Latencies are wall-clock milliseconds with counts and p50/p95/p99/max; these reports are raw runner outputs, not individual latency vectors. No live authority data, configs, binary payloads, or provider traffic are archived.

## Original 64-conversation checkpoint control

[`active64-original.tar.gz`](active64-original.tar.gz) preserves the original paired output bytes, not a rerun or a replacement for the older `raw.tar.gz`. See the [charter](../../STORAGE_PERFORMANCE_CHARTER.md#local-checkpoint-correctness-control-not-scale-acceptance) and [compact receipt](checkpoint-control.json). Measured revision `e0fa2da38cb6a9f5e641e99540606ef7723336b1`, tree `a27bcd01f50374ff778e08f5382e46cc499ca50d`.

| SHA-256 | Original member |
|---|---|
| `f3442018e623c36ad1f13a1f03f12082fc3aac118d0615ecd145bfda9690c963` | `active64.json` |
| `7ada7c3cf13a6876d6b493f101db54a6b7c8b1b2875adb35e21dbaff1aa256ec` | `active64-legs/0-tofudb.json` |
| `615708899aee64c1e9de883efe3dbc2d86b698927be49d04d22a030d94f37bd9` | `active64-legs/0-sqlite.json` |

Archive SHA-256: `b16ab64e99332e9db8cb62b82a45d204a40e7e2f432eee11bcff4334b3050514`. Extract to a new directory and run `sha256sum -c MANIFEST.sha256` there. The archive includes original leg/pair JSON and console logs, source inventories, the focused test log, route probe logs and reports, exit records, and executable/library identities. It includes no executable/library payloads, private configuration, or authority databases. The paired authorities were cleaned up; content and replay counts are original oracle readbacks. Probe exit 0 did not mean a clean console: ancillary dispatch-drain errors are preserved. Formal P99 rows had `comparable=false` and null ratios (ineligible, not observed losses). Neither an active-checkpoint kill nor power loss was tested.

## DolphinFS preflight: not cleared

The charter's separate-approval sentence at [lines 2802–2805](../../STORAGE_PERFORMANCE_CHARTER.md#L2802-L2805) was introduced by unsigned `fcefb05fd34ee231f304c4ecb0b2144f36fbae42` (Git author/committer `your-username`, subject “Correct DolphinFS cohort interpretation and document offline replay”). The diff replaced rerunning the retained driver with offline analysis and warned that the old driver can launch additional legs against its fixed ledger, binary and root. Git attribution is not an owner approval receipt. The current assignment authorizes disposable benchmarks without live interference and requires reviewed preflight before a DolphinFS campaign. There is no verified blanket owner rule that the target must be on a different volume; conversely neither permission to run disposable fixtures nor a spare directory proves interference isolation.

Read-only mount inventory shows distinct SSD device candidates under `hadoop-hldy-nlp` (0:60), `hadoop-mlp-ckpt` (0:103), `hadoop-autoresearch-sh` (0:115) and `hadoop-nlp-hl02` (0:110); a personal-looking mount (0:111) is HDD, not an SSD control. Different mount/device IDs and CodeLab placement are not proof of allocated scratch, disjoint backend, or actual userspace preload routing. Existing project/production aliases and old campaign roots are not targets. A point-in-time host sample (load1 5.37 around 21:53 UTC) is not a clearance. No DolphinFS target reads/writes, live telemetry, or campaign were attempted as part of preflight.

Before review can clear a launch, identify the provisioned scratch allocation, which client/backend it reaches, how staging avoids interference too, and the reserved execution window. For each leg, capture a fresh quiet-window and foreign-benchmark check including relevant remote jobs, not just local process names; agree on production observation/abort signals and monitor through staging, seed, workload and drain. Quiet monitoring cannot prevent the first shared-volume stall. The old 180-CPU campaign used load1 <30 for 60 s before starting and invalidated legs above 64 at 5 s sampling or with foreign overlap; these are historical controls, not portable magic thresholds. Use a new driver, proof/output and fresh per-leg roots, and retain effective durability, checkpoint passes/failures and interference receipts; don't run the historical launcher. No verified allocation or claimed safe shared-resource arrangement was available. The missing input is a concrete allocation and isolation/coordination evidence, not another blanket permission to benchmark.

## Receipt result

Both pair commands and all four legs returned **1**; the historical runner marked each leg correct and current-source qualified under its then-current checker, but fewer than 1,000 conversations, and the pair has fewer than two repeats. The aggregate's “a leg failed or timed out” generic label is triggered by nonzero status; inspect leg errors, integrity and coverage separately. Every leg observed 16 stream events, recovered 4 running and 4 completed Attempts with exact final content, and returned integrity 0 with workload error count 0. Exact carried-event replay recovered 4/4 per arm in the replay pair; plain did not request that oracle. The running reopen is before release of stream writes. Native used a direct kill/wait path; SQLite same-store reopen and readback were observed, but signal exit is unknown at both historical cutpoints because its helper allowed a clean stop/reopen fallback. Neither cutpoint demonstrates a kill during in-flight writes or power-loss survival. Console logs are retained even where ancillary/background operations log unavailable during restart; the zero count is the runner's workload counter, not an assertion that every console line was clean.

SQLite reported zero background or writer checkpoint passes and zero failed passes, with maxima of 740 frames (plain) and 773 (replay). No active checkpoint coverage or latency conclusion follows. Earlier standalone WAL/checkpointer correctness tests are separate from these pairs; the previous 207-test suite was not repeated. Both pairs use the same order, not a balanced performance experiment.

Native executable: `566b212bf7aafa30db7f5d2dee6b0f4aadd12cdbb7fb4c0dae76de360be42c1c`. Compiled and independent checkout storage fingerprint: `443fdee4ce2136e2fbf90e64e0a0316e9667a898d22645e06106fde1f62b78e1`. Runner source digest in all four legs: `2d96fe8c83d53e19ddf270b82821b5592fa16e0c408f0bec5065ccb4aa4e424a`. See `identity.txt` inside the archive for library pins and environment. Archive SHA-256: `9dcf012e03f79530e6cb196ac06c1c7658b4d65ca7b08bf870a0019a4f5ca085`.
