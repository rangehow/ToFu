# Outbound network authority

Tofu-owned outbound requests enter the shared network policy automatically.
Application code uses `lib/http_transport/client.py`; libraries that need a
session use the managed client factories in `lib/http_transport/`. Provider
selection, credentials, message retries and business authorization remain with
their domain owners.

## Routing and delivery

`lib/proxy.py` owns configured proxies, bypass rules and eligible route
construction. `lib/network_routes.py` owns reachability, concurrent probe sharing,
preferred routes and recovery. Health belongs to an origin (scheme, host,
effective port) and one concrete route, never to every destination behind a
proxy. An enabled global proxy serves any external origin. Subscription scope
is an explicit request policy and includes subscription plus global entries;
new subscription integrations do not need a new routing implementation.
Existing provider host recognition supplies the default for legacy callers.
Desktop delegation keeps its independent owner/host authorization boundary.

Loopback, registered direct hosts and configured/environment bypasses remain
direct. An explicit per-request proxy pins that request's route. Probe traffic
is the lower transport boundary: it never recursively requests route admission,
forwards application credentials or invokes an arbitrary business POST.
Generic HTTP probes use HEAD on the credential-free origin; the documented
subscription health probe retains its provider-specific policy.

Cold routes share a concurrent probe race and return the first usable path.
Healthy cached routes return immediately while due alternatives recover.
Configuration changes invalidate measurements; unchanged settings preserve
them. Probe results that started before a real request or topology change
cannot overwrite fresher evidence.

Requests and synchronous/asynchronous HTTPX (including MCP httpx2) route at their adapter/transport boundary, after request
preparation and before response-body consumption. This preserves multipart
bodies, redirects, streaming and client hooks. Only proven connection-setup
failures advance to another eligible route. Response status, response read
timeout, write failure and ambiguous disconnect never replay the request.
Each route is attempted once in that request's route plan; existing domain
retry contracts remain outside this layer.

Problem: only known subscription endpoints received route selection and
connection failover, while generic and async requests could repeatedly select
an unavailable proxy. Decision: common route admission at the HTTP transport
boundary, with request-scoped policy and typed failures. Alternatives rejected:
per-channel retries; changing timeout numbers; probing with business requests;
global monkeypatching of requests/httpx; treating every timeout as replay-safe.

## Provider pacing

A provider that limits requests per window states the limit only in its
responses. `lib/http_transport/provider_pacing.py` turns those signals into
first-in, first-out send admission for one quota lane: an opaque, non-secret
identity the request owner names (model dispatch uses `Slot.quota_lane()`:
routed provider, credential ID, wire model) with the window the provider counts
that quota over (dispatch: `quota_watch.WINDOW_S`, the gateway's minute; a
`RateLimit-Policy` window replaces it). The provider's window is the only time
constant; nothing is configured.

- **Signals.** A refusal naming a request limit over the window (the gateway
  body `App:**NNNN在模型:M每分钟请求次数超过限制`) teaches the limit: how many of
  the lane's requests were written within the window before the refused one.
  `Retry-After` (RFC 9110) and an exhausted request window of the
  `x-ratelimit-*-requests`, `anthropic-ratelimit-requests-*` or `RateLimit-*`
  headers hold the whole lane until the named instant; a limit those headers
  state replaces a learned one. The LLM transports attach a refused response's
  parsed headers to its `RateLimitError` (`attach_provider_rate_signals`);
  bodies stay with their domain classifier.
- **Admission.** A lane is unpaced until it has a limit. A paced lane admits a
  request while fewer than the limit occupy the trailing window, otherwise at
  the instant the oldest leaves it. Grants follow the order each request
  first joined: one that comes back (refused, or moved to another lane)
  passes its first ticket's order and keeps its turn, so none starves. A
  request occupies the window from when the managed transport writes it
  (`note_wire_send` in `send_routed_request` and `RoutingAsyncTransport`), not
  from its grant: preparing a large body takes long enough that an older
  request can reach the provider after a newer one is granted. It occupies
  one window plus the round trip of the lane's latest refusal, which bounds
  how late after the write the provider's limit check can count it; refusals
  at the window's edge would otherwise teach a limit one too low.
- **Learned limits stay current.** A learned limit may describe a moment when
  another workload shared the quota. While requests wait behind it, one
  request per provider window goes beyond it: served, the limit retires (the
  provider took more); refused, it is learned again. A declared limit needs no
  probe.
- **Ownership.** The pacer never sends, retries, selects a route or reads a
  body. Its owner waits out `PacingTicket.poll()` delays with its own
  abortable, metered wait, binds the transport call with `paced_send`, and
  reports the provider's refusal or answer. Model dispatch paces after the
  shared-contention gate and before transport
  (`lib/llm_dispatch/_api_contention.py`); immediate-only callers yield with
  the lane's delay instead of waiting.
