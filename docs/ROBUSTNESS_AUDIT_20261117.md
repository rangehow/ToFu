# Robustness audit 2026-11-17: fault injection through the two-process runtime

Scope: prove by injecting faults that Tofu survives the failures top agent
harnesses handle, and fix what breaks (owner request 2026-11-17; robustness
lane). The suite, its isolation and the per-fault contract are defined in
[defensive patterns](DEFENSIVE_PATTERNS.md#fault-contracts-of-the-chaos-suite);
this report records what it found. It is a dated audit: once its findings are
reflected in their owning documents, delete it rather than extending it.

## Method

`tests/chaos/` starts an isolated instance per xdist worker: a resident storage
host (`NativeStorageService`, SQLite authority), the resident execution host
(`ExecutionService` running `serverctl_pkg.execution_host`) and a separate API
host (`server.py`, `TOFU_PROCESS_ROLE=api`). Each has its own data root, port,
`HOME` and `TMPDIR`, and an allowlisted environment tagged with
`TOFU_CHAOS_INSTANCE`, so the leak scan finds descendants by environment.
Turns enter through the public v4 API. The only model is a scripted Anthropic
endpoint. Every process runs under `benchmarks/agent_storage/faultfs.c` over
the data root, with DolphinFS's ENOENT-on-unlinked-file semantics on for every
run, and ENOSPC or barrier latency armed per test. No live host was touched or
restarted.

Coverage (21 cases, `make test-chaos`, 2 min 35 s on 4 workers):

| Fault | Test |
|---|---|
| Baseline tool round trip | `test_baseline_tool_round_trip` |
| Stream drop mid-response, 429, 503, 500, malformed SSE | `test_transient_provider_fault_before_any_effect[*]` |
| Persistent provider 5xx | `test_persistent_provider_outage_waits_visibly_until_stopped` |
| Invalid tool-argument JSON | `test_invalid_tool_json_is_answered_without_running_the_tool` |
| Tool subprocess killed / hung and stopped | `test_tool_subprocess_killed_mid_call`, `test_hung_tool_is_stopped_by_the_owner` |
| MCP server added at runtime, crash mid-call and upgraded restart, tool disabled | `test_mcp_*` |
| Execution host SIGKILL mid-tool and mid-stream, recovery | `test_execution_host_killed_*` |
| API host restart mid-stream, `Last-Event-ID` resume | `test_api_host_restart_mid_stream_resumes_the_client_stream` |
| Client disconnect during streaming | `test_client_disconnect_does_not_cancel_the_turn` |
| Storage host frozen; killed and restarted | `test_storage_host_frozen_mid_turn`, `test_storage_host_killed_mid_turn_and_restarted` |
| Disk full; slow barriers on the data root | `test_disk_full_on_the_data_dir`, `test_slow_storage_medium` |

Every case asserts C1-C5; the module teardown asserts that no tagged process
and no held lock file outlives the instance.

## Findings

Each finding was recorded as a failing specification before its fix.

| ID | Severity | Finding | Failing case first | Fix |
|---|---|---|---|---|
| R1 | Medium | The agent-loop load test was broken by today's DolphinFS fix (`9d98324471`): `tofudb_load_test._child_environment` gained a `scratch` parameter, and `agent_loop_load_test._driver_environment` kept calling it positionally, so `TOFUDB_BIN` became the digest and `TMPDIR` the engine path. Its spec passed by accident because the engine path lay under the root. | `tests/tofudb/test_agent_loop_load_test.py` now pins `TOFUDB_BIN`, the digest and a host-local `TMPDIR` outside the root (`TypeError` before the fix). | `6cc78c1867`: pass a host-local scratch from `serverctl_pkg.local_placement.temporary_root`. |
| R2 | Medium (C5) | Shutdown orphaned the MCP vendored pre-warm. The startup worker ran each `uv run … import <server>` warm with `subprocess.run(timeout=300)`, and stop only set a flag and waited 2 s, so a cold warm outlived the API and execution hosts and kept `data/mcp-cache/uv/.lock` for the next boot. Found by the instance leak scan on the first boot. | `tests/mcp_protocol/test_mcp_startup_lifecycle.py::test_stop_retires_a_running_prewarm_process_tree` (stop returned `False`, the grandchild survived). | `8a325c4cd4`: the warm runs as an owned process group retired through `process_group.retire_process_group` on completion, deadline or the worker's stop event. |
| R3 | Low (C4) | A structured `config.model` was accepted by create-turn, persisted an Attempt, and failed only at executor start with the raw `'dict' object has no attribute 'lower'`. | `tests/conversation/test_turn_config_model_boundary.py` (Turns were created). | `37b414a5c1`: a typed 400 for `config.model` naming `config.modelRef`, before any Turn exists. |
| R4 | High | MCP management never reached the execution host. Connect, disconnect, remove and catalog install acted on the API host's bridge, but Turns call tools through the execution host's, which auto-connects only its boot-time config. A server added at runtime answered `tool_not_enabled` with no candidates in every Turn until the execution host restarted; a disconnected or removed server kept serving Turns. | `tests/chaos/test_chaos_faults.py::test_mcp_server_added_at_runtime_serves_execution_host_turns` (180 s of `tool_not_enabled`). | `37b414a5c1`: owner method `mcp_reconcile` over the execution command control, queued on the execution host's owned MCP startup worker ([decision](modules/tools_execution.md)). |
| R5 | High (authority) | A tool the owner disabled in Settings was refused only by the API host's bridge; Turns in the execution host kept offering and running it. | `test_mcp_tool_disabled_in_settings_is_refused_in_execution_host_turns`. | `37b414a5c1`: `mcp_reconcile` action `tools` applies the stored `disabled_tools` grant without reconnecting. |
| R6 | Medium | Stale MCP schemas after a server restart (today's incident). After a crash mid-call the bridge kept the dead process's catalog until the keepalive sweep, so an upgraded server's calls were validated against the old schema and the model was told `missing argument: note`. | `test_mcp_server_crash_mid_call_then_restart_with_a_new_schema`, failing with the fix reversed; `tests/mcp_protocol/test_mcp_disconnect_recovery.py::test_a_dead_transport_refreshes_the_catalog_without_replaying_the_call`. | `37b414a5c1`: a non-replayable call on a dead transport reconnects (re-listing tools) before re-raising; never replays ([decision](modules/tools_execution.md)). |
| R7 | Medium | Found while fixing R4: the command-control transport validated every owner method's result as the background-activity shape and every request's params as empty, so a new owner method could never be acknowledged. Unit specs that mocked `call` passed; only the real two-process run exposed it. | `tests/tasks_pkg/test_command_control_transport.py::test_mcp_reconcile_round_trips_its_scheduling_verdict` and the table-driven malformed-result cases. | `37b414a5c1`: explicit request and result shapes for `mcp_reconcile`. |

## Behaviors confirmed, not defects

The suite's first expectations were corrected against the owning contracts:

- A persistent provider 5xx does not fail the Turn. It waits on the shared
  backoff schedule (10 requests in 120 s observed) with a visible `retrying`
  phase until the owner stops it ([LLM I/O](modules/llm_io.md), "Unreachable
  endpoints"). Failing fast was rejected there.
- Invalid tool-argument JSON regenerates the round rather than feeding back an
  error result; the malformed call never runs.
- After a killed tool and a text-only reply, the loop's
  `[SYSTEM: UNRESOLVED TOOL FAILURE]` nudge asks for one more round. A model
  that repeats itself commits its text twice; a real model ends with
  `[END_TURN: …]`.
- Execution-host SIGKILL during a tool: recovery retired the orphaned group
  and the side effect ran exactly once. API restart, client disconnect, storage
  freeze/kill, ENOSPC and slow barriers all settled without lost content.

## Seed incidents of 2026-11-17

| Incident | Status |
|---|---|
| Stale MCP schemas after a server restart | Reproduced (R6) and fixed; the same process split also produced R4 and R5. |
| DolphinFS fails I/O on open-but-unlinked files | Every chaos run keeps `FAULTFS_UNLINKED_ENOENT=1` over the data root; no case failed under it. The day's fix regressed the agent-loop load test (R1, fixed). |
| Chromium socket path too long | Fixed upstream for task checkouts (`9a9c0dea08`). Not exercised here: the two-process path binds no Tofu-owned AF_UNIX listener (command control is loopback TCP). Residual: this Worker's own `TMPDIR` is still 125 bytes, over the 108-byte `sun_path` limit, because the live host was deliberately not restarted. |
| Coordinator notice Turns with no authority | Not exercised: the suite drives ordinary chat Turns. Fixed upstream (`eb7dfd789b`, `nonhuman_request` refusal); not re-verified here. |

## Open items

- Every execution-host stop logs `execution exit evidence for pid … was not
  recorded: process exit is not proved by local kernel evidence`. This was
  observed in each instance teardown but not investigated.
- Pre-existing failures, confirmed identical on a clean checkout of the shared
  branch tip:
  - `tests/tools/test_tool_search_loop_breaker.py`: 2 cases, `task owner requires a numeric user_id`.
  - `tests/conversation/test_usage_recovery.py::test_unknown_write_is_checked_against_real_file_publication_evidence`: 5 cases.
  - `tests/conversation/test_conversation_steer_reply_quotes.py`: 2 cases.

  `tests/meta/test_gitignore_covers_export_excludes.py` also fails on six
  `scripts/` files this lane does not touch.
