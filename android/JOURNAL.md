# Project Journal

> This file is the project's **evolution journal** — a living record the AI
> assistant maintains across sessions. It is NOT a rules file (project rules
> live in CLAUDE.md / AGENTS.md if present) and NOT a versioned changelog.
> It is a free-form dev log of *how and why* this project changes over time.

## How to use this file

- **Read it first.** At the start of a session, read this journal to understand
  how the project reached its current state before making changes.
- **Keep it current — on your own initiative.** After any meaningful change,
  append a dated entry yourself. This is a standing, pre-authorized action: you
  do NOT need to ask the user before adding an entry, and you should not pause
  to request permission to update the journal. Do not rewrite or delete past
  entries — the history is the point; only ever append.
- **Record the *why*, not just the *what*.** A diff shows what changed; this
  journal explains the reasoning a future reader (human or model) could not
  reconstruct from the code alone.

## What to record

- **Experimental projects:** methods/approaches tried, why each was adopted or
  abandoned, hyperparameter or design changes, and experiment results
  (metrics, observations, dead ends).
- **Engineering projects:** technology-selection changes and their rationale,
  refactoring steps and their motivation, architectural decisions, and the
  current status / known issues / next steps.

## Entries

<!-- Append newest entries at the top. Suggested format:

### YYYY-MM-DD — short title
- **Change:** what changed
- **Why:** the reasoning / problem being solved
- **Result / status:** outcome, metrics, or current state
-->

### 2026-09-30 — Server-address QR + wide-screen two-column grid
- **Change:** (a) **Server-side QR** in `routes/api_v1/mobile.py`:
  `GET /api/v1/mobile/connect` (scan-to-connect page) and
  `GET /api/v1/mobile/connect/link?url=&name=` (JSON `{url,name,deepLink,qr}`)
  build a `tofu://connect` deep link and its PNG data URI via `lib.qr`. The page
  resolves the address from `location` in the browser, so it is correct behind
  any reverse-proxy prefix; tests in `tests/api/test_api_v1_mobile.py` (incl. the
  auth-401 list). (b) **Android wide-screen list** is now a `LazyVerticalGrid`
  with `GridCells.Adaptive(320.dp)` capped at `ListMaxWidth` (840dp) — one
  column on phones, two on tablets. (c) **Form whitespace**: `FormSection` gains
  a ruled header (label + hairline) and wider spacing. (d) Screenshot list grown
  to three servers so the tablet grid shows a wrap.
- **Why:** the scanner had no official producer (the operator had to hand-make a
  QR), and a tablet list was a single 600dp column. Both are the "it technically
  works but isn't a product" gaps.
- **Design notes:** the deep link (not the bare URL) is what gets encoded, so a
  generic QR app also opens Tofu via the registered scheme. The address is built
  client-side because the server cannot know its external proxy prefix
  (`request.url_root` would drop `/proxy/15000/`). URL/name are percent-encoded;
  non-http(s) `url` is 400.
- **Verification:** backend `tests/api/test_api_v1_mobile.py` 10 passed; v1
  endpoint index regenerated (495→497) and green; Android
  `./gradlew test :app:assembleDebug :app:lintDebug :app:assembleRelease` green,
  snapshots re-recorded. Measured: tablet list content span 0.45w → 0.63w
  (the 840dp cap) after switching to the grid.
- **Caveat:** the model cannot view images, so the 2-column count is inferred
  from `GridCells.Adaptive` semantics + the width change, not eyeballed; and the
  on-device scan of the server QR is still unverified.

