/* ═══════════════════════════════════════════════════════════════════
   core/error_envelope.js — extracted from core.js (split 2026-05-28)

   Typed error envelope helpers: ERROR_KIND_LABELS, isErrorEnvelope, normalize/render/kind/message.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ── Typed error envelope helpers ───────────────────────────────────────
 *
 * Backend emits errors as a typed dict (see lib/error_envelope.py):
 *   { kind, severity, retryable, message, hint, detail, model, context,
 *     source, raw }
 *
 * The frontend stores the dict verbatim on assistantMsg.error and renders
 * via renderErrorEnvelope() below. Legacy plain strings (e.g. very old
 * persisted messages, or frontend-set "Server offline" strings) are
 * normalised through normalizeErrorEnvelope() so the renderer never has
 * to branch on shape.
 */
const ERROR_KIND_LABELS = {
  quota:                 'Quota exhausted',
  ratelimit:             'Rate limited',
  permission:            'Permission denied',
  no_slot:               'No keys available',
  dispatch_exhausted:    'All keys exhausted',
  timeout:               'Timed out',
  network:               'Network error',
  endpoint_unreachable:  'Endpoint unreachable',
  content_filter:        'Content filter',
  invalid_image:         'Image rejected',
  prompt_too_long:       'Prompt too long',
  stream_only:           'Stream-only model',
  model_limit:           'Model limit',
  tool_rounds_exhausted: 'Tool budget',
  tool_timeout:          'Tool timeout',
  premature_close:       'Stream cut off',
  abnormal_stop:         'Abnormal stop',
  aborted:               'Stopped',
  server_offline:        'Server offline',
  internal:              'Internal error',
  generic:               'Error',
  bad_request:           'Bad request',
  content_refused:       'Quality check failed',
  upstream_error:        'Upstream error',
};

function isErrorEnvelope(obj) {
  return !!obj && typeof obj === 'object'
    && typeof obj.kind === 'string'
    && typeof obj.message === 'string';
}

function normalizeErrorEnvelope(err) {
  if (err == null || err === '') return null;
  if (isErrorEnvelope(err)) return err;
  if (typeof err === 'string') {
    /* Legacy or frontend-stamped strings — wrap in a generic envelope so
     * the renderer logic stays branch-free. The 'server offline' string
     * stamped by the frontend stream-timer gets its own kind so the UI
     * can recognize it without regex. */
    const isServerOffline = /server offline/i.test(err);
    return {
      kind: isServerOffline ? 'server_offline' : 'generic',
      severity: 'warning',
      retryable: isServerOffline,
      message: err,
      hint: '',
      detail: err.slice(0, 300),
      model: '', context: '', source: 'frontend-legacy',
      raw: err,
    };
  }
  /* Unknown shape — best effort. */
  try {
    return {
      kind: 'generic',
      severity: 'error', retryable: false,
      message: 'Unknown error',
      hint: '',
      detail: JSON.stringify(err).slice(0, 300),
      model: '', context: '', source: 'frontend-unknown', raw: '',
    };
  } catch (_e) {
    return {
      kind: 'generic',
      severity: 'error', retryable: false,
      message: 'Unknown error',
      hint: '', detail: '', model: '', context: '', source: 'frontend-unknown', raw: '',
    };
  }
}

/* Guarded t(): this module is bundled AFTER i18n.js in production so t() is
 * always present (→ zh, the primary UI). Several Node extraction-and-eval
 * harnesses eval this file standalone; fall back to the key so t() never
 * throws ReferenceError there. Never English. */
function _envT(key, params) {
  return (typeof t === 'function') ? t(key, params) : key;
}

/* ── Keyed i18n resolution (2026-07-25) ──────────────────────────────
 * Modern backends ship titleKey/hintKey on the envelope next to the legacy
 * bilingual message/hint. Resolve keys in the CURRENT UI language; return
 * null when the key is absent from this bundle's table (old frontend +
 * new backend, or table drift) so every caller degrades to the legacy
 * bilingual strings — identical to today's rendering. `_i18n` is the
 * dictionary var from i18n.js (bundled earlier); standalone-eval harnesses
 * lack it → null → legacy fallback, so they stay green. */
