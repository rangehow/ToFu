"""Fair-comparison model relay for native sandbox Terminal-Bench runs.

Both harness arms call this one relay; it forwards every chat completion to the
same FRIDAY deployment with the same AppId pool, so model identity, rate limits
and keys are identical across arms. ``--temperature/--top-p/--max-tokens``
override every request identically, which is how a protocol's sampling is
enforced. Credentials are read from the live Tofu model-routing authority
inside this process only; sandboxes see a key-less URL.

The gateway's Mt-Session-Id carrier is forwarded unchanged for every arm and
retry when the client sends it. No identity is synthesized or put in the
prompt. On the chat face other client headers, including authorization, are
not forwarded; the Anthropic face's allow-list is below.

The Anthropic Messages face (``--anthropic-model``) forwards each harness's own
request to FRIDAY's Anthropic face with only ``model`` pinned: sampling,
thinking, effort and cache_control stay the harness's, because those are what
a cache comparison measures. Besides Mt-Session-Id it forwards the request
semantics headers ``anthropic-version`` and ``anthropic-beta`` and the gateway
trace id ``M-TraceId``. One normalization is documented in
:func:`compact_pi_effort_markers`: Pi's empty mid-conversation effort markers,
which FRIDAY rejects, become the top-level effort their last marker asks for.
``--preserve-pi-effort-markers`` disables it. Each record carries the request's cache-relevant facts
(breakpoints and TTLs, digests of tools, system and every message without
``cache_control``, joined tool-name bytes, a digest of the affinity value) and
the provider-reported usage, never prompt text or the affinity value itself.

Entry point: ``python -m evaluations.terminal_bench.relay --log FILE ...``.
Dependencies: aiohttp (server/upstream), httpx (credential reveal).

The Responses face (``--responses-model``) is the same passthrough for
harnesses that speak only the OpenAI Responses API (Codex): only ``model`` is
pinned and the usage is read from the terminal ``response.*`` event. A
request without ``max_output_tokens`` gets ``--responses-max-output-tokens``
when that is set, and the record says so.

Keys: only credentials whose key hint ends in a ``--key-suffixes`` entry are
ever revealed. ``--max-rpm`` bounds upstream request starts (every face,
every retry) within any trailing minute, an owner-assigned key budget.
``--pin-file`` holds per-face request pins every arm gets, for a model whose
reasoning or sampling fields differ from the chat-face flags above:
``{"chat"|"anthropic"|"responses": {"set": {path: value}, "omit": [path]}}``
with dotted paths into the JSON body (``output_config.effort``); each record
lists the replaced values under ``pinned``.
``--max-tpm`` bounds the estimated input tokens of those starts the same way
(eval-quota rule 12, FRIDAY's second per-AppId+model limit): each estimate
comes from the request body before it is sent and errs high. A 429 naming
tpm halves the token budget, any other 429 the request budget. With
``--budget-file`` both budgets live in a JSON file: halvings are written
there, so a restarted relay keeps them, and an operator (or a guard) can lower
them without a restart. ``--assume-full-window`` starts a successor relay as
if its draining predecessor had used both whole budgets in the last minute.
``--first-body-dir`` writes the first request body of each (arm, task) for
the injected-context audit; every request record lists the
``tofu-context`` block ids and date markers its prompt carries.

Ingress (``relay_ingress``): with ``--ingress-key-file`` every route below answers
HTTP 401 before its handler, and before any upstream call, unless the request
carries the run's relay key as ``Authorization: Bearer`` or ``x-api-key``; the
runner puts that key in the agents' API-key placeholder. A relay without the
flag listens on loopback only. A key FRIDAY reports disabled (HTTP 401
``App ID 已被禁用``) is retired like any 401/402/403 key; once no key is left
every request gets a typed non-retryable HTTP 403 (``relay_key_disabled``)
instead of a retryable 502.

Routes (``<arm>`` is a free label used only for accounting; ``<task>`` names
the trial's task so every request is attributed to its trial):
  POST /arm/<arm>/task/<task>/v1/chat/completions
       (also without /v1, without /task/<task>, and without /arm/<arm>)
  POST /arm/<arm>/task/<task>/anthropic/v1/messages[/count_tokens]
       (also without /task/<task>, and without /arm/<arm>)
  POST /arm/<arm>/task/<task>/v1/responses  (also without /v1, /task/<task>, /arm/<arm>)
  GET  /arm/<arm>/v1/models
  GET  /probe, /arm/<arm>/probe        reachability check from inside a sandbox
  GET  /arm/<arm>/task/<task>/held     seconds that trial's requests were held in the gates (+ passes in progress)
  GET  /files/<name>                   static artifacts (e.g. the tofu wheel)
"""
import argparse
import asyncio
import collections
import hashlib
import itertools
import json
import math
import os
import random
import re
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from evaluations.terminal_bench import relay_ingress

PIN_FACES = ("chat", "anthropic", "responses")
# FRIDAY's per-minute RPM/TPM validation text and the rate-limit codes of the OpenAI and
# Anthropic dialects. FRIDAY also delivers them inside HTTP 200 bodies and SSE streams
# (``response.failed``, an ``error`` event, a chat ``data:`` line carrying its 429 body),
# where no status code reveals them; ``Relay.after_stream`` treats those as upstream 429s.
_RATE_LIMIT_MARKERS = ("每分钟请求次数超过限制", "tpm超过限制", "rate_limit_exceeded", "rate_limit_error",
                       "insufficient_quota", "exceeded your current quota")
UPSTREAM = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_UPSTREAM = "https://api.openai.com/v1/anthropic/v1/messages"
RESPONSES_UPSTREAM = "https://api.openai.com/v1/responses"
# Terminal Responses stream events: the one carrying the final status and usage.
_RESPONSES_TERMINAL = ("response.completed", "response.incomplete", "response.failed")
_RESPONSES_EVENT_MARKERS = tuple(f'"{kind}"'.encode() for kind in ("response.created",) + _RESPONSES_TERMINAL) \
    + (b'"type":"error"', b'"type": "error"')
_TOFU_BLOCK = re.compile(r"tofu-context:([a-z0-9_]+):start")
# Ambient date carriers: Tofu's current_date block, Codex's environment_context, Claude Code's
# "# currentDate / Today's date is ..." reminder and OpenCode's <env> "Today's date: ..." line.
_DATE_MARKERS = ("tofu-context:current_date", "<current_date>", "Current date:", "# currentDate",
                 "Today's date")
LONG_DIGITS = re.compile(r"\d{12,}")
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
# Anthropic's documented overload status joins the gateway's retryable set.
ANTHROPIC_RETRY_STATUSES = RETRY_STATUSES | {529}
# Client headers the Anthropic face forwards: request semantics (API version,
# beta flags), the gateway's cache affinity carrier and its trace id.
ANTHROPIC_FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta", "mt-session-id", "m-traceid")
# SSE ``data:`` lines worth parsing; content deltas are passed through unread.
_ANTHROPIC_EVENT_MARKERS = (b'"message_start"', b'"message_delta"', b'"content_block_start"', b'"error"')


