/* ===== migrated source: main/conversation_turn_store.js ===== */
let _roleAvatarIcons = null;
function _resolveRoleAvatarIcons() {
  if (!_roleAvatarIcons && typeof createRoleAvatarIcons === 'function') {
    _roleAvatarIcons = createRoleAvatarIcons(
      typeof BASE_PATH === 'string' ? BASE_PATH : '',
    );
  }
  return _roleAvatarIcons;
}

function _throttledTurnCachePut(conv) {
  try {
    if (typeof ConvCache === 'undefined' || !conv) return;
    const now = Date.now();
    if (now - (conv._lastTurnCachePutAt || 0) < 1000) return;
    conv._lastTurnCachePutAt = now;
    ConvCache.put(conv);
  } catch (_e) { /* local cache is best-effort */ }
}

function _nativeTurnState(conv, turnId) {
  const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv?.id);
  const state = store?.getState?.();
  const turn = state?.turnsById?.[turnId];
  return turn ? { store, state, turn } : null;
}

function _nextGeneratedNativeTurn(state, inputTurn) {
  const laneId = inputTurn?.laneId || 'main';
  const ordered = state?.laneOrder?.[laneId] || [];
  const position = ordered.indexOf(inputTurn?.turnId);
  if (position < 0) return null;
  for (const candidateId of ordered.slice(position + 1)) {
    const candidate = state.turnsById[candidateId];
    if (candidate && ['assistant', 'planner'].includes(candidate.actor)) return candidate;
    if (candidate?.actor === 'human') break;
  }
  return null;
}

function _nativeRegenerateInputOptions(resolved, inputEdit) {
  const inputTurn = resolved.state.turnsById?.[resolved.turn.parentTurnId];
  if (!inputTurn) return {};
  let inputProjection = inputTurn.projection || {};
  let shouldUpdateInput = false;
  if (inputEdit) {
    /* The editor reports the message's images AFTER its edits. Replace rather
     * than append: a kept image is already in the projection, and removal
     * would be impossible if the reported list were merged back in. */
    if (inputEdit.images) {
      inputProjection = { ...inputProjection, images: [...inputEdit.images] };
    }
    inputProjection = _nativeTurnEditProjection(
      inputProjection, String(inputEdit.content ?? ''),
    );
    shouldUpdateInput = true;
  }
  if (typeof runtimeScope.buildTurnCtxSnapshot === 'function'
      && typeof rebindTurnInputContext === 'function') {
    inputProjection = rebindTurnInputContext(
      inputProjection, runtimeScope.buildTurnCtxSnapshot(),
    );
    shouldUpdateInput = true;
  }
  return shouldUpdateInput ? {
    inputUpdate: inputProjection,
    expectedInputProjectionRevision: Number(inputTurn.projectionRevision || 0),
  } : {};
}

/* Recovery never creates a durable browser flag. A page reload leaves the
 * ordinary settled Stop/Continue/Regenerate controls as the only continuation. */
const _explicitFallbackRecovery = createExplicitFallbackRecovery({
  hydrate(conversationId) {
    return runtimeScope.ConversationTurnStore.hydrateConversation({ id: conversationId });
  },
  subscribe(conversationId, changed) {
    const store = runtimeScope.ConversationTurnStore.ensureRuntimeStore(conversationId);
    return store.subscribe(changed);
  },
  readState(conversationId) {
    return runtimeScope.ConversationTurnStore.readRuntimeState(conversationId);
  },
  async prepareTarget(offer, state, signal) {
    const conv = (typeof conversations !== 'undefined'
      ? conversations.find(item => item?.id === offer.conversationId) : null)
      || { id: offer.conversationId };
    const phase = state.livePhase;
    const turn = state.turnsById[offer.turnId];
    const active = state.activeAttemptPhase?.attemptId
      ? state.activeAttemptPhase.attemptId === offer.attemptId
      : turn?.currentAttemptId === offer.attemptId;
    if (!active || !configuredFallbackPhaseOffer(
        phase, turn, _registeredFallbackRoute(offer.conversationId))
        || state.commandPending[offer.turnId]
        || turn.projectionRevision !== offer.projectionRevision
        || turn.currentAttemptId !== offer.attemptId
        || turn.projection?.model && turn.projection.model !== offer.currentModel) {
      throw new Error('The 429 wait or fallback route changed; no Attempt was stopped.');
    }
    const previous = _captureFallbackSelectionEffects();
    try {
      const row = _registeredModels.find(item => item.model_id === offer.model);
      if (!row || selectModelRoute(row) !== true
          || config.model !== offer.model) {
        throw new Error('The configured fallback is not an available chat route.');
      }
      const resolved = await _buildConvConfig(conv);
      /* The read resolved the fallback route explicitly selected above. Do
       * not let its conv_settings write-back silently switch it later. */
      if (signal.aborted) throw new DOMException('Recovery superseded', 'AbortError');
      if (resolved.disableModelFallback === true) {
        throw new Error('This execution disables model fallback.');
      }
      for (const key of ['autopilot', 'activeFlow', 'flowDefinition', 'flowBuiltin', 'flowId', '_projectDispatchAdmission']) {
        if (resolved[key]) throw new Error('Explicit fallback is limited to ordinary main-lane chat.');
      }
      return { config: resolved };
    } finally {
      _restoreFallbackSelectionEffects(previous);
    }
  },
  contextInputUpdate(turn, state) {
    return _nativeResumeCtxOptions({ turn, state }, 'checkpoint_resume');
  },
  regenerateInputUpdate(turn, state) {
    return _nativeRegenerateInputOptions({ turn, state });
  },
  abortAttempt(attemptId) {
    return runtimeScope.ConversationTurnStore.abortAttempt(attemptId);
  },
  operateAttempt(command) {
    return runtimeScope.ConversationTurnStore.operateConversation(
      { id: command.conversationId }, command.turnId, command.operation, command.config, {
        commandId: command.commandId,
        ...(command.resumeAnchor ? { resumeAnchor: command.resumeAnchor } : {}),
        ...(command.inputUpdate ? { inputUpdate: command.inputUpdate } : {}),
        ...(command.expectedInputProjectionRevision !== undefined
          ? { expectedInputProjectionRevision: command.expectedInputProjectionRevision } : {}),
      },
    );
  },
  hasAuthoritativeCommand(conversationId, commandId) {
    return runtimeScope.ConversationTurnStore.hasAuthoritativeCommand(conversationId, commandId);
  },
  reportError(error) {
    if (typeof showToast === 'function') {
      showToast(error?.message || 'Explicit fallback recovery failed', 'error');
    }
  },
  commandId: createTurnCommandId,
});

/* Resume operations that reopen an interrupted attempt on the same lane.
 * Mirrors the server-side _RESUME_MODEL_SWITCH_OPERATIONS set: only these
 * may carry the contextSnapshot-only rebind that records a pause →
 * switch-model → continue on the submitted turn's info rail. */
const _RESUME_CTX_OPERATIONS = new Set([
  'continue', 'checkpoint_resume', 'answer_guidance']);

function _nativeResumeCtxOptions(resolved, operation) {
  if (!_RESUME_CTX_OPERATIONS.has(operation)) return {};
  if (typeof runtimeScope.buildTurnCtxSnapshot !== 'function'
      || typeof resumeCtxInputUpdate !== 'function') return {};
  const inputUpdate = resumeCtxInputUpdate(runtimeScope.buildTurnCtxSnapshot());
  if (!inputUpdate) return {};
  const inputTurn = resolved?.state?.turnsById?.[resolved.turn.parentTurnId];
  return {
    inputUpdate,
    expectedInputProjectionRevision: Number(inputTurn?.projectionRevision || 0),
  };
}
const _laterTurnsReviewPorts = Object.freeze({
  showChoice: (config) => showChoice(config),
  translate: (key, params) => (typeof t === 'function' ? t(key, params) : key),
  formatTime(epochMs) {
    const value = new Date(epochMs);
    return Number.isNaN(value.getTime()) ? '' : value.toLocaleString([], {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    });
  },
});

/* Regenerating a Turn that is not the latest of its lane would discard every
 * later Turn, the owner's own messages included (a resume completes it in
 * place and keeps them). The server refuses that (409 LaterTurnsRefusal)
 * until the owner acknowledges exactly the Turns it lists; keep is the first
 * and default choice. Resolves the command response, or null when the owner
 * kept the later Turns. */
async function _operateWithLaterTurnsReview(conv, resolved, turnId, operation, cfg, options) {
  let current = options;
  let retriedEmptyClosure = false;
  for (;;) {
    try {
      return await runtimeScope.ConversationTurnStore.operateConversation(
        conv, turnId, operation, cfg, current,
      );
    } catch (error) {
      const refusal = laterTurnsRefusal(error);
      if (!refusal) throw error;
      if (refusal.closure.count === 0 && !retriedEmptyClosure) {
        /* The Turns the owner agreed to discard are already gone: nothing
         * later remains, so the command no longer needs an acknowledgement. */
        retriedEmptyClosure = true;
        current = {
          ...current,
          commandId: laterTurnsDiscardCommandId(options.commandId, []),
          discardLaterTurnIds: undefined,
        };
        continue;
      }
      const acknowledged = await reviewLaterTurnsDiscard(
        refusal, resolved.turn.laneId || 'main', _laterTurnsReviewPorts,
      );
      if (!acknowledged) return null;
      current = {
        ...current,
        commandId: laterTurnsDiscardCommandId(options.commandId, acknowledged),
        discardLaterTurnIds: acknowledged,
      };
    }
  }
}

async function _operateNativeTurn(conv, turnId, operation, inputEdit) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved || typeof runtimeScope.ConversationTurnStore === 'undefined') return false;
  if (entityRecordValues(resolved.state.turnsById || {}).some(turn =>
    turn && (turn.status === 'pending' || turn.status === 'running'))) {
    if (typeof showToast === 'function') {
      showToast('Stop the current attempt before starting another operation.', 'info');
    }
    return false;
  }
  try {
    const operationOptions = {
      commandId: `${operation}:${turnId}:${resolved.turn.projectionRevision || 0}`,
      ...(operation === 'regenerate'
        ? _nativeRegenerateInputOptions(resolved, inputEdit)
        : _nativeResumeCtxOptions(resolved, operation)),
    };
    const cfg = await _buildConvConfig(conv);
    const response = await _operateWithLaterTurnsReview(
      conv, resolved, turnId, operation, cfg, operationOptions,
    );
    return response !== null;
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* authoritative recovery is best effort */ }
    if (typeof showToast === 'function') {
      showToast(error?.message || `${operation} failed`, 'error');
    }
    return false;
  }
}

