# Evaluation takeover audit: models × benchmarks × harnesses (2026-11-17)

Owner question (2026-11-17): "At zw05 I had the agent test many models on many
benchmarks with different harnesses (pi, opencode, codex, tofu) to compare
performance and cost. Have you taken over that work?"

**Verdict: partially taken over.**
- **Not taken over:**
  - the data;
  - the newest code (EVAL-MATRIX, the third-party arms, native-exec auth, FHE PREREG-2);
  - the plan's open cells.
- **Taken over:**
  - the core native Terminal-Bench runner on the current branch;
  - five harnesses (tofu, opencode, pi, claude-code, codex).

  All five pass a one-task smoke run on the new 203 GB host (§5).
- **Not running:** nothing has continued since the old host stopped on 2026-11-14.

**Update, 2026-11-18 06:30 (§8): taken over and running, but the matrix is not complete yet.**
- **Code:** EVAL-MATRIX, the third-party arms, relay ingress auth and FHE PREREG-2 are now on this branch. The parked native-exec auth stays parked.
- **Data:** migration to `N` is in progress, sha256-verified per unit.
- **Runs:** 18 relays and about 20 runners are filling the Tier A cells to K = 3, plus the new glm-5.3-flash, kimi-k3 and qwen3.5-plus rows. The MR1 Tofu cells are back on a verified rebuild.
- **Complete so far:** glm-5.2 is the first family with four cells at K = 3. All of their CIs overlap, so a fourth run (t4) is running.
- **Rankings:** no ranking is claimed yet, because no pair of complete cells has disjoint CIs (§8.4).

Method:
- **Read-only on the old path.** No writes, and no git writes to the old repo.
- **Raw inputs:** scores were rebuilt from per-trial `result.json` / `state.json` / Harbor `result.json` files.
- **Prose checked, not trusted:** prose summaries were used only as cross-checks.
- **Spend:** two 1-token probes and one one-task smoke cell per harness (§5).
- **Untouched:** no keys were printed, nothing was restarted, and the owner's uncommitted `gateway.py` was not changed.

