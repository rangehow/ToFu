"""Who may call the Terminal-Bench relay, and its typed answer once its upstream keys are unusable.

Responsibility: what the relay (``relay.py``), the runner (``runner.py``, ``__main__.py``) and the
VM trial share about relay ingress.

- A per-run relay key. The runner creates it (``create_key_file``: random URL-safe text written
  once with ``O_EXCL`` at mode 0600; an existing file is kept, so the relay and every runner of one
  run share it) and the relay reads the same file at start (``read_key_file`` refuses a file other
  users can read). The key never appears on a command line, in a log record or in a persisted
  spec: the runner puts it in place of the agents' API-key placeholder (``RELAY_KEY_PLACEHOLDER``)
  only in the spec bytes it uploads to the VM (``uploaded_spec``), so agents need no change.
- ``ingress_middleware``: every route answers HTTP 401 before its handler runs unless the request
  carries the key as ``Authorization: Bearer <key>`` or ``x-api-key: <key>``. A refused request
  makes no upstream call; its log record names the peer, method, path and which credential header
  was present, never a header value.
- ``require_ingress_key``: a relay listening beyond loopback must have a key.
- A disabled key. FRIDAY answers a disabled AppId with HTTP 401 ``当前 App ID 已被禁用``
  (``is_key_disabled``). Once every key of a relay is retired, each request gets a typed
  non-retryable error without an upstream call (``no_usable_key_response``): HTTP 403,
  ``x-should-retry: false`` (the OpenAI and Anthropic SDKs obey it) and error type
  ``relay_key_disabled`` (``relay_key_retired`` when no retirement carried the signature). It used
  to be a 502, which Tofu's dispatch waits out without end (gateway 5xx is waitable, owner
  directive 2026-08-20; ``lib/llm/llm_errors.py`` ``_GATEWAY_THROTTLE_STATUS``): after the
  2026-11-13 16:51 disable of …0733 one relay answered 16,222 such 502s, 15,110 of them to Tofu
  trials retrying about 90 times a minute for up to an hour each.

Dependencies: standard library; aiohttp for the relay-side helpers (imported where used).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

# The API key every agent config carries for the relay (harness, third_party, *_contract).
RELAY_KEY_PLACEHOLDER = "relay-held"
# FRIDAY's HTTP 401 text for a disabled AppId: "当前 App ID 已被禁用，请前往Friday控制台 ...". FRIDAY sends it as raw
# UTF-8; the \u-escaped JSON form is matched too.
DISABLED_KEY_MARKER = "App ID 已被禁用"
_DISABLED_KEY_FORMS = (DISABLED_KEY_MARKER, json.dumps(DISABLED_KEY_MARKER)[1:-1])
KEY_DISABLED = "relay_key_disabled"
KEY_RETIRED = "relay_key_retired"
UNAUTHORIZED = "relay_unauthorized"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# secrets.token_urlsafe's alphabet: a key is one header-safe token.
_KEY_TEXT = re.compile(r"[A-Za-z0-9_-]+")
# The OpenAI and Anthropic SDKs (and Claude Code through them) skip their retries on this header.
_NOT_RETRYABLE = {"x-should-retry": "false"}


def create_key_file(path) -> Path:
    """The run's relay key file: created with a fresh key at mode 0600, or kept when it exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        read_key_file(path)
        return path
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")
    return path


def read_key_file(path) -> str:
    """The key in ``path``; refuses a file another user can read, or anything but one key token."""
    path = Path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise SystemExit(f"{path}: no relay key file (python -m evaluations.terminal_bench relay-key {path})")
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"{path}: the relay key file must be a regular file")
    if info.st_mode & 0o077:
        raise SystemExit(f"{path}: the relay key file must be readable by its owner only (chmod 600)")
    key = path.read_text(encoding="ascii", errors="replace").strip()
    if not _KEY_TEXT.fullmatch(key) or key == RELAY_KEY_PLACEHOLDER:
        raise SystemExit(f"{path}: not a relay key (create one with python -m evaluations.terminal_bench relay-key)")
    return key


