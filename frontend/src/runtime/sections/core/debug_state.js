/* ===== migrated source: core/debug_state.js ===== */
/* Thin compatibility port; collection, bounds, and lifecycle are typed. */
const _debugRuntimeOwner = createDebugRuntimeOwner({
  now: () => Date.now(),
  writeConsole: (level, message) => console.log(`[${level}]`, message),
  warnConsole: (message, error) => console.warn(message, error),
  currentUrl: () => location.href,
  userAgent: () => navigator.userAgent,
  conversationCount: () => conversations?.length || 0,
  report: (payload) => Api.clientError.report(payload),
  subscribeError: (listener) => {
    window.addEventListener('error', listener);
    return () => window.removeEventListener('error', listener);
  },
  subscribeUnhandledRejection: (listener) => {
    window.addEventListener('unhandledrejection', listener);
    return () => window.removeEventListener('unhandledrejection', listener);
  },
  resolveClipboardWrite: () => {
    const clipboard = navigator.clipboard;
    return clipboard?.writeText ? (text) => clipboard.writeText(text) : null;
  },
  createClipboardTextarea: () => document.createElement('textarea'),
  appendClipboardTextarea: (textarea) => document.body.appendChild(textarea),
  removeClipboardTextarea: (textarea) => textarea.remove(),
  executeClipboardCopy: () => { document.execCommand('copy'); },
  activeConversationId: () => activeConvId,
  conversations: () => conversations,
  config: () => config,
  visible: () => Boolean(debugVisible),
  setVisible: (value) => { debugVisible = value; },
  readTurnState: (conversationId) =>
    runtimeScope.ConversationTurnRead?.state?.(conversationId),
});
_debugRuntimeOwner.start();
retainedCompositionLifecycle.add(() => _debugRuntimeOwner.dispose());
const debugLog = _debugRuntimeOwner.debugLog;
const _reportClientError = _debugRuntimeOwner.reportClientError;
function _safeClipboardWrite(text) {
  return _debugRuntimeOwner.safeClipboardWrite(text);
}
const _riTaskIdForRound = _debugRuntimeOwner.taskIdForRound;
const DebugShellState = _debugRuntimeOwner.shellState;
runtimeScope.__tofuDiagRing = _debugRuntimeOwner.diagnosticRing;

// Crash breadcrumbs live in this tab's sessionStorage so they survive a
// renderer crash; the next page instance reports them once boot is ready.
// A visible page also publishes an origin record, so that any later tab of the
// same owner and browser session reports it if the page's tab was closed instead.
const _pageCrashJournal = startPageCrashJournal({
  storage: window.sessionStorage,
  lifecycle: window,
  wasDiscarded: () => Boolean(document.wasDiscarded),
  hasOpener: () => Boolean(window.opener),
  navigationType: () => performance.getEntriesByType?.('navigation')?.[0]?.type || '',
  now: () => Date.now(),
  newInstanceId: () => newPageInstanceId(crypto),
  environment: () => crashEnvironment(navigator, document),
  context: () => crashContext(location.hash, activeConvId,
    runtimeScope.ConversationTurnRead?.state?.(activeConvId)),
  crashReport: window.crashReport || null,
  originStorage: resolveBrowserLocalStorage() ?? null,
  locks: navigator.locks ?? null,
  browserSession: () => browserSessionId(document, () => newPageInstanceId(crypto),
    location.protocol === 'https:'),
  visibility: document,
});
const _disposePageCrashInputs = installPageCrashInputs(_pageCrashJournal, window, console);
const _sendPendingPageCrashes = () => {
  // Another tab's record is reported only to its own owner.
  void initCurrentUserId()
    .then((ownerId) => _pageCrashJournal.noteOwner(ownerId))
    .then(() => _pageCrashJournal.sendPending((report) => Api.clientCrash.report(report).catch((error) => {
      // The server refused the report itself; resending it cannot succeed.
      if (error?.status === 400 || error?.status === 413) return null;
      throw error;
    })));
};
window.addEventListener('tofu:boot-ready', _sendPendingPageCrashes);
// The composition is destroyed at beforeunload, before the document's final
// pagehide; the journal itself stays installed so that pagehide still clears
// the alive marker (otherwise every clean reload would read as a crash).
retainedCompositionLifecycle.add(() => {
  window.removeEventListener('tofu:boot-ready', _sendPendingPageCrashes);
  _disposePageCrashInputs();
});

// Passive diagnostics use the canonical read service and the existing relay.
const _responsivenessInteractions = createBrowserInteractionContext(document, () => performance.now());
retainedCompositionLifecycle.add(() => _responsivenessInteractions.dispose());
const _responsivenessMonitor = startBrowserResponsivenessMonitor({
  now: () => performance.now(),
  wallNow: () => Date.now(),
  schedule: window,
  lifecycle: window,
  interactionContext: (startMs, endMs) => _responsivenessInteractions.context(startMs, endMs),
  timeOriginMs: performance.timeOrigin,
  visibility: document,
  observe: (type, receive) => {
    if (typeof PerformanceObserver === 'undefined'
        || !PerformanceObserver.supportedEntryTypes.includes(type)) return null;
    const observer = new PerformanceObserver((list) => receive(list.getEntries()));
    observer.observe({ type, buffered: false });
    return { disconnect: () => observer.disconnect(), takeRecords: () => observer.takeRecords() };
  },
  context: () => responsivenessContext(activeConvId,
    runtimeScope.ConversationTurnRead?.state?.(activeConvId)),
  memory: () => performance.memory ?? null,
  domElementCount: () => document.getElementsByTagName('*').length,
  report: (message) => {
    console.warn(message);
    const evidence = freezeEvidenceFromReport(message);
    if (evidence) _pageCrashJournal.recordFreeze(evidence);
    runtimeScope.__clientLogRelay?.flush({ urgent: true });
  },
});
retainedCompositionLifecycle.add(() => _responsivenessMonitor.dispose());