def load_keys(origin, provider_id, suffixes=None):
    """Owner credentials of one provider from the live Tofu model-routing authority.

    Plaintext is resolved through the audited reveal endpoint and kept in memory only.
    With ``suffixes`` only credentials whose key hint ends in one of them are revealed.
    """
    import httpx

    with httpx.Client(base_url=origin, timeout=30, trust_env=False) as client:
        document = client.get("/api/v4/model-routing").json()["data"]["model_routing"]
        accesses = {a["provider_access_id"] for a in document["provider_accesses"]
                    if a["provider_id"] == provider_id}
        keys = []
        for credential in document["credentials"]:
            if credential["provider_access_id"] in accesses and credential.get("enabled") \
                    and credential.get("kind") == "api_key":
                if suffixes is not None and str(credential.get("key_hint") or "")[-4:] not in suffixes:
                    continue
                reply = client.post(f"/api/v4/model-routing/credentials/{credential['credential_id']}/secret/reveal")
                secret = (reply.json().get("data") or {}).get("secret") or ""
                try:
                    envelope = json.loads(secret)
                except ValueError:
                    envelope = None
                if isinstance(envelope, dict):
                    secret = envelope.get("api_key") or ""
                if secret.strip():
                    keys.append((credential["key_hint"][-4:], secret.strip()))
    if not keys:
        wanted = f" with a key hint ending in {sorted(suffixes)}" if suffixes is not None else ""
        raise SystemExit(f"no api_key credentials for provider {provider_id!r}{wanted}")
    return keys


# Request body bytes per input token (cached included) of FRIDAY kimi-k3 at the 1st percentile of the relay logs of
# 2026-11-12/13 for the harnesses FHE runs serve (Tofu on Messages 3.15, Codex on Responses 3.65), so an estimate
# errs high for them (eval-quota rule 12; checked: 99.1% / 98.9% of logged calls). Another harness needs its own check.
BYTES_PER_INPUT_TOKEN = {"messages": 3.1, "count_tokens": 3.1, "responses": 3.6, "chat": 3.1}


def estimated_input_tokens(kind, body_bytes: int) -> int:
    """Input tokens a request will count for, from its body size, erring high."""
    ratio = BYTES_PER_INPUT_TOKEN.get(kind, min(BYTES_PER_INPUT_TOKEN.values()))
    return math.ceil(body_bytes / ratio)