def require_ingress_key(host: str, key_file) -> None:
    """A relay reachable from other hosts must authenticate its callers."""
    if not key_file and host not in LOOPBACK_HOSTS:
        raise SystemExit(f"--host {host} accepts connections from other hosts: start the relay with "
                         "--ingress-key-file (python -m evaluations.terminal_bench relay-key FILE creates one)")


def presented_credentials(headers) -> tuple[str, list[str]]:
    """Which credential headers a request carries (for the log) and their values (for the check only)."""
    values, kinds = [], []
    scheme, _, token = (headers.get("Authorization") or "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        kinds.append("bearer")
        values.append(token.strip())
    elif headers.get("Authorization"):
        kinds.append("authorization-other")
    if headers.get("x-api-key"):
        kinds.append("x-api-key")
        values.append(headers["x-api-key"].strip())
    return "+".join(kinds) or "none", values


def carries_key(headers, key: str) -> bool:
    expected = key.encode("utf-8")
    _, values = presented_credentials(headers)
    return any(hmac.compare_digest(value.encode("utf-8", "replace"), expected) for value in values)


def ingress_middleware(key: str, log):
    """aiohttp middleware: a request without the run key gets 401 before any handler (no upstream call)."""
    from aiohttp import web

    @web.middleware
    async def require_run_key(request, handler):
        if carries_key(request.headers, key):
            return await handler(request)
        credential, _ = presented_credentials(request.headers)
        log({"kind": "ingress_refused", "peer": request.remote, "method": request.method,
             "path": request.path[:200], "credential": credential})
        return web.json_response(
            {"error": {"type": UNAUTHORIZED, "code": UNAUTHORIZED, "param": None,
                       "message": f"{UNAUTHORIZED}: this request does not carry the relay key of the run"}},
            status=401, headers=dict(_NOT_RETRYABLE))

    return require_run_key


def is_key_disabled(status: int | None, text: str) -> bool:
    """FRIDAY's disabled-AppId answer: HTTP 401 carrying ``App ID 已被禁用``."""
    return status == 401 and any(form in str(text or "") for form in _DISABLED_KEY_FORMS)


def no_usable_key_response(dialect: str, *, disabled: bool, keys):
    """Typed non-retryable error once every key is retired: HTTP 403, ``x-should-retry: false``.

    ``dialect`` is ``anthropic`` (Messages error envelope) or anything else (OpenAI envelope).
    The text names only key suffixes and avoids wording Tofu's classifier reads as transient.
    """
    from aiohttp import web

    kind = KEY_DISABLED if disabled else KEY_RETIRED
    cause = "FRIDAY disabled its AppId" if disabled else "upstream refused it (HTTP 401/402/403)"
    message = (f"{kind}: no usable upstream key on this relay (keys ending {', '.join(keys)}: {cause}); "
               "the request was not sent upstream and a retry cannot succeed")
    if dialect == "anthropic":
        body = {"type": "error", "error": {"type": kind, "message": message}}
    else:
        body = {"error": {"type": kind, "code": kind, "param": None, "message": message}}
    return web.json_response(body, status=403, headers=dict(_NOT_RETRYABLE))


def uploaded_spec(spec: dict, key: str | None, relay_url: str) -> dict:
    """The spec the VM receives: the run key in place of every API-key placeholder.

    ``relay_ingress`` tells ``vm_trial.py`` the relay origin whose artifact downloads carry the key.
    The persisted ``spec.json`` keeps the placeholder. Without a key the spec is sent as it is.
    """
    if not key:
        return spec

    def substitute(value):
        if isinstance(value, str):
            return value.replace(RELAY_KEY_PLACEHOLDER, key)
        if isinstance(value, dict):
            return {name: substitute(item) for name, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value

    parts = urlsplit(relay_url)
    return {**substitute(spec), "relay_ingress": {"origin": f"{parts.scheme}://{parts.netloc}", "key": key}}
