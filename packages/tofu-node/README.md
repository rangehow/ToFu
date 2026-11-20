# tofu-node — the Tofu node program in Rust

`tofu node` is the node program of the multi-device design
(docs/modules/remote_execution.md, "Nodes and pairing" and "Device routing and
permissions"): every machine runs this one program and is an equal node of each
Home it joined; there is no separate controlled end. It replaces
`lib/desktop_agent` (Phase 6 of docs/RUST_REWRITE_EVIDENCE.md) and speaks the
same wire contracts, embedded from `contracts/` at build time:
`desktop_node_v1` (pairing, tiers, attachments), `desktop_bridge_v3` (poll
frames, limits, replay) and `device_routing_v1` (journal retention).

```
tofu node --join 'https://home.example/#tofu-pair=…'   # join (kind=server), then serve
tofu node --join 'https://home.example 123456'          # the same pairing as its code
tofu node                                               # serve every joined server
tofu node --server URL --bridge-token TOKEN             # one explicit server
          [--allow-write] [--allow-exec] [--allow-all] [--root NAME=PATH] [--poll-interval S]
tofu node --tray [--join …]                             # system tray (src/tray/)
TOFU_DESKTOP_AGENT_TOKEN=… tofu node --local-server URL # packaged app's local link
```

The tray (`tofu node --tray`) lists every joined server with its link verdict
(connected, through the browser, pending + fingerprint, revoked, unreachable,
proxy), joins a server through a native input dialog (zenity/kdialog,
osascript, PowerShell), leaves one, switches Computer Control and the
write/exec/gui/egress/codex_login tiers (in place, persisted in the config's
`computer_control` blob like the Python launcher) and opens a server's
`#tofu-agent-relay` page. Its strings are `desktop/_tk_theme.py` STRINGS,
embedded by build.rs. Linux: StatusNotifierItem + dbusmenu over D-Bus (`ksni`,
pure Rust); macOS/Windows: `tray-icon` (compile-checked only).

The arguments, config file (`TOFU_DESKTOP_CONFIG`, else
`~/.tofu/desktop_agent.json`, locked through `<path>.lock`), per-host slots and
call journal (`<config>.calls/`) are the Python node's, so either program can
take over a host and twins can share one file.

## Invariants

- Confirm before join: a redeemed pairing is pending; the node polls, shows the
  fingerprint and receives no work until the Home's owner confirms it.
- Read-only by default: only `read` unless a tier is switched on; the Home's
  ceiling starts read-only too. A command runs only when its tier is allowed by
  the Home (`home_tiers` on the command) AND the node's switch
  (`src/permissions.rs`).
- One node ID per host (`src/host_identity.rs`, `src/config.rs`): the slot key
  digests the machine ID, the hostname on Linux and the container ID, so
  containers sharing `$HOME` get distinct node IDs and credentials; the digest
  is identical to the Python node's.
- Outbound only: one poll loop per joined server (`src/links.rs`,
  `src/poll.rs`); 401/409 refusals end the link; reconnects back off with
  jitter (60 s cap). Each remote link opens a loopback browser relay
  (`src/relay.rs`, 127.0.0.1:15180–15189, admitting only that server's
  origins): while a signed-in Tofu page carries it, polls ride the page (route
  `browser_relay`), else go direct; behind an SSO edge the page also redeems
  the pairing. The local link (`--local-server`) has no relay and keeps
  retrying on refusal. Home relaying is later work.
- At-most-once side effects: non-read commands are journaled before they run
  (`src/journal.rs`). Codex-login and egress completions retain only a
  `sensitive_done` tombstone, not results/errors: retries explicitly refuse
  rather than reveal the old result or silently execute again. The Python
  journal uses the same format. Legacy sensitive records are redacted on
  open without extending retention; see the sensitive-journal decision in
  `docs/modules/remote_execution.md`.
- No pools: each command runs on its own thread; nothing is sized from CPU or
  memory. Commands run under a supervisor (`tofu node __supervise`, the same
  binary) that retires the whole process tree when the node dies (EOF on its
  stdin) or the command exits: Linux as child subreaper, Windows by a
  kill-on-close Job object. macOS has no subreaper, so every Unix command also
  inherits `TOFU_NODE_TREE=<cookie>` and retirement kills every same-user
  process whose environment carries it (`KERN_PROCARGS2`), which catches
  detached (`setsid`, double-fork) grandchildren. Limit: on macOS a
  descendant that both leaves the process group and scrubs its environment
  survives (`tests/exec.rs`: `cookie_sweep_kills_detached_grandchildren_outside_the_ancestry`,
  `sigkill_of_the_owner_retires_every_descendant`).

## Coverage

Every command of `desktop_node_v1` is implemented
(`commands::tests::every_contract_command_is_implemented`); a tier is declared
to the Home exactly when the node's switch allows it.

## GUI

`desktop_screenshot`, `desktop_gui_action` and `desktop_clipboard`
(`src/commands/gui.rs`) are the port of `lib/desktop_agent/_gui.py` and
`_scaling.py`: same params and result fields, XGA downscale, pyautogui's
tween, fail-safe and pause. Linux uses X11 in pure Rust (x11rb: `GetImage`,
XTEST; Wayland only through XWayland), macOS Core Graphics, Windows GDI and
`SendInput`; the clipboard is arboard, whose X11 owner thread serves the
CLIPBOARD selection for the life of the node. Without a display every GUI
command answers `gui_unavailable: ...`. Differences from the Python node:
`locate` matches on grayscale (normalized cross-correlation, pyautogui's
`confidence` semantics); an unknown `hotkey` key is an error instead of being
ignored; text outside the keyboard map is typed (X11 spare keycode, Unicode
key events on macOS/Windows); a Windows scroll click is one wheel notch.

## Egress, adapter, Codex login

`egress_http`, `egress_http_stream` and `egress_cancel`
(`src/commands/egress.rs`, `egress/wire.rs`) port `lib/desktop_agent/_egress.py`:
the node's own host whitelist, the loopback class limited to its adapter's
port, body/response caps, GET/POST, proxy discovery (env, Windows registry,
`scutil --proxy`, direct; never for loopback), frames meta → base64 body
(64 KiB) → error and one outcome. It uses its own HTTP/1.1 client so a cancel
aborts even a silent upstream, and never follows redirects. `adapter_*`
(`src/commands/adapter.rs`) supervise the CLIProxyAPI sidecar like
`_adapter.py` and resume it with an egress-enabled link. `codex_login_read`
(`src/commands/codex_login.rs`) ports `lib/oauth/codex_local_login.py`
(native `codex app-server` read, renewal, `CODEX_HOME` file fallback; no
refresh token in results; one-shot, no warm app-server).

## Paper builds

`paper_resolve`, `paper_build` and `paper_build_cancel`
(`src/commands/paper.rs`, `paper/`) port `lib/desktop_agent/_paper.py` and
the device side of `lib/paper/builds` (contract `paper_build_v1` embedded;
source snapshot, toolchain probe, latexmk under the supervisor, diagnostics,
citation audit, SyncTeX location, manifest bytes as Python's `json.dumps`).
The engine comes from `TOFU_TEX_ROOT` or PATH. The node checks only the PDF
header/trailer; the Home's strict PDF validation still runs on receipt.

## Verify

```
cd packages/tofu-node
cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --check
cargo check --target x86_64-pc-windows-gnu && cargo check --target x86_64-apple-darwin
```

Against the Python Home (builds the binary through `tests/support/tofu_node.py`):
`pytest -m slow tests/desktop/test_rust_node_interop.py
tests/desktop/test_rust_node_browser_relay.py tests/desktop/test_rust_node_tray.py
tests/desktop/test_device_routing_faults_slow.py tests/desktop/test_rust_node_gui.py
tests/desktop/test_rust_node_egress.py tests/desktop/test_rust_node_codex_login.py
tests/desktop/test_rust_node_paper.py` (GUI needs Xvfb, the real-engine paper case
TinyTeX, the tray test dbus-daemon; each docstring says how to provision it) and `make test-chaos`
(`tests/chaos/test_chaos_nodes.py` runs every fault for both node programs).