class RequestWindow:
    """Upstream request starts in the trailing minute, shared by every face and key.

    ``limit`` is an externally assigned requests-per-minute budget (None: none).
    A caller over the budget waits until the oldest start leaves the window.
    """

    def __init__(self, limit, clock=time.monotonic, sleep=asyncio.sleep):
        self.limit = limit
        self.clock = clock
        self.sleep = sleep
        self.starts = collections.deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Record one request start; returns the seconds waited for budget."""
        if not self.limit:
            return 0.0
        waited = 0.0
        async with self.lock:
            while True:
                now = self.clock()
                while self.starts and now - self.starts[0] >= 60.0:
                    self.starts.popleft()
                if len(self.starts) < self.limit:
                    self.starts.append(now)
                    return round(waited, 3)
                pause = 60.0 - (now - self.starts[0])
                await self.sleep(pause)
                waited += pause

    def in_window(self) -> int:
        now = self.clock()
        return sum(1 for start in self.starts if now - start < 60.0)

    def fill(self):
        """Count the whole budget as started now (a successor's view of a draining predecessor)."""
        if self.limit:
            self.starts.extend([self.clock()] * self.limit)


class TokenWindow:
    """Estimated input tokens of upstream request starts in the trailing minute, shared by every face and key.

    ``limit`` is an externally assigned tokens-per-minute budget (None: none). A start waits until the window
    has room for its estimate; an estimate larger than the whole budget starts alone in an empty window
    rather than never.
    """

    def __init__(self, limit, clock=time.monotonic, sleep=asyncio.sleep):
        self.limit = limit
        self.clock = clock
        self.sleep = sleep
        self.starts = collections.deque()
        self.lock = asyncio.Lock()

    async def acquire(self, tokens: int) -> float:
        """Record one start of ``tokens``; returns the seconds waited for budget."""
        if not self.limit:
            return 0.0
        waited = 0.0
        async with self.lock:
            while True:
                now = self.clock()
                while self.starts and now - self.starts[0][0] >= 60.0:
                    self.starts.popleft()
                if not self.starts or sum(count for _, count in self.starts) + tokens <= self.limit:
                    self.starts.append((now, tokens))
                    return round(waited, 3)
                pause = 60.0 - (now - self.starts[0][0])
                await self.sleep(pause)
                waited += pause

    def in_window(self) -> int:
        now = self.clock()
        return sum(count for start, count in self.starts if now - start < 60.0)

    def fill(self):
        """Count the whole budget as started now (a successor's view of a draining predecessor)."""
        if self.limit:
            self.starts.append((self.clock(), self.limit))


def injected_context(texts) -> dict:
    """Context blocks a prompt carries: Tofu block ids and ambient date markers, never text."""
    joined = "\n".join(text for text in texts if isinstance(text, str))
    return {"tofu_blocks": sorted(set(_TOFU_BLOCK.findall(joined))),
            "date_markers": [marker for marker in _DATE_MARKERS if marker in joined],
            "context_chars": len(joined)}


def _texts(value):
    """Every string under a JSON value (system blocks, message parts, instructions)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def prompt_context(body: dict) -> dict:
    """``injected_context`` of a whole request: system/instructions and every message, because
    harnesses also inject context into user turns (Codex's environment_context, Tofu reminders)."""
    return injected_context(list(_texts(body.get("system"))) + list(_texts(body.get("instructions")))
                            + list(_texts(body.get("messages"))) + list(_texts(body.get("input"))))


def responses_settings(body: dict) -> dict:
    """The harness's own Responses request settings, as sent."""
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    return {"model": body.get("model"), "stream": body.get("stream"), "store": body.get("store"),
            "max_output_tokens": body.get("max_output_tokens"), "reasoning": body.get("reasoning"),
            "temperature": body.get("temperature"), "top_p": body.get("top_p"),
            "tool_choice": body.get("tool_choice"), "parallel_tool_calls": body.get("parallel_tool_calls"),
            "tool_types": sorted({str(tool.get("type")) for tool in tools if isinstance(tool, dict)}),
            "n_tools": len(tools), "n_input": len(body.get("input") or []),
            "h_instructions": _digest(body.get("instructions")), "keys": sorted(body)}


class ResponsesStreamFacts:
    """Status, limits and usage read from a Responses SSE stream as it passes through."""

    def __init__(self):
        self.pending = b""
        self.facts = {}

    def feed(self, chunk: bytes) -> None:
        self.pending += chunk
        *lines, self.pending = self.pending.split(b"\n")
        for line in lines:
            self.line(line)

    def close(self) -> None:
        if self.pending:
            self.line(self.pending)
            self.pending = b""

    def line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:") or not any(marker in line for marker in _RESPONSES_EVENT_MARKERS):
            return
        try:
            event = json.loads(line[5:])
        except ValueError:
            return
        if isinstance(event, dict):
            self.event(event)

    def event(self, event: dict) -> None:
        kind = event.get("type")
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        if kind == "response.created":
            self.facts.update(response_model=response.get("model"),
                              upstream_max_output_tokens=response.get("max_output_tokens"),
                              upstream_reasoning=response.get("reasoning"))
        elif kind in _RESPONSES_TERMINAL:
            self.facts.update(response_status=response.get("status"), usage=response.get("usage"),
                              incomplete_details=response.get("incomplete_details"),
                              response_error=str(response.get("error"))[:300] if response.get("error") else None,
                              output_types=sorted({str(item.get("type")) for item in response.get("output") or []
                                                   if isinstance(item, dict)}))
        elif kind == "error":
            self.facts["stream_error"] = str(event.get("error") or event.get("message"))[:300]


def _digest(value) -> str:
    """Short content digest; ASCII JSON so lone surrogates in tool output still hash."""
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _without_cache_control(value):
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    return value


def _breakpoint(where: str, block) -> str | None:
    marker = block.get("cache_control") if isinstance(block, dict) else None
    if not isinstance(marker, dict):
        return None
    return f"{where}:{marker.get('ttl') or '5m'}"


def compact_pi_effort_markers(body: dict) -> dict:
    """Fold Pi's mid-conversation effort markers into ``output_config.effort``.

    Pi 1.4.0's built-in Opus 5.5 definition declares ``supportsMidConvoEffort``:
    it asks for a constant top-level ``output_config.effort`` of ``high`` and
    puts the requested effort in empty ``system`` messages before every
    assistant turn (``insertThinkingLevelMessages``). FRIDAY's Anthropic face
    rejects those markers on every request, which makes the model unusable
    through Pi's own defaults:

    - ``content: []``          -> 400 ``system content must contain at least one block``
    - ``content: [{text: ""}]`` -> 400 ``system text blocks must contain non-whitespace text``
    - no ``content`` key       -> 400 ``...content: Input should be a valid array``

    This fold drops exactly those markers, keeps the LAST one as the effort in
    force for the response, and leaves ``anthropic-beta`` and ``thinking`` (Pi
    declares its own ``block_binding``) untouched. No text is added: a non-empty
    system marker would be prompt content and would change the cache prefix.
    The record keeps the marker count and the folded value in ``pi_effort_fold``.
    """
    markers = [message.get("output_config") or {} for message in body.get("messages") or []
               if isinstance(message, dict) and message.get("role") == "system" and "output_config" in message]
    if not markers:
        return {"markers": 0}
    before = (body.get("output_config") or {}).get("effort")
    effort = markers[-1].get("effort")
    body["messages"] = [message for message in body["messages"]
                        if not (isinstance(message, dict) and message.get("role") == "system"
                                and "output_config" in message)]
    if effort:
        body["output_config"] = {**(body.get("output_config") or {}), "effort": effort}
    return {"markers": len(markers), "effort": effort, "top_level_before": before}


def anthropic_settings(body: dict) -> dict:
    """The harness's own model, sampling, thinking and effort settings, as sent.

    ``effort_markers`` counts mid-conversation ``system`` messages that carry
    ``output_config`` (Pi's managed effort); the last one is the effort in
    force for the response being requested.
    """
    markers = [message.get("output_config") or {} for message in body.get("messages") or []
               if isinstance(message, dict) and message.get("role") == "system" and "output_config" in message]
    metadata = body.get("metadata")
    return {"model": body.get("model"), "max_tokens": body.get("max_tokens"), "stream": body.get("stream"),
            "thinking": body.get("thinking"), "output_config": body.get("output_config"),
            "temperature": body.get("temperature"), "top_p": body.get("top_p"), "top_k": body.get("top_k"),
            "tool_choice": body.get("tool_choice"), "effort_markers": len(markers),
            "last_effort_marker": markers[-1].get("effort") if markers else None,
            "metadata_keys": sorted(metadata) if isinstance(metadata, dict) else None,
            "keys": sorted(body)}


def anthropic_cache_facts(body: dict, affinity_id: str | None) -> dict:
    """Everything in one Messages request that decides prompt-cache reuse, without its text.

    Digests exclude ``cache_control`` so consecutive requests of one trial can be
    compared block by block; the breakpoints are listed separately with their TTL.
    """
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    system = body.get("system")
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    breakpoints = [mark for index, tool in enumerate(tools) if (mark := _breakpoint(f"tools[{index}]", tool))]
    if isinstance(system, list):
        breakpoints += [mark for index, block in enumerate(system)
                        if (mark := _breakpoint(f"system[{index}]", block))]
    for index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            breakpoints += [mark for position, block in enumerate(content)
                            if (mark := _breakpoint(f"messages[{index}][{position}]", block))]
    names = [tool.get("name") for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
    return {
        "n_messages": len(messages), "n_tools": len(tools),
        "tool_name_bytes": sum(len(name.encode("utf-8")) + 1 for name in names),
        "deferred_tools": sum(1 for tool in tools if isinstance(tool, dict) and tool.get("defer_loading")),
        "breakpoints": breakpoints,
        "h_tools": _digest(_without_cache_control(tools)),
        "h_system": _digest(_without_cache_control(system)),
        "h_params": _digest({key: body.get(key) for key in ("thinking", "output_config", "tool_choice")}),
        "h_messages": [_digest(_without_cache_control(message)) for message in messages],
        "affinity_digest": hashlib.sha256(affinity_id.encode("utf-8")).hexdigest()[:12] if affinity_id else "",
    }


class AnthropicStreamFacts:
    """Usage and response identity read from an Anthropic SSE stream as it passes through."""

    def __init__(self):
        self.pending = b""
        self.facts = {}

    def feed(self, chunk: bytes) -> None:
        self.pending += chunk
        *lines, self.pending = self.pending.split(b"\n")
        for line in lines:
            self.line(line)

    def close(self) -> None:
        if self.pending:
            self.line(self.pending)
            self.pending = b""

    def line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        if b'"estimated_tokens"' in line:
            self.facts["estimated_tokens"] = True
        if not any(marker in line for marker in _ANTHROPIC_EVENT_MARKERS):
            return
        try:
            event = json.loads(line[5:])
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start" and isinstance(event.get("message"), dict):
            message = event["message"]
            self.facts.update(usage_start=message.get("usage"), response_model=message.get("model"),
                              message_id=str(message.get("id") or "")[:48],
                              context_management="context_management" in message)
        elif kind == "message_delta":
            self.facts["usage_delta"] = event.get("usage")
            self.facts["stop_reason"] = (event.get("delta") or {}).get("stop_reason")
        elif kind == "content_block_start":
            block_type = str((event.get("content_block") or {}).get("type"))
            blocks = self.facts.setdefault("blocks", {})
            blocks[block_type] = blocks.get(block_type, 0) + 1
        elif kind == "error":
            self.facts["stream_error"] = str(event.get("error"))[:300]


def body_error(event) -> str | None:
    """The error a JSON body or SSE event carries at top level (``error``, or FRIDAY's 429 body
    ``{"status": 429, "message": ...}``); None for anything else, such as model content."""
    if not isinstance(event, dict):
        return None
    if event.get("status") == 429:
        return f"429 {event.get('message')}"[:300]
    if event.get("error"):
        return str(event["error"])[:300]
    return None


def chat_stream_usage(tail: bytes) -> dict | None:
    """The last ``usage`` a chat SSE stream reported in a ``data:`` line, if any."""
    for line in reversed(tail.split(b"\n")):
        line = line.strip()
        if line.startswith(b"data:") and b'"usage"' in line:
            try:
                usage = json.loads(line[5:].strip()).get("usage")
            except Exception:
                continue
            if usage:
                return usage
    return None


def chat_stream_error(tail: bytes) -> str | None:
    """The last error a chat SSE stream carried in a ``data:`` line, if any."""
    for line in reversed(tail.split(b"\n")):
        line = line.strip()
        if not line.startswith(b"data:") or (b'"error"' not in line and b'"status"' not in line):
            continue
        try:
            error = body_error(json.loads(line[5:]))
        except ValueError:
            continue
        if error:
            return error
    return None


def is_rate_limit(error) -> bool:
    text = str(error or "")
    return text.startswith("429 ") or any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _pin_parent(body: dict, path: str, *, create: bool):
    """(container, leaf) for a dotted path; ``(None, leaf)`` when absent and not created."""
    *parents, leaf = path.split(".")
    node = body
    for name in parents:
        child = node.get(name)
        if not isinstance(child, dict):
            if not create:
                return None, leaf
            child = node[name] = {}
        node = child
    return node, leaf


def apply_pins(body: dict, pins: dict | None) -> dict:
    """Remove ``omit`` paths, then set ``set`` paths; returns each touched path's previous value."""
    replaced = {}
    for path in (pins or {}).get("omit") or ():
        node, leaf = _pin_parent(body, path, create=False)
        if node is not None and leaf in node:
            replaced[path] = node.pop(leaf)
    for path, value in ((pins or {}).get("set") or {}).items():
        node, leaf = _pin_parent(body, path, create=True)
        replaced.setdefault(path, node.get(leaf))
        node[leaf] = value
    return replaced


# Headers the relay sets itself: credentials are injected per key and never configured.
_RELAY_OWNED_HEADERS = frozenset({"authorization", "x-api-key", "content-type", "accept-encoding"})


def parse_upstream_headers(items) -> dict:
    """``--upstream-header NAME=VALUE`` (repeatable), sent upstream on every face.

    FRIDAY's namespaced routes need one (``x-context-provider: tokrouter`` / ``kunlun``,
    the routing store's connection ``extra_headers``); without it the tokrouter GPT route
    answers a hollow HTTP 200 with an empty message and zero tokens.
    """
    headers = {}
    for item in items or ():
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep or not name or not value:
            raise SystemExit(f"--upstream-header {item!r}: expected NAME=VALUE")
        if name.lower() in _RELAY_OWNED_HEADERS:
            raise SystemExit(f"--upstream-header {name}: set by the relay itself (credentials stay per key)")
        headers[name] = value
    return headers


def load_pins(path) -> dict:
    """``--pin-file``: ``{face: {"set": {path: value}, "omit": [path]}}`` for faces in PIN_FACES."""
    if not path:
        return {}
    pins = json.loads(Path(path).read_text(encoding="utf-8"))
    unknown = sorted(set(pins) - set(PIN_FACES))
    if unknown or not all(isinstance(rule, dict) and set(rule) <= {"set", "omit"} for rule in pins.values()):
        raise SystemExit(f"{path}: faces must be {PIN_FACES} with only 'set'/'omit' (unknown: {unknown})")
    return pins


def anthropic_error(status: int, message: str) -> web.Response:
    return web.json_response({"type": "error", "error": {"type": "api_error", "message": message}}, status=status)


class Relay:
    def __init__(self, args):
        self.args = args
        key_file = getattr(args, "ingress_key_file", None)
        self.ingress_key = relay_ingress.read_key_file(key_file) if key_file else None
        wanted = args.key_suffixes.split(",") if args.key_suffixes else None
        pairs = load_keys(args.origin, args.provider, wanted)
        if wanted is not None:
            pairs = [pair for pair in pairs if pair[0] in wanted]
        self.key_names = [name for name, _ in pairs]
        self.window = RequestWindow(getattr(args, "max_rpm", None))
        self.tokens = TokenWindow(getattr(args, "max_tpm", None))
        self.budget_file = Path(args.budget_file) if getattr(args, "budget_file", None) else None
        self.budget_stamp = None
        if self.budget_file is not None:
            if self.budget_file.exists():
                self.refresh_budgets()
            else:
                self.save_budgets()
        if getattr(args, "assume_full_window", False):
            self.window.fill()
            self.tokens.fill()
        self.first_bodies = set()
        self.keys = [secret for _, secret in pairs]
        self.key_cycle = itertools.cycle(range(len(self.keys)))
        self.cool_until = [0.0] * len(self.keys)
        self.dead = set()
        # Retired keys whose refusal carried FRIDAY's disabled-AppId signature.
        self.disabled = set()
        self.pins = load_pins(getattr(args, "pin_file", None))
        self.upstream_headers = parse_upstream_headers(getattr(args, "upstream_header", None))
        self.log_path = Path(args.log)
        self.files = Path(args.files_dir) if args.files_dir else None
        self.inflight = {}
        self.session = None
        # Seconds each trial's (arm, task) requests spent held in the gates before going upstream, for the
        # runner's deadline extension (PREREG-2 Amendment 7): ended passes, rebuilt from this log at a restart,
        # plus the passes in progress.
        self.clock = time.monotonic
        self.held = collections.defaultdict(float)
        self.holding = {}
        self.load_held()

    async def start(self, app):
        self.session = ClientSession(
            connector=TCPConnector(limit=0, ttl_dns_cache=300),
            timeout=ClientTimeout(total=None, sock_connect=30, sock_read=self.args.read_timeout),
            trust_env=False)

    async def stop(self, app):
        await self.session.close()

    def scrub(self, text):
        text = str(text)
        for key in self.keys:
            text = text.replace(key, "<key>")
        return LONG_DIGITS.sub("<digits>", text)

    def load_held(self):
        if not self.log_path.exists():
            return
        with self.log_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("held_s"):
                    self.held[(str(record.get("arm")), str(record.get("task")))] += float(record["held_s"])

    def enter_gates(self, record):
        """Start timing one pass through the key cool-down, request window and token window (their locks too)."""
        gate = object()
        self.holding[gate] = ((str(record.get("arm")), str(record.get("task"))), self.clock())
        return gate

    def leave_gates(self, gate, record):
        trial, began = self.holding.pop(gate)
        held = self.clock() - began
        self.held[trial] += held
        record["held_s"] = round(record.get("held_s", 0.0) + held, 3)

    def held_seconds(self, trial):
        """(seconds held so far, passes in progress) for one (arm, task)."""
        now = self.clock()
        current = [now - began for key, began in self.holding.values() if key == trial]
        return round(self.held.get(trial, 0.0) + sum(current), 3), len(current)

    async def held_total(self, request):
        trial = (request.match_info["arm"], request.match_info["task"])
        held, holding = self.held_seconds(trial)
        return web.json_response({"arm": trial[0], "task": trial[1], "held_s": held, "holding": holding})

    def log(self, record):
        record["ts"] = round(time.time(), 3)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def probe(self, request):
        arm = request.match_info.get("arm", "-")
        self.log({"kind": "probe", "arm": arm, "peer": request.remote, "query": dict(request.query)})
        return web.json_response({"ok": True, "peer": request.remote, "arm": arm})

    async def models(self, request):
        return web.json_response({"object": "list", "data": [
            {"id": self.args.model, "object": "model", "owned_by": "relay"}]})

    async def file(self, request):
        if self.files is None:
            raise web.HTTPNotFound()
        name = request.match_info["name"]
        path = (self.files / name).resolve()
        if path.parent != self.files.resolve() or not path.is_file():
            raise web.HTTPNotFound()
        self.log({"kind": "file", "name": name, "peer": request.remote})
        return web.FileResponse(path)

    def shape(self, body, arm):
        """Same model, sampling and pinned effort for every arm; everything else is the harness's own request."""
        original = {k: body.get(k) for k in ("model", "temperature", "top_p", "max_tokens",
                                              "max_completion_tokens", "stream", "reasoning_effort",
                                              "thinking", "tool_stream")}
        body["model"] = self.args.model
        if self.args.max_tokens is not None:
            body.pop("max_completion_tokens", None)
            body["max_tokens"] = self.args.max_tokens
        if self.args.temperature is not None:
            body["temperature"] = self.args.temperature
        if self.args.top_p is not None:
            body["top_p"] = self.args.top_p
        if self.args.reasoning_effort is not None:
            body["reasoning_effort"] = self.args.reasoning_effort
        return original

    def pin(self, face, body, record):
        replaced = apply_pins(body, self.pins.get(face))
        if replaced:
            record["pinned"] = replaced

    async def pick_key(self, deadline):
        """Next live key not cooling down after a 429; waits for the earliest one otherwise."""
        while True:
            live = [i for i in range(len(self.keys)) if i not in self.dead]
            if not live:
                return None
            now = time.monotonic()
            for _ in range(len(self.keys)):
                index = next(self.key_cycle)
                if index in live and self.cool_until[index] <= now:
                    return index
            wake = min(self.cool_until[i] for i in live)
            if wake > deadline:
                return None
            await asyncio.sleep(max(0.05, wake - now) + random.random() * 0.5)

    async def chat(self, request):
        arm = request.match_info.get("arm", "-")
        started = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "invalid JSON body"}}, status=400)
        original = self.shape(body, arm)
        stream = bool(body.get("stream"))
        affinity_id = request.headers.get("Mt-Session-Id")
        record = {"kind": "chat", "arm": arm, "task": request.match_info.get("task", ""),
                  "peer": request.remote, "orig": original,
                  "n_messages": len(body.get("messages") or []), "n_tools": len(body.get("tools") or [])}
        self.pin("chat", body, record)
        # Chat-face harnesses inject their own system prompts and tools too: the same
        # context inventory and first-body archive as the Anthropic and Responses faces.
        record["context"] = prompt_context(body)
        record["sent"] = {k: body.get(k) for k in ("temperature", "top_p", "max_tokens", "reasoning_effort",
                                                    "thinking", "enable_thinking")}
        record["cache_affinity"] = "Mt-Session-Id" if affinity_id else ""
        self.archive_first_body(record, body)

        def headers_for(key):
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                       "Accept-Encoding": "identity"}
            if affinity_id:
                headers["Mt-Session-Id"] = affinity_id
            headers.update(self.upstream_headers)
            return headers

        def failure(status, error):
            return web.json_response({"error": {"message": f"relay upstream failure: {error}",
                                                "type": "upstream_error"}}, status=status)

        return await self.dispatch(request, arm=arm, url=UPSTREAM, body=body, headers_for=headers_for,
                                   record=record, started=started, retry_statuses=RETRY_STATUSES,
                                   forward=lambda upstream: self.forward(request, upstream, stream, record, started),
                                   failure=failure)

    def archive_anthropic_body(self, record, body, forwarded):
        """Write one request body for later replay, when --anthropic-body-dir is set.

        Off by default: bodies are prompt text, so this is a debugging facility
        for a bounded probe, not a run-time log. The affinity value is replaced
        by a marker so a replay can reproduce or remove the header.
        """
        directory = Path(self.args.anthropic_body_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = '{}__{}__{:04d}__{:d}.json'.format(
            str(record.get('arm')).replace('/', '_'), str(record.get('task')).replace('/', '_'),
            record.get('n_messages') or 0, round(time.time() * 1000))
        payload = {'body': body, 'headers': forwarded, 'arm': record.get('arm'), 'task': record.get('task')}
        (directory / name).write_text(json.dumps(payload, ensure_ascii=False))

    async def messages(self, request):
        """Anthropic Messages passthrough: the harness's request with only ``model`` pinned."""
        return await self.anthropic(request, ANTHROPIC_UPSTREAM, kind="messages")

    async def count_tokens(self, request):
        return await self.anthropic(request, ANTHROPIC_UPSTREAM + "/count_tokens", kind="count_tokens")

    async def anthropic(self, request, url, *, kind):
        arm = request.match_info.get("arm", "-")
        started = time.monotonic()
        if not self.args.anthropic_model:
            return anthropic_error(404, "this relay has no --anthropic-model")
        try:
            body = await request.json()
        except Exception:
            return anthropic_error(400, "invalid JSON body")
        if not isinstance(body, dict):
            return anthropic_error(400, "the request body must be a JSON object")
        forwarded = {name.lower(): value for name, value in request.headers.items()
                     if name.lower() in ANTHROPIC_FORWARDED_HEADERS}
        affinity_id = forwarded.get("mt-session-id")
        stream = kind == "messages" and bool(body.get("stream"))
        record = {"kind": kind, "arm": arm, "task": request.match_info.get("task", ""), "peer": request.remote,
                  "orig": anthropic_settings(body), "sent_model": self.args.anthropic_model,
                  "headers_in": sorted({name.lower() for name in request.headers}),
                  "anthropic_beta": forwarded.get("anthropic-beta", ""),
                  "anthropic_version": forwarded.get("anthropic-version", ""),
                  "trace_id": forwarded.get("m-traceid", ""),
                  "cache_affinity": "Mt-Session-Id" if affinity_id else "",
                  "body_bytes": request.content_length,
                  **anthropic_cache_facts(body, affinity_id)}
        body["model"] = self.args.anthropic_model
        if kind == "messages":
            record["context"] = prompt_context(body)
            self.archive_first_body(record, body)
        if kind == "messages" and self.args.anthropic_body_dir:
            self.archive_anthropic_body(record, body, forwarded)
        if kind == "messages" and not self.args.preserve_pi_effort_markers:
            record["pi_effort_fold"] = compact_pi_effort_markers(body)
        if kind == "messages":
            self.pin("anthropic", body, record)
        record["sent_effort"] = (body.get("output_config") or {}).get("effort")
        record["sent_thinking"] = body.get("thinking")

        def headers_for(key):
            headers = {"x-api-key": key, "Authorization": f"Bearer {key}",
                       "Content-Type": "application/json", "Accept-Encoding": "identity"}
            headers.update(forwarded)
            headers.setdefault("anthropic-version", "2023-06-01")
            headers.update(self.upstream_headers)
            return headers

        def failure(status, error):
            return anthropic_error(status, f"relay upstream failure: {error}")

        return await self.dispatch(request, arm=arm, url=url, body=body, headers_for=headers_for,
                                   record=record, started=started, retry_statuses=ANTHROPIC_RETRY_STATUSES,
                                   forward=lambda upstream: self.forward_anthropic(
                                       request, upstream, stream, record, started),
                                   failure=failure)

    def archive_first_body(self, record, body):
        """With --first-body-dir, keep the first request of each (arm, task): the injected-context audit."""
        directory = getattr(self.args, "first_body_dir", None)
        trial = (str(record.get("arm")), str(record.get("task")))
        if not directory or trial in self.first_bodies:
            return
        self.first_bodies.add(trial)
        path = Path(directory) / "{}__{}__{}.json".format(
            trial[0].replace("/", "_"), trial[1].replace("/", "_"), record.get("kind"))
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        text = json.dumps({"arm": trial[0], "task": trial[1], "kind": record.get("kind"),
                           "context": record.get("context"), "body": body}, ensure_ascii=False)
        for key in self.keys:
            text = text.replace(key, "<key>")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        record["first_body"] = path.name

    def refresh_budgets(self):
        """Apply ``--budget-file`` when it changed since it was last read (it is authoritative)."""
        try:
            stamp = self.budget_file.stat().st_mtime_ns
        except OSError:
            return
        if stamp == self.budget_stamp:
            return
        budgets = json.loads(self.budget_file.read_text(encoding="utf-8"))
        self.window.limit = budgets.get("max_rpm")
        self.tokens.limit = budgets.get("max_tpm")
        self.budget_stamp = stamp

    def save_budgets(self):
        payload = json.dumps({"max_rpm": self.window.limit, "max_tpm": self.tokens.limit,
                              "written_by": "relay", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n"
        temporary = self.budget_file.with_name(self.budget_file.name + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.budget_file)
        self.budget_stamp = self.budget_file.stat().st_mtime_ns

    def rate_limited(self, key_index, record, error_text=""):
        """The shared eval-quota rules for a 429 (``.eval-runs/eval-quota.json``, rules 6 and 12).

        The key (one AppId) rests until the next full minute plus 60 s, so no
        429 is retried inside the minute that produced it. Other keys stay
        usable. The first 429 of a pause also halves one budget for the rest of
        this relay's life, as "resume at half" requires: the token budget when
        the 429 names tpm and a token budget is set, otherwise ``--max-rpm``.
        """
        now, wall = time.monotonic(), time.time()
        resume = now + ((wall // 60) + 1) * 60 + 60 - wall
        fresh = self.cool_until[key_index] <= now
        self.cool_until[key_index] = max(self.cool_until[key_index], resume)
        if not fresh:
            return
        if self.budget_file is not None:
            self.refresh_budgets()
        budget = "tpm" if "tpm" in str(error_text).lower() and self.tokens.limit else "rpm"
        if budget == "tpm":
            self.tokens.limit = max(1, self.tokens.limit // 2)
        elif self.window.limit:
            self.window.limit = max(1, self.window.limit // 2)
        if self.budget_file is not None:
            self.save_budgets()
        self.log({"kind": "rate_limited", "key": self.key_names[key_index], "arm": record.get("arm"),
                  "task": record.get("task"), "pause_s": round(resume - now, 1), "halved": budget,
                  "max_rpm_now": self.window.limit, "max_tpm_now": self.tokens.limit})

    def after_stream(self, record, error):
        """An error inside a 200 response; a rate limit there counts as an upstream 429.

        The client already received the body as sent (its own retry logic runs),
        but the quota rule applies as for an HTTP 429: the stop file, the key's
        rest and the halved window. ``stream_429`` marks the record throttled.
        """
        if not error:
            return
        record.setdefault("stream_error", str(error)[:300])
        if not is_rate_limit(error) or record.get("key") not in self.key_names:
            return
        record["stream_429"] = True
        key_index = self.key_names.index(record["key"])
        self.note_throttle(record, key_index)
        self.rate_limited(key_index, record, error)

    def note_throttle(self, record, key_index):
        """With --stop-file, the first upstream 429 creates that file: runners stop starting trials.

        The request itself is still retried after the key's cool-down; only new
        trials stop, so a throttled in-flight trial finishes and is reported as
        throttled instead of being cut off.
        """
        self.write_stop_file(record, key_index, "upstream_429")

    def write_stop_file(self, record, key_index, reason: str):
        """Create --stop-file once (the first event wins); runners start no new trial while it exists.

        Written at the first upstream 429 and at any key retirement (HTTP 401/402/403): a retired key
        serves no further request, so a trial started after it could only measure the outage.
        """
        path = getattr(self.args, "stop_file", None)
        if not path:
            return
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"reason": reason, "ts": time.time(), "arm": record.get("arm"),
                       "task": record.get("task"), "kind": record.get("kind"),
                       "key": self.key_names[key_index]}, handle)
        self.log({"kind": "stop_file_written", "path": str(path), "reason": reason, "arm": record.get("arm"),
                  "task": record.get("task"), "key": self.key_names[key_index]})

    async def responses(self, request):
        """OpenAI Responses passthrough: the harness's request with only ``model`` pinned."""
        arm = request.match_info.get("arm", "-")
        started = time.monotonic()
        if not self.args.responses_model:
            return web.json_response({"error": {"message": "this relay has no --responses-model",
                                                "type": "invalid_request_error"}}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "invalid JSON body"}}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": {"message": "the request body must be a JSON object"}}, status=400)
        affinity_id = request.headers.get("Mt-Session-Id")
        stream = bool(body.get("stream"))
        record = {"kind": "responses", "arm": arm, "task": request.match_info.get("task", ""),
                  "peer": request.remote, "orig": responses_settings(body),
                  "sent_model": self.args.responses_model,
                  "headers_in": sorted({name.lower() for name in request.headers}),
                  "cache_affinity": "Mt-Session-Id" if affinity_id else "",
                  "body_bytes": request.content_length, "context": prompt_context(body)}
        body["model"] = self.args.responses_model
        self.pin("responses", body, record)
        record["sent_reasoning"] = body.get("reasoning")
        filled = getattr(self.args, "responses_max_output_tokens", None)
        if filled and body.get("max_output_tokens") is None:
            body["max_output_tokens"] = filled
            record["max_output_tokens_filled"] = filled
        self.archive_first_body(record, body)

        def headers_for(key):
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                       "Accept-Encoding": "identity"}
            if affinity_id:
                headers["Mt-Session-Id"] = affinity_id
            headers.update(self.upstream_headers)
            return headers

        def failure(status, error):
            return web.json_response({"error": {"message": f"relay upstream failure: {error}",
                                                "type": "upstream_error"}}, status=status)

        return await self.dispatch(request, arm=arm, url=RESPONSES_UPSTREAM, body=body, headers_for=headers_for,
                                   record=record, started=started, retry_statuses=RETRY_STATUSES,
                                   forward=lambda upstream: self.forward_responses(
                                       request, upstream, stream, record, started),
                                   failure=failure)

    async def forward_responses(self, request, upstream, stream, record, started):
        facts = ResponsesStreamFacts()
        try:
            if not stream:
                data = await upstream.read()
                try:
                    reply = json.loads(data)
                except ValueError:
                    reply = {}
                if isinstance(reply, dict):
                    facts.event({"type": "response.completed", "response": reply})
                    facts.facts.update(response_model=reply.get("model"),
                                       upstream_max_output_tokens=reply.get("max_output_tokens"))
                return web.Response(body=data, status=200, content_type="application/json")
            response = web.StreamResponse(status=200, headers={
                "Content-Type": upstream.headers.get("Content-Type", "text/event-stream"),
                "Cache-Control": "no-cache"})
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                facts.feed(chunk)
                await response.write(chunk)
            await response.write_eof()
            return response
        except (ConnectionResetError, asyncio.CancelledError) as gone:
            record["client_gone"] = type(gone).__name__
            raise
        finally:
            upstream.release()
            facts.close()
            record.update(facts.facts)
            record["latency_s"] = round(time.monotonic() - started, 3)
            self.after_stream(record, record.get("response_error") or record.get("stream_error"))
            self.log(record)

    async def dispatch(self, request, *, arm, url, body, headers_for, record, started, retry_statuses,
                       forward, failure):
        """One upstream call under the shared key pool, 429 cool-down, request budget and retry budget."""
        self.inflight[arm] = self.inflight.get(arm, 0) + 1
        attempt, delay, last = 0, 2.0, {"status": None, "error": "no live key"}
        deadline = started + self.args.retry_budget
        try:
            while True:
                gate = self.enter_gates(record)
                try:
                    key_index = await self.pick_key(deadline)
                    if key_index is not None:
                        attempt += 1
                        if self.budget_file is not None:
                            self.refresh_budgets()
                        waited = await self.window.acquire()
                        if waited:
                            record["rpm_wait_s"] = round(record.get("rpm_wait_s", 0.0) + waited, 3)
                        if self.tokens.limit:
                            estimate = estimated_input_tokens(record.get("kind"),
                                                              record.get("body_bytes") or len(json.dumps(body)))
                            record["tpm_estimate"] = estimate
                            waited = await self.tokens.acquire(estimate)
                            if waited:
                                record["tpm_wait_s"] = round(record.get("tpm_wait_s", 0.0) + waited, 3)
                finally:
                    self.leave_gates(gate, record)
                if key_index is None:
                    break
                try:
                    upstream = await self.session.post(url, json=body, headers=headers_for(self.keys[key_index]))
                except (asyncio.TimeoutError, OSError, Exception) as error:  # transport failure
                    last = {"status": None, "error": self.scrub(f"{type(error).__name__}: {str(error)[:300]}")}
                    upstream = None
                if upstream is not None and upstream.status == 200:
                    record.update(status=200, attempts=attempt, key=self.key_names[key_index],
                                  ttfb_s=round(time.monotonic() - started, 3))
                    return await forward(upstream)
                if upstream is not None:
                    text = self.scrub((await upstream.text())[:600])
                    upstream.release()
                    last = {"status": upstream.status, "error": text}
                    if upstream.status in (401, 402, 403):
                        disabled = relay_ingress.is_key_disabled(upstream.status, text)
                        self.dead.add(key_index)
                        if disabled:
                            self.disabled.add(key_index)
                        self.log({"kind": "key_retired", "key": self.key_names[key_index],
                                  "status": upstream.status, "disabled": disabled, "error": text[:160]})
                        self.write_stop_file(record, key_index, f"key_retired_{upstream.status}")
                        continue
                    if upstream.status not in retry_statuses:
                        break
                record.setdefault("retries", []).append({"status": last["status"], "key": self.key_names[key_index],
                                                         "error": (last["error"] or "")[:160]})
                if last["status"] == 429:
                    self.note_throttle(record, key_index)
                    self.rate_limited(key_index, record, last["error"])
                    continue
                if time.monotonic() + delay > deadline:
                    break
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30.0)
            if len(self.dead) == len(self.keys):
                # No key can serve again in this relay's life: say so in a form clients do not retry.
                disabled = bool(self.disabled)
                record.update(status=403, attempts=attempt, latency_s=round(time.monotonic() - started, 3),
                              error=relay_ingress.KEY_DISABLED if disabled else relay_ingress.KEY_RETIRED)
                self.log(record)
                dialect = "anthropic" if record.get("kind") in ("messages", "count_tokens") else "openai"
                return relay_ingress.no_usable_key_response(dialect, disabled=disabled, keys=self.key_names)
            record.update(status=last["status"] or 502, attempts=attempt, error=last["error"],
                          latency_s=round(time.monotonic() - started, 3))
            self.log(record)
            return failure(last["status"] if last["status"] else 502, last["error"])
        finally:
            self.inflight[arm] -= 1

    async def forward(self, request, upstream, stream, record, started):
        usage = error = None
        tail = b""
        try:
            if not stream:
                data = await upstream.read()
                try:
                    reply = json.loads(data)
                    usage, error = reply.get("usage"), body_error(reply)
                except Exception:
                    pass
                response = web.Response(body=data, status=200, content_type="application/json")
                return response
            response = web.StreamResponse(status=200, headers={
                "Content-Type": upstream.headers.get("Content-Type", "text/event-stream"),
                "Cache-Control": "no-cache"})
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                # Received before it is relayed: a client that leaves early (Pi closes
                # once it has the answer) still leaves the usage and errors on record.
                tail = (tail + chunk)[-8192:]
                await response.write(chunk)
            await response.write_eof()
            return response
        except (ConnectionResetError, asyncio.CancelledError) as gone:
            record["client_gone"] = type(gone).__name__
            raise
        finally:
            upstream.release()
            if tail:
                error, usage = chat_stream_error(tail), chat_stream_usage(tail)
            record.update(usage=usage, latency_s=round(time.monotonic() - started, 3))
            self.after_stream(record, error)
            self.log(record)

    async def forward_anthropic(self, request, upstream, stream, record, started):
        facts = AnthropicStreamFacts()
        try:
            if not stream:
                data = await upstream.read()
                try:
                    reply = json.loads(data)
                except ValueError:
                    reply = {}
                if isinstance(reply, dict):
                    facts.facts.update(usage=reply.get("usage"), input_tokens=reply.get("input_tokens"),
                                       response_model=reply.get("model"), stop_reason=reply.get("stop_reason"),
                                       message_id=str(reply.get("id") or "")[:48])
                return web.Response(body=data, status=200, content_type="application/json")
            response = web.StreamResponse(status=200, headers={
                "Content-Type": upstream.headers.get("Content-Type", "text/event-stream"),
                "Cache-Control": "no-cache"})
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                facts.feed(chunk)
                await response.write(chunk)
            await response.write_eof()
            return response
        except (ConnectionResetError, asyncio.CancelledError) as gone:
            record["client_gone"] = type(gone).__name__
            raise
        finally:
            upstream.release()
            facts.close()
            record.update(facts.facts)
            record["latency_s"] = round(time.monotonic() - started, 3)
            self.after_stream(record, record.get("stream_error"))
            self.log(record)

    async def stats(self, request):
        now = time.monotonic()
        return web.json_response({"inflight": self.inflight, "keys": self.key_names,
                                  "max_rpm": self.window.limit, "starts_last_minute": self.window.in_window(),
                                  "max_tpm": self.tokens.limit, "tokens_last_minute": self.tokens.in_window(),
                                  "dead": [self.key_names[i] for i in sorted(self.dead)],
                                  "disabled": [self.key_names[i] for i in sorted(self.disabled)],
                                  "cooling": [self.key_names[i] for i, t in enumerate(self.cool_until) if t > now]})


def build_app(relay: Relay) -> web.Application:
    middlewares = [relay_ingress.ingress_middleware(relay.ingress_key, relay.log)] if relay.ingress_key else []
    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=middlewares)
    app.on_startup.append(relay.start)
    app.on_cleanup.append(relay.stop)
    for prefix in ("", "/arm/{arm}", "/arm/{arm}/task/{task}"):
        for path in ("/v1/chat/completions", "/chat/completions"):
            app.router.add_post(prefix + path, relay.chat)
        app.router.add_get(prefix + "/v1/models", relay.models)
        app.router.add_get(prefix + "/models", relay.models)
        app.router.add_get(prefix + "/probe", relay.probe)
        app.router.add_post(prefix + "/anthropic/v1/messages", relay.messages)
        app.router.add_post(prefix + "/anthropic/v1/messages/count_tokens", relay.count_tokens)
        for path in ("/v1/responses", "/responses"):
            app.router.add_post(prefix + path, relay.responses)
    app.router.add_get("/files/{name}", relay.file)
    app.router.add_get("/stats", relay.stats)
    app.router.add_get("/arm/{arm}/task/{task}/held", relay.held_total)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18431)
    parser.add_argument("--origin", default="http://127.0.0.1:15000", help="live Tofu application")
    parser.add_argument("--provider", default="example-corp")
    parser.add_argument("--key-suffixes", help="comma list of key hint suffixes (last 4 chars) to use")
    parser.add_argument("--model", default="glm-5.3-flash-m17")
    parser.add_argument("--anthropic-model",
                        help="FRIDAY wire model for the Anthropic Messages face (enables /anthropic/v1/messages)")
    parser.add_argument("--responses-model",
                        help="FRIDAY wire model for the OpenAI Responses face (enables /v1/responses)")
    parser.add_argument("--responses-max-output-tokens", type=int,
                        help="max_output_tokens for Responses requests that omit it (recorded per request)")
    parser.add_argument("--max-rpm", type=int,
                        help="owner-assigned budget: upstream request starts per trailing minute, all faces")
    parser.add_argument("--max-tpm", type=int,
                        help="owner-assigned budget: estimated input tokens of those starts per trailing minute "
                             "(eval-quota cap_tpm)")
    parser.add_argument("--budget-file",
                        help="JSON {max_rpm, max_tpm}: read at start when present (else written from the flags), "
                             "re-read when it changes, rewritten on every halving")
    parser.add_argument("--assume-full-window", action="store_true",
                        help="start as if both budgets were used in the last minute (a successor relay while its "
                             "predecessor drains)")
    parser.add_argument("--first-body-dir",
                        help="write the first request body of each (arm, task) here (prompt text, mode 0600)")
    parser.add_argument("--stop-file",
                        help="created on the first upstream 429; runners given the same path start no new trials")
    parser.add_argument("--upstream-header", action="append", default=[],
                        help="NAME=VALUE sent upstream on every face, e.g. x-context-provider=tokrouter (no credentials)")
    parser.add_argument("--pin-file",
                        help='per-face request pins {"chat"|"anthropic"|"responses": {"set": {...}, "omit": [...]}}')
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--retry-budget", type=float, default=900.0)
    parser.add_argument("--read-timeout", type=float, default=900.0)
    parser.add_argument("--anthropic-body-dir",
                        help="debugging: also write each Anthropic request body to this directory "
                             "(prompt text; off by default)")
    parser.add_argument("--preserve-pi-effort-markers", action="store_true",
                        help="pass Pi's empty mid-conversation effort markers through "
                             "unchanged (FRIDAY rejects them; used only to measure that)")
    parser.add_argument("--log", required=True)
    parser.add_argument("--files-dir")
    parser.add_argument("--ingress-key-file",
                        help="the run's relay key file (python -m evaluations.terminal_bench relay-key FILE): every "
                             "request must carry the key; required unless --host is loopback")
    args = parser.parse_args()
    relay_ingress.require_ingress_key(args.host, args.ingress_key_file)
    relay = Relay(args)
    asyncio.run(serve(build_app(relay), relay, args))