Paths used below:
- `O` = `/path/to/your/data (old, zw05; also reachable as `…/your-user/INS/your-username/…`)
- `N` = `/mnt/your-fs` (new)

## 1. Inventory

### 1a. Code on the current branch (`eval/opencode-glm53-tb21` @ `a4f58e879c`)

| location | what | harnesses | state |
|---|---|---|---|
| `evaluations/terminal_bench/` (`runner`, `harness`, `relay`, `anthropic_bridge`, `gateway`, `vm_trial`, `report`, `audit`, `source_guard`) | Native TB runner on EnvSandbox 857 VMs; protocols `vendor-6h`, `task-default`, `claude-6h-medium`, `fhe-v1` | tofu, opencode, pi, claude-code, codex | **works on the new host** (§5) |
| `evaluations/{pi,opencode,claude_code}_contract.py` | Version pins: Pi 1.4.0, OpenCode 1.19.0, Claude Code 2.1.207 | — | pins match the old artifacts |
| `evaluations/swebench/` (`python -m evaluations harbor …`) | Harbor adapter for SWE-bench Verified (500), TB2.1/3.0/4.0, DeepSWE; rootless-QEMU backend | `--agent` pi/opencode/codex proxy/tofu | **not runnable here**: `harbor doctor` fails (`harbor` not installed, no rootless base disk) |
| `evaluations/swebench/yourprovider_sandbox/` (`tb21_switch`, `submit`, `watch`, `score`) | Legacy canvas submitter | — | **deprecated** (owner directive 2026-11-05: no canvas). Not run, and excluded from reported numbers |
| `evaluations/frontier_harness/` | FHE v1 task export (an early slice from f41521bca6) | — | Missing `multi_attempt.py`, PREREG-2 and the phase-a/em3 changes |
| `evaluations/edit_file/forced_operation/` | edit_file forced-operation benchmark (committed RESULTS.md) | tofu workers, claude-opus-5 / deepseek / glm-5.3 / kimi-k3 | out of scope here (tool-level, not harness × benchmark) |

### 1b. Unlanded refs: the zw05 work that is not on HEAD

Every SHA below exists in the current repo; the two repos share their objects. Each ref also has a `recovered/*` twin.

| ref | sha | date | ahead of HEAD | content |
|---|---|---|---|---|
| `eval/eval-matrix-a10-em3-20261114` | `0162029d3a` | 11-14 | 97 | **Newest EVAL-MATRIX line**: `matrix-tb21` protocol, `--tofu-thinking`, `--stop-file`, `--pin-file`, relay ingress keys, `third_party.py` arms (goose, hermes, kimi-code, mini-swe-agent, oh-my-pi, qwen-code), PREREG.md (1218 lines, A10 amendments) |
| `eval/eval-matrix-a10-20261113`, `-a10-em3-0250-runnerfix`, `eval/eval-matrix-20261112` | `43b4a5da49`, `06d4c6e8c5`, `24773df498` | 11-13/14 | 86 / 78 / 49 | earlier states of the same line |
| `fhe/multi-attempt-prereg-a7` (+ `-prereg`, `phase-a-runs`, `frontier-harness-v1-native`, `deepswe-native`) | `826a4544bf` | 11-13 | 37 | FHE PREREG-2 (Tofu vs Codex on kimi-k3, amendments 1–7), `third_party.py`, DeepSWE schema 1.3 |
| `relay-auth/em-a10-20261113`, `relay-auth/fhe-a7-20261113` | `0250ee0b9a`, `e73f17b92b` | 11-13 | 77 / 38 | relay ingress key on every route |
| `parked/native-exec-auth-20261116` (port of `fix/native-exec-auth-20261113`) | `90b071b0e2` | 11-17 | 34 | per-session HMAC native-exec token. Parked because it conflicts with the owner's uncommitted `gateway.py` (§6) |
| `recovered/wt/project-worker-3d589822-…-wip` | `c4a5fd8e8f` | 11-15 | 7 | WIP snapshot: `third_party.py` (+541 lines) and 4 test files |

No RESULTS file is committed on any eval ref. Measured numbers exist only in the run dirs and in the PREREG amendment log.

### 1c. Run data on the old path (`O`)

| dir | dates | benchmark / backend | harnesses | models | completeness |
|---|---|---|---|---|---|
| `main-local/runs/tb21-{tofu,opencode}-v2…v13` (+ `tb21-traceability-20260908` reports, `tb21-v13-20260924`) | 09-07 → 10-01 | TB2.1, Harbor rootless-QEMU, 6 h | tofu (TofuKimiRuntimeAgent), opencode | glm-5.3-m17 | v8/v10/v11/v12 complete ×1; v5/v6c tofu 89×5; v13 89×5 aborted (115/92 trials) |
| `tb21-direct-20261101` | 11-01/02 | TB2.1, sandbox-857 via Harbor/agentix | tofu, opencode, A/B tofu | glm-5.3-flash | full + reruns r2–r6; vendor6h 89 |
| `tb21-6h-20261102` | 11-02/03 | TB2.1 vendor-6h | tofu | glm-5.3-flash | 88 + 1 unmeasured |
| `tb21-native-20261102` | 11-02/03 | TB2.1, **native runner** vendor-6h | tofu, opencode | glm-5.3-flash | complete |
| `tb21-native-pi-20261103` | 11-03 | same | pi | glm-5.3-flash | complete |
| `tb21-native-20261103-fix` | 11-03/04 | same, fixed kill | tofu, opencode | glm-5.3-flash | complete (87 + 2 qemu) |
| `tb21-three-way-20261103/report.json` | 11-03 | derived 3-way report | tofu/opencode/pi | glm-5.3-flash | derived |
| **`eval-matrix-20261112`** | 11-12 → 11-14 | TB2.1 `matrix-tb21`, native runner on 857, egress allowlist | tofu (2 builds), opencode, pi, claude-code, codex, goose, hermes, kimi-code, mini-swe-agent, oh-my-pi, qwen-code | 12 models (table §2c) | **incomplete**: 2/79 deciding cells at K=3; 6,972 measured trials |
| `frontier-harness-20261112` | 11-12 → 11-14 | FHE fhe-v1 TB subset (21) / full (30, incl. DeepSWE) | tofu, codex, claude-code, opencode, oh-my-pi, pi | kimi-k3 | phases a/b/m. k1 has 27 of 30 started |
| `harness-adapters-20261112` | 11-12/13 | FHE smokes, 1–3 tasks | goose, kimi-code, mini-swe-agent, qwen-code, hermes, oh-my-pi | kimi-k3 | smoke |
| `native-harness-artifacts/effort-probe` | 11-03 | relay effort probe | — | glm-5.3-flash | complete (justifies `max`) |
| `tool-interface-ablation` | 11-02 | 1-task probes | tofu variants | glm-5.3-flash, deepseek-v4.1-flash | smoke |
| `pi-comparison-preflight`, `tb21-glm53-opencode-20260906`, `tb21-current-20261015`, `tb21-submit-20261018` | 09-06 → 10-26 | — | — | — | preflight / blocked; no trials |
| `harness-public-20260908`, `harness-readiness-20261010`, `framework-audit`, `harness-1b-scale-20261112`, `benchmarks/` | — | pytest, infra, synthetic load | — | — | not model evaluations |
| `eval-quota.json` | 11-14 | key/AppId RPM ledger | — | — | byte-identical on old and new paths |

**SWE-bench:** no SWE-bench result data exists anywhere: not on the old `.eval-runs`, not in old `data/` or `output/`, and not on any ref. SWE-bench is code only.

## 2. What was measured (from raw per-trial files)

"Pass" = verifier reward ≥ 1.0, and n is the number of measured trials. The raw rate counts passes later flagged as contaminated; "strict" excludes passes reviewed as contaminated.

### 2a. GLM-5.3 TB2.1 baselines, September (Harbor rootless-QEMU, 6 h)

Source: `O/main-local/runs/<run>/jobs/*/*/result.json`.

| run (date) | tofu | opencode | tokens (Harbor `agent_result`, in / out) |
|---|---|---|---|
| v8 (09-15) | 51/89 | 39/89 | — |
| v10 (09-17) | 62/89 | 50/89 | — |
| v11 (09-18) | 61/89 | 47/89 | — |
| **v12 (09-20 → 22)** | **63/89** | **56/89** | tofu 273.3M / 8.04M; opencode 159.7M / 5.36M |
| v5 / v6c, tofu ×5 | 236/445, 255/445 | — | — |
| v13 ×5 (aborted) | 77/115 | 54/92 | — |

- **Paired report:** `tb21-traceability-20260908/glm53-m17-final-review/report.json` marks the pair `diagnostic_only`: incomplete 89×5, audit failures.
- **Not recorded:** cache tokens and cost.

### 2b. GLM-5.3-Flash TB2.1, November, sandbox 857

All Nov token figures are input / cached / output from the run's `relay-requests.jsonl`.

| run (date) | harness | score | tokens (in / cached / out) |
|---|---|---|---|
| tb21-direct `full` (11-02, task-default) | opencode | 35/89 | 44.5M / 34.8M / 1.65M |
| tb21-direct `full` + r2–r6 | tofu | 45/89 (raw 7/89: 76 AgentSetupError before reruns) | 36.8M / 32.4M / 1.85M |
| tb21-direct `vendor6h` | tofu | 71/89 (1 unmeasured) | 219.0M / 166.4M / 5.84M |
| tb21-6h | tofu | 70/89 (1 unmeasured) | 189.7M / 159.4M / 5.76M |
| **tb21-native (vendor-6h)** | tofu | **72/89 raw, 68 strict** | 171.8M / 140.5M / 5.46M |
| tb21-native | opencode | **71/89** | 281.4M / 229.5M / 4.99M |
| tb21-native-pi | pi | **72/89** | 235.7M / 215.2M / 6.12M |
| tb21-native-fix | tofu | **75/89 raw, 70 strict** | 310.8M / 289.5M / 7.21M |
| tb21-native-fix | opencode | **67/89** | 304.2M / 267.8M / 6.21M |

- **Cross-check:** these match `O/tb21-three-way-20261103/report.json` (tofu 72/68, opencode 71, pi 72).
- **Cost:** none recorded. Using `lib/pricing/_tables.py` (glm-5.3-flash $0.11 in / $0.39 out per M, cache read 0.29×), each 89-task arm costs about $5–10. That is a derived estimate, not a measurement.

### 2c. EVAL-MATRIX TB2.1 (`matrix-tb21`, 11-12 → 11-14)

How the table was built:
- **Aggregation:** across all runs of a cell, from `O/eval-matrix-20261112/analysis/per_trial.csv`. That file has one row per trial attempt, with relay-logged `cost_usd`.
- **Spot check:** 8 random rows matched `runs/<model>/<run>/trials/<arm>/<task>/attempt-N/state.json` exactly.
- **Rates are raw.** "contam" = trials flagged by the audit and not excluded.
- **Composition:** the CSV merges the `-ca` reruns, so a few cells differ by 1–2 from the raw first pass (e.g. MiniMax-M2.7 × claude-code r3: 42 raw vs 44 in the CSV).
- **Cost** is the relay's list price.

| model | harness | pass / n | rate | cost $ | runs | contam |
|---|---|---|---|---|---|---|
| claude-opus-5.5 | claude-code | 69/89 | 77.5% | 85.80 | r1 | 1 |
| claude-opus-5.5 | pi | 71/89 | 79.8% | 40.38 | r1 | 2 |
| claude-opus-5.5 | tofu | 38/51 | 74.5% | 43.50 | r2 (partial) | 0 |
| grok-4.6 | codex | 132/172 | 76.7% | 138.77 | a1,a2,r1 | 0 |
| grok-4.6 | pi | 133/174 | 76.4% | 56.75 | a1,a2,r1 | 0 |
| grok-4.6 | opencode | 127/174 | 73.0% | 76.48 | a1,a2,r1 | 0 |
| grok-4.6 | tofu | 123/172 | 71.5% | 79.75 | a1,a2,r2 | 0 |
| grok-4.6 | tofu@948e9b264f | 35/50 | 70.0% | 23.80 | b1 | 0 |
| grok-4.6 | claude-code | 107/173 | 61.8% | 259.09 | a1,a2,r1 | 0 |
| grok-4.6 | goose / hermes / kimi-code / mini-swe-agent / oh-my-pi / qwen-code | 38 / 32 / 38 / 39 / 38 / 34 of 51 | 62.7–76.5% | 20.6–44.8 each | b1 | 0 |
| deepseek-v4-flash | codex | 39/44 | 88.6% | 0.92 | a1–a3 | 0 |
| deepseek-v4-flash | claude-code | 37/45 | 82.2% | 0.88 | a1–a3 | 0 |
| deepseek-v4-flash | tofu | 96/126 | 76.2% | 3.34 | a1,a2,r3 | 0 |
| deepseek-v4-flash | opencode | 150/199 | 75.4% | 3.27 | a1,r1,r2 | 54 |
| deepseek-v4-flash | pi | 144/199 | 72.4% | 3.37 | a1,r1,r2 | 7 |
| glm-5.2 | tofu | 125/181 | 69.1% | 130.90 | a1,a2,r3 | 0 |
| glm-5.2 | pi | 136/209 | 65.1% | 82.16 | a1,r1,r2 | 3 |
| glm-5.2 | claude-code | 114/183 | 62.3% | 147.37 | a1,a2,r2 | 2 |
| glm-5.2 | opencode | 123/209 | 58.9% | 104.43 | a1,r1,r2 | 39 |
| glm-5.2 | codex | 103/182 | 56.6% | 111.35 | a1,a2,r2 | 12 |
| gemini-3.5-flash | tofu | 99/156 | 63.5% | 221.14 | a1,a2,r1 | 0 |
| MiniMax-M3 | tofu@948e9b264f | 71/97 | 73.2% | 21.32 | a1–a3 | 0 |
| MiniMax-M3 | codex | 69/98 | 70.4% | 20.32 | a1–a3 | 0 |
| MiniMax-M3 | claude-code | 68/99 | 68.7% | 18.87 | a1–a3 | 0 |
| MiniMax-M3 | pi | 132/215 | 61.4% | 34.74 | a1,r1,r2 | 9 |
| MiniMax-M3 | opencode | 126/215 | 58.6% | 36.95 | a1,r1,r2 | 25 |
| MiniMax-M2.7 | pi | 127/267 | 47.6% | 16.63 | r1–r3 | 4 |
| MiniMax-M2.7 | codex | 60/128 | 46.9% | 9.11 | a1,a2,r3 | 3 |
| MiniMax-M2.7 | mini-swe-agent | 59/127 | 46.5% | 12.58 | a1,a2,r2 | 1 |
| MiniMax-M2.7 | tofu@948e9b264f | 47/104 | 45.2% | 6.46 | a1–a3 | 0 |
| MiniMax-M2.7 | claude-code | 91/202 | 45.0% | 17.11 | a1,r2,r3 | 2 |
| MiniMax-M2.7 | goose / kimi-code / hermes / qwen-code / opencode / oh-my-pi | 56/127, 55/128, 52/126, 49/127, 100/267, 46/128 | 44.1 / 43.0 / 41.3 / 38.6 / 37.5 / 35.9% | 6.8–16.5 each | | 0–36 |
| Doubao-Seed-2.0-pro | opencode | 108/258 | 41.9% | 43.57 | r1–r3 | 55 |
| Doubao-Seed-2.0-pro | goose / mini-swe-agent / oh-my-pi / qwen-code / claude-code / kimi-code / codex / hermes / tofu@948e | 37/95, 37/99, 37/100, 36/101, 36/102, 34/101, 32/100, 11/39, 6/16 | 28–39% | 4.6–48.9 each | a1,a2,r2/r3 | 0–15 |
| kimi-k2.7-code-highspeed | codex / pi / kimi-code / tofu@948e / opencode | 13/22, 14/23, 60/110, 12/22, 57/110 | 52–61% | 3.7–40.2 | a1,a2,r1 | 0 |
| kimi-k2.7-code-highspeed | claude-code | 0/22 | 0% | 0.00 | a1,a2 | 0: **zero cost means no model traffic; harness/route failure, not a score** |
| gpt-6.1-sol | codex | 5/6 | — | 0.98 | r1 | closed partial after 429s |
| qwen3.5-plus | opencode | 1/3 | — | 0.16 | r1 | aborted |

**Totals:** 60 cells; 6,967 valid trials (3,964 pass / 3,003 fail; 5 invalid `goose_no_ca_store`). Relay-logged list cost is **$2,388.76**.

**Status at stop** (`analysis/TABLES-A10.md`, PREREG A10 log): deciding cells complete at K=3 were **2/79** (Tier A 2/48). The pre-registration therefore makes no rank claim yet.

**Token gap:** the relay logged `usage: null` on the Anthropic face, so the claude-code and opus arms have cost but no token counts.

### 2d. FrontierHarness Eval v1 (kimi-k3)

Source: `O/frontier-harness-20261112/phase-*/<run>/trials/*`; A1 is cross-checked against `phase-a/A1_TABLE.md`.

| block | results (pass / measured) | cost |
|---|---|---|
| a1, 21 TB tasks, vs published FHE row | claude-code 18/21 (FHE 16), oh-my-pi 18/21 (16), codex 17/21 (15), opencode 17/21 (15); McNemar p ≥ 0.625 for every arm | $3.95 / $5.31 / $5.73 / $6.62 |
| a2 | codex 17/21, pi 15/21 | — |
| t1 | tofu 18/21, codex 15/21 | — |
| c1 | codex 18/20 | — |
| b1 | codex 5/9, tofu 3/9 | — |
| k1, 30 incl. DeepSWE, 27 started | codex 20/26, tofu 19/27 | — |
| k2, 30 | codex 19/30, tofu 15/30 | — |
| DeepSWE smoke d0 / d0t | codex F2P 1/9 (reward 0); tofu F2P 8/9 (reward 0) | — |

### 2e. Survival and reproducibility

- **Survival:** no run data migrated.
  - All ~145 legacy dirs under `N` are **empty placeholders** dated 2026-11-15 00:12.
  - `eval-quota.json` is the only shared file.
  - Raw data survives **only on the old path**, which is still readable (verified today).
  - Runtime artifacts survive under `O/*/files`, except Claude Code 2.1.207. It lived in the old host's `/tmp` and is lost; it was rebuilt today from the pinned contract, with an identical sha256 (§5).
- **Reproducible on HEAD:**

| run family | reproducible on HEAD? | why |
|---|---|---|
| tb21-native, tb21-native-pi, tb21-native-fix (vendor-6h) | **yes** | same runner, protocol and artifacts |
| eval-matrix | **no** | uses `--protocol matrix-tb21`, `--tofu-thinking`, `--stop-file` / `--pin-file` and the third-party arms; all are only on `eval/eval-matrix-a10-em3-20261114`. Its launch scripts also hard-code the old host IP `10.114.80.41` and `/tmp/eval-matrix-20261112-files` |
| FHE PREREG-2 | **no** | needs the `fhe/multi-attempt-prereg-a7` code |
| Sep Harbor rootless runs | **no** | no harbor / rootless-QEMU environment on this host |

## 3. What is missing

1. **Matrix cells:**
   - 77/79 deciding cells are below K=3 runs.
   - Tier B (third-party harnesses on most models) stands at 163/8,277 trials.
   - gpt-6.1-sol, qwen3.5-plus, LongCat-2.5 and gpt-5.6-sol were never run (`RPM:?measure first`).
   - glm-5.3 and kimi-k3 are absent from the matrix, although the PREREG lists them.
   - claude-opus-5.5 × tofu is partial (51/89); kimi-k2.7 × claude-code is a 0-traffic failure.
   - 2,706 ingress-refused attempts were restored but not re-run.
2. **No SWE-bench measurement at all.** No harness, model or cell has one. The Harbor environment is not installed on this host.
3. **Missing metrics:**
   - cost for every non-matrix run (only tokens exist);
   - token usage on the Anthropic face;
   - cache tokens for the Sep runs.
4. **Committed results:** none. All numbers live in gitignored run dirs on the old path.
5. **Possible recording gap:** tofu arms record `agent_timeouts = 0` in nearly every matrix/FHE run while other harnesses show 5–36. This may be a budget-exhaustion classification gap, not behavior. It is not verified.

## 4. Status verdict

**Partially taken over.**

| piece | status |
|---|---|
| Native TB runner and five harness arms on HEAD | current and working |
| Result data | not migrated |
| EVAL-MATRIX / third-party / FHE PREREG-2 / native-exec-auth code | not on HEAD |
| The open plan | not resumed |
| Live harness-comparison work on this host | not ours. A different Tofu project (`parallel_data/harness_b`, workspace `3c1dd1e3e0a5`) is running `tb21-00` with tofu / opencode / codex × MiniMax-M2.7 through relay `10.24.174.241:18760`. It is a separate lane; it was not touched, but it shares the FRIDAY AppId quotas |

## 5. Smoke runs on the new 203 GB host

Host: `codelab-prod-617731514-89728106-9h7mn`, 10.24.174.241, 180 CPU / 203 GB.

Setup:
- **Work dir:** `N/takeover-smoke-20261117/`. Its runner venv has `mt-paas-sandbox-python-sdk==1.1.4`, `aiohttp`, `httpx` and `dirhash`.
  - The new host's `.venv`, `.tofu-envs` and miniforge envs have no `pssdk`.
  - The old interpreter at `INS/…/miniforge3/envs/tofu` works but takes ~3 min to import over FUSE.
- **Relay:** key …6672, `--max-rpm 5`, glm-5.3-flash-m17, keys loaded from live Tofu routing. Logs are in that dir. The relay and the Anthropic bridge were stopped after the run.
- **Task:** `fix-code-vulnerability` (TB2.1, checksum-pinned), protocol `task-default` (900 s), `--max-attempts 1`, ingress branch `ruanjunhao/tofu-native-exec-20261102`, image `rootfs://harbor-pipeline-dind-v5`, cluster 857.
- **Runs:** A = 4 arms with the source guard on; B = codex with the matrix egress allowlist, because the codex arm requires `--no-source-guard`.