- **Choosing a lane.** `ProviderPacer.admission_delay` says how long a request
  would wait on a lane, without joining it. Model dispatch prefers, among the
  eligible slots, the ones whose request could be sent soonest (pacing delay
  and the gate's probe delay), so automatic work spreads over the keys
  configured for a Model; a warm conversation keeps its key until that key's
  queue is a full provider window deep (`LLMDispatcher._select`), because a
  move pays a cold prefill.
- **What a lane counts.** Every request this process sends through model
  dispatch on that credential and wire model: conversation rounds, auxiliary
  calls and request-scoped route groups alike. Another process on the same
  key (Tofu's API host or execution host, an evaluation relay) is visible only
  through the provider: a refusal teaches this process its share of the
  window, and the capacity probe retires that share when the other workload
  leaves.
- **Retention.** State is per process and in memory. A lane with waiting or
  in-window requests is never evicted; idle lanes keep their learned limit
  under the route-health retention bound (64 idle lanes, 24-hour expiry).

Problem (2026-11-13): Tofu's own bursts of 10-17 kimi-k3.1-pro requests a
minute on AppId …5409 were sent until the gateway refused them (six Watchdog
quota episodes 04:54-05:51; about ten conversation rounds accepted per
minute). The shared-contention gate then held the rest behind 0.3→15 s probes,
each one refused until the window freed (holds up to 35.8 s), and released
every waiter at once on a success. Nothing paced a send before a refusal, and
429 headers were dropped before classification. Decision: pace each quota lane
from the provider's own signals at this authority, first in, first out, with
the occupancy measured from the actual write. Alternatives rejected: a
configured requests-per-minute per key (a setting that ages; gateway limits
change per AppId and model); shorter gate probe spacing (still sends into a
full window and keeps the herd release); moving every paced request to the
freest key at once (a warm conversation's cold prefill can cost more time than
its queue; the owner lifted the cost constraint on 2026-11-13, not the time);
pacing inside one domain such as translation (another domain-local selector);
and measuring occupancy from the grant (refused at window boundaries in the
loopback spec when an older, larger request reached the provider later).

## Verification

The executable contract is `tests/proxy/test_unified_network.py`, plus route,
proxy, HTTP-client, subscription-stream and channel suites. Fault injection
covers unavailable routes, one delivered body, multipart preparation, ambiguous
delivery, bypass rules, origin isolation and both async response modes.
Provider pacing is specified by `tests/proxy/test_provider_pacing.py` (fake
clock), `tests/llm_dispatch/test_provider_pacing_admission.py` (dispatch
admission and lane choice) and
`tests/llm_dispatch/test_provider_pacing_loopback.py`: a loopback provider
enforcing a per-minute limit per key, driven through real dispatch and both
managed transports on a scaled clock.


## Integration and lifecycle

OAuth helpers in `lib/oauth/http.py` and model request preparation carry explicit
subscription scope, including future provider hosts. The public
`lib.http_transport` facade serves optional plugins. Search 0.12.27 exposes
prepared-Requests, HTTPX-client and anonymous-browser proxy ports; its host
binding uses this same authority, without an independent HTTP failover chain.
Browser contexts select their initial destination's route; their subresources
and redirects retain the browser's context-wide proxy semantics. Command
transport probes reuse the route specifications with their credential policy.
Standalone SDKs and pre-dependency bootstrap retain their own transport lifetime;
they must receive policy through their explicit environment/client composition
ports, rather than importing a running application's mutable state.
A long-lived SDK session that Tofu restarts (the Feishu bot) is admitted on
the route it actually takes before each session, waits that route's
`RouteManager.circuit_delay` (policy circuits included) when not admitted, and
reports each session end as a failed request and its first delivered event as
a successful one, so restarts follow the shared circuit schedule
([integrations_api.md](modules/integrations_api.md#feishu-reconnection)).
A library that would fetch an asset on first use does not run on a request
path: tokenizer vocabularies ship with the install, so token counting opens no
connection ([context engineering](modules/context_engineering.md)).

Connection pools retain idle connections without limiting active requests.
Configuration generations retire idle proxy pools; active response streams
release theirs after closure. Async cancellation joins route-wait and cleanup
ownership. A cancelled synchronous model attempt cannot send after route probing.

**Owner interruption.** A synchronous owner that must stop a request in
flight binds a `RequestInterruption` around it
(`outbound.network_interruption`, `lib/http_transport/interruption.py`).
Every managed connection the binding thread's requests use, new or pooled,
joins the handle; `interrupt()` is final and shuts those sockets down, so
connection setup or a response wait (a long poll the server holds) fails at
once, and a later request under the handle is refused before route selection,
like `network_cancellation`. The owner's interruption never counts against a
route's health. Requests outside the binding, including the same thread's
later ones on a pooled connection, are untouched. *Problem (2026-11-10):* the
微信 channel's `close()` could only set a flag while its run thread sat in
iLink's 35 s `getupdates` hold, so each restart outlived the session's stop
contract. *Decision:* interruption at the connection boundary every managed
session already shares. *Alternatives rejected:* closing the session's pool
(urllib3 closes only idle connections), closing the socket (a blocked reader
is not woken on Linux), shortening the hold or the read timeout (tuned knobs
that still ignore the close), and abandoning the request on a helper thread
(the poll stays live after its owner stopped). Interrupting an in-flight send
makes its outcome unknown, so owners bind it around waits, not around sends
whose duplicate they could not reconcile.
HTTPX CONNECT phase is observed through httpcore tracing: its read timeout is
connection setup, while application-header/body read timeouts remain ambiguous.

Health retention reuses the existing netpath cache policy: 64 idle origins,
24-hour expiry, with in-flight probes protected. No request or probe is refused
to fit that cache. The independent netpath background poller is retired; Settings
projects the common route health. A controlled 1,000-origin replay retained
64 origins / 77,888 traced bytes versus 1,000 / 411,898 before the cache bound.
1,000 warm route selections issued zero probes (median 6.187 microseconds on
this host). These measurements cover routing overhead, not Internet latency.