function _envResolveI18n(key, params) {
  if (!key) return null;
  if (typeof _i18n === 'undefined' || !_i18n[key]) return null;
  /* Same fallback chain as t(), but undefined-aware: an EMPTY entry (e.g.
   * `aborted`'s hint) resolves to '' — meaningful "deliberately no text",
   * distinct from null (= unknown key → legacy fallback). t() can't be used
   * here because its `entry[lang] || entry.zh || key` chain key-echoes on ''. */
  const entry = _i18n[key];
  const lang = (typeof _i18nLang === 'string') ? _i18nLang : 'zh';
  let text = (typeof entry[lang] === 'string') ? entry[lang]
    : (typeof entry.zh === 'string') ? entry.zh : key;
  if (text && params) {
    for (const k in params) {
      if (Object.prototype.hasOwnProperty.call(params, k)) {
        text = text.replace(new RegExp('\\{' + k + '\\}', 'g'), params[k]);
      }
    }
  }
  return text;  // [env-i18n-resolve]
}

/* Localized title line (+ model suffix), or null when unresolvable. */
function _envLocalizedTitle(env) {
  const base = _envResolveI18n(env.titleKey);
  if (base == null) return null;
  const suffix = env.model
    ? (_envResolveI18n('err.k._modelSuffix', { model: env.model }) || '')
    : '';
  return base + suffix;
}

/* Localized hint block (header + body), or null when unresolvable.
 * A key that resolves to an EMPTY string (e.g. `aborted`) means "no hint
 * for this kind" — return '' so the caller skips the block entirely. */
function _envLocalizedHint(env) {
  const body = _envResolveI18n(env.hintKey);
  if (body == null) return null;
  if (!body) return '';
  const head = _envResolveI18n('err.k._howToFix') || 'How to fix:';
  return head + '\n' + body;
}

/* Is this envelope RECOVERABLE by the offline-recovery path?
 *
 * ONLY ``server_offline`` qualifies. That kind is stamped by the frontend
 * health-check / poll circuit breaker (`_forceFinishDeadStream`) AFTER a task
 * was already running server-side — so its finishReason becomes
 * ``server_offline`` and `_recoverOfflineConversations` can re-fetch the conv
 * and adopt the server's completed result.
 *
 * ``network`` is deliberately EXCLUDED: it is stamped at ``context:'chat-start'``
 * when the POST that STARTS the turn fails — no task ever ran, finishReason is
 * never ``server_offline``, and a Recover button would scan, find nothing, and
 * no-op while telling the user "your result may be saved". That is the exact
 * false-hope we refuse. ``premature_close``/``abnormal_stop`` are UPSTREAM
 * (server↔gateway) failures emitted only after server-side retries are
 * exhausted — there is no saved result either; their correct action is Retry. */
function _envIsRecoverable(env) {
  return !!env && env.kind === 'server_offline';
}

/* Conservative mojibake repair (2026-07-26) — display layer ONLY.
 * Envelopes persisted before the backend wire-sanitize fix keep garbled
 * detail strings ('è¯·æ±‚å¤±è´¥' for '请求失败'). Fires ONLY when the text
 * smells of UTF-8-decoded-as-latin1 damage AND a strict UTF-8 round-trip
 * gains CJK — otherwise returns the input untouched, so clean text (incl.
 * real accented Latin) is never rewritten. Applied at render time; the
 * stored envelope stays verbatim. */
function _envRepairMojibake(text) {
  if (!text || typeof text !== 'string') return text;
  let suspect = false;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if ((c >= 0x80 && c <= 0xff) || c === 0x201a) { suspect = true; break; }
  }
  if (!suspect) return text;
  const bytes = [];
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c <= 0xff) { bytes.push(c); continue; }
    if (c === 0x201a) { bytes.push(0x82); continue; }
    return text;  // not latin-1/cp1252 encodable → not this damage shape
  }
  let repaired;
  try {
    repaired = new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(bytes));
  } catch (_e) {
    return text;
  }
  const hasCJK = (s) => /[\u4e00-\u9fff]/.test(s);
  if (hasCJK(repaired) && !hasCJK(text)) return repaired;  // [env-mojibake-repair]
  return text;
}