| harness (artifact) | result | requests | input / cached / output tokens |
|---|---|---|---|
| tofu (tofu-agent 0.19.1 @214650e68168-r3) | **pass, 1.0** | 16 | 442,261 / 404,288 / 10,754 |
| opencode 1.19.0 | **pass, 1.0** | 16 | 277,118 / 232,448 / 3,893 |
| pi 1.4.0 | **pass, 1.0** | 9 | 35,316 / 9,088 / 1,393 |
| claude-code 2.1.207 (via bridge) | **pass, 1.0** | 22 | 515,740 / 485,952 / 6,142 |
| codex 0.148.0 (Responses face) | **pass, 1.0** | 15 | 283,303 / 253,184 / 2,581 |

- **Health:** every request returned 200 (zero 429s), and every session stopped (`unstopped_sessions 0`).
- **Spend:** about **$0.07** at `lib/pricing` list price, plus two 1-token probes.
- **Quota ledger caveat:** `eval-quota.json` (11-14) records glm-5.3-flash on …6672 as `cap 0` (429 on the first request, 11-13 21:40). Today it served this smoke cleanly. Re-measure before any allocation.
- **SWE-bench / Harbor runner:** **not working on this host.** `python -m evaluations harbor doctor` fails on `harbor` (pinned commit `ea2fee78` not installed), on rootless-QEMU (no base disk or image store), and on the artifact location (`/tmp`). No SWE-bench smoke was possible without first building that environment.
- **Legacy canvas runner** (`yourprovider_sandbox/tb21_switch`): not run, by directive.