async def serve(app, relay, args):
    """Bind as soon as the port is free, so a successor can take over from a draining relay."""
    import signal

    runner = web.AppRunner(app, access_log=None, shutdown_timeout=120)
    await runner.setup()
    while True:
        try:
            await web.TCPSite(runner, args.host, args.port).start()
            break
        except OSError:
            await asyncio.sleep(0.2)
    print(f"relay listening on {args.host}:{args.port} keys={relay.key_names} model={args.model} "
          f"anthropic_model={args.anthropic_model} responses_model={args.responses_model} "
          f"max_rpm={relay.window.limit} max_tpm={relay.tokens.limit} read_timeout={args.read_timeout} "
          f"pins={json.dumps(relay.pins)} "
          f"ingress={'run-key' if relay.ingress_key else 'loopback-only'}", flush=True)
    relay.log({"kind": "relay_start", "keys": relay.key_names, "model": args.model,
               "anthropic_model": args.anthropic_model, "responses_model": args.responses_model,
               "max_rpm": relay.window.limit, "max_tpm": relay.tokens.limit, "pins": relay.pins,
               "temperature": args.temperature,
               "top_p": args.top_p, "max_tokens": args.max_tokens, "reasoning_effort": args.reasoning_effort,
               "responses_max_output_tokens": args.responses_max_output_tokens, "pid": os.getpid(),
               "upstream_headers": relay.upstream_headers, "code": str(Path(__file__).resolve()),
               "ingress": "run-key" if relay.ingress_key else "loopback-only"})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()


if __name__ == "__main__":
    main()