/* Pending-resume bridge (runtimeScope.PendingResume): hydrate an interrupted
 * conversation, then run the EXACT manual "继续" path (_operateNativeTurn) on
 * its newest resumable turn — config resolution, ctx rebind, idempotent
 * command identity, and error toasts stay identical to a user click. */
async function _resumeInterruptedConversationById(conversationId) {
  const catalog = typeof conversations !== 'undefined' ? conversations : [];
  const conv = catalog.find((item) => item && item.id === conversationId)
    || { id: conversationId };
  const service = runtimeScope.ConversationTurnStore;
  if (!service || typeof service.hydrateConversation !== 'function') return false;
  try {
    await service.hydrateConversation(conv);
  } catch (error) {
    console.warn('[PendingResume] hydrate failed for', conversationId, error);
    return false;
  }
  const pick = pickPendingResumeTurn(
    runtimeScope.ConversationTurnRead?.ordered?.(conv) || [],
  );
  if (!pick) return false;
  return _operateNativeTurn(conv, pick.turnId, pick.operation);
}

/* Late ask_human answer: the settled turn's settlement still offers
 * answer_guidance, so the answer completes the interrupted question round
 * inside a fresh attempt instead of reviving the dead process-local request. */
async function _answerHumanGuidanceLate(conv, turnId, responseText) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved || typeof runtimeScope.ConversationTurnStore === 'undefined') {
    throw new Error('Unknown turn; refresh the authoritative snapshot.');
  }
  if (entityRecordValues(resolved.state.turnsById || {}).some(turn =>
    turn && (turn.status === 'pending' || turn.status === 'running'))) {
    throw new Error('Stop the current attempt before answering.');
  }
  const cfg = await _buildConvConfig(conv);
  await runtimeScope.ConversationTurnStore.operateConversation(
    conv, turnId, 'answer_guidance', cfg, {
      commandId: `answer_guidance:${turnId}:${resolved.turn.projectionRevision || 0}`,
      humanResponse: responseText,
      ..._nativeResumeCtxOptions(resolved, 'answer_guidance'),
    },
  );
}
runtimeScope.answerHumanGuidanceLate = _answerHumanGuidanceLate;

async function _regenerateNativeInput(conv, inputTurnId) {
  const resolved = _nativeTurnState(conv, inputTurnId);
  const target = resolved && _nextGeneratedNativeTurn(resolved.state, resolved.turn);
  if (!target) {
    if (typeof showToast === 'function') {
      showToast('No generated turn follows this input.', 'error');
    }
    return;
  }
  return _operateNativeTurn(conv, target.turnId, 'regenerate');
}

function _nativeTurnEditProjection(projection, content) {
  const nextProjection = { ...projection, content };
  delete nextProjection.originalContent;
  delete nextProjection.translatedContent;
  delete nextProjection.translation;
  return nextProjection;
}

async function _commitNativeTurnEdit(conv, turnId, projection, content) {
  try {
    await runtimeScope.ConversationTurnStore.updateConversationTurn(
      conv, turnId, _nativeTurnEditProjection(projection, content),
    );
    return true;
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* authoritative recovery is best effort */ }
    if (typeof showToast === 'function') {
      showToast(error?.message || 'Edit failed', 'error');
    }
    return false;
  }
}

function _findRenderedNativeTurnNode(turnId) {
  const root = document.getElementById('chatInner');
  if (!root) return null;
  const safeId = typeof CSS !== 'undefined' && CSS.escape
    ? CSS.escape(turnId) : String(turnId).replace(/[^\w-]/g, '');
  return root.querySelector(`[data-turn-id="${safeId}"]`);
}

/* Transient copy chrome: swap the copy glyph for a check on clipboard
 * success.  Kept out of the view model on purpose — the surface only
 * re-renders the action bar when the action list changes, so a DOM-level
 * flash survives unrelated commits and harmlessly vanishes on a real one. */
function _flashTurnCopyFeedback(turnId, blockId) {
  const turnNode = _findRenderedNativeTurnNode(turnId);
  if (!turnNode) return;
  let scope = turnNode;
  let action = 'copy';
  if (blockId) {
    /* Block ids carry colons ('text:llm-1') — match by dataset, not by a
     * CSS-escaped attribute selector. */
    scope = Array.from(turnNode.querySelectorAll('[data-block-id]')).find(
      node => node.dataset.blockId === blockId,
    ) || null;
    action = 'copy-block';
    if (!scope) return;
  }
  const button = scope.querySelector?.(
    `button[data-conversation-action="${action}"]`,
  );
  const icon = button?.querySelector?.('svg.msg-action-icon');
  if (!button || !icon) return;
  if (!button._tofuCopyFlash) {
    const checkIcon = icon.cloneNode(false);
    const checkPath = icon.ownerDocument.createElementNS(
      'http://www.w3.org/2000/svg', 'path',
    );
    checkPath.setAttribute('d', 'M20 6 9 17l-5-5');
    checkPath.setAttribute('stroke', 'currentColor');
    checkPath.setAttribute('stroke-width', '2');
    checkPath.setAttribute('stroke-linecap', 'round');
    checkPath.setAttribute('stroke-linejoin', 'round');
    checkIcon.appendChild(checkPath);
    button._tofuCopyFlash = { originalIcon: icon, checkIcon, timer: 0 };
    icon.replaceWith(checkIcon);
    button.classList.add('msg-action-btn--copied');
  }
  clearTimeout(button._tofuCopyFlash.timer);
  button._tofuCopyFlash.timer = setTimeout(() => {
    const flash = button._tofuCopyFlash;
    button._tofuCopyFlash = null;
    if (flash?.checkIcon.parentNode === button) {
      flash.checkIcon.replaceWith(flash.originalIcon);
    }
    button.classList.remove('msg-action-btn--copied');
  }, 1500);
}

/* Snapshot images carry server-relative previews ('/api/images/<f>' or a lazy
 * '/api/v4/conversations/.../images/<i>?projectionRevision=N' read). An <img>
 * outside the renderer must apply the same BASE_PATH resolution, or the thumb
 * 404s behind a reverse proxy. */
function _turnImageDisplayUrl(url) {
  const value = String(url || '');
  return typeof apiUrl === 'function' && value.startsWith('/') ? apiUrl(value) : value;
}

function _isLazyTurnImagePreview(image) {
  return typeof image?.preview === 'string'
    && image.preview.startsWith('/api/v4/conversations/')
    && !image.url && !image.base64;
}

function _blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
    reader.onerror = () => reject(reader.error || new Error('Image read failed'));
    reader.readAsDataURL(blob);
  });
}

/* A lazily projected image has no bytes in the browser (base64 stripped) and
 * its preview is fenced to the CURRENT projection revision, which the edit
 * itself bumps. Writing it back verbatim would leave a turn image that can
 * never load again and that the model cannot see. Re-home those bytes as an
 * uploaded image so the edited projection references a durable URL. */
async function _durableEditedImages(images) {
  return Promise.all((images || []).map(async (image) => {
    if (!_isLazyTurnImagePreview(image)) return image;
    const response = await Api.images.readTurnImage(image.preview);
    if (!response?.ok) throw new Error(`Image reload failed (${response.status})`);
    const blob = await response.blob();
    const mediaType = blob.type || image.mediaType || 'image/png';
    const uploaded = await Api.images.upload({ base64: await _blobToBase64(blob), mediaType });
    if (!uploaded?.url) throw new Error('Image upload failed');
    return { ...image, mediaType, url: uploaded.url, preview: uploaded.url };
  }));
}

async function _editNativeTurn(conv, turnId) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved) return;
  const projection = resolved.turn.projection || {};
  const currentText = resolved.turn.actor === 'human'
    ? (projection.originalContent || projection.content || '')
    : (projection.content || '');
  const hasAttachments = Boolean(
    projection.images?.length || projection.videos?.length
      || projection.pdfTexts?.length || projection.attachments?.length,
  );
  if (typeof openTurnInlineEditor === 'function') {
    const session = openTurnInlineEditor({
      conversationId: conv.id,
      turnId,
      text: currentText,
      allowEmpty: hasAttachments,
      canResend: resolved.turn.actor === 'human'
        && Boolean(_nextGeneratedNativeTurn(resolved.state, resolved.turn)),
      /* Submitted images seed the editor's attachment strip so they stay
       * visible — and removable — while the message is being edited. */
      existingImages: (projection.images || []).map((image) => ({
        payload: image,
        preview: _turnImageDisplayUrl(
          typeof image?.preview === 'string' ? image.preview : image?.url),
      })),
      findTurnNode: _findRenderedNativeTurnNode,
      translate: (key) => (typeof t === 'function' ? t(key) : key),
      /* Paste-to-attach inside the edit session: reuse the composer's shared
       * compress + upload core so the entry shape matches a send-draft image
       * exactly ({base64, mediaType, preview, sizeKB, url?}). The composer
       * pendingImages draft is NOT touched — this attaches to the turn. */
      onImageAttach: typeof processImageFile === 'function'
        ? async (file) => {
          try {
            const d = await processImageFile(file);
            return { payload: d, preview: d.preview || d.url || '' };
          } catch (_error) {
            if (typeof showToast === 'function') {
              showToast(
                typeof t === 'function' ? t('upload.imageSkipped') : 'Image skipped',
                'warning',
              );
            }
            return null;
          }
        }
        : undefined,
      async onSubmit({ text, resend, images: editedImages }) {
        let images;
        try {
          images = editedImages && await _durableEditedImages(editedImages);
        } catch (error) {
          if (typeof showToast === 'function') {
            showToast(error?.message || 'Edit failed', 'error');
          }
          return false;
        }
        if (resend) {
          const fresh = _nativeTurnState(conv, turnId);
          const target = fresh
            && _nextGeneratedNativeTurn(fresh.state, fresh.turn);
          if (target) {
            return _operateNativeTurn(conv, target.turnId, 'regenerate', {
              content: text,
              images,
            });
          }
          if (typeof showToast === 'function') {
            showToast('No generated turn follows this input.', 'error');
          }
          return false;
        }
        /* Same replace semantics as resend: the strip is the message's full
         * image list, so dropped images must disappear from the projection. */
        const nextProjection = images
          ? { ...projection, images: [...images] }
          : projection;
        return _commitNativeTurnEdit(conv, turnId, nextProjection, text);
      },
    });
    if (session) return;
  }
  /* Turns windowed out of the DOM keep the detached prompt path. */
  if (typeof showPrompt !== 'function') return;
  const edited = await showPrompt(
    typeof t === 'function' ? t('msgAction.edit') : 'Edit message',
    { defaultValue: currentText },
  );
  if (edited == null) return;
  const content = String(edited).trim();
  if (!content && !hasAttachments) return;
  await _commitNativeTurnEdit(conv, turnId, projection, content);
}

