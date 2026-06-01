---
name: relay-billing-architecture
description: Multi-tenant relay billing — ALL 6 phases complete (users, payments, janitor, admin UI)
enabled: true
tags: [billing, relay, multi-tenant, architecture]
created: 2026-05-27T04:24:07Z
updated: 2026-05-27T04:46:31Z
---

# Tofu Relay — Billing & Multi-Tenant Architecture

## Status: ALL PHASES COMPLETE (2026-05-27)

The relay-billing layer activates in `auth_mode=multi-user` deployments
and is a no-op everywhere else. Personal/private installs leave the
tables empty; the unified auth gate's `local_admin_context()` covers
single-user use.

## Unit: micro-credits (µ)

* 1 credit = 1,000,000 µ
* 1 credit ≈ US $0.001 at the canonical conversion (1000 credits/$)
* All amounts in code & DB are **integer micro-credits** — no floats,
  no rounding bugs. Conversion to display happens only at the
  presentation boundary (`format_credits`). Provider minor units
  (cents / 分) convert via `lib.billing.payments._common.minor_to_micro`.

## Layered API (`lib/billing/`)

| Module | Purpose |
|---|---|
| `pricing.py` | Per-model unit prices, hot-reloaded from `data/config/pricing.json`. Family-prefix fallback. |
| `cost.py` | Pure tokens-to-credits arithmetic. `compute_request_cost`, `estimate_request_cost`, margin applied here. |
| `ledger.py` | Append-only log. **Source of truth.** `find_existing()` for idempotency on `(kind, ref_type, ref_id)`. |
| `wallet.py` | Atomic debit/deposit/reserve/settle. Per-user lock + DB transaction. |
| `users.py` | Tenant user CRUD + bcrypt-or-PBKDF2 password hashing. Table `tenant_users` (avoids collision with chat-schema `users`). |
| `janitor.py` | Background sweep that releases stale reservations (>30min). Spawned by `server.py` boot. |
| `payments/__init__.py` | Facade. |
| `payments/_common.py` | `record_payment` + `mark_payment_settled` — both idempotent on `(provider, provider_id)`. |
| `payments/stripe.py` | Stripe webhook with HMAC-SHA256 signature verify. Handles `payment_intent.succeeded` and `checkout.session.completed`. |
| `payments/alipay.py` | Alipay async-notify with RSA2 signature verify (via `cryptography` lib). Plus `create_alipay_order()` for synchronous order minting. |

## Reservation choreography

Three ledger rows for a billable LLM request:
1. `reserve(-estimate)` — pre-flight hold, blocks if balance too low (HTTP **402**)
2. `reserve_release(+estimate)` — refunds the hold on completion
3. `debit(-actual)` — final usage charge

`settle()` posts (2) + (3) atomically. Idempotent on `ref_id=task_id`.

`routes/api_v1/chat.py` does pre-flight reserve before `spawn_task()`,
post-flight settle after the task terminates. Estimator uses
`prompt_chars // 4` as a cheap proxy for prompt-token count, plus
the caller's `max_tokens` (or 1024 default), with 1.5× headroom.

## Janitor

`lib/billing/janitor.py:start_janitor()` spawns a daemon thread.
Sweeps every 5 min for `reserve` ledger entries older than 30 min
that have no matching `reserve_release` or `debit`. Skips refs whose
`ref_id` is still a running task in `lib.tasks_pkg.tasks`.
Idempotent — second sweep is a no-op.

Env knobs: `TOFU_BILLING_JANITOR_INTERVAL` (default 300),
`TOFU_BILLING_JANITOR_TTL` (default 1800), `TOFU_BILLING_JANITOR=0`
to disable.

## DB schema (v20)

`tenant_users`, `billing_ledger`, `billing_wallets`,
`billing_redeem_codes`, `billing_payments`. The api_keys row gets a
new `user_id` field (in JSON store, not DB). `AuthContext.user_id`
flows through to chat dispatch and billing.

## Routes

```
# Public
GET  /api/v1/billing/pricing              public price card
POST /api/v1/billing/webhooks/stripe      Stripe webhook receiver
POST /api/v1/billing/webhooks/alipay      Alipay async-notify
POST /api/v1/users/signup                 (gated by relay.json)
POST /api/v1/users/login
POST /api/v1/users/logout
GET  /api/v1/users/me

# Authenticated (self / admin via ?user_id=)
GET  /api/v1/billing/wallet
GET  /api/v1/billing/ledger
GET  /api/v1/billing/payments
POST /api/v1/billing/redeem
POST /api/v1/billing/checkout             returns redirect URL

# Admin only
POST /api/v1/billing/deposit              manual top-up
POST /api/v1/billing/debit                manual debit
POST /api/v1/billing/redeem-codes         mint a batch
GET  /api/v1/billing/redeem-codes         list codes
GET  /api/v1/users                        list tenant users
POST /api/v1/users                        admin onboarding
GET  /api/v1/users/{id}
PATCH /api/v1/users/{id}                  role / status updates
POST /api/v1/users/{id}/keys              mint key for user
```

