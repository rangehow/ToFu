# Server-Built AGENT-ONLY Installers — Design

> Status: DRAFT v1 (2026-08-02), epic `pt_59b62951aad2463e`.
> Splits the fused "one installer, two roles" distribution into TWO
> components: the controlled-machine **agent** (no frontend, no server
> stack) and the full desktop app (this machine = server + client).
> Sibling of `DESKTOP_CLIENT_BUILD_DESIGN.md` (same toolchain, same
> store, same NSIS authoring); depends on the egress epic
> `pt_4ea6bf05deaa46f0` only for the curl_cffi note in §4.4.

## 1. The agreement and the gap

Owner directive (2026-08-02, this epic's conversation):

> "There are two installable components: one is the medium for the server
> to control client computers (including bridges for OAuth subscription
> login); the other is a complete client for Windows users who use their
> local machine as both server and client. I believe they should be
> separated — the former hardly requires any frontend interface;
> configuration capability is sufficient."

The audit behind that directive (all verified in-tree 2026-08-02):

| Role | Code | Distribution |
|---|---|---|
| Controlled machine (agent) | `lib/desktop_agent/` — standalone-clean package, CLI entry `python -m lib.desktop_agent` (`_run.py::main`), zero frontend | **NONE.** Obtainable only by installing the FULL desktop app, or by copying the whole repo to the machine and running the module from source |
| Full desktop (server + client) | `desktop/launcher.py` + `tofu.spec` — PyInstaller-frozen Quart server + frontend + DB + tray, with an in-process agent thread | `Tofu-Setup-<ver>-win64.exe`, ~152.9 MB, server-built (`lib/desktop_dist/winbuilder.py`), served from Local Control |

The fusion is a **distribution** fact, not a code fact — and it has three
concrete costs today:

1. **A machine whose ONLY role is to be controlled must install a second
   entire Tofu.** `tofu.spec` bundles `collect_submodules('lib')` (the
   whole server + route tree), Hypercorn, psycopg2, playwright,
   trafilatura, pymupdf — 152.7 MB / 3316 files measured (S2,
   2026-08-01) — so that `launcher.py` can spawn a Quart child process
   and open a browser. A relay/egress-exit machine needs none of it.
2. **Local Control steers the controlled-machine case to the full
   installer.** `_lcRenderDesktop`'s remote branch (the ONLY branch that
   needs a token) offers `downloads[]` containing exactly one kind of
   artifact — the full app — and says "在你自己的电脑安装桌面版，再连过来".
3. **The egress design already ran up the debt.** `DESKTOP_EGRESS_DESIGN
   .md` §11 marks "拷贝 chatui 仓库到办公机，pip install requests" as the
   *temporary* path "桌面安装包就绪前" — and its §5 note plans an "agent
   打包流程" that was never built. The egress epic's deploy target is
   precisely the machine this epic's component serves.

## 2. Measured constraints (all verified on this host / in tree, 2026-08-02)

| Fact | Consequence |
|---|---|
| Module-level import sweep of `lib/desktop_agent/`: `requests`, `lib.log` (stdlib-only), `lib.json_store`; `pyautogui` / `pyperclip` / `psutil` / Pillow are **guarded optional** imports (`_gui.py:38-53`, `_exec.py:92-95`). `config.py` adds only `lib.json_store` | The frozen agent needs NONE of Quart/Flask/Hypercorn/psycopg2/playwright/trafilatura/pymupdf/lxml/bs4/mcp — i.e. none of the 152 MB bulk. The size claim is an import-graph fact, not a hope |
| Full payload measured 152.7 MB / 3316 files (S2 payload, 2026-08-01) | Agent-only onedir estimate ≤ ~45 MB (Python 3.12 runtime + requests + pyautogui stack + pillow + psutil + pystray/pywin32 + tkinter + optional curl_cffi). **Estimate, to be measured in slice A1** — the toolchain is provisioned, weighing is one build |
| Userspace Wine toolchain + native NSIS are provisioned and proven (`wintoolchain.py` S1–S3, epic `pt_ce4261579c1b4c64`); `installer.nsi.tmpl` + `test_installer_parity.py` exist | The agent build is a SECOND PyInstaller target on the SAME toolchain. Zero new infrastructure; the parity-contract pattern already absorbs "two authorings, one contract", so "two components, one contract" is the same move again |
| The preseed contract is launch-side: `preseed_server.json` next to the exe, one-shot, non-secret (URL only), never overrides an existing attachment (`launcher.py::_import_preseed`) | Reusing it for the agent costs moving one small function to a shared module |
| `launcher.py::_prompt_connect_line` + `desktop/_tk_theme.py` are a dependency-light config UI (tkinter — shipped by the python.org Windows Python the payload already embeds); `lib/desktop_agent/config.py::parse_connect_line` owns the connect-line wire format | The agent's entire "configuration capability" (owner's words) already exists as ~100 lines of tkinter. The dialog parses through the same owner of the format, so UI and wire can never drift |
| `desktop_agent/config.py` persists enabled-state + permission tiers via `save_computer_control` / `load_computer_control`, read identically by the full app's tray | One config floor for BOTH components: a machine that later installs the full app (or replaces it with the agent) inherits the owner's permission choices instead of re-asking |
| `curl_cffi` is NOT yet imported by `_egress.py` — the egress design lists it as a planned OPTIONAL dependency (TLS fingerprint, §5 note) | The agent spec declares it as an optional hidden import **now**, so the day the egress epic lands the dependency the packaging needs no change |