async function _deleteNativeTurn(conv, turnId) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved) return;
  const turnIds = [turnId];
  if (resolved.turn.actor === 'human') {
    const generated = _nextGeneratedNativeTurn(resolved.state, resolved.turn);
    if (generated) turnIds.push(generated.turnId);
  }
  const confirmed = typeof showConfirm !== 'function' || await showConfirm(
    turnIds.length > 1 ? 'Delete this turn and its reply?' : 'Delete this message?',
    { danger: true },
  );
  if (!confirmed) return;
  try {
    await runtimeScope.ConversationTurnStore.deleteConversationTurns(conv, turnIds);
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* authoritative recovery is best effort */ }
    if (typeof showToast === 'function') {
      showToast(error?.message || 'Delete failed', 'error');
    }
  }
}

/** Human-only promotion of an assistant conclusion into executable Charter.
 * The conclusion is prefilled from the authoritative Turn projection, and a
 * registered immutable Checker version is mandatory. */
async function _promoteNativeDecision(conv, turnId, projection) {
  const projectPath = String(conv?.projectPath || '').trim();
  const api = typeof Api !== 'undefined' ? Api.project : null;
  if (!projectPath || !api?.brainCheckers || !api?.promoteDecision) {
    if (typeof showToast === 'function') {
      showToast('Attach a project before promoting a decision.', 'warning');
    }
    return;
  }
  try {
    const sourceText = String(
      projection?.originalContent || projection?.content || '',
    ).trim();
    const catalog = await api.brainCheckers(projectPath);
    const checkers = Array.isArray(catalog?.items)
      ? catalog.items.filter(item => item?.enabled) : [];
    if (!checkers.length) {
      if (typeof showToast === 'function') {
        showToast(
          'No checker is registered. Register one in Project Charter first, or export the answer to docs.',
          'warning',
        );
      }
      if (typeof runtimeScope.openProjectBrain === 'function') {
        runtimeScope.openProjectBrain({ tab: 'charter', path: projectPath });
      }
      return;
    }
    if (typeof showPrompt !== 'function') return;
    const decisionText = await showPrompt('Promote as executable decision', {
      defaultValue: sourceText.slice(0, 4000),
    });
    if (!String(decisionText || '').trim()) return;
    const choices = checkers.map(item =>
      `${item.checkerId}@${item.version} — ${item.label || item.checkerId}`,
    );
    const selected = await showPrompt(
      `Select checker version:\n${choices.join('\n')}`,
      { defaultValue: `${checkers[0].checkerId}@${checkers[0].version}` },
    );
    if (!String(selected || '').trim()) return;
    const match = /^(.+)@(\d+)$/.exec(String(selected).trim());
    const checker = match && checkers.find(item =>
      String(item.checkerId) === match[1]
        && Number(item.version) === Number(match[2]),
    );
    if (!checker) {
      if (typeof showToast === 'function') {
        showToast('Choose an exact registered checker ID and version.', 'error');
      }
      return;
    }
    const identity = `decision:${conv.id}:${turnId}`.slice(0, 128);
    await api.promoteDecision(projectPath, {
      decisionId: identity,
      text: String(decisionText).trim(),
      checkerRef: { id: checker.checkerId, version: checker.version },
      sourceConversationId: conv.id,
      sourceTurnId: turnId,
    });
    if (typeof showToast === 'function') {
      showToast('Decision promoted with its checker.', 'success');
    }
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(error?.message || 'Decision promotion failed', 'error');
    }
  }
}

async function _createNativeBranch(conv, parentTurnId) {
  if (!conv || !parentTurnId
      || typeof runtimeScope.ConversationTurnStore === 'undefined') return;
  const title = typeof showPrompt === 'function'
    ? await showPrompt(typeof t === 'function' ? t('branch.namePrompt') : 'Branch name')
    : '';
  if (!title?.trim()) return;
  try {
    await runtimeScope.ConversationTurnStore.createBranchLane(
      conv, parentTurnId, { title: title.trim(), kind: 'branch' },
    );
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(error?.message || 'Branch creation failed', 'error');
    }
  }
}

async function _createNativeBranchFromSelection(
  conv, parentTurnId, title, selectedText,
) {
  if (!conv || !parentTurnId || !title?.trim()
      || typeof runtimeScope.ConversationTurnStore === 'undefined') return false;
  return createSelectionBranch({
    create: () => runtimeScope.ConversationTurnStore.createBranchLane(
      conv, parentTurnId, {
        title: title.trim(), kind: 'branch',
        anchorText: String(selectedText || '').slice(0, 200),
        parentSelection: String(selectedText || ''),
      },
    ),
    isCurrent: () => getActiveConv()?.id === conv.id,
    open: laneId => {
      runtimeScope.ConversationSurfacePresentation?.setExpandedBranchLane?.(conv.id, laneId);
      _openNativeBranchComposer(conv, parentTurnId, laneId);
      document.getElementById('userInput')?.focus();
    },
    error: error => {
      if (typeof showToast === 'function') {
        showToast(error?.message || t('selection.failed'), 'error');
      }
    },
  });
}

const _nativeBranchComposer = createBranchComposerSession();

function _nativeBranchDescriptor(conv, parentTurnId, laneId) {
  const state = runtimeScope.ConversationTurnStore
    ?.ensureRuntimeStore?.(conv.id)?.getState?.();
  const parent = state?.turnsById?.[parentTurnId];
  return parent?.projection?._branchLanes?.find(
    descriptor => descriptor?.laneId === laneId,
  ) || null;
}

function _renderNativeBranchComposerChrome() {
  const target = _nativeBranchComposer.current();
  const input = document.getElementById('userInput');
  if (input) {
    input.placeholder = target
      ? (typeof t === 'function'
        ? t('branch.inputPlaceholder', { title: target.title })
        : `Reply in ${target.title}`)
      : t('chat.messagePlaceholder');
  }
  let banner = document.getElementById('branch-mode-banner');
  if (!target) {
    if (banner) banner.remove();
    return;
  }
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'branch-mode-banner';
    banner.className = 'branch-mode-banner';
    const inputBox = document.querySelector('.input-box');
    if (inputBox?.parentElement) inputBox.parentElement.insertBefore(banner, inputBox);
  }
  const label = document.createElement('span');
  label.className = 'branch-mode-banner-text';
  label.textContent = typeof t === 'function'
    ? t('branch.modeBanner', { title: target.title })
    : `Replying in ${target.title}`;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'branch-mode-banner-exit';
  close.textContent = `✕ ${typeof t === 'function' ? t('branch.exit') : 'Exit'}`;
  close.addEventListener('click', closeBranchPanel);
  banner.replaceChildren(label, close);
}

function _openNativeBranchComposer(conv, parentTurnId, laneId) {
  const descriptor = _nativeBranchDescriptor(conv, parentTurnId, laneId);
  if (!descriptor) return;
  _nativeBranchComposer.open({
    conversationId: conv.id,
    parentTurnId,
    laneId,
    title: descriptor.title || 'Branch',
  });
  _renderNativeBranchComposerChrome();
}

function _closeNativeBranchComposer(collapseSurface) {
  const previous = _nativeBranchComposer.close();
  _renderNativeBranchComposerChrome();
  if (collapseSurface && previous) {
    runtimeScope.ConversationSurfacePresentation?.setExpandedBranchLane?.(
      previous.conversationId, null,
    );
  }
}

function closeBranchPanel() {
  _closeNativeBranchComposer(true);
}

function isBranchModeActive() {
  return _nativeBranchComposer.isActive(
    typeof activeConvId === 'string' ? activeConvId : undefined,
  );
}

function getActiveBranchContext() {
  return _nativeBranchComposer.current();
}

window.addEventListener('tofu:language-change', _renderNativeBranchComposerChrome);

async function sendBranchMessage(text, images) {
  const target = _nativeBranchComposer.current();
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!target || !conv || conv.id !== target.conversationId) return;
  const store = runtimeScope.ConversationTurnStore.ensureRuntimeStore(conv.id);
  const state = store.getState();
  const laneTurnIds = state.laneOrder[target.laneId] || [];
  const live = [...laneTurnIds].reverse().map(turnId => state.turnsById[turnId])
    .find(turn => turn && (turn.status === 'pending' || turn.status === 'running'));
  if (live) return;
  let replyQuotes = [];
  if (typeof getPendingReplyQuotes === 'function') {
    replyQuotes = getPendingReplyQuotes() || [];
    if (replyQuotes.length) clearReplyQuote();
  }
  const payload = {
    text: String(text || ''),
    images: images || [],
    ...(replyQuotes.length ? { replyQuotes } : {}),
  };
  try {
    const config = await _buildConvConfig(conv);
    const descriptor = _nativeBranchDescriptor(
      conv, target.parentTurnId, target.laneId,
    ) || { laneId: target.laneId, title: target.title };
    await runtimeScope.ConversationTurnStore.submitBranch(
      conv,
      { ...descriptor, laneId: target.laneId },
      target.parentTurnId,
      payload,
      config,
      {
        commandId: typeof _newClientMsgId === 'function'
          ? _newClientMsgId() : `branch:${target.laneId}:${Date.now()}`,
        kind: 'branch_reply',
        actor: 'assistant',
      },
    );
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* best-effort recovery */ }
    if (typeof showToast === 'function') {
      showToast(error?.message || 'Branch send failed', 'error');
    }
  }
}

function promptNewBranch(parentTurnId, preTitle, selectedText) {
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!conv || !parentTurnId) return Promise.resolve(false);
  return _createNativeBranchFromSelection(
    conv, parentTurnId, preTitle || String(selectedText || '').slice(0, 40), selectedText,
  );
}

async function _deleteNativeBranch(conv, parentTurnId, laneId) {
  if (!conv || !parentTurnId || !laneId
      || typeof runtimeScope.ConversationTurnStore === 'undefined') return;
  const confirmed = typeof showConfirm !== 'function' || await showConfirm(
    typeof t === 'function' ? t('branch.deleteConfirm') : 'Delete this branch?',
    { danger: true },
  );
  if (!confirmed) return;
  const state = _nativeTurnState(conv, parentTurnId)?.state;
  const live = [...(state?.laneOrder?.[laneId] || [])].reverse()
    .map(id => state.turnsById[id])
    .find(turn => turn?.currentAttemptId
      && ['pending', 'running'].includes(turn.status));
  if (live?.currentAttemptId) {
    runtimeScope.ConversationTurnStore.abortAttempt(live.currentAttemptId)
      .catch(error => console.warn('[ConversationSurface] branch abort failed', error));
  }
  try {
    await runtimeScope.ConversationTurnStore.deleteBranchLane(
      conv, parentTurnId, laneId,
    );
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); } catch (_e) { /* best effort */ }
    if (typeof showToast === 'function') showToast('Branch delete failed', 'error');
  }
}