### 2026-09-30 — Phone/tablet layout + Paparazzi device screenshots
- **Change:** (a) **Responsive width.** New `ContentMaxWidth` (600dp) +
  `CappedContent`/`CappedScrollContent`/`ContentWidth` in `Components.kt`; the
  list, add/edit form, and onboarding cap their single column and center it on
  wide screens. The list FAB moved INSIDE the capped column (Scaffold's own FAB
  sat at the screen corner, detached from centered content on a tablet). The
  WebView is intentionally left full-bleed. (b) **Onboarding background** now
  paints `MaterialTheme.colorScheme.background`, matching the rest of the shell
  (it previously sat on the bare `Surface` colour — white vs the list's cream).
  (c) **`TofuTheme`** status-bar cast is now `(view.context as? Activity)` so the
  theme renders under layoutlib/previews instead of crashing. (d) **Paparazzi
  screenshot harness** (`app.cash.paparazzi` 1.3.5) with
  `ScreenshotTest.kt` rendering list/add/onboarding at Pixel 5 (phone), Nexus 5
  (small phone), Pixel 6 Pro (large phone), Nexus 10 landscape + portrait, and a
  dark phone variant, in zh-rCN. Golden PNGs committed under
  `app/src/test/snapshots/images/`.
- **Why:** the client targets both phones and tablets, but the single-column
  screens stretched nearly edge-to-edge on a wide tablet. With no emulator on
  this host (no KVM / no system image), a JVM `layoutlib` renderer is the way to
  actually see device shapes. Measured before/after on the tablet list: content
  span 0.98w → 0.45w; add form 0.97w → 0.44w; and the onboarding surface colour
  now matches (251,247,238) instead of white.
- **Harness notes:** Paparazzi does not provide an `ActivityResultRegistryOwner`,
  which `rememberLauncherForActivityResult` (the QR scanner) requires — the test
  supplies a no-op owner. Snapshots are committed; `./gradlew test` verifies
  against them (a renderer change re-records with `recordPaparazziDebug`). The
  Paparazzi plugin adds layoutlib download time to CI's first `test` run.
- **Verification:** `./gradlew test :app:assembleDebug :app:lintDebug
  :app:assembleRelease` green with the plugin applied (119+ unit tests incl. the
  Paparazzi verifications). Phone renders unchanged (~0.90–0.93w).
- **Caveat:** this model cannot view images, so the PNGs are for the human
  reviewer; automated checks here are pixel measurements (content bounds,
  background colour), not visual judgement.

### 2026-09-30 — Polish pass: leak/alloc fixes, compatibility, bilingual UI
- **Change:** (a) **Leak:** `WebScreen`'s `AndroidView` had no `onRelease`, so
  every visit to a server leaked a `WebView` + renderer; added `onRelease`
  (stopLoading → about:blank → detach → `destroy()`). (b)
  `SupervisorControls`' default `SupervisorClient()` parameter was re-evaluated
  on every recomposition (each a fresh OkHttp client); made it nullable +
  `remember`ed. The add form's `HealthProbe` is `remember`ed too. (c)
  `UpdateChecker` now probes servers concurrently (`async`/`awaitAll`) instead of
  serially. (d) Recomposition: per-row status setter and MainActivity navigation
  callbacks are `remember`ed, and the header's running-count aggregate is read
  inside `HomeHeader` so one card's status change no longer recomposes the list.
  (e) `OnboardingScreen` scrolls. (f) Shared 48dp `TouchTarget` in
  `Components.kt`; new inline buttons apply it. (g)
  `enableOnBackInvokedCallback` + `<monochrome>` launcher layer + `tools:targetApi`.
  (h) **Bilingual:** every UI string moved to `values/strings.xml` + a new
  `values-zh-rCN/strings.xml` (112 keys, parity-checked), consumed via
  `stringResource`; `ProfileListScreen`'s `summaryText` uses
  `pluralStringResource`, and `serverStateLabel` maps `ServerState` in the UI
  (leaving `ServerLifecycle.label`, which pure tests assert, English).
- **Why:** the audit found a per-visit renderer leak and per-recomposition
  OkHttp allocation on the hottest surfaces, plus a UI that was English-only for
  a bilingual product. These are the "it feels slow/janky and foreign" class of
  defects that unit tests never catch.
- **Deliberate scope:** session-layer diagnostic strings (`ServerLifecycle`,
  `TofuProbe.guidance`, `LoginRetryPolicy`) stay English — they are
  near-technical, pinned by pure tests, and localizing them would drag resources
  into the tested pure layer.
- **Verification (this host):** `test-local.sh` → **170 pure-JVM + 6 Robolectric
  green**; `./gradlew test :app:assembleDebug :app:lintDebug :app:assembleRelease`
  all green; lint now reports **0 MissingTranslation** and the previous
  `MonochromeLauncherIcon` warning is gone (the remaining advisories are the
  intentional LAN-cleartext base config and pre-existing GradleDependency).
- **Still unverified on a device:** the teardown path under a real back-press,
  scroll behavior at large font scale, and the zh layout (CJK metrics differ
  from English).

### 2026-09-30 — Install & configure: in-app update check, QR/paste/deep-link onboarding, first-run
- **Change:** (a) **Update check** — pure `session/UpdateCheck.kt` (numeric
  segment version compare + tolerant field extraction from `/api/health`) and
  `session/UpdateChecker.kt` (`UpdateProbe` fetches, `UpdateChecker` aggregates
  the newest advertised version across every configured server). The list shows
  a one-line banner with a download link when a newer version is advertised.
  (b) **Onboarding** — pure `session/DeepLink.kt` parses a scan/paste/deep-link
  payload into `(url, alias?)`; `data/OnboardingStore.kt` is a one-flag
  SharedPreferences; `ui/OnboardingScreen.kt` + `ui/QrScan.kt` (ZXing embedded
  `ScanContract`) give three ways to connect; `AddEditScreen` gains Scan/Paste
  actions and `prefillUrl/prefillAlias`; `AndroidManifest.xml` adds a
  `tofu://connect` VIEW filter (`launchMode=singleTop`) + `CAMERA`; MainActivity
  handles `onCreate`/`onNewIntent` deep links and routes first-run to onboarding.
  (c) Version bumped to `0.1.18` / `versionCode 19`, `routes/common.py`
  `MOBILE_CLIENT_VERSION` kept in lockstep.
- **Why:** The whole "connect a phone" flow was typing a ~90-char UUID URL and a
  password twice, with no way to know the installed APK was stale. The fix is to
  remove the typing (scan/paste/deep link) and remove the staleness (compare the
  installed build against what the server already advertises). Both are grounded
  in data the backend already serves, so no new server contract is invented.
- **Design notes:** All three onboarding transports converge on **one** parser
  (`DeepLink`), so a payload is validated identically everywhere and a
  non-Tofu QR is a clean no-op. The update check is a public, credential-free
  `GET /api/health` **per server**, because a user can point at a stale sandbox
  while their primary server carries the release; it is one-shot per process and
  short-timeout so it never taxes startup. Onboarding is retired by the *first
  successful add* (observed on the profile flow), not only by the Skip button,
  so it cannot reappear for a user who connected normally.
- **Dependency:** added `com.journeyapps:zxing-android-embedded:4.3.0` (brings
  `zxing:core` + the capture Activity + the AndroidX `ScanContract`); `CAMERA`
  permission is declared with `uses-feature ... required=false` so camera-less
  devices still install.
- **Verification (this host):** `test-local.sh` → **170 pure-JVM + 6 Robolectric
  green** (new `UpdateCheckTest`, `UpdateCheckerTest`, `DeepLinkTest`).
  `./gradlew test :app:assembleDebug :app:lintDebug` green (all variants), and
  `:app:assembleRelease` green (R8 + lintVitalRelease) with the new dependency.
  `tests/server/test_mobile_client_apk_url.py` green after the version bump.
- **Still unverified on a device:** QR capture, cold/warm deep-link delivery, the
  update banner's browser hand-off, and the report sheet (from the prior
  increment). Nothing here is exercised by an emulator run yet.

### 2026-09-30 — "Report a problem": one visible support surface, native diagnostics ring, server intake
- **Change:** Replaced the hidden "copy diagnostics" FAB with an end-to-end
  report flow. New pure `session/DiagLog.kt` (bounded, thread-safe ring of
  D/I/W/E events, clipped at capture time) and `session/ClientReport.kt` (total,
  pure JSON builder that clips and redacts); new pure-JVM `DiagLogTest`,
  `ClientReportTest`, `SessionManagerSubmitReportTest`. `SessionManager` gains
  `submitReport(profile, json)` — a best-effort, never-throwing
  `POST {baseUrl}/api/v1/mobile/client-report` carrying the session cookie.
  The UI is one `ui/ReportProblemSheet.kt` reached from three places: the
  **home header** (works with no server open), the **load-failure cover**, and
  the WebView **tools FAB**. `WebScreen` records console output, main-frame
  load failures, re-auth outcomes, TLS decisions and renderer death into the
  ring, and merges the SPA's `__tofuCollectDiagnostics()` blob when the page can
  still answer. Backend: new `routes/api_v1/client_support.py` (POST ingest +
  GET recent) over a new bounded `lib/client_report_store.py`
  (`<data_root>/client_reports/*.jsonl`, self-rotating, newest 8 retained),
  covered by `tests/api/test_api_v1_client_report.py`.
- **Why:** The user's report should reach the maintainer without copy-paste
  archaeology, and it must work in the exact case that matters — a wedged SPA.
  The old design failed both: the entry point was a low-alpha debug handle, and
  the payload was collected by the very page that might be dead (and then only
  ever landed on the clipboard). So the shell now keeps its own bounded event
  tail and its own entry points, and offers three transports — the system share
  sheet (always works, no server), the clipboard, and a server POST — instead of
  forcing the user to find a paste target while something is broken.
- **Security posture:** The builder and the route BOTH bound and redact; the
  route re-clips every field and caps the body at 1 MiB rather than trusting the
  client. The profile contributes only alias/host/auth-type — the vault password
  is never an input. Free text is masked for `password=`/`token=`/`api_key:`/
  `Bearer …`/`code-server-session=…`, so a token that leaked into a console line
  is scrubbed before it leaves the device (and again on intake).
- **Deliberate scope:** The endpoint sits behind the normal auth gate; on an
  `open`-mode personal server it works with no extra credential, and on a
  `private`-mode server the app does not hold a Tofu token, so "Send to server"
  degrades to an honest status while Share/Copy remain available. Recorded in
  README.