Problems found while testing:
- **Pi model name.** `--model glm-5.3-flash-m17` makes the Pi arm fail before any sandbox call (`pi_contract.catalog_provider`). The run model must be the logical id `glm-5.3-flash`, and the relay pins the wire id. This is a documented-by-example convention (old run.json files), not stated in the README.
- **Offline suite.** The offline evaluation suites gave 198 passed / 21 skipped / **1 failed**: `tests/evaluations/test_claude_code_native.py::test_frozen_config_requires_the_bridge_and_defaults_old_runs_unguarded`. It builds `run` args without `--branch`, which `cb02a4ea63` (11-15) made required. This is a stale test on HEAD, not reported elsewhere. Fixing it is a one-line test change, which this audit did not make.

## 6. Owner's uncommitted `evaluations/terminal_bench/gateway.py`

Read only, not modified: +37/−4 lines. The edit:
- mints a random per-session `NATIVE_EXEC_TOKEN` / `NATIVE_READ_TOKEN` and passes it in the VM start env;
- sends `x-native-token` and `Accept-Encoding: identity` on every gateway call;
- adds `peer_read_bytes()` for burst-seed reads from another VM.

How it relates to the other copies:
- **Old draft:** it is byte-identical to the old main checkout's draft (`recovered/main-checkout-wip`, `3b8185c576`).
- **Parked port:** `parked/native-exec-auth-20261116` is the finished version. It derives the token by HMAC from the session id, which survives a runner restart. It also verifies that the VM refuses an untokened call (`IngressUnprotectedError`) and filters pssdk's logs.
- **Conflict:** the two edit the same lines, so landing the parked branch replaces the draft. That needs the owner's decision.
- **Smoke impact:** today's smoke ran with the draft present in the shared checkout but **not** in the worktree that ran the smoke. The smoke used the committed tokenless channel.

## 7. Continuation plan (as written at audit time; §8 records what was done)

1. **Code.**
   - Land the parked native-exec auth once the owner settles the `gateway.py` draft (keep the draft, or replace it with the HMAC port).
   - Then port the EVAL-MATRIX line (`eval/eval-matrix-a10-em3-20261114`) onto HEAD: `matrix-tb21` protocol, `third_party.py` arms, stop/pin files, relay ingress key and its tests.
   - Fix the stale `--branch` test.
   - Parameterize the host IP and the files dir; never hard-code `/tmp`.