What is deliberately NOT reused from `launcher.py`: the server spawn, the
browser auto-open, the component manager, the GitHub update check, and the
"no remote attachment ⇒ poll my own local server" default. An agent-only
build HAS no local server — a missing attachment is a first-run dialog,
not a silent default.

## 3. What "separated" compiles in

Four axes, mirroring the client-build doc's discipline:

1. **Component kind** — manifest entries gain `kind: 'full' | 'agent'`
   (absent ⇒ `'full'`, back-compat). Selection is EXPLICIT (route
   parameter / UI branch), never inferred from the UA — a Windows visitor
   can legitimately want either.
2. **Version currency** — built from `git archive HEAD`, same snapshot
   discipline as the full build (a build never carries uncommitted
   working-tree state). The agent payload cache keys on the AGENT's own
   deps stamp (its much smaller requirement set), so agent payload reuse
   across full-app-only dependency bumps is even better than today's.
3. **Preseed** — identical mechanism; the server URL comes from the build
   request context, never guessed. The token still arrives via the minted
   connect line (Phase 2 per-user token-baked installers stay the
   separate, already-designed epic).
4. **Egress readiness** — `curl_cffi` hidden-imported when present (§2).

## 4. Architecture

### 4.1 `desktop/agent_launcher.py` — the agent's process (~150 lines)

One file, four acts, no server anywhere in it:

```
main():
  1. _import_preseed()            # shared seam, moved not copied (§4.2)
  2. attachment = remote_server() # lib.desktop_agent.config
     if none: _prompt_connect_line()   # shared seam; cancel ⇒ exit 0
  3. perms = load_computer_control() merged over safe_default()  # deny-all floor
  4. run_agent(url, perms, bridge_secret=secret, stop_event=tray_stop)
     # in the FOREGROUND — the agent loop IS this process
```

Plus a **minimal pystray tray** (this is the "configuration capability",
the whole of it): `[Server: <url>]` (disabled label — the silence gap the
full app's tray already fixed), `[Connect to a different Tofu…]`,
`[Permissions ▸ write / exec / gui / egress]` (live-mutating the shared
perms dict, persisted on click — the same toggle mechanics as
`launcher.py`), `[Quit]`. `console=False`, all diagnostics through a
null-safe `_log` teeing to `<exe>/data/desktop-agent.log` — the
windowed-build logging discipline is copied verbatim from `launcher.py`
(its docstring item 1 exists because this exact trap bit before).

### 4.2 Shared seams — `desktop/connect_ui.py` (NEW, ~200 lines, a MOVE)

`_import_preseed` and `_prompt_connect_line` move out of `launcher.py`
into `desktop/connect_ui.py`; both launchers import from there. A move,
not a copy — two copies of the connect dialog or the preseed contract
would drift, and the parity-suite philosophy of this subsystem is that
the CONTRACT, not the duplication, is the single source of truth.
`launcher.py`'s call sites are otherwise untouched (the full app's
behaviour is byte-identical; its suite proves it).

### 4.3 `tofu-agent.spec` — the second PyInstaller target

