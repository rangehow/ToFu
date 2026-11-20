# tofu-manager guidance

Rust lifecycle manager, Phase 2 of `docs/RUST_REWRITE_EVIDENCE.md` ("Phase 2
progress"). It replaces `supervisor.py` + `server_manager.LifecycleManager`
behind the same HTTP protocol, state file and `logs/server-manager.log` layout;
the Python manager stays the default until the owner sets `TOFU_MANAGER_BINARY`.

- Behaviour parity is defined by the Python owner. Change both, or change the
  Python one first and port it; never let the two drift silently.
- Every refusal is decided before `stop_locked(.., keep_desired_running=true)`
  in `LifecycleManager::restart` (the commit point); after it, failures go to
  `replacement_failed` and the failure budget, never to a latch.
- Python policy is reached only through `serverctl_pkg/manager_bridge.py`; add
  an operation there rather than spawning other Python entry points.
- Threads: never size anything from the host; requests get a thread each.
- Worker leases (`runtime.rs`) and checkout ownership (`ownership.rs`) are
  ports with the Python owners' exact rules; parity faults that cannot be
  monkeypatched into a process are injected by `parity_shim.c` (`LD_PRELOAD`).
- Verify with `nice -n 19 ionice -c3 cargo test|clippy -p tofu-manager`,
  `cargo fmt -p tofu-manager --check`, then
  `tests/server/rust_manager_parity` (both implementations) and update
  `tests/server/rust_manager_parity/mapping.json` when coverage changes.