function _stopNativeBranch(conv, laneId) {
  if (!conv || !laneId || typeof runtimeScope.ConversationTurnStore === 'undefined') return;
  const store = runtimeScope.ConversationTurnStore.ensureRuntimeStore(conv.id);
  const ids = store.getState().laneOrder[laneId] || [];
  const live = [...ids].reverse().map(id => store.getState().turnsById[id]).find(turn =>
    turn?.currentAttemptId && ['pending', 'running'].includes(turn.status));
  if (live?.currentAttemptId) {
    runtimeScope.ConversationTurnStore.abortAttempt(live.currentAttemptId)
      .catch(error => console.warn('[ConversationSurface] branch stop failed', error));
  }
}

async function _removeNativeQueueItem(conv, queueId) {
  const service = runtimeScope.ConversationTurnStore;
  if (!conv || !queueId || !service?.cancelQueuedTurn) return;
  try {
    const response = await service.cancelQueuedTurn(conv, queueId);
    if (!response?.cancelled) throw new Error('Queue removal was rejected');
    const projection = response.inputTurn?.projection || {};
    const attachments = Array.isArray(projection.attachments)
      ? projection.attachments : [];
    if (response.inputTurn && typeof _restoreCapturedComposerDraft === 'function') {
      _restoreCapturedComposerDraft({
        inputValue: projection.originalContent || projection.content || '',
        images: Array.isArray(projection.images) ? projection.images : [],
        pdfTexts: [
          ...(Array.isArray(projection.pdfTexts) ? projection.pdfTexts : []),
          ...attachments.filter((item) => item?.kind === 'document'),
        ],
        videos: [
          ...(Array.isArray(projection.videos) ? projection.videos : []),
          ...attachments.filter((item) => item?.kind === 'video'),
        ],
        replyQuotes: Array.isArray(projection.replyQuotes)
          ? projection.replyQuotes : [],
        convRefs: Array.isArray(projection.convRefs) ? projection.convRefs : [],
      });
    }
    if (typeof updateSendButton === 'function') updateSendButton();
  } catch (error) {
    if (typeof showToast === 'function') showToast('Queue removal failed', 'error');
    console.warn('[ConversationSurface] queue removal failed', error);
  }
}

async function _mutateNativeTurnFiles(conv, turnId, operation) {
  const service = runtimeScope.ConversationTurnStore;
  if (!conv || !turnId || !service?.mutateConversationFileChanges) return;
  const current = service.ensureRuntimeStore(conv.id)
    .getState().turnsById[turnId];
  const count = current?.projection?.fileChanges?.count || 0;
  if (operation === 'undo' && typeof showConfirm === 'function') {
    const confirmed = await showConfirm(
      typeof t === 'function'
        ? t('project.undoTurnConfirm', { count })
        : `Undo changes to ${count} file${count === 1 ? '' : 's'}?`,
      { danger: true },
    );
    if (!confirmed) return;
  }
  try {
    const response = await service.mutateConversationFileChanges(
      conv, turnId, operation,
    );
    const effect = response?.effect || {};
    if (typeof showToast === 'function') {
      const amount = operation === 'undo' ? effect.undone : effect.redone;
      showToast(
        operation === 'undo' ? '↩️' : '↪️',
        operation === 'undo' ? 'Undo Complete' : 'Redo Complete',
        Number.isFinite(Number(amount))
          ? `${operation === 'undo' ? 'Reverted' : 'Re-applied'} ${amount} file change${
              Number(amount) === 1 ? '' : 's'}`
          : '',
        4000,
      );
    }
    if (typeof rescanProject === 'function') void rescanProject();
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(
        error?.body?.message || error?.message
          || `${operation === 'undo' ? 'Undo' : 'Redo'} failed`,
        'error',
      );
    }
  }
}