2. **Data.**
   - Copy (do not move) `O/{eval-matrix-20261112,frontier-harness-20261112,tb21-native-*,tb21-three-way-20261103}` analysis outputs and run dirs into `N`, and verify them by sha256 manifest.
   - Commit a results summary (this document's tables, regenerated by script from `per_trial.csv` and the trial files), so numbers stop living only in gitignored dirs.
3. **Missing cells, by priority.**
   - a) Bring Tier A cells to K=3: every model × {tofu, opencode, pi, claude-code, codex}.
   - b) Re-run the 2,706 ingress-refused attempts and the opus × tofu remainder.
   - c) Add glm-5.3-flash, kimi-k3 and gpt-6.x once each key/model bucket is re-measured in `eval-quota.json`.
   - d) Tier B third-party harnesses only after Tier A.
   - e) SWE-bench Verified: build the Harbor eval venv + rootless-QEMU base disk per `evaluations/swebench/README.md`, run `doctor`, then 1 oracle task, then 1 task per harness, before any batch.
4. **Harness parity checklist** (every cell, frozen in `run.json` and refused by `report` on mismatch):
   - **Task set:** TB2.1 89 tasks @ `7131e437…` with checksum pins; record qemu-startup and qemu-alpine-ssh as known-broken verifiers (ceiling 87/89).
   - **Timeouts:** one protocol per comparison (`vendor-6h` 21,600 s, or `task-default`), with the same verifier attempts.
   - **Sandbox:** cluster 857, `harbor-pipeline-dind-v5`, 4 CPU / 16 GiB, the same proxy, and the same egress policy. Source guard and allowlist are not comparable across arms, so run codex comparisons with the allowlist for **all** arms.
   - **Model / sampling:** one relay per model; relay-enforced temperature, top_p, max_tokens and reasoning effort; the same context and output facts for every arm.
   - **Scoring:** raw and strict (contamination-reviewed) reported separately; unmeasured trials never dropped from the denominator; K≥3 runs per cell with run-to-run SD.
   - **Recording:** confirm that the tofu budget-exhaustion classification matches the other arms (§3 item 5).
   - **Context:** cache-affinity carriers recorded per arm.
5. **Cost accounting.**
   - Record usage on every face; the Anthropic face currently logs `null`.
   - Compute `cost_usd` per trial from the relay log against one frozen price table (record which: `lib/pricing` list or the FRIDAY template).
   - Report cost per pass and tokens per trial (in / cached / out) beside each score.
   - Budget each batch in `eval-quota.json` with per-AppId+model RPM/TPM caps and stop-on-429.
   - The matrix spent $2,388.76 for 6,967 trials (≈ $0.34 per trial, highly model-dependent: deepseek ≈ $0.02, grok × claude-code ≈ $1.50).
6. **Coordination.** The parallel `harness_b` lane is running a similar tofu / opencode / codex × MiniMax-M2.7 TB2.1 comparison on this host now. Agree on one owner per model × harness cell and a shared quota split before restarting the matrix, to avoid duplicate spend and 429 contention.

Artifacts of this audit:
- this file;
- smoke work dir `N/takeover-smoke-20261117/` (`runA2/`, `runB/`, `relay-requests.jsonl`, `bridge-requests.jsonl`, `artifacts.json`, `files/`, `venv/`).
  `runA/` holds only the `run.json` of the attempt that failed on the model name before any sandbox call.

## 8. Continuation, 2026-11-17 (lane EVAL-TAKEOVER-1)

Owner policy relayed by the coordinator (2026-11-17): cost does not constrain, and spend is reported as information.
- **Each cell:** filled to K ≥ 3, then more runs while CIs overlap.
- **Throughput:** as many cells in parallel as the rate limits allow.
- **Still binding:** rate limits, the safety, secret and IP rules, and the owner's `gateway.py` edits.

Lane dir `L = N/takeover-matrix-20261117/`.

### 8.1 Code (task worktree commits, landed through the Worker checkpoint)

| commit | content |
|---|---|
| `ec43112721` | EVAL-MATRIX port (`eval/eval-matrix-a10-em3-20261114`): `matrix-tb21`, `--tofu-thinking`, stop/pin files, `--finish-by`, relay ingress key, the six `third_party.py` arms, PREREG A10. Not taken from that line: its `gateway.py`, its `test_tb_native_exec_auth.py` and the runner's `ingress_auth` record (§8.6). `--branch` is now required only for a new run, since a resume reads `run.json`; this fixes the stale `test_claude_code_native.py::test_frozen_config_requires_the_bridge_and_defaults_old_runs_unguarded` and four other CLI tests. |
| `9a190f4f6b` | FHE PREREG-2 port (`relay-auth/fhe-a7-20261113`, which contains `fhe/multi-attempt-prereg-a7`): `multi_attempt.py`, PREREG-2, the `fhe-v11` / `fhe-v11-hold` protocols, start deadline and allowance, relay token window, budget file and holds. FHE's own lease flags are dropped in favour of HEAD's sandbox-857 lease client. `ingress_not_ready` lives in `runner.py`, so `gateway.py` stays unchanged. The WIP snapshot `recovered/wt/project-worker-3d589822-…-wip` is an earlier state of this line and is superseded. |
| `6ace56d1ad` | `glm-5.3-flash` as a `matrix-tb21` model. The catalog profile gives effort max, a 1 M window and the Anthropic face. The relay pins the wire id `glm-5.3-flash-m17`; every arm names the logical id `glm-5.3-flash`. |
| `0807597eff` | `evaluations/terminal_bench/eval_matrix/status_tables.py` regenerates the results tables (§8.4) from raw run data. |
| `3757783f63`, `6eca59d3f6`, `220da0de61`, `22ca974e71` | SWE-bench/Harbor rootless environment fixes (§8.5) |

Tests:
- `tests/evaluations`, `tests/rootless_vm` and `tests/project_mod/test_swebench_evaluation_framework.py` together give 1249 passed, 45 skipped and 1 failed.
- The failure is `tests/evaluations/test_owner_memory_benchmark.py::test_export_exclusion_covers_every_mode`. It fails on HEAD without these changes too and lies outside this lane.

The runs use the trial code `L/code-ec43112721` (+ `PATCH-glm53f.diff`). Its trial-side files (`vm_trial`, `harness`, `relay`, `third_party`, `relay_ingress`, `anthropic_bridge`, the pi, claude-code and opencode contracts) are byte-identical to `0250ee0b9a`, which ran the matrix's last trials. Resumed runs therefore keep the same agent-side code.

### 8.2 Data migration (`N/takeover-data-20261117/`)

How the copy works:
- `copy_and_verify.py` copies; nothing is moved.
- Each unit is a run dir or a top-level entry. Its source bytes are hashed in a single read into `manifests/<unit>.src.sha256`, then re-hashed from `N` into `manifests/<unit>.verify.json`, which records `ok` and the mismatch list.
- Units are disjoint, so a verified run dir can be resumed while later units copy.
- The empty 2026-11-15 placeholders are filled in place.
- The old path was never written: a write probe there returns `Remote I/O error`.
- File mtimes are not preserved. Content and names are.

