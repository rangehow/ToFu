# HOT_PATH
"""Streaming chat completion with SSE parsing (sync transport).

Public API:
  - stream_chat(body, ...) → (assistant_msg, finish_reason, usage)

The SSE parsing / error classification / tool-call accumulation / anomaly
diagnostics live in ``lib/llm/_sse_core.py`` and are shared with the async
transport (``lib/llm/astream.py``). This module is the thin ``requests``
shell: it opens the stream, feeds lines to the core, and keeps the
retry/backoff wrapper.
"""

import time

import requests

from lib.llm._sse_core import (
    SSEAccumulator,
    classify_status_error,
    prepare_request,
)
from lib.llm._transport import (
    CONNECT_TIMEOUT,
    MAX_STREAM_RETRIES,
    abortable_sleep,
    apply_model_limit_retry,
    attach_limit_learned,
    get_sync_session,
    prepare_retryable_wait,
)
from lib.llm_errors import (
    AbortedError,
    ContentFilterError,
    EndpointUnreachableError,
    ModelLimitError,
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    _RETRYABLE,
    decode_error_body,
)
from lib.log import get_logger
from lib.proxy import proxies_for, report_outcome as _proxy_report_outcome

logger = get_logger(__name__)


def stream_chat(body, *, on_thinking=None, on_content=None,
                on_tool_call_ready=None,
                abort_check=None, log_prefix='', api_key=None, base_url=None,
                extra_headers=None, api_protocol='openai', oauth='',
                on_attempt_restart=None):
    """Streaming chat completion with callbacks.

    Automatically retries on transient connection errors up to
    MAX_STREAM_RETRIES times.

    ``on_attempt_restart`` (optional): fired with ``reason=<str>`` whenever an
    in-flight attempt is being DISCARDED and the request is about to restart
    from scratch. Any content/thinking already delivered via on_content /
    on_thinking during that attempt will be re-streamed — the callee must drop
    its partial accumulation (e.g. truncate back to the per-round base) so the
    re-streamed text does not stack on the abandoned attempt's tail.

    Returns:
        (assistant_msg, finish_reason, usage)

    Raises:
        RateLimitError, PermissionError_, AbortedError,
        ContentFilterError, PromptTooLongError, RetryableAPIError
    """
    def _fire_attempt_restart(reason: str) -> None:
        if on_attempt_restart is None:
            return
        try:
            on_attempt_restart(reason=reason)
        except Exception as _oar_e:
            logger.debug('%s on_attempt_restart raised: %s', log_prefix, _oar_e)

    last_err = None
    _limit_learned = None
    for attempt in range(1 + MAX_STREAM_RETRIES):
        try:
            msg, finish_reason, usage = _stream_chat_once(
                body, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                attempt=attempt, api_key=api_key, base_url=base_url,
                extra_headers=extra_headers, api_protocol=api_protocol,
                oauth=oauth)
            usage = attach_limit_learned(usage, _limit_learned)
            return msg, finish_reason, usage
        except (RateLimitError, PermissionError_, AbortedError, ContentFilterError, PromptTooLongError, EndpointUnreachableError):
            # EndpointUnreachableError: the host is down — retrying it on
            # the SAME slot just burns another connect timeout. Escape to
            # the dispatch layer, which cools this slot and fails over.
            raise
        except ModelLimitError as e:
            _limit_learned = apply_model_limit_retry(body, e, log_prefix)
            # The clamped retry restarts from scratch — anything streamed so
            # far belongs to a discarded attempt.
            _fire_attempt_restart('model-limit clamp retry')
            continue
        except _RETRYABLE as e:
            last_err = e
            if attempt < MAX_STREAM_RETRIES:
                # Another attempt WILL run from scratch — the partial stream
                # from this attempt is being abandoned.
                _fire_attempt_restart('transport retry: %s' % e.__class__.__name__)
            wait = prepare_retryable_wait(attempt, e, abort_check, log_prefix)
            abortable_sleep(wait, abort_check)
    raise last_err


def _stream_chat_once(body, *, on_thinking=None, on_content=None,
                      on_tool_call_ready=None,
                      abort_check=None, log_prefix='', attempt=0,
                      api_key=None, base_url=None, extra_headers=None,
                      api_protocol='openai', oauth=''):
    """Single attempt at a streaming chat completion (sync transport)."""
    plan = prepare_request(
        body, attempt=attempt, log_prefix=log_prefix,
        api_key=api_key, base_url=base_url, extra_headers=extra_headers,
        api_protocol=api_protocol, oauth=oauth)

    # ``prepare_request`` already opened the RawSSEDumper fd (when enabled), so
    # a single outer try/finally must guard EVERY exit path — including the
    # connect-phase re-raise below, which used to escape before the dumper was
    # closed and leaked the fd once per retry against a down endpoint.
    resp = None
    try:
        _conn_t0 = time.monotonic()
        try:
            resp = get_sync_session().post(
                plan.url, headers=plan.hdrs, json=plan.body,
                stream=True, timeout=(CONNECT_TIMEOUT, 300),
                proxies=proxies_for(plan.url))
            _proxy_report_outcome(
                plan.url, True, (time.monotonic() - _conn_t0) * 1000.0)
        except requests.exceptions.ConnectionError as e:
            _proxy_report_outcome(plan.url, False)
            # Connect-phase failure (ConnectTimeout / connection refused /
            # SYN dropped) = the endpoint is down. Convert to
            # EndpointUnreachableError so it escapes the same-key retry loop
            # and the dispatch layer fails over to a healthy slot instead of
            # burning CONNECT_TIMEOUT × MAX_STREAM_RETRIES on a dead host.
            logger.warning('%s ✖ Endpoint unreachable (connect phase) %s: %s',
                           log_prefix, plan.url, e)
            raise EndpointUnreachableError(
                'endpoint unreachable: %s' % e, base_url=plan.url) from e

        resp_trace = resp.headers.get('M-TraceId', '')
        if resp_trace and resp_trace != plan.trace_id:
            logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

        if resp.status_code != 200:
            # decode_error_body, NOT resp.text: requests falls back to
            # ISO-8859-1 for text/* without charset, garbling UTF-8 CJK
            # gateway error pages into mojibake (toio 400 incident 2026-07-25).
            classify_status_error(resp.status_code, decode_error_body(resp),
                                  body=plan.body,
                                  log_prefix=log_prefix, raw_dumper=plan.raw_dumper)

        resp.encoding = 'utf-8'

        acc = SSEAccumulator(
            plan.body, plan.trace_id, plan.raw_dumper, plan.codex_translator,
            plan.t0, url=plan.url, log_prefix=log_prefix,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            anthropic_translator=plan.anthropic_translator)

        for line in resp.iter_lines(decode_unicode=True):
            if abort_check and abort_check():
                acc.mark_aborted()
                break
            if acc.feed_line(line):
                break

        acc.fire_final_tool_callback()
        return acc.finalize(resp_trace=resp_trace)
    finally:
        try:
            if plan.raw_dumper.enabled and plan.raw_dumper._fh is not None:
                plan.raw_dumper.finish(error=True)
        except Exception as e:
            logger.debug('%s RawSSEDumper.finish(error=True) failed: %s', log_prefix, e)
        if resp is not None:
            resp.close()