const _nativeTurnRenderers = createClassicConversationRenderers({

  configuredFallbackTarget: _registeredFallbackRoute,
  explicitFallbackRetry(conversationId, turnId) {
    const retry = _explicitFallbackRecovery.retryOffer();
    return retry?.conversationId === conversationId && retry.turnId === turnId
      ? { model: retry.model } : null;
  },
  usageRecovery: {
    inspect(conversationId, turnId, signal) {
      return conversationSyncApi.usageRecovery(conversationId, turnId, { signal, timeout: 30_000 });
    },
    recover(conversationId, turnId, body) {
      return conversationSyncApi.recoverUsage(conversationId, turnId, body);
    },
    createCommandId: createTurnCommandId,
    onAccepted(conversationId) {
      const conv = conversations.find(item => item?.id === conversationId) || { id: conversationId };
      void runtimeScope.ConversationTurnStore.hydrateConversation(conv)
        .catch(error => console.warn('[UsageRecovery] authoritative refresh failed', error));
    },
  },
  choiceInteractions(conversationId, turnId) {
    return _guidanceAnswerPorts(conversationId, turnId, (request) => replyToGuidanceInChat(request));
  },
  inspectToolEffect(reference, signal) {
    return conversationSyncApi.inspectToolEffect(
      reference.conversationId, reference.turnId, reference.operationId, reference.ownerScope,
      { signal, timeout: 30_000 },
    );
  },
  subscribeToolCards(listener) {
    return toolCardPresentation.subscribe(listener);
  },
  debugEnabled() {
    return typeof _featureFlags !== 'undefined' && !!_featureFlags?.debug_mode;
  },
  loadToolResult(reference, signal) {
    return conversationSyncApi.turnToolResult(
      reference.conversationId, reference.turnId, reference.roundIndex,
      reference.projectionRevision, reference.ownerScope,
      { signal, timeout: 30_000 },
    );
  },
  renderSafeMarkdownHtml(markdown, options) {
    return typeof renderMarkdown === 'function'
      ? renderMarkdown(markdown, options)
      : escapeHtml(markdown);
  },
  renderTurnAvatarHtml(turn) {
    const initiator = turn?.metadata?.origin?.initiator;
    if (initiator === 'peer' || initiator === 'operator') {
      return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        + 'stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 '
        + '4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path '
        + 'd="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>';
    }
    if (turn.actor === 'planner') {
      return _resolveRoleAvatarIcons()?.plannerHtml || '';
    }
    if (turn.actor === 'critic' || turn.actor === 'virtual_user') {
      return _resolveRoleAvatarIcons()?.criticHtml || '';
    }
    if (turn.actor === 'human' && (!initiator || initiator === 'human')) {
      return _resolveRoleAvatarIcons()?.userHtml || '';
    }
    return _resolveRoleAvatarIcons()?.workerHtml || '';
  },
  renderTurnContextParts(block) {
    const rendered = runtimeScope.renderTurnCtxNote?.(block?.value?.snapshot);
    return rendered && typeof rendered === 'object'
      ? { fold: rendered.fold || '', rail: rendered.rail || '' }
      : null;
  },
  renderToolBlockHtml(block, turn) {
    const sourceRound = block.round || {
      attemptId: block.source?.attemptId,
      taskId: block.source?.taskId,
      roundNum: block.source?.llmRound == null ? undefined : Number(block.source.llmRound) + 1,
      llmRound: block.source?.llmRound,
      agentId: block.source?.agentId,
      toolCallId: block.toolCallId,
      toolName: block.name,
      toolArgs: block.input,
      toolContent: block.result?.content,
      result: block.result,
      status: block.result?.status,
    };
    /* A settled turn can never receive a human-guidance answer again — the
     * backend pending-request map is process-local and the blocked handler
     * either finalized the round itself (user Stop) or died with the process
     * (server restart, which leaves the persisted round at awaiting_human
     * forever). The renderer downgrades those leftovers to an expired,
     * non-interactive card off this flag. */
    const turnSettled = !(turn.status === 'pending' || turn.status === 'running');
    /* A settled turn that died inside ask_human keeps the persisted question
     * round at awaiting_human forever. When its settlement offers the
     * answer_guidance resume option for THIS question, the expired card
     * stays answerable: the late answer becomes the round's tool result in
     * a new attempt and generation continues from the answered call. */
    const resumeOptions = turn.source?.settlement?.resumeOptions;
    const answerGuidanceOfferedFor = (round) => {
      const guidanceId = typeof round?.guidanceId === 'string'
        ? round.guidanceId : '';
      return turnSettled && Boolean(guidanceId)
        && Array.isArray(resumeOptions)
        && resumeOptions.some((option) => (
          option?.operation === 'answer_guidance'
          && option?.anchor?.guidanceId === guidanceId
        ));
    };
    const decorateRound = (round) => {
      /* A resumed Turn contains rounds from several attempts. Prefer the
       * round-owned task; using the Turn's latest task made every historical
       * R-link open the resumed executor and erased the only presentation
       * boundary between repeated llmRound counters. */
      const ownedTaskId = round?.taskId || round?._taskId || '';
      const compatibleLatestTask = !round?.attemptId
        || round.attemptId === turn.attemptId;
      const taskId = ownedTaskId || (compatibleLatestTask ? turn.taskId : '');
      return {
        ...humanGuidancePresentation.decorate(
          turn.source.conversationId, round,
        ),
        _turnId: turn.turnId,
        _turnSettled: turnSettled,
        ...(answerGuidanceOfferedFor(round) ? { _hgAnswerGuidance: true } : {}),
        ...(taskId ? { _taskId: taskId } : {}),
      };
    };
    const round = decorateRound(sourceRound);
    const allRounds = Array.isArray(turn.source.projection.toolRounds)
      // Sibling title/swarm lookup reads canonical args/result identity only.
      // Reuse the immutable source array so its WeakMap title index is computed
      // once per projection; decorate only the displayed row's local guidance.
      ? turn.source.projection.toolRounds : [round];
    if (typeof _renderToolSlot === 'function') return _renderToolSlot(round, allRounds);
    return typeof renderToolRoundsHTML === 'function'
      ? renderToolRoundsHTML([round], turn.status === 'pending' || turn.status === 'running')
      : '';
  },
  renderInjectionBlockHtml(block, turn) {
    const fieldByChannel = {
      inbox: '_inboxInjects', peer: '_peerInjects',
      'user-steer': '_userSteerInjects', 'stall-nudge': '_stallNudges',
      'background-command': '_bgCommandInjects',
    };
    const field = fieldByChannel[block.channel];
    if (!field || typeof _rehydrateInjectRows !== 'function') return '';
    const projection = { [field]: block.items };
    const rounds = _rehydrateInjectRows(projection, []).map((round) => ({
      ...humanGuidancePresentation.decorate(
        turn.source.conversationId, round,
      ),
      _turnId: turn.turnId,
      ...(turn.taskId ? { _taskId: turn.taskId } : {}),
    }));
    if (typeof _renderToolSlot === 'function') {
      return rounds.map(round => _renderToolSlot(round, rounds)).join('');
    }
    return typeof renderToolRoundsHTML === 'function'
      ? renderToolRoundsHTML(rounds, false) : '';
  },
  renderTurnFooterHtml(turn) {
    if (turn.actor === 'human'
        || !['completed', 'interrupted', 'truncated', 'failed'].includes(turn.status)) {
      return '';
    }
    if (typeof renderFinishInfo !== 'function') return '';
    /* Finish presentation consumes the selected Turn directly.  Do not join
     * back through the positional compatibility message array: that would
     * make an otherwise keyed render depend on array order and a second
     * projection snapshot. */
    const finishProjection = {
      ...turn.source.projection,
      _conversationId: turn.source.conversationId,
      ...(turn.taskId ? { _taskId: turn.taskId } : {}),
      _turnStatus: turn.status,
      _turnSettlement: turn.source.settlement || {},
      /* Only the completion-date fallback comes from the TurnRecord.
       * Execution duration is already folded in projection.timingTrace. */
      _turnUpdatedAt: Number(turn.source.updatedAt) || 0,
      _commandPending: turn.commandPending || null,
      waitingOn: turn.source.projection.waitingOn,
    };
    if (turn.metadata.fallbackInTimeline) {
      delete finishProjection.fallbackModel;
      delete finishProjection.fallbackFrom;
      delete finishProjection.fallbackReason;
      delete finishProjection.fallbackKind;
    }
    const html = renderFinishInfo(finishProjection, turn.turnId);
    if (!html) return '';
    const holder = document.createElement('div');
    holder.innerHTML = html;
    const finish = holder.firstElementChild;
    return finish?.classList.contains('message-finish') ? finish.innerHTML : html;
  },
  renderContextProvenanceHtml,
  contextProvenanceFacts,
  renderProvenanceBlockHtml(block) {
    const value = block.value || {};
    const passive = { ...value };
    const awaiting = !value.mcpLoginHint?.phase || value.mcpLoginHint.phase === 'awaiting_approval';
    if (awaiting) delete passive.mcpLoginHint;
    if (Array.isArray(passive.preferencesLearned)) {
      passive.preferencesLearned = passive.preferencesLearned.filter(item => !item.pending);
      if (!passive.preferencesLearned.length) delete passive.preferencesLearned;
    }
    return (awaiting ? renderMcpLoginHintHtml(value.mcpLoginHint) : '')
      + renderTurnProvenanceHtml(passive)
      + renderPreferenceLearnedHtml(value.preferencesLearned);
  },
  resolveMediaUrl(url) {
    return typeof apiUrl === 'function' && String(url || '').startsWith('/')
      ? apiUrl(url) : String(url || '');
  },
  /* Rich-response figures own their bitmap as an object URL, so they cannot
   * reach the Markdown lightbox through source markup. Same opener, same
   * zoom behavior. */
  previewImage(source) {
    if (typeof openImagePreview === 'function') openImagePreview(source);
  },
  localizedText(key, fallback, values) {
    const localizedValues = values && typeof values === 'object'
      ? { ...values } : values;
    const reasonKey = localizedValues?.reasonKey;
    if (reasonKey && typeof t === 'function') {
      const reason = t(reasonKey);
      if (reason && reason !== reasonKey) localizedValues.reason = reason;
    }
    const translated = typeof t === 'function' ? t(key, localizedValues) : '';
    return translated && translated !== key ? translated : fallback;
  },
  /* Lazy full stop report for one concluded goal run: the fold record only
   * carries the bounded excerpt; fetch the complete text persisted with the
   * durable GoalRun transition. */
  fetchAutopilotRunReport(runId) {
    if (!runId || typeof activeConvId !== 'string' || !activeConvId) {
      return Promise.resolve(null);
    }
    if (!(typeof Api !== 'undefined' && Api.chat?.autopilotReport)) {
      return Promise.resolve(null);
    }
    return Api.chat.autopilotReport(activeConvId, runId)
      .then((result) => (result && typeof result.report === 'string'
        ? { report: result.report } : null))
      .catch(() => null);
  },
  async resumeGoalRun(runId) {
    const { createOrchestrationTaskRequestClient } = await import('../features/orchestration/task-request');
    const result = await createOrchestrationTaskRequestClient({
      api: resolveOrchestrationApiClient(Api),
    }).resume(runId);
    return { ok: result.ok === true, status: Number(result.status || 0),
      runId: String(result.runId || ''), error: String(result.error || '') };
  },
  roleLabel(turn) {
    const initiator = turn?.metadata?.origin?.initiator;
    const originLabels = {
      autopilot: ['initiator.autopilot', 'Autopilot'],
      proactive: ['initiator.proactive', 'Proactive Agent'],
      timer: ['initiator.timer', 'Timer'],
      brain: ['initiator.brain', 'Project Brain'],
      peer: ['peer.senderLabel', 'Peer'],
      operator: ['peer.operatorLabel', 'Operator'],
      swarm: ['initiator.swarm', 'Auto-continued'],
    };
    if (originLabels[initiator]) {
      const [key, fallback] = originLabels[initiator];
      const translated = typeof t === 'function' ? t(key) : '';
      return translated && translated !== key ? translated : fallback;
    }
    if (turn.actor === 'human') {
      return typeof t === 'function' ? t('role.you') : 'You';
    }
    return {
      assistant: 'Agent', planner: 'Planner', critic: 'Critic',
      virtual_user: 'Autopilot',
    }[turn.actor] || turn.actor;
  },
  actionLabel(action, turn, actionView) {
    const keys = {
      copy: 'msgAction.copy', inspect: 'msgAction.inspect', review: 'turnReview.title',
      edit: 'msgAction.edit', regenerate: 'msgAction.regen',
      resume: 'msgAction.continue', translate: 'msgAction.translate',
      export: 'msgAction.export', 'promote-decision': 'msgAction.promoteDecision',
      branch: 'branch.add', delete: 'msgAction.delete',
    };
    const fallbacks = {
      copy: 'Copy', inspect: 'Inspect', review: 'Task acceptance', edit: 'Edit', regenerate: 'Regen',
      resume: 'Continue',
      translate: 'Translate', export: 'Export',
      'promote-decision': 'Promote as decision', branch: 'Branch', delete: 'Delete',
    };
    /* A failed turn whose settlement offers no resumable checkpoint still
     * renders the generic 'resume' action; label it for what it does. */
    const labelAction = action === 'resume'
        && actionView?.operation === 'regenerate' ? 'regenerate' : action;
    const key = labelAction === 'translate'
        && turn?.metadata?.translation?.available
        && turn.metadata.translation.displayMode === 'translated'
      ? 'msgAction.original' : (keys[labelAction] || labelAction);
    const translated = typeof t === 'function' ? t(key) : '';
    const fallback = labelAction === 'translate' && key === 'msgAction.original'
      ? 'Original' : (fallbacks[labelAction] || labelAction);
    return translated && translated !== key ? translated : fallback;
  },
  formatTimestamp(timestamp) {
    const value = new Date(timestamp);
    return Number.isNaN(value.getTime()) ? '' : value.toLocaleString([], {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  },
});

const _planDecisionBar = createPlanDecisionBar({
  translate(key) {
    return typeof t === 'function' ? t(key) : '';
  },
  onContinueDiscussion(conversationId) {
    if (typeof activeConvId !== 'undefined' && activeConvId !== conversationId) return;
    if (typeof setAgentMode === 'function') setAgentMode('plan');
    document.getElementById('userInput')?.focus();
  },
  async onExecute(conversationId, decision, contextMode) {
    const service = runtimeScope.ConversationTurnStore;
    const conv = service?.findConversation?.(conversationId)
      || (typeof conversations !== 'undefined'
        ? conversations.find(item => item?.id === conversationId) : null);
    if (!conv || !service?.executeConversationPlan
        || (typeof activeConvId !== 'undefined'
          && activeConvId !== conversationId)) {
      throw new Error('Conversation execution runtime is unavailable.');
    }
    const config = await _buildConvConfig(conv);
    const hadAutopilot = Boolean(conv.autopilotEnabled);
    await service.executeConversationPlan(
      conv,
      decision.sourceTurnId,
      decision.planId,
      decision.sourceProjectionRevision,
      contextMode,
      config,
    );
    if (typeof activeConvId !== 'undefined' && activeConvId === conversationId) {
      if (typeof _applyAgentModeUI === 'function') _applyAgentModeUI('standard');
      if (typeof _applyFlowUI === 'function') _applyFlowUI('');
      if (typeof _applyImageGenUI === 'function') _applyImageGenUI(false);
      if (hadAutopilot && typeof _disarmAutopilot === 'function') {
        _disarmAutopilot();
      }
      if (typeof updateSendButton === 'function') updateSendButton();
    }
    if (typeof ConvCache !== 'undefined') void ConvCache.put(conv);
  },
  onError(error, conversationId) {
    const conv = runtimeScope.ConversationTurnStore?.findConversation?.(
      conversationId,
    ) || (typeof conversations !== 'undefined'
      ? conversations.find(item => item?.id === conversationId) : null);
    if (conv) {
      void runtimeScope.ConversationTurnStore?.hydrateConversation?.(conv)
        .catch(() => {});
    }
    if (typeof showToast === 'function'
        && (typeof activeConvId === 'undefined'
          || activeConvId === conversationId)) {
      showToast(
        error?.body?.message || error?.message || 'Plan execution failed',
        'error',
      );
    }
  },
});

runtimeScope.PaperPreviewPresentation = createPaperPreviewChanges({
  changed(changes) {
    const entry = runtimeScope._paperPreviewReceipts;
    if (typeof entry === 'function') void Promise.resolve(entry(changes)).catch((error) => {
      console.warn('[PaperPreview] feature unavailable:', error);
    });
  },
  discover(conversationId, hint) {
    runtimeScope._paperPreviewDiscover(conversationId, hint);
  },
  activated(conversationId) {
    const entry = featureRegistry._paperPreviewActivate;
    if (typeof entry === 'function') entry(conversationId);

    const conv = runtimeScope.ConversationTurnStore?.findConversation?.(conversationId);
    if (conv?.paperDocument) runtimeScope._paperPreviewDiscover(conversationId);
  },
});

runtimeScope.PlanDecisionPresentation = Object.freeze({
  activateConversation(conversationId) {
    _planDecisionBar.activateConversation(conversationId || null);
  },
});

const _automationTimeClient = createAutomationManagementClient();
// Temporary retained bridge: remove with the shell migration; the typed owner
// holds the only dialog lifetime and borrows existing conversation lifecycle.
const _turnReviewPresentation = createTurnReviewPresentation({
  activeConversationId: () => typeof getActiveConv === 'function' ? getActiveConv()?.id || null : null,
  load: (convId, turnId, signal) => Api.conversations.turnReview(convId, turnId, signal),
  save: (convId, turnId, command, signal) => Api.conversations.saveTurnReview(convId, turnId, command, signal),
  facts: (convId, signal) => Api.conversations.workingFacts(convId, signal),
  outputs: async (convId, signal) => {
    const value = await Api.artifacts.forConv(convId, signal);
    if (!Array.isArray(value?.artifacts)) throw new Error(t('turnReview.outputsError'));
    return value.artifacts.map(item => ({ id: String(item.id), title: String(item.title || '') }));
  },
  openOutput: id => window.open(Api.artifacts.viewUrl(id), '_blank', 'noopener'),
  text: (key, fallback) => { const value = t(key); return value && value !== key ? value : fallback; },
  reportError: message => { if (typeof showToast === 'function') showToast(message, 'error'); },
});
if (typeof retainedCompositionLifecycle !== 'undefined') {
  retainedCompositionLifecycle.add(() => _turnReviewPresentation.destroy());
}

const _conversationSurfaceController = createConversationSurfaceController({
  loadTaskTimePresentations(conversationId, attemptId, signal) {
    return _automationTimeClient.presentations(conversationId, attemptId, { signal });
  },
  taskTimePresentationError(error) {
    debugLog(`[automation-time] ${error?.message || error}`);
  },
  isActive(conversationId) {
    return typeof activeConvId !== 'undefined' && activeConvId === conversationId;
  },
  getContainer() {
    return document.getElementById('chatInner');
  },
  schedule: createAnimationFrameScheduler(globalThis).schedule,
  nativeRenderers: {
    ..._nativeTurnRenderers,
    renderPlanDecision(node, decision, context) {
      _planDecisionBar.activateConversation(context.conversationId);
      _planDecisionBar.render(node, context.conversationId, decision);
    },
  },
  requestInspectorEnabled() {
    return Boolean(
      typeof _featureFlags !== 'undefined' && _featureFlags?.debug_mode,
    );
  },
  /* Settled turns whose projection predates the server-side cost fold have
   * no authoritative msg.cost; renderFinishInfo falls back to calcCostCny,
   * whose micro-batch lands AFTER the render commit. The batch then requests
   * an authoritative re-render, but nothing in the Turn changed, so the
   * surface footer compare would skip the re-render and the ¥ tag never
   * appears. The signature makes the fill visible to that compare. Cache
   * misses return null and simply rejoin the already-queued batch — no extra
   * requests. */
  costSignatureSnapshot(state) {
    const signatures = new Map();
    if (typeof calcCostCny !== 'function') return signatures;
    const turnsById = state?.turnsById || {};
    for (const turnId of entityRecordKeys(turnsById)) {
      const turn = turnsById[turnId];
      if (!turn || turn.actor !== 'assistant') continue;
      if (!['completed', 'interrupted', 'truncated', 'failed']
          .includes(turn.status)) continue;
      const projection = turn.projection || {};
      if (projection.cost || !projection.usage) continue;
      const cost = calcCostCny(
        projection.usage,
        projection.model || '',
        projection.providerId || projection.provider_id || '',
      );
      if (cost && cost.costCny > 0) {
        signatures.set(turnId, String(cost.costCny));
      }
    }
    return signatures;
  },
  getScrollViewport() {
    return document.getElementById('chatContainer');
  },
  windowing: {
    label(direction, count) {
      return t(
        direction === 'earlier' ? 'chat.loadEarlierTurns' : 'chat.loadLaterTurns',
        { n: count },
      );
    },
    blockLabel(count) {
      return t('chat.loadEarlierBlocks', { n: count });
    },

    historyStatusLabel(state) {
      return t(state === 'loading' ? 'chat.historyLoading' : 'chat.historyFailed');
    },

    historyRetryLabel() { return t('chat.historyRetry'); },
  },
  roundOutlineLabel(key, n) {
    return t(key, { n });
  },
  transportNoticeLabel(state) {
    const keys = {
      recovering: 'sync.transportRecovering',
      degraded: 'sync.transportDegraded',
      offline: 'sync.transportOffline',
    };
    return t(keys[state] || 'sync.transportRecovering');
  },
  onIntent(intent) {
    const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
    if (!conv || conv.id !== intent.conversationId) return;
    if (intent.type === 'load-earlier-turns' && intent.laneId) {
      return runtimeScope.ConversationTurnStore?.loadConversationTurnPage?.(
        conv, intent.laneId, intent.beforeOrdinal, intent.limit,
      );
    }
    if (intent.type === 'remove-queue' && intent.queueId) {
      void _removeNativeQueueItem(conv, intent.queueId);
      return;
    }
    if (intent.type === 'open-project-brain') {
        if (typeof runtimeScope.openProjectBrain === 'function') {
          runtimeScope.openProjectBrain();
        }
      return;
    }
    if (intent.type === 'open-conversation' && intent.operation) {
      if (typeof loadConversation === 'function') loadConversation(intent.operation);
      return;
    }
    if (intent.type === 'open-compaction' && intent.operation) {
        if (typeof runtimeScope.openCompactionViewer === 'function') {
          runtimeScope.openCompactionViewer(conv.id, intent.operation);
      }
      return;
    }
    if (intent.type === 'cancel-image-generation') {
      runtimeScope._igCancelGeneration?.();
      return;
    }
    if (intent.type === 'open-artifact' && intent.operation) {
      runtimeScope.Artifacts?.open?.(intent.operation);
      return;
    }
    const safeAttachmentUrl = (value, allowImageData) => {
      const raw = typeof value === 'string' ? value.trim() : '';
      if (/^(?:https?:|blob:)/i.test(raw)) return raw;
      // Root-relative API URLs must carry the deployment BASE_PATH (same as
      // the renderer's resolveMediaUrl port), or the modal requests them
      // from the server root and 404s when the app is served under a subpath.
      if (raw.startsWith('/')) {
        return typeof apiUrl === 'function' ? apiUrl(raw) : raw;
      }
      if (/^\.\.?\//.test(raw)) return raw;
      return allowImageData && /^data:image\/(?:avif|gif|jpe?g|png|webp);base64,/i.test(raw)
        ? raw : '';
    };
    /* Injection-lane (steer/peer) thumbnails carry their own previewSource
     * and need no positional attachments lookup or turn-scoped projection;
     * open them before the turn guard so a direct source still reaches the
     * lightbox. */
    if (intent.type === 'preview-image' && typeof intent.previewSource === 'string') {
      const source = safeAttachmentUrl(intent.previewSource, true);
      if (source && typeof openImagePreview === 'function') openImagePreview(source);
      return;
    }
    if (!intent.turnId) return;
    if (intent.type === 'undo-turn-files' || intent.type === 'redo-turn-files') {
      void _mutateNativeTurnFiles(
        conv,
        intent.turnId,
        intent.type === 'undo-turn-files' ? 'undo' : 'redo',
      );
      return;
    }
    const resolved = _nativeTurnState(conv, intent.turnId);
    const transientTurn = runtimeScope.ConversationTransientTurns?.get?.(
      conv.id, intent.turnId,
    );
    if (!resolved && !transientTurn) return;
    if (intent.type === 'review' && resolved?.turn?.actor === 'assistant' && resolved.turn.laneId === 'main') {
      void _turnReviewPresentation.open(conv.id, intent.turnId); return;
    }
    const projection = resolved?.turn?.projection || transientTurn?.projection || {};
    const attachmentIndex = Number.parseInt(intent.operation || '', 10);
    if (intent.type === 'toggle-branch' && intent.laneId) {
      if (intent.operation === 'open') {
        _openNativeBranchComposer(conv, intent.turnId, intent.laneId);
      } else {
        _closeNativeBranchComposer(false);
      }
      return;
    }
    else if (intent.type === 'delete-branch' && intent.laneId) {
      void _deleteNativeBranch(conv, intent.turnId, intent.laneId);
    }
    else if (intent.type === 'stop-branch' && intent.laneId) {
      _stopNativeBranch(conv, intent.laneId);
    }
    else if (intent.type === 'preview-image'
        && (typeof intent.previewSource === 'string'
          || Number.isInteger(attachmentIndex))) {
      /* Injection-lane (steer/peer) thumbnails carry their own previewSource;
       * ordinary bubble images resolve through the positional images[] index. */
      const source = typeof intent.previewSource === 'string'
        ? safeAttachmentUrl(intent.previewSource, true)
        : safeAttachmentUrl(
          projection.images?.[attachmentIndex]?.preview, true,
        );
      if (source && typeof openImagePreview === 'function') openImagePreview(source);
    }
    else if (intent.type === 'preview-generated-image'
        && Number.isInteger(attachmentIndex)) {
      const source = safeAttachmentUrl(
        projection.imageGeneration?.results?.[attachmentIndex]?.imageUrl, true,
      );
      if (source && typeof openImagePreview === 'function') openImagePreview(source);
    }
    else if (intent.type === 'retry-image-generation'
        && Number.isInteger(attachmentIndex)) {
      runtimeScope._igRetryGenerationTurn?.(conv, intent.turnId, attachmentIndex);
    }
    else if (intent.type === 'open-media' && intent.operation) {
      const mediaAttachment = projection.attachments?.find(
        (item) => item?.attachmentId === intent.operation,
      );
      const isImage = /^image\//i.test(String(mediaAttachment?.mimeType || ''));
      const source = safeAttachmentUrl(mediaAttachment?.sourceUrl, isImage);
      if (source && isImage && typeof openImagePreview === 'function') openImagePreview(source);
      else if (source && typeof openVideoUrl === 'function') openVideoUrl(source);
    }
    else if (intent.type === 'open-video' && Number.isInteger(attachmentIndex)) {
      const source = safeAttachmentUrl(
        projection.videos?.[attachmentIndex]?.video_url, false,
      );
      if (source && typeof openVideoUrl === 'function') openVideoUrl(source);
    }
    else if (intent.type === 'preview-document' && Number.isInteger(attachmentIndex)) {
      const documentAttachment = projection.pdfTexts?.[attachmentIndex];
      if (documentAttachment && typeof openTextPreview === 'function') {
        openTextPreview(
          documentAttachment.name || 'Document',
          `${documentAttachment.pages || '?'} pages`,
          documentAttachment.text || '',
        );
      }
    }
    else if (intent.type === 'copy' && typeof _safeClipboardWrite === 'function') {
      const translatedSegments = (projection.segments || []).filter(segment =>
        (segment.type === 'text' || segment.type === 'thinking')
          && segment.translatedText).map(segment => segment.translatedText);
      const copyText = intent.operation === 'copy-translated'
        ? (projection.translatedContent || translatedSegments.join('\n\n')
          || projection.content || '')
        : (projection.originalContent || projection.content || '');
      _safeClipboardWrite(copyText)
        .then(() => _flashTurnCopyFeedback(intent.turnId))
        .catch(() => {});
    }
    else if (intent.type === 'copy-block' && intent.blockId
        && typeof _safeClipboardWrite === 'function') {
      /* Per-block copy: resolve the exact segment the button belongs to.
       * The turn-level Copy above only reaches projection.content (the final
       * answer); mid-turn prose lives on its own text segment. */
      const segment = (projection.segments || []).find(item =>
        item && item.blockId === intent.blockId && item.type === 'text');
      if (segment) {
        const actor = (resolved?.turn || transientTurn)?.actor;
        const terminalish = Boolean(segment.terminal || segment.deliverable);
        const authoritative = terminalish
          ? (projection.content ?? segment.text) : segment.text;
        const humanOriginal = actor === 'human' && terminalish
          ? projection.originalContent : undefined;
        const original = humanOriginal || authoritative || '';
        const translated = humanOriginal ? authoritative
          : (segment.translatedText
            || (terminalish ? projection.translatedContent : undefined));
        const blockText = intent.operation === 'copy-translated' && translated
          ? translated : original;
        _safeClipboardWrite(blockText)
          .then(() => _flashTurnCopyFeedback(intent.turnId, intent.blockId))
          .catch(() => {});
      } else if (intent.blockId.startsWith('coordinator-report:')) {
        /* A Coordinator report read inside the reply that published it keeps
         * its own Turn: copy that report, never the hosting reply. */
        const report = _nativeTurnState(conv, intent.blockId.slice('coordinator-report:'.length))
          ?.turn?.projection || {};
        const reportText = intent.operation === 'copy-translated' && report.translatedContent
          ? report.translatedContent : (report.content || '');
        _safeClipboardWrite(reportText)
          .then(() => _flashTurnCopyFeedback(intent.turnId, intent.blockId))
          .catch(() => {});
      }
    }
    else if (intent.type === 'inspect' && intent.operation
        && typeof runtimeScope.openRequestInspectorForTask === 'function') {
      void runtimeScope.openRequestInspectorForTask(intent.operation);
    }
    else if (intent.type === 'edit') {
      void _editNativeTurn(conv, intent.turnId);
    }
    else if (intent.type === 'regenerate') {
      void _regenerateNativeInput(conv, intent.turnId);
    }
    else if (intent.type === 'resume' && intent.operation) {
      void _operateNativeTurn(conv, intent.turnId, intent.operation);
    }
    else if (intent.type === 'use-configured-fallback') {
      const phase = resolved?.state?.livePhase;
      const offer = configuredFallbackPhaseOffer(
        phase, resolved?.turn, _registeredFallbackRoute(conv.id),
      );
      if (offer) void _explicitFallbackRecovery.use(offer);
    }
    else if (intent.type === 'retry-configured-fallback') {
      const retry = _explicitFallbackRecovery.retryOffer();
      if (retry?.conversationId === conv.id && retry.turnId === intent.turnId) {
        void _explicitFallbackRecovery.retry();
      }
    }
    else if (intent.type === 'translate') {
      void _runManualTurnTranslation(
        conv, intent.turnId, projection.content || '',
      );
    }
    else if (intent.type === 'export') {
      if (typeof ExportImages !== 'undefined') {
        ExportImages.exportMessageWithPreview(intent.turnId);
      }
    }
    else if (intent.type === 'promote-decision') {
      void _promoteNativeDecision(conv, intent.turnId, projection);
    }
    else if (intent.type === 'branch') void _createNativeBranch(conv, intent.turnId);
    else if (intent.type === 'delete') void _deleteNativeTurn(conv, intent.turnId);
  },
  onConversationMounted(conv) {
    _turnReviewPresentation.activate(conv.id);
    runtimeScope.Artifacts?.activateConversation?.(conv);
  },
  afterConversationCommit(conv, state, _force, viewModel, mountedTurnIds) {
    if (conversationReview.viewed(conv.id,
      runtimeScope.ConversationTurnRead.ordered(conv),
      mountedTurnIds)) renderConversationList();
    runtimeScope.renderBackgroundAgents?.();
    /* A cold selection commits its empty state before the snapshot lands.
     * Its served model is still unknown there, so the notice the selection
     * held keeps the composer still until a commit with Turns replaces it. */
    const awaitingSnapshot = conv._turnSnapshotRequired && !viewModel?.mainLane?.turns?.length;
    if (!awaitingSnapshot) {
      renderComposerModelNotice(document.getElementById('composerModelNotice'), viewModel, {
        localizedText: (key, fallback, values) => {
          const label = t(key, values);
          return label && label !== key ? label : undefined;
        },
      });
    }

    runtimeScope.ConversationTurnStore?.notifyRenderedState?.(
      conv.id, state,
      mountedTurnIds,
    );
    /* Running and terminal revisions reconcile into the same data-turn-id
     * node; this adapter never creates an alternate message identity. */
    _updateScrollToBottomBtn();
    if (typeof buildTurnNav === 'function') buildTurnNav(conv);
    if (typeof _cvRefreshContextGauge === 'function') _cvRefreshContextGauge();
    if (typeof reconcileTurnInlineEditors === 'function') {
      reconcileTurnInlineEditors();
    }
  },
});

const _conversationTransientTurnOverlay = createTransientTurnOverlay();

runtimeScope.ConversationSurfacePresentation = Object.freeze({
  activateConversation(conversationId) {
    _turnReviewPresentation.activate(conversationId || null);
  },
  isMounted(conversationId) {
    return _conversationSurfaceController.isMounted(conversationId);
  },
  ownsConversation(conversationId) {
    return _conversationSurfaceController.ownsConversation(conversationId);
  },
  revealTurn(conversationId, turnId, target) {
    return _conversationSurfaceController.revealTurn(conversationId, turnId, target);
  },
  isNearLatest(threshold) {
    return _conversationSurfaceController.isNearLatest(threshold);
  },
  followLatest(intent) {
    _conversationSurfaceController.followLatest(intent);
  },
  setTranslationActivity(conversationId, turnId, activity) {
    _conversationSurfaceController.setTranslationActivity(
      conversationId, turnId, activity || null,
    );
  },
  setExpandedBranchLane(conversationId, laneId) {
    _conversationSurfaceController.setExpandedBranchLane(conversationId, laneId || null);
  },
});

function _conversationReadState(conversationOrId) {
  const conversationId = typeof conversationOrId === 'string'
    ? conversationOrId : conversationOrId?.id;
  return conversationId
    ? runtimeScope.ConversationTurnStore?.readRuntimeState?.(conversationId) || null
    : null;
}

/* Ports of the one ask_human answer control (rich-response/guidance-answer.ts) for one question's Turn:
 * the classic card answers in the open conversation; a card outside it (the studio person panel) first
 * opens that conversation so the quoted chat reply leaves through the same composer and send pipeline. */
function _guidanceAnswerPorts(conversationId, turnId, replyInChat) {
  return createAuthenticatedChoiceInteractionPorts({
    conversationId, turnId,
    commandId: createTurnCommandId,
    text: (key, fallback) => { const value = typeof t === 'function' ? t(key) : ''; return value && value !== key ? value : fallback; },
    replyInChat,
    displayTranslation: (request) => _guidanceDisplayTranslation(request),
  });
}
runtimeScope.guidanceAnswerPorts = (conversationId, turnId) => _guidanceAnswerPorts(
  conversationId, turnId, async (request) => {
    if (getActiveConv()?.id !== request.conversationId) await loadConversation(request.conversationId);
    return replyToGuidanceInChat(request);
  });

/* A question card reads in the conversation's language: when the conversation translates, the question
 * and its options are shown translated (EN->CN, the same browser translation the retained question
 * overlay uses, cached in its presentation store); null leaves them as written. */
const _GUIDANCE_TRANSLATION_SEPARATOR = '\n‖‖‖\n';
async function _guidanceDisplayTranslation({ conversationId, guidanceId, question, options, descriptions }) {
  const conv = conversations.find(item => item?.id === conversationId);
  if (!conv || !convAutoTranslate(conv)) return null;
  const project = (state) => state?.translatedQuestion ? {
    question: state.translatedQuestion,
    options: options.map((option, index) => state.translatedOptions?.[index]?.label || option),
    descriptions: descriptions.map((text, index) => state.translatedOptions?.[index]?.description || text),
  } : null;
  const known = project(humanGuidancePresentation.read(conversationId, guidanceId));
  if (known) return known;
  const parts = [question, ...options.flatMap((option, index) => [option, descriptions[index] || ''])];
  const translated = String(await _callTranslateAPI(parts.join(_GUIDANCE_TRANSLATION_SEPARATOR), 'Chinese', 'English') || '')
    .split(/\n?‖‖‖\n?/);
  if (!translated[0]) return null;
  const translatedOptions = options.map((_option, index) => ({
    ...(translated[1 + index * 2] ? { label: translated[1 + index * 2] } : {}),
    ...(translated[2 + index * 2] && descriptions[index] ? { description: translated[2 + index * 2] } : {}),
  }));
  return project(humanGuidancePresentation.patch(conversationId, guidanceId, {
    translating: false, translatedQuestion: translated[0], translatedOptions,
  }));
}

runtimeScope.ConversationTurnRead = Object.freeze({
  state: _conversationReadState,
  activeAttemptIds(conversationOrId) {
    return activeConversationAttemptIds(_conversationReadState(conversationOrId));
  },
  activeMainAttemptId(conversationOrId) {
    return activeMainConversationAttemptId(_conversationReadState(conversationOrId));
  },
  ordered(conversationOrId, laneId) {
    return orderedConversationTurns(
      _conversationReadState(conversationOrId), laneId || 'main',
    );
  },
  latest(conversationOrId, predicate, laneId) {
    return latestConversationTurn(
      _conversationReadState(conversationOrId), predicate, laneId || 'main',
    );
  },
  hasActor(conversationOrId, actor, laneId) {
    return conversationHasActor(
      _conversationReadState(conversationOrId), actor, laneId || 'main',
    );
  },
});

const _backgroundActivityEvidenceChanged = createBackgroundActivityEvidenceObserver();
function _renderConversationSurfaceState(conv, state, repaint) {
  _pageCrashJournal.noteContext();

  // The reducer receipt (store-driven renders only) lets both observers skip
  // Turns a transition did not write.
  const update = repaint?.update;
  if (_backgroundActivityEvidenceChanged(state, update)) runtimeScope.refreshBackgroundActivity?.();
  if (conv && !conv._localOnly) {
    runtimeScope.CompactionHistoryState?.observe?.(
      conv.id, conversationCompactionArchiveIds(state, update),
    );
  }
  const transientState = _conversationTransientTurnOverlay.compose(state);
  const presentationState = runtimeScope.ConversationSwarmPresentation?.compose?.(
    conv, state, transientState,
  ) || transientState;
  return _conversationSurfaceController.render(
    conv,
    presentationState,
    repaint || {},
  );
}

runtimeScope.ConversationTransientTurns = Object.freeze({
  upsert(conv, turn) {
    if (!conv || !turn) return false;
    _conversationTransientTurnOverlay.upsert(turn);
    const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv.id);
    return store ? _renderConversationSurfaceState(conv, store.getState()) : false;
  },
  remove(conv, turnId) {
    if (!conv || !turnId) return false;
    const removed = _conversationTransientTurnOverlay.remove(conv.id, turnId);
    const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv.id);
    if (store) _renderConversationSurfaceState(conv, store.getState());
    return removed;
  },
  replace(conv, previousTurnId, turn) {
    if (!conv || !turn) return false;
    if (previousTurnId && previousTurnId !== turn.turnId) {
      _conversationTransientTurnOverlay.remove(conv.id, previousTurnId);
    }
    _conversationTransientTurnOverlay.upsert(turn);
    const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv.id);
    return store ? _renderConversationSurfaceState(conv, store.getState()) : false;
  },
  get(conversationId, turnId) {
    return _conversationTransientTurnOverlay.get(conversationId, turnId);
  },
  clear(conversationId) {
    _conversationTransientTurnOverlay.clear(conversationId);
  },
});