- **Also fixed (blocked release, pre-existing at HEAD):** `assembleRelease`
  failed R8 with "Missing class com.google.errorprone.annotations.*" referenced
  by `com.google.crypto.tink` (pulled in by `androidx.security:security-crypto`).
  Those are compile-time-only annotations, so `app/proguard-rules.pro` now
  `-dontwarn`s `com.google.errorprone.annotations.**` and `javax.annotation.**`;
  the signed release APK builds again.
- **Verification (this host):** `./test-local.sh` → **155 pure-JVM + 6
  Robolectric green**. Real toolchain (JDK 17 + the cached Android SDK at
  `~/Android/Sdk`): `./gradlew test :app:assembleDebug :app:lintDebug` →
  **186 unit tests green, lint clean**; `:app:assembleRelease` also green
  (R8 + lintVitalRelease) and produces the signed APK. Backend focused tests
  green: `tests/api/test_api_v1_client_report.py` (7),
  `test_api_v1_endpoint_index.py`, `test_api_v4_product.py`,
  `test_api_contract_drift.py`, `test_api_contract_artifacts_parity.py`; the v1
  endpoint index regenerated (the 3 unrelated endpoints that were already
  missing from it at HEAD are now included alongside the 2 new ones).
- **Still unverified on a device:** the sheet's Share/Copy/Send round trip and
  the WebView-bridge delivery of the web blob need on-device eyes.
- **Next increments (scoped, not started):** in-app update check against
  `/api/health`'s `mobile_client_version` + one-tap download; QR/paste/deep-link
  server onboarding; first-run guidance.

### 2026-09-02 — vscode-tunnel hardening pass: retry, meta preflight, native-visibility bridge, camera capture
- **Change:** `SessionManager.login` now retries only transport `Error`s with a
  bounded backoff (`LoginRetryPolicy`: 3 attempts, 1s/2.5s — definitive outcomes
  never re-POST); a post-login `GET {base}/api/v4/meta` preflight (`ApiMetaGate`)
  swaps `Success` for `Incompatible` only on a definitive apiMajor /
  minAndroidBuild refusal and fails open on 404/garbage/transport. `WebScreen`
  gained: the `TofuNative.requestReauth` JS bridge + `tofu:native-visibility`
  dispatch from ON_START/ON_STOP (mirrored by `frontend/src/core/native-bridge.ts`,
  whose effective-hidden fold the SPA's budget layers now consult — a WebView
  never flips document.visibilityState when the app backgrounds, so foreground
  cadence layers would otherwise keep hammering the vscode proxy tunnel from a
  pocket), a camera-capture offer in the file chooser (`ACTION_IMAGE_CAPTURE` +
  FileProvider cache staging, `res/xml/file_paths.xml`), dead-link fixes
  (target=_blank / external scheme routing), a DownloadListener, and an SSL-error
  surface. `ReauthWebViewClient` caps consecutive failed headless re-auths.
- **Why:** the app's primary runtime is a vscode port-forwarding tunnel
  (`…/proxy/15000/`), where blips surface as transport errors (not HTTP status)
  and the outer gateway's session cookie can die mid-page; the shell is the only
  layer that can re-login headlessly, and the SPA the only layer that knows when
  its polls are pointless.
- **Deliberate scope call:** `AuthType.NONE` profiles skip the meta preflight
  entirely — with no outer gateway the WebView reaches Tofu directly and the
  SPA's own 426/upgrade handling surfaces a mismatch in-page; a headless probe
  would only add a tunnel round-trip and double the dead-server wait. The
  zero-request short-circuit pin in the degrade suite stands.
- **Tests:** new pure-JVM suites `ApiMetaGateTest`, `SessionManagerLoginRetryTest`,
  `SessionManagerPreflightTest`; `SessionManagerPreflightTest` rides the full
  password handshake (GET form → POST 302+cookie → meta). Frontend owner spec
  `tests/meta/test_native_bridge_owner.py` pins the visibility fold (OR semantics,
  flip-only events, unsubscribe), the reauth gate (30s rate limit, reason
  normalize/truncate, failed forward never consumes the budget), and the
  bare-edge-401/403 discrimination shared with `TofuProbe`. Tier counts now
  132 pure-JVM + 6 Robolectric, both green via `test-local.sh` (libexec kotlinc
  2.4.10 + JDK 17 from the conda pkgs cache).

### 2026-08-29 — LAN cleartext scoping + CI guards for versionCode / keystore / APK rename (audit hardening)
- **Change:** Added `app/src/main/res/xml/network_security_config.xml` (referenced
  via `android:networkSecurityConfig`, replacing `usesCleartextTraffic="false"`).
  Hardened `.github/workflows/build-android-apk.yml`: checkout now fetches full
  history (`fetch-depth: 0`); added a `versionCode` strictly-greater guard (tag
  builds), a keystore sha256-unchanged guard (all builds), and made the
  release-APK rename assert exactly one APK instead of `ls | head -1`.
- **Why:** (1) The profile form accepts `http://` LAN URLs but cleartext was
  globally blocked → `ERR_CLEARTEXT_NOT_PERMITTED`. Android's
  network-security-config matches hostnames only (exact or a leading
  `*.example.com` wildcard) — there is no CIDR/IP-range form, so private-LAN
  cleartext cannot be scoped per address. The minimal correct fix is base-level
  cleartext, safe because the shell's single navigation entry point is the
  user-configured `profile.baseUrl` (no address bar, no arbitrary browsing);
  OkHttp (headless login) is likewise only pointed at the profile host. (2)
  `versionCode` bump and the committed keystore were manual/no-guard invariants —
  a missed bump or accidental keystore change would silently break downgrade
  /in-place-update behavior. (3) `ls …/release/*.apk | head -1` would silently
  pick the wrong APK if Gradle ever emitted more than one.
- **Result / status:** Workflow parses (`yaml.safe_load`); both XML files
  well-formed; the new guard/rename shell passes `bash -n` and functional tests
  (0/2/1-APK cases, and the no-previous-tag skip path). No `android-v*` tags
  exist yet, so both guards skip on the first release. Not built/run on this
  host — no Android SDK / kotlinc / JDK 17, so `test-local.sh` and `./gradlew`
  could not be exercised (documented limitation, not a code change).

### 2026-08-05 — activate() stamped recency with a full-row write from a stale snapshot; test-local.sh un-staled
- **Change:** `SessionController.activate()` now bumps recency with the targeted
  `ProfileDao.touchLastUsed` and RE-READS the row (`getById`) before login, returning
  `ProfileResult` (renamed from `EditResult`, now shared with `editProfile`); the
  ViewModel navigates with the re-read row. `migrateProxyAuthDefaults()` flips auth
  types with the new targeted `ProfileDao.setAuthType`. Commits `f410acd` (fix) +
  `39596c9` (harness).
- **Why:** same hazard class as the 2026-07-28 `@Update` rollback, on the hottest
  path of all — `activate()` runs on EVERY card tap with a row rendered from the
  `observeAll()` Flow, a snapshot that lags any write not yet re-emitted. The old
  `dao.update(profile.copy(lastUsedAt = …))` rewrote the WHOLE row from that stale
  snapshot: it would reinstate a `cookie_host` a concurrent login had just stamped
  (silently re-locking the supervisor controls the user had already unlocked) or
  cleared. The migration loop had the same interleaving window (suspends on every
  write over a `getAllOnce()` snapshot). The fake DAO's new `afterSnapshot` hook is
  what makes the interleaving reproducible in a test at all.