## UI surfaces

**Operator (you):** continues to use `/` (the chat UI) for daily work.
Settings → 4 new tabs **only when** `auth_mode=multi-user` AND your
account has `admin` scope:
  * **用户 (Users)** — list, top-up, suspend
  * **定价 (Pricing)** — read-only price table view
  * **兑换码 (Redeem Codes)** — mint batches with one click
  * **支付 (Payments)** — Stripe/Alipay receipts

Implementation: `static/js/relay-admin.js` + 4 panels in
`index.html`. Visibility gate: `_shouldShowAdminTabs()` calls
`/api/v1/auth/mode` + `/api/v1/users/me`; tabs hidden via inline
`style="display:none"` until both checks pass.

**Customers:** lightweight standalone HTML pages.
  * `/login`, `/signup` — `static/login.html` (single page, switches
    by URL hash)
  * `/dashboard` — `static/dashboard.html` (Wallet / Keys / Usage /
    Docs / Account tabs)

Both customer pages use plain fetch against `/api/v1/*` — no bundle
dependency, no chat-UI overhead.

## Hot path: chat.py reserve + settle

```python
# Pre-flight (after create_task, before spawn):
if auth.user_id:
    reservation_micro = estimate_request_cost(model, prompt_tokens, max_completion)
    try:
        reserve(auth.user_id, reservation_micro, ref_id=task['id'])
        task['_billing_reservation_micro'] = reservation_micro
    except InsufficientFunds:
        return api_error(..., status=402)

# Post-flight (after task terminal):
if auth.user_id:
    cost = compute_request_cost(model, input_tok, output_tok)
    if reserved > 0:
        settle(auth.user_id, reserved_micro=reserved,
               actual_micro=cost.micro, ref_id=task['id'])
    elif cost.micro > 0:
        debit(auth.user_id, cost.micro, ref_id=task['id'],
              allow_negative=True)
```

## Operator config files

* `data/config/pricing.json` — per-model prices (auto-seeded on first
  read, hot-reloadable via mtime check)
* `data/config/relay.json` — `{signup_enabled, signup_default_role,
  signup_welcome_credit_micro}`
* `data/config/payments.json` — `{stripe.{secret_key, webhook_secret},
  alipay.{app_id, private_key_pem, alipay_public_key_pem},
  credit_per_minor_unit}`

## Testing

* `tests/test_billing.py` (14) — pricing, cost, ledger, wallet, reserve/settle
* `tests/test_billing_phase2.py` (14) — users (signup/login/auth),
  payments (record/settle idempotency), Stripe webhook (signature,
  unknown event, payment_intent.succeeded), Alipay sign-string
  canonicalisation, janitor (release stale, skip running)
* `tests/conftest.py` pins `TOFU_AUTH_MODE=private` so suites that
  expect 401 keep working.

168/168 tests pass across the auth + billing + chat surfaces.

## Phases NOT yet built (future work)

- Per-user payments view in admin Users tab (drill-down). Currently
  the Payments tab shows the operator's own payments only.
- Pricing-table editor (currently read-only in the UI). Operator can
  edit `data/config/pricing.json` directly; hot-reload picks it up.
- Email verification flow for signup.
- OIDC / SSO login (the `metadata.oidc_sub` field is reserved).
- Stripe Checkout Session creation route (currently returns 501;
  operator must mint the session via Stripe's own API and point
  `metadata.user_id` at us).
- Per-user `/api/v1/billing/usage-by-day` analytics endpoint
  (the dashboard's chart currently uses the per-key `/api/v1/usage`
  which doesn't aggregate across keys-of-same-user).

## Key invariants (don't break)

1. **Ledger is append-only.** A "refund" is a positive entry, not
   removal of the original debit.
2. **Wallet balance is a cache.** `recompute_balance()` from the
   ledger always equals the cached value.
3. **Idempotent on `(user_id, kind, ref_type, ref_id)`.** Re-posting
   the same operation is a no-op. Webhook re-deliveries are safe.
4. **Integer micro-credits everywhere.** Floats are forbidden in the
   DB and all internal APIs. Only `format_credits()` produces a
   float, and only for display.
5. **`tenant_users` ≠ `users`.** The chat schema's `users` table is a
   different shape. Don't confuse them.
6. **Webhook routes are public.** Auth via signature, not Bearer.
   `/api/v1/billing/webhooks/stripe` and `/api/v1/billing/webhooks/alipay`
   are in `_PUBLIC_EXACT`.
7. **Settle BEFORE response is returned.** Pre-flight reserve also
   commits before `spawn_task()` runs. Both ensure a crashed/aborted
   task can never lose money for the user (janitor catches stragglers).

