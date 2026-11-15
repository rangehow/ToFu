# Browser crash reproduction harness

Owner symptom (2026-11-08 onward): the Tofu tab in the owner's Chrome 155 on
macOS shows the sad tab 「喔唷，崩溃啦！… 错误代码：5」 after escalating
freezes (1–28 s) while it shows the live Coordinator. Code 5 is the renderer's
termination signal, SIGTRAP: Chrome crashed itself on purpose (an OOM handler
or a failed CHECK, `base::ImmediateCrash`), never a JS exception. This
directory holds the reproduction instruments and the before/after check every
proposed fix has to pass. Findings and numbers live in the investigation
report, not here.

## Instruments

| File | Role |
|---|---|
| `soak.py` | Long-running recorder over raw CDP. It never enables the page's Network/Runtime domains, which make DevTools keep response bodies and console arguments. Per sample it records Performance metrics, `Runtime.getHeapUsage` (V8 + Oilpan), DOM counters, the long-animation-frame breakdown (every script, not only the slowest; style/layout; unattributed), long tasks, DOM mutation rate and EventSource events/bytes per type. RSS/PSS of every Chrome process is sampled from `/proc` on its own clock (`processes.jsonl`), so a frozen page cannot stop it. On a schedule it also takes forced-GC heap samples with the detached-node count and CPU-profile windows that split main-thread self time between Tofu bundles, `chrome-extension://` scripts, GC and native work. `Target.targetCrashed` records the sad-tab `errorCode`. `--cpu-throttle N` slows the page's CPU the way a slower or busier machine would. `--hidden-after/--hidden-for` hides the tab behind another tab for a period. `--summarize DIR` rebuilds `summary.json`, with fixed checkpoints at 5/15/30/60/120/240/360/480/600 min. |
| `readonly_proxy.mjs` | TLS + HTTP/2 front door for watching **production** read-only, the way the owner's VS Code port proxy serves it (`https://…/proxy/15000/`). It forwards only GET/HEAD/OPTIONS; every other method gets a local 405 and is logged, never forwarded. On `/api/push` it forwards only subscribe/unsubscribe/ping frames and the read-only JSON-RPC allowlist of `lib/control_rpc.py`; `abort` and anything else is dropped. `--frontend-dir` overlays another build's `static/vite` on the same live server for a same-time A/B. `--frontend-upstream URL` is the overlay for a change that also edits the server-composed page (index.html, application-shell fragments): the page and other non-API paths come from a scratch server running the candidate checkout, while `/api` reads and push stay live under the same rules. Spec: `tests/diagnostics/test_browser_crash_repro_proxy.py`. |
| `heap_duplicates.py` | Snapshots a soak tab's heap (after forced GC) and reports duplicated large strings by the TurnRecord projection fields that hold each copy, e.g. `same turn: segments.[].result.content \| toolRounds.[].toolContent`. Field names and byte totals only; the snapshot is deleted afterwards. |
| `compare.py` | Prints the before/after table from finished soak runs: frames ≥1 s / ≥5 s, max frame, main-thread and script ms/s, cost per dispatched stream event, Tofu share of busy CPU, renderer RSS, worst page-response latency, hangs, crashes, post-GC heap. |
| `fetch_extension.py` | Downloads a Chrome Web Store CRX3 by ID and checks the header's `crx_id`. It pins the store public key as manifest `key`, so `--load-extension` keeps the real ID. |
| `immersive_translate_returning_user.js` | Puts a fresh Immersive Translate profile into a long-time user's state. Without it, the one-time float-ball guide plays a looping Lottie arrow: a 60 fps rAF loop with SVG attribute writes, i.e. a style+layout every frame. |
| `immersive_translate_always_translate.js` | Makes Immersive Translate translate the soak page: "always translate" for the origin, target language English. The Chinese UI with a zh-CN target is skipped as already in the target language. |
| `tests/visual/test_browser_crash_repro_stress.py` | Isolated stress, opt-in. Real app, durable worker, v4 command API, SSE multiplexer and renderers. Only the token source and `web_search` execution are stubbed. It seeds the owner's sidebar (200 conversations) and a Coordinator-scale transcript (180 turns × 12 tool rounds), then streams 100 deltas/s while `soak.py` measures the tab at 6× CPU throttle. |