- **Result / status:** **107 pure-JVM + 3 Robolectric green** via `test-local.sh`.
- **Harness repair (same commit series):** `test-local.sh` had rotted behind the
  session layer — its generated `CookieBridge` stub predated `CookieSink.cookieHeader`,
  and `InteractiveSso.kt` / `ServerLifecycle.kt` (pure) were never in the tier-1
  sources, so the pure tier no longer even compiled, and
  `SessionManagerInteractiveSsoTest` had NO local runner. Fixed the stub, added the
  pure mains + four suites; `SupervisorRunner` deliberately stays Gradle-tier
  (needs `SupervisorClient` → `android.webkit` + `org.json`). Toolchain note: no
  kotlinc dist on this box — ran the embeddable compiler jar through a synthetic
  `kotlin-home` shim (`-kotlin-home` pointing at a dir whose `lib/` symlinks the
  `kotlin-stdlib/reflect/script-runtime` jars from `.testharness/libs`), which makes
  stdlib auto-resolution behave exactly like a real distribution.

### 2026-07-28 — A full-row `@Update` could silently roll back a concurrent edit (owner review)
- **Change:** New `ProfileDao.setCookieHost(id, host)` — a targeted
  `@Query("UPDATE profiles SET cookie_host = :host WHERE id = :id")`. Both stamping sites use it.
  `SessionController.editProfile` now returns `EditResult(login, persisted)` and
  `SessionManager.updateUrlAndReauth` returns `ReauthResult(login, persisted)`; `submitEdit`
  navigates with `persisted`. Commit `f9978a3`.
- **Why (defect 1 — the write was too wide):** `ProfileDao.update` is Room's `@Update`, which
  rewrites the **whole row** from whatever `Profile` instance the caller holds. Both cookieHost
  stamps passed a **long-lived snapshot**: `WebScreen` keeps the profile it was opened with for the
  entire session, and an interactive SSO sign-in can take minutes (IdP redirect, one-time code).
  Any edit landing in that window — including the `cookieHost = null` invalidation
  `updateUrlAndReauth` performs on a host change — would be **reverted by the stamp**. The fix is
  not "remember to re-read first": it is to make the write narrow, so *only this column changes*
  is a property of the **query** rather than a discipline every caller must keep. The
  headless-login stamp got the same treatment — identical pattern, identical hazard, and leaving
  one of the two wide would just wait for the next reader to copy the wrong one.
- **Why (defect 2 — the caller invented the row):** `submitEdit` rebuilt the edited profile as
  `current.copy(alias, baseUrl, authType, projectPath)` and navigated with it. That copy carries
  the **pre-edit `cookieHost`**, while `editProfile`'s host-change branch had just nulled it. So
  the object handed to the WebView was **known to disagree with the database** — and for SSO that
  object is what the entire session runs on. The two branches persist *different* rows (the
  host-change path also refreshes `instanceUuid`), so reconstructing at the call site is wrong
  exactly when it matters. Both functions now **return what they wrote**.
- **Result / status:** **122 unit tests pass** (+1), lint clean, both APKs signed.
  **Neuter-verified:** restoring `dao.update(profile.copy(cookieHost = host))` fails both
  `stamping_does_not_roll_back_a_concurrent_edit` and
  `completing_sso_in_the_webview_stamps_cookie_host`. The new guard renames *and* re-paths the row
  **after** the snapshot is taken, then asserts the stamp records the sign-in without reverting
  either field — the fake DAO implements `setCookieHost` with real single-column semantics, so it
  can actually tell a targeted write apart from a full-row overwrite.
- **The recurring shape, now six for six:** every defect this session came from **conflating two
  things that merely co-occur**. This one: *"I know this row's identity"* with *"I know this row's
  current contents."* Holding a valid `id` does not license rewriting the other columns from a
  snapshot taken minutes ago.
- **Still unverified on a device** — the SSO round trip (Open → IdP → back → Start/Stop unlocked)
  is exercised only by unit tests.


### 2026-07-28 — SSO sign-in was a closed loop: Open did nothing, Start locked forever (owner review)
- **Change:** New pure `session/InteractiveSso.kt` (`shouldOpenWebView`, `completedSignIn`,
  `hostToStamp`); `SessionManager.noteInteractiveSignIn()` stamps `cookieHost` after an in-WebView
  sign-in; `CookieSink.cookieHeader()` reads the jar back; `ReauthWebViewClient` stands down for
  SSO profiles; the `NeedsSso` `UiStatus` and its copy are deleted. Commit `fa3bdcb`.
