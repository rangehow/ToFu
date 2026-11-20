/* ===== migrated source: ui/send_button.js ===== */
/* Composer send/stop control derived from authoritative turn state. */
function _activeConversationAttemptIds(conv) {
  return conv
    ? [...(runtimeScope.ConversationTurnRead?.activeAttemptIds?.(conv) || [])]
    : [];
}

function _stopTranslation(conv) {
  Api.chat.abortConv(conv.id).catch((error) =>
    console.warn('[stop] translation abort marker failed:', error));
  conv._translateAborted = true;
  conv._translating = false;
  const controller = conv._translateAbortCtrl;
  conv._translateAbortCtrl = null;
  // Send-time translation runs inside the generation-start lease. Ending it
  // ends that startup too; a live startup Stop would invite repeat clicks that
  // each re-send the conversation abort while the send recovers.
  if (controller && controller === conv._genStartCtrl) {
    conv._genStartStop = controller;
    conv._genStartCtrl = null;
  }
  controller?.abort();
  updateSendButton();
  renderConversationList();
}

function _stopGenerationStartup(conv) {
  Api.chat.abortConv(conv.id).catch((error) =>
    console.warn('[stop] startup abort marker failed:', error));
  const controller = conv._genStartCtrl;
  conv._genStartStop = controller;
  conv._genStartCtrl = null;
  controller?.abort();
  updateSendButton();
  renderConversationList();
}

function updateSendButton() {
  const conv = getActiveConv();
  const translating = Boolean(conv?._translating);
  const startupConnecting = Boolean(conv?._genStartCtrl);
  const turnBusy = Boolean(conv && convIsBusy(conv));
  // A stopped startup still awaits its submission's answer; show it stopping.
  const turnStore = runtimeScope.ConversationTurnStore;
  const attemptStop = conv && turnStore?.attemptStopState?.(conv);
  const stop = () => {
    if (!conv || conv._genStartStop) return;
    if (translating) {
      _stopTranslation(conv);
      return;
    }
    if (startupConnecting) {
      _stopGenerationStartup(conv);
      return;
    }
    turnStore?.stopActiveAttempts?.(conv);
  };
  const state = updateComposerSendControls({
    document,
    sendBlocked: isBootSendBlocked(),
    blockedLabel: t('boot.sendPaused'),
    translating,
    startupConnecting,
    turnBusy,
    stopping: Boolean(conv?._genStartStop || attemptStop),
    hasAttachmentDraft: pendingImages.length > 0
      || (typeof pendingPdfTexts !== 'undefined' && pendingPdfTexts.length > 0)
      || (typeof pendingVideos !== 'undefined' && pendingVideos.length > 0),
    queueCount: conv ? _dispatchableQueueCount(conv.id) : 0,
    labels: {
      send: t('orch.ai.send'),
      sendTitle: t('paper.send'),
      stop: t('branch.stopGen'),
      stopping: t('stop.stopping'),
      stopRefresh: !conv?._genStartStop && attemptStop === 'refresh' ? t('projectCoordination.refresh') : undefined,
    },
    onSend: sendMessage,
    onStop: stop,
    requestRefresh: updateSendButton,
  });
  if (typeof _setAgentModeLocked === 'function') _setAgentModeLocked(state.busy);
}