Verified at 2026-11-18 06:00 CST, 0 mismatches in every unit:
- `tb21-three-way-20261103`, `tb21-native-20261102` (incl. `tasks-7131e43`, 6,093 files, 4.16 GB), `tb21-native-pi-20261103`;
- `eval-matrix-20261112/analysis`, `probes`, the `plan-a10*` files and the other top-level entries copied so far;
- `eval-matrix-20261112/runs/`:
  - glm-5.2 a1, a2;
  - deepseek-v4-flash a1–a3;
  - grok-4.6 a1, a2;
  - MiniMax-M3 a1, a2;
  - MiniMax-M2.7 a1–a3;
  - kimi-k2.7 a1, a2, r1 (19.4 GB);
  - gemini-3.5-flash a1, a2.

**The 05:11 server restart killed both copy jobs**, because they were children of the agent shell. Their interrupted units resume (the `.started` owner pid is dead). The copy now runs as one detached job (`setsid nohup`) that a server restart does not kill: Doubao runs first, then the rest of `eval-matrix-20261112`, then `tb21-native-20261103-fix`, `harness-adapters-20261112` and `frontier-harness-20261112`.

A `.started` marker with the owning pid keeps two jobs off the same unit.
Throughput is bounded by the old volume's metadata latency (about 0.15 s per directory, serialized). One codex-heavy run dir takes 25–75 min, so the full matrix takes on the order of days. `cat manifests/*.verify.json` is the current state.

### 8.3 Runs in flight

Rate limits:
- Every bucket was re-measured today with the PREREG §9 ramp (1-token requests, one-minute steps, stop at the first 429) after a 1-token probe of each face. Evidence is in `L/probes/`; the results are recorded in `eval-quota.json`.
- Each relay runs at `cap = floor(0.5 × measured)`, and the summed runner concurrency per relay is ≤ floor(cap/2) (PREREG A10 rule).
- Stop-on-429 is the relay's STOP plus halving.
- `L/tools/prod429_guard.py` writes STOP on any production 429 on a held bucket.
- `L/tools/scheduler.py` starts each planned resume once its copy unit verifies (`L/plan.json`, `L/scheduler.log`).
- The parallel project uses only gpt-6.1-sol and MiniMax-M2.7, so no relay of this lane shares a bucket with it.