/* Sole repaint bridge for retained feature modules. A side effect may request
 * convergence, but only the authoritative keyed Surface may write chat DOM. */
runtimeScope.renderAuthoritativeConversationSurface = function (conversationId, repaint) {
  const service = runtimeScope.ConversationTurnStore;
  const conv = typeof conversations !== 'undefined'
    ? conversations.find(item => item?.id === conversationId) : null;
  if (!service || !conv) return false;
  const store = service.ensureRuntimeStore(conversationId);
  const state = store?.getState?.();
  if (!state || (!store._snapshotLoaded
      && entityRecordKeys(state.turnsById || {}).length === 0)) return false;
  return _renderConversationSurfaceState(conv, state, repaint);
};

/* Retained feature modules may request convergence, but never choose a DOM
 * renderer. If a snapshot is already present this commits synchronously;
 * otherwise one coalesced hydration feeds TurnStore and Surface. */
runtimeScope.requestAuthoritativeConversationRender = function (
  conversationId, repaint,
) {
  if (runtimeScope.renderAuthoritativeConversationSurface(
    conversationId, repaint || {},
  )) return true;
  const service = runtimeScope.ConversationTurnStore;
  const conv = service?.findConversation?.(conversationId)
    || (typeof conversations !== 'undefined'
      ? conversations.find(item => item?.id === conversationId) : null);
  if (!service?.ensureRuntimeStore || !conv) return false;
  const store = service.ensureRuntimeStore(conversationId);
  const state = store?.getState?.();
  const rendered = state
    ? _renderConversationSurfaceState(conv, state, repaint || {}) : false;
  if (store?._snapshotLoaded || conv._localOnly) return Boolean(rendered);
  if (!service.hydrateConversation) return Boolean(rendered);
  Promise.resolve(service.hydrateConversation(conv)).catch((error) => {
    console.warn(
      '[ConversationSurface] authoritative render hydration failed:',
      error?.message || error,
    );
  });
  return true;
};

