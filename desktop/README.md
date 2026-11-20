# Tofu Desktop Builds

Standalone installers for Windows, macOS, and Linux — no Python, no conda, no terminal needed.

**One artifact (docs/modules/remote_execution.md):** this directory builds
the desktop app (`tofu.spec` → `Tofu-Setup-*`, server + client + tray,
~153 MB). Every install is a peer node that joins servers by pairing (owner
decision 2026-11-15); there is no separate controlled-end build.

## Quick Build (local)

```bash
# 1. Install build dependencies
pip install -r requirements.txt
pip install -r desktop/requirements-desktop.txt

# 2. Generate platform icons
python scripts/gen_desktop_icons.py

# 3. Build
pyinstaller tofu.spec

# Output: dist/Tofu/
# Run:    dist/Tofu/Tofu        (Linux/macOS)
#         dist/Tofu/Tofu.exe    (Windows)
```

## What's included

The installer bundles:
- Python 3.12 runtime
- Quart + the personal application dependencies
- The local SQLite Storage Sidecar (the only desktop storage authority)
- Playwright Python package (browser binary downloaded separately)
- Frontend (HTML/CSS/JS — no build step)
- Startup **role window** (bilingual zh/en): declares "this computer runs your Tofu
  server" — and doubles as the computer-control panel (enable toggle, permission
  tiers, connect-to-remote), the controls that used to be tray-only
- Bilingual system tray with Open/Control panel/Computer Control/Quit controls
- Auto-opens browser on startup

## First-Launch Experience

Every launch opens the **role window** (`desktop/role_window.py`, themed by
`_tk_theme`, bilingual zh/en): it declares "this computer runs your Tofu server"
and lists every server this node joined with its live link state. The window IS
the control panel: computer-control toggle, permission tiers (read-only by
default), "Join a server…" (paste a pairing link), leave a server, start at
login and copy diagnostics, all the controls that used to be tray-only. Unchecking "Show this window at startup"
sends future launches straight to the tray, which keeps a **Control panel…**
item as the way back. Contract: `docs/modules/remote_execution.md`.

**Live status (2026-11-10).** The panel is built once and refreshed in place
every few seconds: the server card shows whether the server child is starting,
running, or stopped (with its exit code and an **Open log** button), and the
computer-control card shows why enabling failed or how the in-process agent's
link to the local server is doing. Toggles are antialiased switches
(`_tk_theme.Switch`), and pixel sizes follow the DPI scale. The in-process
agent authenticates to the server child with one launcher-minted capability
(`lib/desktop_wire/process_agent_token.py`), passed through the child environment and
removed from it at server boot so tool subprocesses never inherit it.

**Tray-first (2026-08-04).** On Windows the tray icon starts BEFORE the role
window can ever hide: pystray's `icon.run()` owns the main thread from second
zero, while a dedicated tk host thread (`desktop/_tk_host.py`) owns every
window and dialog (tray callbacks marshal window work to it). Both the
in-window "Minimize to tray" button and the title-bar minimize hide the window
to the already-running tray — an earlier window-then-tray sequence made
"minimize to tray" structurally impossible (the tray did not exist yet) and
the window vanished. Off Windows (macOS demands the main thread for both
frameworks) the legacy window-then-tray sequence stands. The native surface
resolves an explicit per-platform font stack (the tk default fell through to
SimSun serif on Chinese-locale Windows) and `detect_lang()` understands
Windows display-name locales (`Chinese (Simplified)_China` → zh).

The **installer only ships files and launches the app** — it never shells out to
download components (a PyInstaller `--onedir` bundle has no standalone
`python.exe`, so the old installer `[Run]` step that called
`_internal\python.exe -m playwright install` was dead code that also silently
downloaded nothing). Instead, on first launch the **app itself** shows a dialog
asking which optional components to download.

The dialog is the branded **component manager** (`desktop/post_install.py` on
`desktop/_tk_theme.py`): it follows the OS light/dark mode, speaks English or
Chinese by OS locale (`TOFU_THEME` / `TOFU_LANG` override), and — critically —
**stays open during installation with a live per-component status row and an
overall progress bar**. (The original flow closed the window and downloaded
~165 MB invisibly on a background thread; failures were only written to a log
file.) Success and failure messages are shown in the window, and the same
manager is reachable later from the tray menu. The Windows wizard itself
(2026-08-04 redesign, `desktop/installer.nsi.tmpl`) is a fully custom
nsDialogs wizard — flat brand-purple header band with the tofu cube and version,
Segoe UI / Microsoft YaHei labels on the native dialog face, a marquee
progress bar (no 3316-line log pane), `/SOLID lzma` (~153→120 MB), DPI-aware chrome, bilingual en/zh, `/S` silent installs, and
Add/Remove-Programs registration. Its page art is rendered at wrap time by
`lib/desktop_dist/installer_art.py`; the CI's Inno authoring keeps the
classic wizard with the branded sidebar bitmap, and the macOS DMG window
is branded as well (`scripts/gen_desktop_icons.py` emits the wizard
bitmaps and the DMG window art alongside the icons).