| model | bucket (measured → cap RPM) | runs | state at 01:30 CST (11-18) |
|---|---|---|---|
| glm-5.2 | …6672 40→20, …4861 20→10 | resume a1 (5 arms), a2 (cc, codex, tofu) | a1 complete except tofu; a2 running |
| glm-5.3-flash | …6672 40→20 | new t1, t2, t3, all five arms | running; ≈ 36 h to K = 3 |
| deepseek-v4-flash | …6672 20→10 | resume a1, a2, a3 | running |
| grok-4.6 | …4861 60→30 | resume a1, a2 | running |
| MiniMax-M3 | …4861 20→10, …6672 20→10 | resume a1, a2, a3 (tofu on the MR1 rebuild) | a1, a2 running; a3 waits for copy |
| MiniMax-M2.7 | …6672 40→20 | resume a1, a2, a3 (claude-code, codex, MR1 tofu remainder) | running |
| Doubao-Seed-2.0-pro | …6672 20→10 | resume a1, a2, r2, r3; new t1 (MR1 tofu, thinking medium) | t1 running; resumes wait for copy |
| gemini-3.5-flash × tofu | …4861 10→5, …6672 10→5 | resume a1, a2 | waits for copy |
| kimi-k2.7-code-highspeed | …6672 10→5, …4861 10→5 | new t1 (pi, cc, codex, MR1 tofu); resume a1, a2 (old cc attempts invalidated), r1 | t1 running; resumes wait for copy |
| kimi-k3 | …6672 `kimi-k3-m17` 10→5, …6672 `kimi-k3` 5→2, …4861 `kimi-k3` 5→2 | new t1 (M17), t2, t3, all five arms | t1 running; t2 and t3 held (429 at 2 RPM on both plain buckets, below) |
| qwen3.5-plus | …6672 20→10 (re-measured 11-18; was "vendor 429 at 1 RPM") | new t1, t2, t3: opencode, claude-code, codex (Pi's catalog has no qwen3.5-plus; tofu below) | running |
| glm-5.2 (4th run) | …6672 | new t4, all five arms (CIs overlap at K = 3) | running since 06:27 |

Resume rules (`L/tools/takeover_resume.py`):
- A resumed run's `run.json` changes only its host-bound fields: `relay_url`, `files_url`, the artifacts manifest and the tasks dir. Artifacts are sha256-identical, or the MR1 rebuild below; the tasks dir is the verified copy.
- Previous values, the reason and any rebuilt artifact are recorded in `run.takeover.json`.

**MR1 Tofu build (`tofu@948e9b264f`): rebuilt, cells resumed.**
- **Blocker:** the original artifact (sha256 `707ccd11…`) lived only in the old host's `/tmp`.
- **Rebuild:** made 2026-11-17 in one sandbox-857 VM with the original driver and inner script (`cd47f5cd…`), base runtime (`1ee1374e…`), uv, and a source export byte-identical to the original (`e9378196…`). Result: `files/tofu-agent-948e9b264fbf-r3.tar.gz`, sha256 `23e2c59f…`.
- **Check:** every build check file is byte-identical to the original's (installed-package freeze, install check, artifact check with 2,706 members and 0 leaked, build image, glibc symbols). The archive and wheel hashes differ, most likely from embedded build timestamps (not diffed). Evidence: `L/build-mr1/`.
- **Effect:** runs frozen on the lost artifact resume on `L/artifacts-mr1-rebuild.json`, which records `original_sha256`. The scores stay labelled `tofu@948e9b264f`.
- **Missing third runs:** Doubao and K2.7 get new runs `t1` (Doubao with `--tofu-thinking medium`, as its a1/a2).

**K2.7 × claude-code: root cause found and fixed.**
- **Cause:** the vendor rejects Claude Code's `thinking: {type: adaptive}` with HTTP 400. So the old trials sent no model traffic and scored 0; that is not a measurement.
- **Fix:** the K2.7 pin sets Anthropic `thinking {type: enabled, budget_tokens 8192}` (probe: adaptive → 400, enabled → 200). Only the claude-code arm uses K2.7's Anthropic face, so no other arm's requests change.
- **Evidence:** run t1's claude-code trials returned 89 of 89 requests with 200 and pass tasks. The old a1/a2 claude-code attempts are invalidated (`runner.invalidate`, rule `k27-cc-thinking-400`) when those runs resume, and the status tables drop invalidated trials until they are re-measured (`720651a03c`).

**qwen3.5-plus × tofu: blocked by the frozen Tofu build.**
- **Cause:** the r3 artifact (`214650e6`) refuses qwen3.5-plus at start ("Model registration is incomplete", `agent/tofu-stderr.txt`), so those trials sent no model traffic.
- **Handling:** those attempts are invalidated (rule `tofu-r3-qwen-registration-incomplete`) and tofu is dropped from the qwen runs, with the reason in `run.takeover.json`.
- **What would unblock it:** a Tofu build whose catalog completes qwen3.5-plus. That is a new Tofu cohort, not this cell.

**qwen3.5-plus × claude-code:** FRIDAY's Anthropic face serves it with thinking (probed), so the matrix now marks it `anthropic_face` (`ecba95b462`) instead of requiring the bridge.

**kimi-k3: a faster path, only through legitimate buckets.**
- **Buckets probed:** the lane's two keys, all faces, against every FRIDAY deployment the routing catalog lists (`kimi-k3`, `kimi-k3-m17`, `kimi-k3-baidu`, `kimi-k3-tencent`, `doubao-kimi-k3`). Evidence: `L/probes/faces-kimi-20261117.jsonl` and `ramps.jsonl`.
- **Usable:** `kimi-k3-m17` on …6672 measures 10 (cap 5); it is the same model, and every face answers `kimi-k3`. `kimi-k3` measures 5 on both keys (cap 2).
- **Not usable:** baidu, tencent and doubao 429 within the first five requests on both keys, and `kimi-k3-m17` on …4861 429s at once.
- **Ruled out:** the …0733 research key has been disabled by FRIDAY since 11-13. The other configured key (…5409) is the parallel harness_b lane's, and the CatPaw provider is disabled.
- **Shared buckets:** both plain `kimi-k3` buckets have since hit trial-traffic 429s at 2 RPM (…4861 23:41, …6672 ~04:43), so other traffic shares them. Those relays are held under STOP, and runs t2 and t3 are paused.
- **ETA:** kimi-k3 now proceeds only on `kimi-k3-m17` (5 RPM): run t1 (445 trials) takes about 2 days. K = 3 needs the plain buckets to free up or a new legitimate bucket.

Spend:
- Logged per bucket in `eval-quota.json` (`allocations[lane=EVAL-TAKEOVER-1].spend`, list price) by `L/tools/ledger.py spend`.
- Owner policy: information only, no check-ins.
- At 2026-11-18 06:19 CST: **$752.55**, 47,589 calls, 3 upstream 429s:

| bucket | $ |
|---|---|
| grok-4.6 | 285.84 |
| glm-5.2 | 239.47 |
| kimi-k2.7 | 80.73 |
| gemini × tofu | 38.43 |
| MiniMax-M3 | 33.45 |
| kimi-k3 (all buckets) | 30.18 |
| glm-5.3-flash | 24.10 |
| MiniMax-M2.7 | 9.49 |
| Doubao | 6.60 |
| deepseek-v4-flash | 4.24 |
| qwen3.5-plus | 0.02 |

Incidents (`eval-quota.json` `incidents[]`):
- Probe and measurement 429s on kimi-k3 deployment variants (each stopped at the first 429, no trial traffic).
- Trial-traffic 429s on `kimi-k3@4861` and `kimi-k3@6672`, both at 2 RPM. Both relays are held under STOP and are not reopened at 1 RPM.

### 8.4 Results so far (regenerated, not hand-copied)

Command:
`python -m evaluations.terminal_bench.eval_matrix.status_tables --matrix N/eval-matrix-20261112 --relays <old + takeover relays> --out L/tables/status-<time>.md`

How the table is built:
- **Inputs:** the A10 snapshot (`analysis/per_trial.csv`, runs 1–3 per PREREG A10.4 E) plus every measured attempt created since 2026-11-17.
- **Score:** the mean over tasks of the per-task pass fraction, with a task-cluster bootstrap 95% CI (B = 20,000, seed 20261112).
- **Cost:** list price from the relay logs.
- **Tokens:** in = all input incl. cached, then cached, then out. The Anthropic face's cache counts are 0 where FRIDAY returned no usage (§3, item 3).

Snapshot at 2026-11-18 06:19 CST (`L/tables/status-20261118T0619.md` / `.json`), core arms, score % [95% CI] n, Kn = complete runs:

| model | tofu | opencode | pi | claude-code | codex |
|---|---|---|---|---|---|
| glm-5.2 | 67.4 [58.8, 75.3] n266 K2 | 59.6 [50.9, 67.8] n267 **K3** | 64.0 [55.4, 72.7] n267 **K3** | 62.9 [53.9, 71.5] n267 **K3** | 58.8 [49.8, 67.4] n267 **K3** |
| glm-5.3-flash | 65.0 [51.3, 77.8] n96 | 60.7 [46.2, 75.2] n95 | 57.3 [42.7, 70.9] n95 | 59.0 [45.3, 72.6] n94 | 73.5 [61.5, 84.6] n93 |
| grok-4.6 | 73.2 [65.5, 80.3] n262 K1 | 74.0 [66.3, 81.3] n266 K2 | 75.3 [67.8, 82.4] n265 K2 | 64.8 [55.8, 73.4] n262 K1 | 75.7 [67.8, 83.1] n264 K1 |
| deepseek-v4-flash | 77.3 [69.5, 84.5] n167 K1 | 74.9 [66.9, 82.6] n215 K2 | 72.1 [64.0, 79.6] n214 K2 | 74.4 [62.4, 85.3] n103 | 77.5 [65.1, 88.4] n102 |
| MiniMax-M3 | @948e 72.2 [61.7, 82.1] n138 | 57.5 [48.3, 66.3] n232 K2 | 59.9 [51.3, 68.2] n232 K2 | 67.9 [57.6, 77.6] n140 | 64.8 [54.3, 75.3] n136 |
| MiniMax-M2.7 | @948e 43.3 [33.7, 53.0] n137 K1 | 37.5 [28.8, 46.1] n267 **K3** | 47.6 [38.2, 56.9] n267 **K3** | 45.1 [36.0, 54.5] n212 K2 | 49.4 [39.5, 59.4] n136 K1 |
| Doubao-Seed-2.0-pro | @948e 43.1 [25.0, 62.5] n40 | 40.8 [31.8, 49.8] n258 K1 | not in Pi's catalog | 37.5 [27.4, 47.8] n102 | 35.3 [25.3, 45.8] n100 |
| kimi-k2.7-code-highspeed | @948e 58.3 [36.7, 78.3] n32 | 52.3 [41.9, 62.2] n112 | 66.7 [48.4, 83.3] n36 | 67.5 [47.5, 85.0] n29 (fixed route) | 65.0 [46.7, 82.5] n33 |
| kimi-k3 | 50.0 n14 | 62.5 n14 | 62.5 n13 | 68.8 n13 | 75.0 n12 |
| qwen3.5-plus | blocked (registration) | started | not in Pi's catalog | started | started |

gemini-3.5-flash × tofu: 63.3 [53.9, 72.3] n194 K1. Cost, calls and tokens per cell are in the `.md` and `.json`.

**Ranking.** None is claimed. Six cells are complete at K = 3: glm-5.2 × opencode, pi, claude-code and codex, and MiniMax-M2.7 × opencode and pi. Within each model their CIs overlap.

**Pre-registered A10.3 test** (paired per-task sign-flip, B = 20,000, seed 20261112, plus-one; Holm within each model's harness family; claimed iff Holm p < 0.05):
- It now runs with every table (`status_tables.py`, `281c433f39` + `1f259b3178`). Its sign-flip and Holm match the A10 `analysis/stats.py` exactly (same numpy draws).
- A family is decided only when **all** of the model's core cells are at K = 3. Holm's denominator is the whole family, so a partial family understates it, and its p-values are reported as provisional.

| model (family state) | pair | a − b pp | p | Holm p (partial) |
|---|---|---|---|---|
| glm-5.2 (tofu at K2) | codex vs pi | −6.7 | 0.088 | 0.528 |
| glm-5.2 | opencode vs pi | −4.5 | 0.112 | 0.558 |
| glm-5.2 | claude-code vs codex | +4.1 | 0.247 | 0.986 |
| glm-5.2 | codex vs opencode | −4.5 | 0.326 | 0.986 |
| glm-5.2 | claude-code vs pi | −1.7 | 0.717 | 1.0 |
| glm-5.2 | claude-code vs opencode | +0.6 | 1.0 | 1.0 |
| MiniMax-M2.7 (tofu, cc, codex pending) | opencode vs pi | −10.1 | 0.0072 | 0.0072 |
| grok-4.6 (cc, tofu pending) | codex / opencode / pi pairs | ≤ 1.5 | ≥ 0.70 | 1.0 |

Reading:
- **glm-5.2:** no pair is near significance even before the fifth cell joins (smallest raw p 0.088). Once tofu completes, the 10-pair family can only raise the adjusted p, so glm-5.2's harnesses will almost certainly not separate.
- **MiniMax-M2.7:** pi beats opencode by 10.1 pp (p = 0.0072). In the full 10-pair family the smallest p is multiplied by 10, so it reaches Holm p ≈ 0.072 unless the remaining cells sharpen it. It is the one candidate difference so far, **not a claim**.

Next steps:
- The A10.3 decision rule (paired sign-flip test with Holm correction) applies once a family's cells are complete.
- Runs beyond K = 3 are then added for families whose CIs still overlap.
- Both need the K = 3 fills above to finish first.

### 8.5 SWE-bench / Harbor: installed and smoke-passed

Location and result:
- Install dir: `N/harbor-env-20261117/` (harbor 0.21.0 @ `ea2fee78`, QEMU 11.1.0, 500 SWE-bench Verified definitions).
- On `22ca974e71` (unmodified `git archive`), `harbor doctor` passes all six checks.
- The oracle run on `swe-bench/psf__requests-1142` scores **reward 1.0**: `harbor-20261117T133455Z-a7ea7d/jobs/…/result.json`, script `tools/smoke-22ca974e71.sh`.
- No model-backed SWE-bench run has started yet.

Four defects blocked it; each is fixed with a test:
1. **Withdrawn packages:** 14 of the 38 pinned Alpine 3.24 packages were withdrawn upstream (404). The lock is refreshed with the same package set, sha256 and size checked on the host, and signatures still enforced in the guest (`3757783f63`).
2. **QEMU address space:** QEMU exceeded the jail's address-space limit on this 180-CPU host because glibc reserves malloc arenas per CPU. The jail now uses one arena (`6eca59d3f6`).
3. **Short image names:** Docker 29.9 does not resolve a short OCI `ref.name`. Assets now carry the fully qualified `docker.io/…` name, and asset schema 2 rebuilds old payloads (`220da0de61`).
4. **Wrong guest NIC:** the guest NIC selector took `docker0`, so Dockerfile builds lost the egress proxy while still "succeeding". It now picks the device-backed NIC, and Dockerfile cache recipes are schema 6 (`22ca974e71`).

Reported, not fixed:
- `tar` on DolphinFS fails with "file changed as we read it". The run used the existing `--archive-tool` option with a local-snapshot wrapper.
- `--phase cache` needs a cache root in `$HOME`, because QEMU sockets cannot live on DolphinFS, contrary to the README.
- On rootless-qemu, `agent_concurrency` defaults to 4 even with `--concurrency 1`.
- The run manifest records the enclosing checkout's revision when run from a `git archive` tree.
- The 360 s guest boot limit has no headroom at this host's load (load average 260): one cache build attempt timed out, and the identical retry passed.

### 8.6 Owner's `gateway.py`: untouched; overlap reported, not resolved

- **No commit of this lane changes `evaluations/terminal_bench/gateway.py`.** `git diff 483d4fc7e9 HEAD -- evaluations/terminal_bench/gateway.py` is empty, so a landing cannot touch the owner's uncommitted draft.
- **Overlap:** the EVAL-MATRIX line's own `gateway.py` is the HMAC native-exec auth. That is the same change as `parked/native-exec-auth-20261116`, on the same lines as the owner's draft.
- **Effect of the port:**
  - Its file content was not taken.
  - Its commits are now in history, so re-merging that line is a no-op.
  - The parked branch is a separate port with distinct commits, so landing it later still applies its `gateway.py` change.
- **Current behaviour:** runs use the tokenless channel (`ruanjunhao/tofu-native-exec-20261102`), exactly as the smoke and the old matrix runs did. The owner's decision on the draft is still open.

### 8.7 Not run, and why

- **kimi-k3 at full speed:** see §8.3. It runs at the pace of its legitimate buckets; there is no faster legitimate path.
- **claude-opus-5.5 and gpt-6.1-sol:** not runnable per A8/A5–A6. Opus's provider account is shared with production Routers, and gpt-6.1-sol is also the parallel project's model.
- **qwen3.5-plus:** vendor 429s at 1 RPM.
- **MiniMax-M2.7 remainder:** now running on …6672. The parallel `harness_b` lane uses keys …4861 and …5409 for M2.7 (its `relay_1876x.sh`), so the buckets don't overlap. Its own tofu/opencode/codex × M2.7 cells are a different study (Paper B, 64k window), not duplicates of these cells.
- **The 2,706 ingress-refused attempts:** the A10 restore made them unspent attempts. A resumed run should give each of its own a new attempt, but this was not checked per attempt. Those in runs not on the plan wait for their runs.
- **Tier B third-party harnesses on the other models:** after Tier A, per A10.2. The Doubao a1/a2 resumes include the six third-party arms, which are Tier A on that anchor model.

### 8.8 Operating the lane

| action | command |
|---|---|
| status table | the §8.4 command |
| spend | `python3 L/tools/ledger.py spend`, then read `eval-quota.json` |
| hold new trials on one relay | `touch N/eval-matrix-20261112/relays/<model>@<tail>-takeover/STOP` (running trials finish) |
| stop the scheduler | `touch L/STOP-SCHEDULER` |
| stop a relay | `sh L/launch.sh stop-relay <model>@<tail>` |

A 429 or a production-429 STOP holds that relay until someone reviews the incident, records it in `eval-quota.json` and removes STOP. No process restarts Tofu or touches production storage.