The browser under test is Chrome for Testing at the owner's major version:
`https://storage.googleapis.com/chrome-for-testing-public/155.0.8059.84/linux64/chrome-linux64.zip`.
No Xvfb on the host, so runs use `--headless=new` (the full browser, not
the headless shell). Pass `--lang=zh-CN --accept-lang=zh-CN,zh` as the owner
uses a Chinese UI.

## Reading the numbers

- A long animation frame is usually many back-to-back stream-event tasks with
  no render opportunity between them. Compare the frame's `script` total with
  its slowest script (`worst[].scripts`). In the browser's own freeze reports
  (`browser-responsiveness-monitor.ts`), only the slowest script is reported,
  so a frame made of dozens of medium Tofu scripts looks "unattributed" there.
- Scripts in extension isolated worlds never appear in main-world LoAF
  entries. Use the CPU-profile split (`ext:<id>` classes) or a
  `devtools.timeline` trace to attribute extension work.
- `--cpu-throttle 6` on the live Coordinator reproduced the owner's freeze
  shape: 1–3 s frames whose slowest script is a few hundred ms. At 16× the
  frames escalated 3.7 → 8.7 → 18 → 70 s.

## Running

Production, read-only (never attach to the owner's tabs):

```sh
node scripts/browser_crash_repro/readonly_proxy.mjs --listen 127.0.0.1:18443 \
  --upstream http://127.0.0.1:15000 --strip-prefix /proxy/15000 --state-dir "$RUN/proxy"
python3 scripts/browser_crash_repro/soak.py --chrome "$CHROME" \
  --url https://127.0.0.1:18443/proxy/15000/ \
  --conversation coordinator-1a390889aa954da2b18551d1b4a32625 \
  --out "$RUN/baseline" --duration 28800 --reload-after-crash \
  --chrome-arg=--lang=zh-CN --chrome-arg=--accept-lang=zh-CN,zh
# the owner's freeze shape: add --cpu-throttle 6
# extensions, long-time-user state:
python3 scripts/browser_crash_repro/fetch_extension.py --id bpoadfkcbjbfhfodiogcnhhhpibjhbnh --out "$RUN/ext/imt"
python3 scripts/browser_crash_repro/fetch_extension.py --id kbfnbcaeplbcioakkpcpgfkobkghlhen --out "$RUN/ext/grammarly"
python3 scripts/browser_crash_repro/soak.py … --extension "$RUN/ext/imt" --extension "$RUN/ext/grammarly" \
  --extension-eval bpoadfkcbjbfhfodiogcnhhhpibjhbnh=scripts/browser_crash_repro/immersive_translate_returning_user.js
```

Delete the run's `soak-profile-*` directories afterwards. The browser profiles
cache the owner's conversation pages; `samples.jsonl`, `events.jsonl`,
`processes.jsonl` and `summary.json` hold metrics only.

Isolated stress, deterministic input:

```sh
TOFU_CRASH_REPRO_STRESS=1 TOFU_CRASH_REPRO_CHROME="$CHROME" TOFU_CRASH_REPRO_OUTPUT="$RUN/stress" \
TOFU_CRASH_REPRO_DURATION_S=7200 \
  .venv/bin/python3 -m pytest -q -p no:cacheprovider -s tests/visual/test_browser_crash_repro_stress.py
```

## Live A/B of a frontend change (production stream, read-only)

The production frontend cannot be replaced behind the proxy, so the candidate is
overlaid: one proxy serves the deployed bundle, a second serves the candidate's
`static/vite` against the same server. Both arms run at the same time on the
same conversation stream. Build the candidate from an exported tree
(`git -C <shared> archive <commit> | tar -x -C "$CAND"`, symlink
`node_modules`, `node scripts/build_frontend.mjs`).

```sh
node scripts/browser_crash_repro/readonly_proxy.mjs --listen 127.0.0.1:18443 \
  --upstream http://127.0.0.1:15000 --strip-prefix /proxy/15000 --state-dir "$RUN/proxy-base" &
node scripts/browser_crash_repro/readonly_proxy.mjs --listen 127.0.0.1:18444 \
  --upstream http://127.0.0.1:15000 --strip-prefix /proxy/15000 --state-dir "$RUN/proxy-cand" \
  --frontend-dir "$CAND/static/vite" &
for throttle in 6 16; do for arm in base:18443 cand:18444; do
  python3 scripts/browser_crash_repro/soak.py --chrome "$CHROME" \
    --url "https://127.0.0.1:${arm#*:}/proxy/15000/" \
    --conversation coordinator-1a390889aa954da2b18551d1b4a32625 \
    --cpu-throttle "$throttle" --duration 1800 --sample-every 30 --gc-every 600 \
    --profile-every 900 --profile-first 300 --profile-window 60 \
    --out "$RUN/L$throttle-${arm%%:*}" --chrome-arg=--lang=zh-CN --chrome-arg=--accept-lang=zh-CN,zh &
done; done; wait
python3 scripts/browser_crash_repro/compare.py "$RUN"/L6-base "$RUN"/L6-cand "$RUN"/L16-base "$RUN"/L16-cand
```

### Reference measurements (2026-11-09, Chrome for Testing 155.0.8059.84)

Live Coordinator, 29 min per arm. Base = deployed bundle (≈33ce4175d7); candidate = 6c18ab5854
(one sidebar render per frame).

| metric | base 6× | candidate 6× | base 16× | candidate 16× |
|---|---|---|---|---|
| stream events dispatched | 25,060 | 25,320 | 13,714 | 25,466 |
| frames ≥1 s / ≥5 s | 222 / 22 | 2 / 0 | 76 / 39 | 51 / 1 |
| max frame | 23.6 s | 1.75 s | 64.9 s | 5.5 s |
| main thread busy | 754 ms/s | 390 ms/s | 1,004 ms/s | 804 ms/s |
| task / script per event | 52.6 / 38.6 ms | 26.9 / 10.6 ms | 125.7 / 107.6 ms | 55.2 / 22.7 ms |
| renderer RSS median / max | 576 / 978 MB | 530 / 587 MB | 597 / 988 MB | 528 / 588 MB |
| worst page response | 18.8 s | 1.1 s | 52.8 s | 3.7 s |
| post-GC JS heap | 112 → 125 MB (10 → 21 min) | 112 → 124 → 137 MB (10/20/30 min) | 103 → 116 MB | 115 → 127 MB |
| renderer crashes | 0 | 0 | 0 | 0 |

Isolated stress pair (6×, 200-row sidebar, fixed driver, 120 min, base 33ce4175d7 vs candidate):
- frames ≥1 s at 15/31/61/120 min: 135/339/785/1,189 (base) vs 5/17/402/1,008 (candidate); both saturate
  once the synthetic transcript passes ~1,000 turns (cost per event grows with transcript length).
- frames ≥5 s 52 vs 6; max frame 102.4 s vs 35.2 s; worst page response 168.9 s vs 47.8 s.
- Renderer RSS bursts (≥800 MB samples, transient, seconds to minutes): 105 samples in three base runs
  (peak 1,966 MB) vs 3 in two candidate runs (one 1,534 MB sample under 30 s). memory-infra cannot
  attribute them: the saturated renderer misses its allocator dump.
- No renderer crashed in any arm.

## Before/after check for a fix

Run the isolated stress on the base commit and on the candidate with the same
Chrome binary and parameters, concurrently on the same host. Both need a
frontend built from their own tree (`node scripts/build_frontend.mjs`). Export
a candidate with `git archive` instead of creating refs or worktrees in the
shared checkout. Copy this directory and the stress test into it if the
candidate predates them. Compare the `checkpoints` rows of each `summary.json`:

1. No `renderer-crash` event, and no probe timeouts (a renderer hang of 90 s or more).
2. JS heap after forced GC does not trend upward from 15 min to the end beyond
   the transcript's own growth. Report MB at each checkpoint, along with
   renderer RSS and DOM nodes.
3. `blockedMsPerMinSinceLast` and `longFrames>=1s` are not worse than the
   base run's at every checkpoint after 15 min. At 6× throttle, the base
   commit's long frames are the symptom the fix has to remove.
4. The CPU-profile self-time split (`cpuProfileSelfMsByClass`) shows where
   the change moved main-thread time.
5. For retention changes, compare `heap_duplicates.py` at the same soak age.
   Reference, production frontend after 5.9 h on the live Coordinator: large
   strings (≥8 KB) 97.8 MB; duplicate copies 43.3 MB, of which 24.6 MB are
   `segments.[].result.content | toolRounds.[].toolContent` and 10.4 MB thinking
   signatures held three times.

For extension interaction, repeat the production soak with the candidate
frontend once it is deployed. The production frontend cannot be swapped
behind the read-only proxy.