- Analysis on `desktop/agent_launcher.py` only.
- hiddenimports: `collect_submodules('lib.desktop_agent')` +
  `lib.log`, `lib.json_store`, `lib.runtime_paths` + `requests` +
  `pystray`, `PIL.Image`, `psutil`, the pyautogui platform stack
  (`pygetwindow/pyscreeze/pytweening/mouseinfo/pyperclip`) + `tkinter` +
  `curl_cffi` when importable (§3.4).
- excludes: the full-app list PLUS the entire server stack — hypercorn,
  quart, flask, psycopg2, playwright, trafilatura, pymupdf, lxml, bs4,
  mcp. The exclude list is load-bearing: the closure must PROVE itself
  small. The smoke gate (§4.5) asserts it.
- No `static/`, no `index.html`, no `browser_extension` datas — the only
  data file is the icon set for the tray + wizard.

### 4.4 `winbuilder.py` — per-target Half A, parametrized Half B

- **Half A** gains a `target ∈ {'full', 'agent'}` dimension:
  `payload-<target>-<git_sha>-<deps_stamp>.tar.zst`. The agent target's
  pip recipe is the agent closure only (pyinstaller + requests + pystray
  + pillow + psutil + pyautogui + pyperclip + optional curl_cffi) — the
  build is minutes, not the full build's half hour, and its deps_stamp
  ignores server requirements entirely.
- **Half B**: `installer.nsi.tmpl` gains `@APP_NAME@`, `@APP_EXE@`,
  `@INSTALL_DIR_NAME@` placeholders. The full build renders the CURRENT
  values (`Tofu` / `Tofu.exe` / `$LOCALAPPDATA\Programs\Tofu`) — the
  parity suite is EXTENDED to pin both renderings, not rewritten. Agent
  artifact: `TofuAgent-Setup-<ver>-win64.exe`,
  `TofuAgent.exe`, `InstallDir $LOCALAPPDATA\Programs\TofuAgent`
  (side-by-side installs never collide), recorded with `kind: 'agent'`,
  `source: 'built'`, plus the preseed metadata — same `record_artifact`
  call shape as today.
- The preseed write (`_write_preseed`) is already target-agnostic — it
  drops the file next to whatever exe the payload carries.

### 4.5 Smoke gate — exit code as verdict, same discipline as TOFU_SMOKE

The full build's `TOFU_SMOKE=1` exists because "the process stayed alive"
is green by construction for a windowed binary. The agent target gets the
same gate with the agent's own assertions, run under `xvfb`/wine exactly
like S2:

```
TOFU_AGENT_SMOKE=1  ⇒  import lib.desktop_agent; assert COMMANDS non-empty;
                       assert 'quart' not in sys.modules and no quart/flask/
                       hypercorn module file exists in the bundle tree;
                       print TOFU_AGENT_SMOKE_OK version=<v> commands=<n>;
                       exit 0 — any failure: traceback + exit 1
```

The import-graph assertion is the frozen-build proof of §2's core claim:
if a future change drags the server stack into the agent closure, the
build goes red, not the user's machine.

### 4.6 Serving — the `kind` axis through the store

- `scripts/release_assets.py`: agent rows live in a NEW
  `AGENT_PLATFORM_ASSETS` table (`('windows', 'x86_64', 'Windows agent
  installer', 'TofuAgent-Setup-*-win64.exe', <min_bytes measured in A1>)`)
  — NOT in `PLATFORM_ASSETS`, so the CI-release parity checks iterating
  that table (GitHub releases carry only full installers) are untouched.
- `store.find_for_platform(os_key, arch, kind='full')`: candidate filter
  gains `entry.get('kind', 'full') == kind`. Default `'full'` ⇒ every
  current caller behaves byte-identically; absent-`kind` legacy entries
  read as `'full'` — zero manifest migration.
- `routes/api_v1/desktop.py`: `_request_platform_downloads` gains the
  kind parameter; the status payload's `downloads[]` entries carry
  `kind`. The remote-case branch requests `kind='agent'` first and
  includes the full installer as the secondary option. The download route
  itself is unchanged (manifest-key serving is already
  component-agnostic).
- The mirror never supplies agent artifacts (GitHub releases have none),
  and `remove_not_in` already never prunes `source='built'`.