| Component | Size | Default | Purpose |
|---|---|---|---|
| **Browser Engine (Chromium)** | ~150 MB | ✅ Recommended | Enables JS-rendered page fetching and browser automation |

The desktop application always uses the local SQLite Sidecar. Distributed
deployments connect the Sidecar to platform-managed PostgreSQL through server
configuration; a desktop component installer never owns database provisioning.

When frozen, the Chromium download relaunches `Tofu.exe` with
`TOFU_PLAYWRIGHT_INSTALL=1` (handled in `desktop/launcher.py`), which drives the
bundled `playwright` package in-process — the correct way to reach the
interpreter inside a onedir bundle.

Users can skip and install later via the tray menu: **Right-click → Install Components...**

If both are skipped, the app still works — it uses SQLite for storage and skips browser-dependent features.

## Install location & writable data

The Windows installer installs **per-user** to
`%LOCALAPPDATA%\Programs\Tofu` (`PrivilegesRequired=lowest`, no UAC/admin
prompt). This matters: the app keeps its `data/` (config and SQLite DB) and
`logs/` **next to the executable**, so the install dir must be
user-writable. A `Program Files` install under `lowest` privileges is NOT
writable and used to crash the app on first launch. If the exe dir ever ends up
read-only anyway, the backend falls back to a per-user data dir
(`%LOCALAPPDATA%\Tofu`) — see `lib/runtime/runtime_paths.py`.

## Architecture

```
dist/Tofu/
├── Tofu(.exe)        ← Main executable (system tray launcher)
├── _internal/        ← Frozen Python + all packages + app code
│   ├── static/       ← Frontend assets
│   ├── index.html
│   ├── lib/          ← Backend logic and Storage Sidecar client
│   ├── routes/       ← Quart blueprints
│   └── ...
└── data/             ← User data (created on first run, portable)
    ├── config/       ← Server config, API keys
    └── tofu.db       ← Local SQLite authority
```

## Database Strategy

The desktop build always uses its local SQLite Storage Sidecar. It neither
downloads nor starts PostgreSQL and it never silently switches authorities.
Enterprise deployments connect server-side Sidecars to platform-managed,
TLS-verified PostgreSQL/Redis; the desktop package remains a client and does
not provision those services.

## Product names

**Tofu** is the one desktop product; its release assets start with `Tofu-`.
`tofu-agent` is the distinct AI runtime SDK. Windows keeps the existing install
directory and uninstall identity, and macOS keeps its bundle identifier, so
upgrades retain settings and replace the existing installation.

## CI / Automated Builds

A push to `main` triggers `.github/workflows/build-desktop.yml` when `VERSION`
does not have a complete release. The matching `CHANGELOG.md` entry is required.
There is no tag-push trigger. The workflow produces:
- `Tofu-Setup-<ver>-win64.exe` — Windows installer (Inno Setup)
- `Tofu-<ver>-macos-arm64.dmg` — macOS disk image, Apple Silicon
- `Tofu-<ver>-macos-x86_64.dmg` — macOS disk image, Intel
- `Tofu-<ver>-linux-{x86_64,aarch64}.deb` — **primary Linux installer** (Debian/Ubuntu):
  installs to `/opt/Tofu` with a system-wide menu entry and icon; user data
  lives in `~/.local/share/Tofu` (the app never writes into `/opt`).
  Install with `sudo apt install ./Tofu-<ver>-linux-{x86_64,aarch64}.deb`.
  (Evaluated against AppImage, which lost on three axes: dpkg-deb ships in
  the CI base image — no downloaded tooling; no FUSE at build time; and no
  FUSE at RUN time, whereas type-2 AppImages need `libfuse2`, which
  Ubuntu 22.04+ no longer installs by default.)
- `Tofu-<ver>-linux-{x86_64,aarch64}.tar.gz` — Linux portable archive, the no-sudo /
  non-Debian fallback (includes `install.sh` — per-user — which registers an
  application-menu entry and themed icon; run it once after extracting)