function renderErrorEnvelope(err) {
  const env = normalizeErrorEnvelope(err);
  if (!env) return '';
  const sev = env.severity === 'error' ? 'error' : 'warning';
  const _recoverable = _envIsRecoverable(env);
  /* For the recoverable connection-drop case, override the "Server offline"
   * jargon title + (often empty) hint with truthful, actionable copy: the
   * result is likely safe on the server, don't regenerate, click Recover. */
  const kindLabel = _recoverable
    ? _envT('err.conn.title')
    : (_envResolveI18n('err.k.' + env.kind + '.chip')
       || ERROR_KIND_LABELS[env.kind] || env.kind || 'Error');
  const detail = _envRepairMojibake(env.detail || env.raw || '');
  const detailBlock = detail
    ? `<div class="error-block-detail" title="${escapeHtml(detail)}">${escapeHtml(detail.length > 220 ? detail.slice(0, 220) + '…' : detail)}</div>`
    : '';
  const _locTitle = _envLocalizedTitle(env);
  const _locHint = _envLocalizedHint(env);
  const hintText = _recoverable ? _envT('err.conn.hint')
    : (_locHint != null ? _locHint : env.hint);
  const hintBlock = hintText
    ? `<div class="error-block-hint">${escapeHtml(hintText)}</div>`
    : '';
  const ctx = env.context
    ? `<span class="error-block-ctx">[${escapeHtml(env.context)}]</span>`
    : '';
  /* Inline Recover button — reuses the existing offline-recovery path (no
   * second mechanism): it reconnects and adopts the server's completed
   * content, then clears the stale error. NOT a regenerate. SVG glyph per
   * §3.4 (never emoji). */
  const recoverBtn = _recoverable
    ? `<div class="error-block-actions" style="margin-top:10px">`
      + `<button class="error-block-recover-btn" type="button"`
      + ` title="${escapeHtml(_envT('err.conn.recoverTip'))}"`
      + ` onclick="_recoverOfflineConversations('manual_button')"`
      + ` style="display:inline-flex;align-items:center;gap:6px;padding:5px 12px;`
      + `font-size:12px;font-weight:600;cursor:pointer;color:inherit;`
      + `background:rgba(245,158,11,0.14);border:1px solid currentColor;`
      + `border-radius:6px;line-height:1.2">`
      + `${(typeof Icon === 'function') ? Icon('refresh', 12) : ''}<span>${escapeHtml(_envT('err.conn.recover'))}</span></button>`
      + `</div>`
    : '';
  return (
    `<div class="error-block error-block--${escapeHtml(sev)} error-block--kind-${escapeHtml(env.kind)}" data-error-kind="${escapeHtml(env.kind)}">` +
      `<div class="error-block-title"><span class="error-block-kind">${escapeHtml(kindLabel)}</span>${ctx}</div>` +
      `<div class="error-block-message">${escapeHtml(_locTitle != null ? _locTitle : _envRepairMojibake(env.message))}</div>` +
      hintBlock +
      detailBlock +
      recoverBtn +
    `</div>`
  );
}

function errorEnvelopeKind(err) {
  const env = normalizeErrorEnvelope(err);
  return env ? env.kind : '';
}

function errorEnvelopeMessage(err) {
  const env = normalizeErrorEnvelope(err);
  if (!env) return '';
  const _loc = _envLocalizedTitle(env);
  return _loc != null ? _loc : _envRepairMojibake(env.message);
}

window.renderErrorEnvelope = renderErrorEnvelope;
window.normalizeErrorEnvelope = normalizeErrorEnvelope;
window.errorEnvelopeKind = errorEnvelopeKind;
window.errorEnvelopeMessage = errorEnvelopeMessage;
window.isErrorEnvelope = isErrorEnvelope;