(function installConversationTurnStore(global) {
  'use strict';
  runtimeScope.ConversationTurnStore = createConversationTurnRuntime({
    api: conversationSyncApi,
    streamClientId: requiredApiTransport.pageRequestId(),

    snapshotViewport() {
      const pane = document.getElementById('chatContainer');
      return {
        width: pane?.clientWidth || window.innerWidth,
        height: pane?.clientHeight || window.innerHeight,
      };
    },
    findConversation(conversationId) {
      if (typeof conversations === 'undefined' || !Array.isArray(conversations)) return null;
      return conversations.find(item => item?.id === conversationId) || null;
    },
    persist: _throttledTurnCachePut,
    onConversationInvalidated(conversationId) {
      _conversationSurfaceController.invalidateTaskTimePresentations(conversationId);
    },
    onStateApplied(conv, state, update) {
      _turnReviewPresentation.observe(conv.id, state.turnsById);
      /* The main lane is kept in ordinal order, so its latest reviewed reply is
       * the whole review input; reading it from the end leaves the rest of a
       * long transcript untouched on every streamed event. */
      const reviewed = latestConversationTurn(state,
        (turn) => isReviewedReply(conv.id, turn), 'main');
      conversationReview.observe(conv.id, reviewed ? [reviewed] : []);
      if (activeConvId === conv.id) runtimeScope.PaperPreviewPresentation.activateConversation(conv.id);
      runtimeScope.PaperPreviewPresentation.observe(conv.id, state, update);
    },
    onDebugFrame(conversationId, frame) {
      /* Durable evidence frames (turn_lifecycle _DEBUG_EVIDENCE_EVENTS).
       * Route to the debug presenter, which is demand-loaded: the port is
       * only installed once the debug panel / Request Inspector chunk has
       * loaded, so a frame that lands before then is a clean no-op (the
       * panel's cold path /debug-messages covers a later open). */
      try {
        runtimeScope.DebugPresentationState?.ingestEvidenceFrame?.(
          conversationId, frame,
        );
      } catch (error) {
        console.warn('[debug-panel] evidence frame failed:',
          frame && frame.type, error);
      }
    },
    isActive(conv) {
      return typeof activeConvId !== 'undefined' && activeConvId === conv.id;
    },
    isDomStale(conv) {
      return typeof activeConvId !== 'undefined' && activeConvId === conv.id
        && !_conversationSurfaceController.ownsConversation(conv.id);
    },
    applySettings(conv, settings, projection) {
      const previousPaper = JSON.stringify(conv.paperDocument || null);
      if (typeof _applySettingsToConv === 'function') _applySettingsToConv(conv, settings, projection);
      if (activeConvId === conv.id && previousPaper !== JSON.stringify(conv.paperDocument || null)) {
        runtimeScope._paperPreviewDiscover(conv.id);
      }
    },
    applySnapshotMetadata(conv, snapshot) {
      void runtimeScope.Artifacts?.hydrateConversation?.(
        conv,
        typeof snapshot?.hasArtifacts === 'boolean'
          ? snapshot.hasArtifacts : undefined,
      );
    },
    renderState(conv, state, repaint) {
      return _renderConversationSurfaceState(conv, state, repaint);
    },
    getTurnMountToken: (() => {
      const tracker = createTurnMountTracker({
        root: typeof document === 'undefined' ? undefined : document,
        observer: typeof MutationObserver === 'function' ? MutationObserver : undefined,
        findTurn(conversationId, turnId) {
          return activeConvId === conversationId
            ? document.getElementById('chatInner')?.querySelector(
              `[data-turn-id="${CSS.escape(turnId)}"]`,
            ) || null : null;
        },
      });
      if (typeof retainedCompositionLifecycle !== 'undefined') {
        retainedCompositionLifecycle.add(() => tracker.destroy());
      }
      return tracker.getToken;
    })(),
    isTurnMounted(conversationId, turnId) {
      const node = activeConvId === conversationId && document.getElementById('chatInner')?.querySelector(
        `[data-turn-id="${CSS.escape(turnId)}"]`,
      );
      // The office keeps the chat column mounted but hidden: a hidden turn has no paint opportunity.
      return Boolean(node) && node.checkVisibility?.() !== false;
    },
    disposeRenderedState(conversationId) {
      _turnReviewPresentation.disposeConversation(conversationId);
      const modelNotice = document.getElementById('composerModelNotice');
      if (modelNotice?.dataset.conversationId === conversationId) {
        renderComposerModelNotice(modelNotice, null);
      }
      const state = runtimeScope.ConversationTurnRead?.state?.(conversationId);
      if (typeof clearFinishInfoPresentation === 'function') {
        clearFinishInfoPresentation(entityRecordKeys(state?.turnsById || {}));
      }
      humanGuidancePresentation.clearConversation(conversationId);
      _conversationSurfaceController.disposeConversation(conversationId);
      _conversationTransientTurnOverlay.clear(conversationId);
    },
    deferTerminalRelease: typeof global.requestAnimationFrame === 'function'
      ? (release) => global.requestAnimationFrame(() =>
          global.requestAnimationFrame(() => release()))
      : undefined,
    buildNavigation(conv) {
      _updateScrollToBottomBtn();
      if (typeof buildTurnNav === 'function') buildTurnNav(conv);
    },
    renderConversationList: typeof requestConversationListRender === 'function'
      ? requestConversationListRender : undefined,
    updateSendButton: typeof updateSendButton === 'function'
      ? updateSendButton : undefined,
    onTurnSettled(conv, turn) {
      runtimeScope._maybeAutoGenerateTitleForSettledTurn?.(conv, turn);
    },
    onProtocolError(error) {
      console.warn('[ConversationSync] stream protocol error:', error && error.message);
    },
    onResyncError(error, turnId) {
      console.warn('[ConversationSync] snapshot recovery failed for turn',
                   turnId, error?.message || error);
    },
    onHealth(conversationId) {
      if (typeof renderConversationList === 'function') renderConversationList();
      if (conversationId === activeConvId) {
        runtimeScope.requestAuthoritativeConversationRender?.(
          conversationId, { force: false, forceScroll: false },
        );
      }
    },
  });
  runtimeScope.createTurnState = runtimeScope.ConversationTurnStore.emptyState;
  runtimeScope.reduceTurnState = runtimeScope.ConversationTurnStore.reducer;
})(window);