- **Why (defect 1 — the exit that did not exist):** `Screen.Web` had **exactly one assignment
  site** — `ProfilesViewModel.handleLogin`'s `LoginResult.Success` branch. `INTERACTIVE_SSO`
  returns `NeedsInteractiveSso`, whose only consumer was a status string:
  *"This server needs interactive sign-in — opening…"*. **Nothing opened anything.** Tap Open →
  read "opening…" → stay on the list → tap again → same.
  This closed a loop with the SSO copy shipped in `ed4aeb1` ("tap Open, sign in once, then Start
  and Stop will work"), which **pointed the user at the one exit that did not exist** — the same
  shape as the original greyed-Start deadlock, one layer up. Five review passes missed it because
  every one of them read the *decision* layer, where the rule looked right; the defect was that
  the decision had **no consumer that could act on it**.
- **Why (defect 2 — signed in, still locked):** even once navigation worked, nothing would ever
  stamp `cookieHost` for SSO. The headless path stamps it at `SessionManager.kt:105`, when it
  injects a cookie **it fetched itself**; an interactive sign-in is performed by the browser
  engine and never passes through that line. So `isSignedIn` stayed false **permanently** and the
  supervisor's Start/Stop could never be used no matter how many times the user signed in.
  `noteInteractiveSignIn` is called from `onPageDone` and is deliberately conservative: it
  requires landing back on our **own** host, **off** the `/login` page (a CSRF/state cookie alone
  is a false positive), with a real cookie in the jar — and is idempotent, because `onPageDone`
  fires on **every** main-frame load and `cookieHost` is part of `CardKey`, so a chatty write
  would reset every card's polled state on each navigation.
- **Why (defect 3 — found while fixing, and fatal to the fix on its own):**
  `ReauthWebViewClient.shouldOverrideUrlLoading` returns `true` for **any** main-frame `/login`
  navigation and defers to a headless re-login. For an SSO profile `login()` returns
  `NeedsInteractiveSso`, so `onReauth`'s `if (result is LoginResult.Success) view.reload()`
  **never fires**. An SSO sign-in *is* a sequence of main-frame navigations through login pages —
  so the client would have swallowed the very flow we just sent the user into, freezing the
  WebView on a blank surface. The user would have been handed in and **still** unable to sign in.
  SSO is now exempt from that intercept and from the 401 trigger.
- **Result / status:** **121 unit tests pass** (+15), lint clean, both APKs signed.
  **Neuter-verified at BOTH levels** — reverting the fixes fails the pure guards
  (`interactive sso must open the webview`, `landing back on our own host with a cookie records
  the sign-in`) *and* the flow-level `completing_sso_in_the_webview_stamps_cookie_host`. The
  two-level split is deliberate: it is the standing lesson from the staleness guards that a pure
  rule tested in isolation says nothing about whether its inputs are wired correctly.
- **`.gitignore` checked (owner asked):** `.tofu*` is anchored to names *starting with* `.tofu`,
  so it cannot match `JOURNAL.md`. `git check-ignore -v JOURNAL.md` reports no match and
  `git ls-files` lists it — tracked, no conflict.
- **The recurring shape, now five for five:** every defect this session came from **conflating
  two things that merely co-occur**. This one is the sharpest yet — *"the code decided X"* was
  conflated with *"X happens"*. A `sealed interface` outcome with no consumer that acts is
  indistinguishable, at the decision layer, from one that works.
- **Still unverified on a device.** Per owner: **no release until an SSO profile is confirmed to
  open, sign in, and unlock Start/Stop on real hardware.**


### 2026-07-28 — Key asymmetry made the guard unreliable; JOURNAL now tracked (owner review)
- **Change:** New `CardKey` data class + pure `isStillCurrent(started, current)`; the identity ref
  is keyed on the SAME `CardKey` as every other per-card state; card message colour now follows
  `RunOutcome.failed`; `JOURNAL.md` removed from `.gitignore`. Commit `595b2c2`.
- **Why (defect 1 — the third version of the same guard):** every per-card state used
  `remember(stateKey)` but the identity ref used a **bare `remember`**. That asymmetry means the
  ref and the state it guards **do not share a lifetime** — on a key change Compose rebuilds one
  and not the other, leaving the card half-new/half-old. Correctness then rested on a
  `listOf(...)` happening to compare equal, which is not something to rely on in a recycled
  `LazyColumn` row.
  - *One correction to the owner's diagnosis, recorded so the next reader isn't misled:*
    `remember(stateKey)` with a `List` passes **one** key (the list object), compared by
    `equals`; a `List` is NOT spread into multiple key parameters. The asymmetry was the real
    defect, not key-spreading.
- **Why the fix is a named type:** `CardKey(id, baseUrl, cookieHost, projectPath)` writes the
  fields that make a result *belong* to a card into the type itself, so a future field cannot be
  silently dropped at one of the several sites the key is built. Neuter-verified: nulling
  `cookieHost`/`projectPath` in `CardKey.of` fails two tests.
- **Why the comparison is its own function:** all three versions of this guard failed **not
  because the rule was hard but because the values fed to it were computed wrongly**. Naming the
  comparison lets a test assert the INPUTS. Added the recycled-slot case
  (`work started before a slot was recycled is not current`) — a `LazyColumn` reusing a row for a
  DIFFERENT profile, which is how scrolling during a start could hand the user into the wrong
  server's WebView.
- **Why (defect 2 — a slow boot looked broken):** the message was rendered unconditionally in
  `colorScheme.error`, but `startTimeoutMessage()` is explicitly NOT an error — `/start` was
  accepted and the server is probably still booting. Painted red it was **visually identical to
  "login failed"**, so a healthy slow boot read as a broken Start. Colour now follows `failed`.
- **`JOURNAL.md` un-ignored.** I added that ignore in `9a0a446` reasoning "per-developer, not
  source". That does not survive scrutiny: this file carries the design reasoning a future
  maintainer actually needs, and it was living on exactly one machine. Now tracked.
- **Result / status:** **106 unit tests pass** (+3), lint clean, both APKs signed. Still
  unverified on a device.
### 2026-07-28 — The staleness guard was dead code — and the test couldn't tell (owner review)
- **Change:** New `session/SupervisorRunner.kt` — `executeSupervisorCall(...)` takes
  `login` / `call` / `isCurrent` as parameters and RETURNS a `RunOutcome` instead of firing
  callbacks. `isCurrent` is a FUNCTION, re-evaluated after the work; the Composable backs it with
  a remembered ref every composition refreshes. Added `catch (t: Throwable)`. Commit `4a9f2f0`.
- **Why:** `stillCurrent` shipped in `92c68e7` as `startedFor == stateKey` inside the Composable.
  `stateKey` is a plain `listOf(...)` recomputed each composition; the coroutine captured the same
  instance it was created with, so **the comparison read one variable against itself and was
  structurally always `true`**. A profile edited mid-poll yields a NEW `stateKey` in a NEW
  invocation that the running coroutine never sees. The guard could not fire. Ever.
- **The real lesson is about the TEST, not the guard.** `completionFor` and its four unit tests
  were correct and passing the entire time — because they called the pure function directly with
  `stillCurrent = false`, **a value production could never produce**. A pure decision function
  tested in isolation proves the RULE is right; it says nothing about whether its INPUTS are
  computed correctly. That gap is precisely where this bug lived, and it is the second time this
  session a green test sat on top of broken behaviour (the first was `locked still allows open`,
  which asserted the deadlock as a contract). **Extracting a pure function is not enough — the
  wiring that feeds it needs its own seam.** Hence `executeSupervisorCall`: the flow itself is now
  the unit under test, driven by fakes, with `isCurrent` mutated mid-call.
- **Second fix:** `catch (CancellationException) { throw e }` was a no-op — nothing else was
  caught, so it protected against nothing. A real `IOException` escaped to the scope's handler and
  **the card silently reverted: the user tapped Start and nothing visibly happened.** Now caught
  and reported, while an AUTO probe still stays silent and cancellation still propagates.
- **Result / status:** **103 unit tests pass** (+10; 12 new flow-level cases), lint clean, both
  APKs signed. **Both neuter checks bite:** capturing `isCurrent()` up-front — i.e. re-creating
  exactly what shipped — fails `a card that changed during the call does not hand off`; removing
  `catch (Throwable)` fails both transport tests.
- **Test-harness gotcha:** `JSONObject().put(...)` returns **null** under the unit-test
  `android.jar` stub (`isReturnDefaultValues = true`), which NPEs on a non-null parameter. Build
  bare `JSONObject()` in fixtures. Five tests failed on this before I spotted it was the harness,
  not the code — and note the new `catch (Throwable)` is what surfaced it as a clean assertion
  failure rather than a crash.


### 2026-07-28 — Start poll outlived its card; `busy` could stick forever (owner review)
- **Change:** `SupervisorControls` now uses `rememberCoroutineScope()`; the `scope` parameter is
  deleted from the control, `ServerCard` and `ProfileListScreen`; `busy = false` moved into a
  `finally`; `CancellationException` re-thrown, not reported; new pure
  `ServerLifecycle.completionFor(action, running, stillCurrent)` + `SupervisorAction` enum.
  Commit `92c68e7`.
- **Why (defect 1 — scope):** the ~30s start poll ran on the Activity's `lifecycleScope`, so it
  outlived the card that spawned it. Press Start then navigate away (or edit the profile, which
  changes `stateKey`) and the coroutine kept writing to discarded state, **survived a rotation**
  (two polls racing the same server), and fired `onServerReady()` — **yanking the user into a
  WebView they had navigated away from**, possibly for a different server. The `scope` param was
  *deleted* rather than left unused: an Activity-wide scope threaded into a per-card control is
  the same trap as the nullable `session` — compiles fine, silently wrong.
- **Why (defect 2 — stuck `busy`):** `busy = false` sat at the tail of each branch, and there
  were **two such tails** (the login-blocked early return and the normal path). That is a
  structural flaw, not an oversight: any future early `return@launch` would miss it too. Any
  throw — a dropped socket mid-poll, a login timeout — escaped both, leaving `busy` stuck true
  and the card pinned to `TRANSITIONING` **for the rest of the process**, clearable only by
  restarting the app. This is a *permanent* version of the all-controls-disabled dead end fixed
  in `ed4aeb1`; the keeps-`canOpen` change mitigated it but the card still lied forever. Now one
  assignment site, in `finally`.
- **Also:** the stringly-typed `action` ("start"/"stop"/else) meant a typo degraded **silently to
  `/status`** — replaced with an exhaustive `SupervisorAction` enum. And the hand-off decision
  moved into the pure layer so "don't navigate a user who left" is testable off-device.
- **Result / status:** **93 unit tests pass** (+4), lint clean, both APKs build signed.
  **Neuter-verified:** ignoring `stillCurrent` fails `a stale start never hands off`.
- **Note:** `WebScreen` still takes `lifecycleScope` deliberately — its work is bound to a live
  WebView that owns the whole screen, not to a recycled list item, so Activity scope is correct
  there. Not every `scope` parameter is the same bug.
- **The recurring shape, now four for four:** every defect this session came from **conflating
  two things that merely co-occur** — "has a session"↔"may act", "is busy"↔"has no way out",
  "needs status"↔"may authenticate", and now "the call is running"↔"the card still exists".


### 2026-07-27 — Auto-probe was silently logging in on every app open (owner review)
- **Change:** New pure `ProbeTrigger` / `ProbePlan` / `ServerLifecycle.probePlan(trigger, signedIn)`.
  The auto-probe is now READ-ONLY; only an explicit tap may log in or report failure.
  Commit `9d8c263`.
- **Why:** The `ed4aeb1` fix made the auto-probe stop requiring a session — correct for
  discoverability, but it reused the SAME `run()` as the buttons, so opening the home screen
  fired a `POST /login` per un-signed-in server. Four consequences, none user-requested:
  1. N servers → N concurrent logins on every cold start.
  2. A wrong-password profile **auto-retried its bad password on every launch** — the classic
     route to account lockout.
  3. An SSO profile can never satisfy a headless login, so it hit `isLoginBlocking` →
     `failed = true` → the card read **"Unreachable" with a red error on every app open**, while
     the server was perfectly healthy. *"Not signed in yet" is not "unreachable".*
  4. **Self-reentry** (not in the owner's report, found while fixing): a successful login writes
     `cookieHost`, which is part of `stateKey`, which resets `running` to null and re-arms the
     `LaunchedEffect` — so the auto-login fired a second round.
- **Root cause:** one code path serving two callers with **completely different licence to cause
  side effects**. A user tap is an explicit request (logging in on their behalf is expected, and a
  failure deserves a visible error); a background probe has nobody asking, so it must not spend a
  login nor paint the card red. Fixed by splitting at that seam — a pure decision function — not
  by threading a boolean through the Composable.
- **Design note:** `AUTO` is read-only *by construction*: with no cookie `proceed = false` (the
  call never happens, card stays `UNKNOWN` / "Tap to check"); with a cookie it asks `/status` and
  nothing more. `mayLogIn` is hard-false for AUTO regardless of session, so no future edit can
  re-enable silent logins without failing a test.
- **Result / status:** **89 unit tests pass** (+5), lint clean, both APKs build signed.
  **Neuter-verified:** giving AUTO the USER plan fails exactly the three read-only guards
  (`auto probe without a session never logs in`, `auto probe never reports failure`,
  `auto probe with a session proceeds read only`).
- **Pattern worth remembering:** all three bugs this session (greyed Start, the
  all-disabled TRANSITIONING window, silent auto-login) were the same mistake in different
  clothes — **conflating two things that merely co-occur**: "has a session" with "is allowed to
  act", "is busy" with "has no way out", "needs a status" with "may authenticate".


### 2026-07-27 — Three dead ends left by the deadlock fix (owner review)
- **Change:** `TRANSITIONING` keeps `canOpen`; start-poll window is a named constant raised
  12s→30s with an actionable timeout message; `session` is now a REQUIRED non-null parameter;
  `NeedsInteractiveSso` is treated as blocking and `explainLoginBlock` is exhaustive.
  Commit `ed4aeb1`.
- **Why:** The owner reviewed `9a0a446` and found the deadlock fix had left three ways to
  reach the same *shape* of dead end:
  1. **The poll window disabled everything.** `busy` outranks `running` in `resolve()`, so the
     entire start-poll window rendered `TRANSITIONING`, whose capabilities were all-false
     including `canOpen`. A cold boot that outlasted 12s left the user on a spinner with
     **nothing to tap** — visually identical to the greyed-Start bug just removed. Worse, the
     12s came from an arbitrary `6 × 2000`. Open now stays live mid-start, the window is 30s,
     and expiry says what happened + what to do (it is NOT an error: `/start` was accepted).
  2. **`session: SessionManager? = null` could silently resurrect the deadlock.** Any call site
     that omitted it would skip login-then-act entirely and 401 on every supervisor call — with
     no compile error and no failing test. Now mandatory and non-null through the whole chain
     (`SupervisorControls` → `ServerCard` → `ProfileListScreen`), so the omission cannot build.
  3. **SSO wasn't in the blocking list at all** — worse than the reported "generic message".
     `run()` only intercepted BadCredentials/NoCredential/Error, so an SSO profile pressing
     Start *proceeded* to the supervisor call, 401'd, and got
     "the start/stop daemon isn't responding" — **blaming the host for an un-completed
     sign-in**. It now blocks, with copy pointing at Open, which for SSO genuinely IS the fix.
- **Design note:** `explainLoginBlock`'s `else` branch is gone — the `when` is exhaustive over
  `LoginResult`, so adding an outcome later is a compile error rather than a silent fallthrough
  to a generic string. That `else` is exactly how defect 3 hid.
- **Also added a general invariant test:** no `ServerState` may leave the user with zero
  available actions. The two deadlocks so far were both instances of that one rule, so it is now
  asserted across the whole enum instead of case by case.
- **Result / status:** **84 unit tests pass** (+7), lint clean, both APKs build signed.
  **Neuter-verified:** reverting all three fixes at once fails exactly the six new guards
  (`transitioning disables mutations but keeps open available`,
  `no state leaves the user with zero actions`, `start poll window is at least 30 seconds`,
  `start timeout message tells the user what to do next`,
  `interactive sso blocks the supervisor call`,
  `sso explanation points at open not at a generic failure`).
- **Still unverified on a device** — the 30s window is now defensible but still a guess; only a
  real cold boot tells us whether it is enough.


### 2026-07-27 — Fix: the app could never START a stopped server (chicken-and-egg deadlock)
- **Change:** Deleted the `LOCKED` state from `ServerLifecycle`; controls now do
  **login-then-act**; `resolve()` ranks a poll result above the cookie check; auto-probe no
  longer requires a session; a successful Start hands the user into the WebView
  (`onServerReady`). Commit `9a0a446`.
- **Why (root cause):** The whole reason `supervisor.py` exists is that *a stopped server can't
  answer "start me"*. The app had **reimplemented that exact deadlock**. A managed server that is
  down has no `cookieHost`, because the only thing that stamped it was a successful Open — and
  Open cannot succeed against a down server. That resolved to `LOCKED`, whose capabilities were
  `canStart = false`, with the copy "Open this server once to sign in". So: Start greyed → user
  taps Open → white screen → back → Start still greyed. **No escape.**
  The underlying mistake was conflating "establish a session" with "open the Tofu page". They are
  independent: the supervisor rides the **code-server** session, and code-server is the *proxy* —
  it stays up while Tofu is down, so `POST /login` succeeds regardless of Tofu's state. Nothing
  technical ever required a session before Start; the coupling was purely an implementation
  artifact.
- **Why LOCKED was deleted rather than relaxed:** once it permitted every action it became
  byte-identical to `UNKNOWN` in capabilities, label *and* chip colour. Two names for one state is
  a trap for the next reader, so the redundant one is gone rather than left as dead nuance.
- **Second, subtler bug found while fixing:** `resolve()` checked `!isSignedIn → LOCKED` **above**
  the `running` check. So even a login-then-act that *demonstrably reached the supervisor* would
  still render as signed-out until Room re-emitted the profile carrying the fresh `cookieHost`.
  A poll result is now authoritative — if the supervisor answered, we reached it.
- **Result / status:** **77 unit tests pass** (+2), lint clean, both APKs build signed.
  The three deadlock guards are **neuter-verified**: reverting `canStart` to `false` on the
  signed-out state fails all three (`signed out server must still be startable`,
  `managed server with no cookie resolves to unknown and can start`,
  `managed but no cookie is unknown not disabled`). The old test
  `locked still allows open` had *frozen the broken behaviour into an assertion* — it asserted
  `canStart == false` — which is why the bug survived the previous review pass. Worth remembering:
  a test can entrench a bug as easily as it can catch one.
- **Still unverified on a device.** The login-then-act path and the auto-open-after-start hand-off
  are exercised only by unit tests; both involve real network timing (`/start` returns before the
  port binds, so we poll up to 6×2s) and need on-device confirmation.
- **Also:** removed the duplicated `JOURNAL.md` block in `.gitignore`.

### 2026-07-27 — Product-grade UI pass + server-lifecycle as a first-class feature (v0.1.16)
- **Change:** (a) New design system `ui/theme/Theme.kt` — colors ported 1:1 from the SPA's
  `:root` CSS custom properties (`--bg-primary #0A0A0C`, `--accent #6E56CF`, radii 12/8/6),
  a tightened type scale, and a warm-cream light scheme matching the launcher icon.
  Deliberately NOT Material You: dynamic color would recolor native chrome against the
  WebView's fixed palette, so the shell would clash with its own content.
  (b) New `ui/Components.kt` — `ServerAvatar` (monogram tile), `StatusDot` (RUNNING breathes),
  `StatusChip`. (c) Home / form / WebView rewritten. (d) New pure state machine
  `session/ServerLifecycle.kt` + `ServerUrl.displayLabel`.
- **Why:** The UI was stock-Material scaffolding: every server row was a bold alias over a
  ~90-char sandbox URL (so rows were visually identical), whether a server was UP was invisible
  until you opened it, the auth picker was three `TextButton`s prefixed with `●`/`○` characters,
  and the supervisor Start/Stop — the "app runs the server" feature — was a text row that never
  polled, so it read `Server: —` until you hunted for Refresh.
- **On "the app should spin up a server itself" — RESEARCHED, verdict: not on-device.**
  Running the real Tofu server *inside the APK* is not feasible, and this is now grounded rather
  than assumed. `chatui/requirements.txt` has 33 direct deps incl. **playwright** (needs a
  Chromium binary) and the **pymupdf==1.27.2.3 trio**. Two hard blockers, verified against
  Google's own docs: (1) **W^X** — Android 10+ untrusted apps cannot invoke `execve()` on files
  in the app's home directory and cannot map `PROT_EXEC` from a writable fd
  (developer.android.com/about/versions/10/behavior-changes-10, "移除了应用主目录的执行权限"),
  so a runtime `pip install` of any native wheel is dead. A *bundled* interpreter (Chaquopy ships
  `.so` inside the APK) is legal, but Chaquopy's curated index carries no pymupdf / tiktoken /
  orjson / playwright. (2) **Playwright has no Android host** — its `_android` API is the inverse
  (a desktop host driving on-device Chrome over adb). Also relevant if revisited: Android 14
  requires every foreground service to declare a type and none fits "local server" (→ `specialUse`,
  Play-reviewed); Android 15 caps `dataSync` / `mediaProcessing` at 6h per 24h.
  **So "self-hosting" ships as the supervisor path instead** — which already existed and simply
  wasn't surfaced. It is now a labelled "Server control" toggle in the form, plus a live status
  chip and Start/Stop on each card that auto-probes on open.
- **Result / status:** **75 unit tests pass, 0 failures** (was 56; +19 covering the lifecycle
  state machine and `displayLabel`), `lintDebug` clean, both `app-debug.apk` (17M) and
  `app-release.apk` (12M) build signed. **NOT yet verified on a device** — the visual result and
  the auto-probe behaviour need on-device eyes.
- **Reviewer-found defects fixed in this pass** (sub-agent review; all four were real):
  1. `SupervisorControls` keyed `running`/`busy`/`failed` on `profile.id` alone. Editing a
     profile's URL clears `cookieHost` but leaves `id` unchanged, so the card kept a **stale
     Running badge**, and the auto-probe (guarded on `running == null`) never re-fired to correct
     it. Now keyed on `(id, baseUrl, cookieHost, projectPath)`.
  2. Reporting state upward via `LaunchedEffect(state)` fired on every card's first composition,
     and each parent write recomposes the whole list (the header reads the aggregate) → a
     full-list recompose per card. Now reported only on an actual state *change*.
  3. `enableEdgeToEdge()` was added without insets: the WebView's bottom — the SPA composer, the
     control the user needs most — sat under the gesture-nav bar. Added `navigationBarsPadding()`
     to the WebView, the form's save area and the home footer; `statusBarsPadding()` to both headers.
  4. Start/Stop/Check were ~30dp tall, under the 48dp touch-target minimum. Raised via `defaultMinSize`.
- **Also:** WebScreen gained a branded first-load cover (avatar + real `onProgressChanged`
  percentage). Rationale: a WebView shell has no browser chrome, so a slow boot rendered as a bare
  white rectangle — **indistinguishable from the white-screen failure mode** of convo
  `mrova3t92jffm7`, i.e. a slow server read as a broken app. The two always-on debug FABs also sat
  on top of the SPA's own controls; they now collapse behind a single low-alpha handle.
- **Env note:** `/tmp/jdk17` and `/tmp/android-sdk` from the 07-17/07-18 sessions were **wiped**
  (they live in `/tmp`). Rebuilt both through the corporate proxy `http://your-corporate-proxy:8412`
  (Temurin 17 via the Adoptium API; `sdkmanager --proxy_host/--proxy_port` for
  `platforms;android-34` + `build-tools;34.0.0`) and re-wrote `~/.gradle/gradle.properties` with
  the `systemProp.*.proxy*` lines. **Expect to redo this every session — `/tmp` is not durable.**
  The conditional Robolectric-offline block added on 07-18 correctly *skipped* (no
  `.testharness/libs` present this time) and Robolectric resolved its runtime jar online via the
  proxy — confirming that conditional was the right call.

### 2026-07-18 — NEXT UP / KNOWN GAP — UI/WebView layer has zero automated test coverage
- **Observation:** All 56 passing unit tests live in the logic layer (session / URL / cookie / Room migration). The UI/WebView layer (`WebScreen.kt` and its file-chooser + reauth glue) has **0% automated coverage**.
- **Why it matters:** Both real production bugs we hit recently landed in exactly this untested layer — (a) Shanghai server white-screen (see convo `mrova3t92jffm7`), and (b) multi-photo upload only sending one image (the 07-17 `ClipData` fix below). Today those fixes are validated only by manual on-device eyeballing, which is the root of the "underperforming/fragile" feeling.
- **Proposed next ticket (do NOT do this in the current session — planning only):**
  1. Extract a **testable seam**: pull the URI-resolution logic out of the `WebScreen` Composable into a pure function, e.g. `fun resolveChooserUris(intent: Intent?, fallback: () -> Array<Uri>): Array<Uri>` that reads `intent.clipData` first and falls back to `FileChooserParams.parseResult()`. Keep the Composable a thin caller.
  2. Add a **Robolectric regression test** that locks the contract "multi-select of N items → N URIs returned" (build an `Intent` with a `ClipData` of N items, assert the resolver returns N) plus the single-select fallback path. This is the guardrail that would have caught the 07-17 bug.
- **Status:** Logged as the strategic follow-up to harden the app. Not started; next session can pick it up directly.

### 2026-07-18 — Full local test/build pipeline green; Robolectric offline fix
- **Change:** Added `testOptions.unitTests.all { systemProperty("robolectric.offline","true"); systemProperty("robolectric.dependency.dir", <repo>/.testharness/libs) }` to `app/build.gradle.kts`.
- **Why:** Running the real CI target `./gradlew :app:testDebugUnitTest` failed 5/56 tests — every Robolectric test (`CookieBridgeRobolectricTest` ×3, `ProfileMigrationTest` ×2) died at `MavenArtifactFetcher.java:129` with `UnknownHostException`. Robolectric fetches its `android-all-instrumented` runtime jar from Maven *at test time* using its OWN resolver, which does NOT honour the `http_proxy` env var, so it can't resolve the host on this network-restricted box. The jars it needs (`android-all-instrumented-13-…` for the `@Config(sdk=[33])` pin) were already cached in `.testharness/libs/` by `fetch-test-deps.sh`, so pointing Robolectric there with offline mode is the clean fix — no code change, no network. This mirrors what the hand-rolled `test-local.sh` already did via `-Drobolectric.*` flags; now the Gradle path does it too.
- **Result / status:** **All 56 unit tests pass, 0 failures/errors** (`./gradlew :app:testDebugUnitTest --offline` → BUILD SUCCESSFUL). `lintDebug` clean (0 errors, 18 advisory warnings: 10 GradleDependency, 4 ObsoleteSdkInt, 2 MonochromeLauncherIcon, 1 ModifierParameter, 1 DataExtractionRules). `assembleDebug` → signed `app-debug.apk` (17M); `assembleRelease` → signed `app-release.apk` (12M, lintVitalRelease passed). The full pipeline (test + lint + both APK variants) is now reproducible locally.
- **Env note:** Toolchain from the 07-17 session persisted intact — `/tmp/jdk17`, `/tmp/android-sdk`, `~/.gradle/gradle.properties` proxy, and all 51 harness jars in `.testharness/libs`. Two-tier network model confirmed: Gradle's own resolver honours the proxy (online resolve works), Robolectric's does NOT (needs the offline dir). Test-only deps (junit/robolectric/androidx.test/coroutines-test) and the androidTest transitive `androidx.collection:collection-jvm:1.4.0` were not in cache from the assemble-only 07-17 run, so first test+lint run must be ONLINE to warm the cache; thereafter `--offline` works.

### 2026-07-17 — Fix multi-photo upload only sending one image (WebView file chooser)
- **Change:** In `WebScreen.kt`, the `fileChooserLauncher` result callback now reads all URIs from the Intent's `ClipData` when present, falling back to `FileChooserParams.parseResult()` only for the single-file case.
- **Why:** The attach button already requested multi-select correctly (`MODE_OPEN_MULTIPLE` → `EXTRA_ALLOW_MULTIPLE`), and the SPA's `handleFileUpload` iterates `e.target.files`. But on a multi-selection the Android picker returns the URIs in `intent.clipData`, leaving `intent.getData()` null. The framework's `parseResult()` only reads `getData()`, so it collapsed the selection to a single file (a well-documented WebView quirk — see Xamarin.Forms#15341). The bug was in the Android shell, not the web app.
- **Result / status:** Code fix applied and **compile-verified**. Built with `:app:assembleDebug` → BUILD SUCCESSFUL, signed `app-debug.apk` (17M) produced. Still needs an on-device check that selecting N photos actually uploads N.
- **Build environment note (for next session):** This sandbox has NO Android toolchain by default and only JDK 7/8 installed, but the corporate HTTP proxy `http://your-corporate-proxy:8412` makes builds fully possible. What was needed: (1) AGP 8.5.2 requires **JDK 17** — downloaded Temurin 17 to `/tmp/jdk17` via the proxy (Adoptium API). (2) The stock JDK 8u45 `cacerts` predates Let's Encrypt's ISRG Root X1, so Maven Central TLS failed with `PKIX path building failed`; JDK 17 ships the root so this is moot once on 17. (3) No Android SDK — installed cmdline-tools + `platforms;android-34` + `build-tools;34.0.0` to `/tmp/android-sdk` via `sdkmanager --proxy_host=your-corporate-proxy --proxy_port=8412`, wrote `local.properties` with `sdk.dir=/tmp/android-sdk`. (4) Gradle proxy: `~/.gradle/gradle.properties` with `systemProp.https.proxyHost/Port`. Build cmd: `JAVA_HOME=/tmp/jdk17 ANDROID_HOME=/tmp/android-sdk ./gradlew :app:assembleDebug`.