- Autobuild: same `TOFU_DESKTOP_DIST_AUTOBUILD` gate, extended — a
  Windows visitor hitting the agent surface with no built agent artifact
  kicks the agent build (stale-while-build ⇒ they get the full installer
  with a note, never a dead end).

### 4.7 Local Control UI (`static/js/local-control.js`)

The remote/egress setup branches render the agent download as the
PRIMARY link — "受控端 · 轻量 (~size)" with the `服务器直连 · built`
provenance chip — and the full installer as a one-line secondary:
"这台电脑也要跑 Tofu 本体？下载完整桌面版". The `local_source` and
`tray` branches are unchanged (their instruction is already correct).
The i18n keys follow the existing `local.desktopDownload*` family.

## 5. Security posture

- **Subtraction, not addition.** The agent build adds no new trust
  decision to the controlled machine: no listening port, no DB, no
  browser auto-open, no outbound GitHub probe. Everything it does, the
  full app's agent thread already does today — minus 100+ MB of code
  that machine never needed.
- Token handling unchanged: the bridge token travels via the minted
  connect line only; the preseed carries the non-secret URL only.
- Permission floor unchanged: `safe_default()` deny-all; tier toggles
  persist through the same `save_computer_control` the full app reads —
  switching components on one machine never silently widens or drops a
  grant.
- The egress whitelist stays server-side; the agent remains a dumb
  executor of an allowlist it does not own.
- Unsigned installer — same SmartScreen note as the full build (signing
  stays the separate, human-credentialed question).

## 6. What stays fused, deliberately

The full desktop app KEEPS its in-process agent: for the standalone user
(this machine = server + client), own-machine computer control with zero
configuration is a feature, and the tray tier-toggles are its config
surface. "Separated" is a distribution fact. The code stays ONE tree with
shared seams (connect dialog, preseed, tk theme, config file, NSIS
template, parity contract) — the components diverge in what they SHIP,
never in what they KNOW.

Alternatives considered and rejected:

- **Zip + script instead of an installer** — the NSIS wrapper is the ~1
  minute cheap half and buys uninstall, shortcuts, and the parity
  contract for free. A zip saves nothing worth having.
- **ONE installer with a components checkbox** — the 152 MB is the
  payload, not the wrapper: a checkbox in one installer still ships every
  byte to every machine. The saving this epic exists for lives in the
  second PAYLOAD, which a checkbox cannot produce.

## 7. Slices

- **A1** — `desktop/connect_ui.py` extraction + `desktop/agent_launcher.py`
  + `tofu-agent.spec` + agent Half A in `winbuilder.py` (`target='agent'`)
  + the §4.5 smoke gate. Tests fake the pipeline exactly like
  `test_winbuilder.py` (staged provisioning, faked downloads); NEUTER on
  the payload cache-hit logic. Acceptance: a REAL agent payload built on
  the provisioned toolchain, **weighed** (the §2 estimate becomes a
  measurement), smoke exit 0 under wine, import-graph proof green.
- **A2** — NSIS template parametrization + Half B target support +
  `test_installer_parity.py` extended to both components + first real
  `TofuAgent-Setup-<ver>-win64.exe` recorded `kind='agent'` in the store.
- **A3** — serving + UI: `AGENT_PLATFORM_ASSETS`, `find_for_platform`
  kind filter, status payload, `_lcRenderDesktop` two-link rendering,
  autobuild gate. Parity suite for the `downloads[]` shape (the
  api-contract discipline: shape pinned by test); frontend JSDOM harness
  for the primary/secondary link branches.
- **A4** (docs only) — `desktop/README.md` + `DESKTOP_EGRESS_DESIGN.md`
  §11: retire the "copy the whole repo" stopgap in favour of the agent
  installer.

## 8. Acceptance

A Windows visitor to Local Control (remote case) is offered
`TofuAgent-Setup-<current>-win64.exe` marked `服务器直连 · built` as the
PRIMARY download. Installed, it is roughly a third (measured in A1) of
the full installer's footprint; on first run it asks for one connect
line (or finds the server preseeded), shows a tray icon with exactly the
permission tiers, and appears in `/api/v1/desktop/devices` — with no
Tofu server, no DB, and no browser ever starting on that machine. The
full installer remains one click away for the "this machine also runs
Tofu" case, on a byte-identical pipeline to today.