- `SHA256SUMS` — checksums covering all of the above

The release is **published immediately and promoted to Latest** (`draft: false` +
`make_latest`), so `…/releases/latest` resolves to it. Two native macOS DMGs are
built (not a universal2 fat binary — the runner interpreter and some C-extension
wheels are single-arch). A completeness gate refuses to publish unless all supported
platform/architecture assets are present, so a partial build never ships as Latest.

## Manual workflow dispatch

Go to Actions → **Build Desktop** → **Run workflow**, select the trusted candidate
ref, leave **Build and verify both macOS candidates** checked (`verify_macos_only:
true`, the manual default), and leave the version override empty. Use a ref with
a matching changelog entry. Only the frontend and native macOS build jobs run;
no Git tag or GitHub Release is written. Unchecking that input enables the normal
release flow, not a dry run.

## macOS release trust

A signed checksum does not grant Gatekeeper trust. The desktop release builds
only the full `Tofu.app` (PyInstaller) and two native DMGs (`create-dmg`), not a
separate controlled-end DMG or a `.pkg`.

1. An authorized release owner must configure `MACOS_CERT_P12` (base64-encoded
   Developer ID **Application** PKCS#12 including the private key) and
   `MACOS_CERT_PASSWORD`, plus `MACOS_NOTARY_APPLE_ID`, `MACOS_NOTARY_TEAM_ID` and
   `MACOS_NOTARY_PASSWORD` (an app-specific password). The certificate and notary
   account must belong to the same Apple Developer team. Do not use an ad-hoc,
   development or Developer ID Installer certificate. The separate
   `RELEASE_SIGNING_PRIVATE_KEY` and `RELEASE_SIGNING_PUBLIC_KEY` must still match
   for publication of signed `SHA256SUMS`.
2. Prepare the proposed new `VERSION` and matching changelog entry on a trusted
   candidate branch, not `main` (a push to `main` can publish). On the approved
   CI repository, run `gh workflow run build-desktop.yml --ref <candidate-ref>
   -f verify_macos_only=true`. Each native architecture must sign via
   PyInstaller, pass the frozen startup smoke, finish and sign the DMG, get an
   **Accepted** Apple notarization result for the DMG, then staple it. CI checks
   the final image integrity and signature, validates its ticket, and assesses
   the DMG and the mounted app with Gatekeeper enabled. It also verifies the
   mounted app's nested code signatures, bundle ID/version, and app/engine
   architecture. Missing signing inputs or any failed check stop the job;
   there is no unsigned publication fallback. The DMG holds the stapled ticket;
   there is no separate downloadable app to notarize.
3. Download the resulting CI artifacts (not a latest release). On a clean Mac
   of each architecture with Gatekeeper enabled, compare the candidate DMG to
   its `.verified.sha256` using `shasum -a 256 -c`, then verify the final DMG with
   `bash scripts/verify_macos_dmg.sh <dmg> <arch> <version> <team-id>`. Open the
   quarantined download in Finder, drag the app to Applications, and launch it.
   Do not remove quarantine or override Gatekeeper if it fails; retain the
   filename, SHA-256 and exact alert.
4. Only after verification and separate approval, send that approved source
   with its **new** version and matching changelog entry through the normal
   release flow. The release build reruns the native checks. Its promotion job
   requires both DMGs to match the bytes verified in that same run before
   signing `SHA256SUMS` and promoting the release. Compare the published
   and mirrored files to those digests. A source fix does not repair previously
   downloaded DMGs. Windows Authenticode signing remains unconfigured in CI.

## Adding New Optional Components

1. Create a new `Component` subclass in `desktop/post_install.py`
2. Implement `is_installed()` and `install()` methods
3. Add to `OPTIONAL_COMPONENTS` list
4. Set `recommended = True` if it should be pre-checked in the dialog

## Updating

The desktop build does not self-install updates, but on startup it does a
best-effort check of the latest published release: when a newer version exists it
shows a **Download update (&lt;tag&gt;)** item in the system-tray menu that opens the
Releases page. The check runs on a daemon thread, is non-blocking, and fails
silently when offline or rate-limited (see `_check_for_update` in
`desktop/launcher.py`). Users then download and run the new installer — on macOS,
pick the `.dmg` matching their chip (`-arm64` = Apple Silicon, `-x86_64` = Intel).

Future: integrate [Sparkle](https://sparkle-project.org/) (macOS) or [winsparkle](https://winsparkle.org/) (Windows) for in-place delta updates.
