# Meituan bundle

Release-tiered site content for the Meituan internal distribution
(`docs/SITE_PACKS.md`). The `internal`/`personal` export modes include this
directory; the `opensource` mode excludes it whole, so the core tree keeps
zero references to company hosts.

## Contents

- `site_packs/modelplaza.json` — ModelPlaza catalogue search/detail and the
  same-origin `compareModels` attribute API (`model-info`).
- `site_packs/friday.json` — Friday Skills Market search/detail over the
  deep-research pipeline.
- `provider_templates/meituan.json` — internal provider offering recipes.
- `tests/` — intranet acceptance scripts (need a live SSO session; never run
  in opensource CI).

## SSO evidence notes (kept from the legacy `DEFAULT_SOURCES` comments)

The `sankuai.com` auth-source entry (projected by the ModelPlaza pack) is an
SSO-walled internal site: anonymous rendering reaches the SSO login page
(ssosv.sankuai.com), never the content, so only a replayed logged-in session
can read it. Reaching these hosts at all also requires listing them in
tofu-search's `allow_private_hosts` — they resolve to RFC-1918 addresses
behind a rotating internal load balancer, so the SSRF guard blocks them by
default. The two gates are deliberately separate: connecting an account must
never silently grant an SSRF exemption.

Cookie names come from a MEASURED anonymous run of the SSO chain (Playwright,
networkidle), not guesses:

- PROVEN, pre-login: the chain sets only telemetry / device-fingerprint /
  PKCE-context cookies — `_lxsdk*`, `WEBDFPID`, `logan_session_token`,
  `webDeviceUuid`, `ctxId` / `ctxId-<client_id>` (httpOnly, per-login
  attempt), and `com.sankuai.speechfe.sft_strategy` on the app host. None of
  these is a session credential.
- NOT PROVEN: the post-login session cookie's name is only issued after a
  successful QR scan, so an anonymous probe cannot see it. `ssoid` is the
  long-standing assumption (it is also the wait-for anchor in
  `tests/_qr_login_capture.py`) but remains UNVERIFIED against a real
  logged-in session.

Hence nothing is marked `required`: a required field with a wrong name would
reject a perfectly good credential paste at the store boundary — a worse
failure than storing a set later found incomplete. Promote to `required`
once a real login confirms the name.
