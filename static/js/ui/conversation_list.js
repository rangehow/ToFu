/* ═══════════════════════════════════════════════════════════════════
   conversation list — extracted from ui.js (split 2026-05-28)

   Conversation list rendering — sidebar conv list, folder tabs, search.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

function stripNoTranslateTags(text) {
  if (!text) return text;
  return text
    .replace(/<\/?notranslate>/gi, '')
    .replace(/<\/?nt>/gi, '')
    .replace(/[⟦\[\(\{【〔《「『]\s*N\s*T\s*_\s*[0-9０-９]+\s*[⟧\]\)\}】〕》」』]/gi, '');
}

/**
 * Targeted single-message PATCH. Sends only the whitelisted keys in `patch`
 * to the server so chatInner actions (edit-only, translate toggle,
 * translation completion) don't fall back to the full-conversation PUT.
 *
 * @param {string} convId       Conversation id.
 * @param {number} msgIdx       Message index within conv.messages.
 * @param {object} patch        Partial message — only whitelisted keys are
 *                              accepted by the server. A literal `null`
 *                              value removes that key from the message.
 * @param {object} [opts]
 * @param {function} [opts.onError]  Callback on server error, receives the
 *                                   parsed error body.
 * @returns {Promise<object|null>}  Server response `{ok, msgCount, msg}` or
 *                                  null on failure.
 */
async function _patchMessageOnServer(convId, msgIdx, patch, opts = {}) {
  if (!convId || msgIdx == null || !patch || Object.keys(patch).length === 0) return null;
  // Prefer stable id addressing when the message has _msgId. The conv is
  // expected on the global `conversations` array; if a caller has only the
  // index, the legacy index endpoint is still served. opts.msgId overrides
  // the lookup (useful when the caller has the id but no live `messages`).
  let msgId = opts.msgId || null;
  if (!msgId && typeof conversations !== 'undefined') {
    try {
      const c = conversations.find(x => x.id === convId);
      const m = c && c.messages && c.messages[msgIdx];
      if (m && m._msgId) msgId = m._msgId;
    } catch (_e) { /* ignore */ }
  }
  const result = await Api.conversations.patchMessage(convId, msgIdx, patch, { msgId });
  if (result && result._error) {
    console.warn('[patchMsg] conv=%s idx=%d msgId=%s status=%d body=%o',
      convId, msgIdx, (msgId || '').slice(0, 8) || '-', result._status, result._body);
    if (typeof opts.onError === 'function') opts.onError(result._body, result._status);
    return null;
  }
  return result;
}

function formatConvTime(ts) {
  if (!ts) return "";
  const d = new Date(ts),
    now = new Date(),
    pad = (n) => String(n).padStart(2, "0");
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const isToday = d.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  let datePart;
  if (isToday) datePart = "Today";
  else if (isYesterday) datePart = "Yesterday";
  else {
    const sameYear = d.getFullYear() === now.getFullYear();
    const months = [
      "Jan",
      "Feb",
      "Mar",
      "Apr",
      "May",
      "Jun",
      "Jul",
      "Aug",
      "Sep",
      "Oct",
      "Nov",
      "Dec",
    ];
    datePart = `${months[d.getMonth()]} ${d.getDate()}${sameYear ? "" : ", " + d.getFullYear()}`;
  }
  return `<span class="conv-date-text">${datePart}</span><span class="conv-date-sep">·</span><span class="conv-date-time">${time}</span>`;
}

let _lastConvListHash = "";
let _lastRenderedSearchQuery = "";   // guard: skip background re-renders in search mode

/* ★ PERF: Fast-path for conversation switch — instead of rebuilding the
 * entire sidebar from scratch (O(N) HTML generation + innerHTML assignment),
 * just move the .active class between two DOM elements (O(1)).
 * Returns true if the fast-path was sufficient, false if a full rebuild is needed. */
let _lastActiveConvId = null;
function _swapActiveConvItem(newActiveId) {
  if (sidebarSearchQuery) return false; // search mode — need full rebuild
  const oldId = _lastActiveConvId;
  if (oldId === newActiveId) return true; // no change
  _lastActiveConvId = newActiveId;
  /* Swap .active class in DOM */
  if (oldId) {
    const oldEl = document.querySelector(`.conv-item[data-conv-id="${CSS.escape(oldId)}"]`);
    if (oldEl) oldEl.classList.remove('active');
  }
  if (newActiveId) {
    const newEl = document.querySelector(`.conv-item[data-conv-id="${CSS.escape(newActiveId)}"]`);
    if (newEl) newEl.classList.add('active');
    else return false; // new conv not in DOM yet — need full rebuild
  }
  /* Invalidate the hash so a subsequent full renderConversationList()
   * won't skip due to stale hash (the hash includes active state). */
  _lastConvListHash = "";
  return true;
}

/* ── Folder tab bar ── */
let _lastFolderTabsHash = '';
let _lastFolderTabsContentHash = '';
let _lastFolderTabsStructHash = '';
let _folderTabsExpanded = false;



function renderFolderTabs(folders, activeFolderId, allConvs) {
  const tabsEl = document.getElementById('folderTabs');
  if (!tabsEl) return;
  try {
    _renderFolderTabsInner(tabsEl, folders, activeFolderId, allConvs);
  } catch (e) {
    console.error('[renderFolderTabs] Error:', e);
    // On error, ensure tabs aren't left in broken state — render minimal fallback
    try { tabsEl.innerHTML = '<div class="folder-tabs-scroll"><button class="folder-tab folder-tab-add" title="New folder"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button></div>'; } catch(_) {}
  }
}

function _renderFolderTabsInner(tabsEl, folders, activeFolderId, allConvs) {
  // Always show tabs — even with 0 folders, show just the "+" button for discoverability
  tabsEl.style.display = '';

  const safeFolders = folders || [];
  const safeConvs = allConvs || [];

  // Compute counts per folder + uncategorized
  const folderIds = new Set(safeFolders.map(f => f.id));
  const countMap = {};
  let uncategorizedCount = 0;
  for (const c of safeConvs) {
    if (c.folderId && folderIds.has(c.folderId)) {
      countMap[c.folderId] = (countMap[c.folderId] || 0) + 1;
    } else {
      uncategorizedCount++;
    }
  }

  // Compute latest activity time per folder for sorting
  const lastActiveMap = {};
  for (const c of safeConvs) {
    if (c.folderId && folderIds.has(c.folderId)) {
      const ts = c.updatedAt || c.createdAt || 0;
      if (!lastActiveMap[c.folderId] || ts > lastActiveMap[c.folderId]) {
        lastActiveMap[c.folderId] = ts;
      }
    }
  }

  // Detect which folders have actively streaming/generating conversations
  const streamingFolderIds = new Set();
  for (const c of safeConvs) {
    if (!c.folderId || !folderIds.has(c.folderId)) continue;
    let isStreaming = (typeof activeStreams !== 'undefined' && activeStreams.has(c.id)) || c.activeTaskId || c._translating || c._memoryPrefetching;
    if (!isStreaming && typeof activeStreams !== 'undefined') {
      const prefix = c.id + ':';
      for (const k of activeStreams.keys()) { if (k.startsWith(prefix)) { isStreaming = true; break; } }
    }
    if (isStreaming) streamingFolderIds.add(c.folderId);
  }

  // Split hash: content hash (folders/counts/names) vs active-tab hash
  // When only the active tab changes, skip full DOM rebuild and just swap .active class
  const streamKey = [...streamingFolderIds].sort().join(',');
  const structHash = `U${uncategorizedCount}|${safeFolders.map(f=>`${f.id}|${f.name}|${f.color||''}|${lastActiveMap[f.id]||0}|${countMap[f.id]||0}`).join(',')}`;
  const contentHash = `${structHash}|S${streamKey}`;
  const fullHash = `${activeFolderId||''}|${contentHash}`;
  if (fullHash === _lastFolderTabsHash) return;

  const contentChanged = contentHash !== _lastFolderTabsContentHash;
  const structChanged = structHash !== _lastFolderTabsStructHash;
  _lastFolderTabsHash = fullHash;
  _lastFolderTabsContentHash = contentHash;
  _lastFolderTabsStructHash = structHash;

  // Fast path: only active tab and/or streaming state changed — update classes in-place, no DOM rebuild
  if (!structChanged) {
    const btns = tabsEl.querySelectorAll('.folder-tab[data-folder-id]');
    btns.forEach(btn => {
      const fid = btn.dataset.folderId;
      btn.classList.toggle('active', fid === (activeFolderId || ''));
      const dot = btn.querySelector('.folder-tab-dot');
      if (dot) dot.classList.toggle('streaming', streamingFolderIds.has(fid));
    });
    return;
  }

  const sortedFolders = [...safeFolders].sort((a, b) => (lastActiveMap[b.id] || 0) - (lastActiveMap[a.id] || 0) || (a.order || 0) - (b.order || 0));

  let html = '';
  html += '<div class="folder-tabs-scroll';
  // Preserve expanded state synchronously to avoid collapse→expand flash
  if (_folderTabsExpanded) html += ' expanded';
  html += '">';
  // "未分类" tab — shows conversations not in any folder (only when folders exist)
  if (sortedFolders.length > 0) {
    const ucBadge = uncategorizedCount > 0 ? `<span class="folder-tab-count">${uncategorizedCount}</span>` : '';
    html += `<button class="folder-tab${!activeFolderId ? ' active' : ''}" data-folder-id="">`;
    html += `<svg class="folder-tab-inbox-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`;
    html += `<span class="folder-tab-name">${t('sidebar.uncategorized')}</span>${ucBadge}</button>`;
  }
  // Folder tabs
  for (const f of sortedFolders) {
    const fcolor = f.color ? escapeHtml(f.color) : 'var(--accent)';
    const fname = escapeHtml(f.name);
    const isActive = activeFolderId === f.id;
    const cnt = countMap[f.id] || 0;
    const badge = cnt > 0 ? `<span class="folder-tab-count">${cnt}</span>` : '';
    html += `<button class="folder-tab${isActive ? ' active' : ''}" data-folder-id="${escapeHtml(f.id)}" title="${fname}">`;
    const dotStreaming = streamingFolderIds.has(f.id) ? ' streaming' : '';
    html += `<span class="folder-tab-dot${dotStreaming}" style="background:${fcolor}"></span>`;
    html += `<span class="folder-tab-name">${fname}</span>${badge}`;
    html += `</button>`;
  }
  // "+" add tab — always visible
  html += `<button class="folder-tab folder-tab-add" title="${t('sidebar.newFolder')}">`;
  html += `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;
  html += `</button>`;
  html += '</div>';
  // Expand/collapse toggle (hidden by default, shown via CSS when overflow detected)
  html += `<button class="folder-tabs-toggle">`;
  html += `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;
  html += `<span class="folder-tabs-toggle-label"></span>`;
  html += `</button>`;
  tabsEl.innerHTML = html;

  // Check overflow after render — if content exceeds visible height, show toggle & count hidden
  requestAnimationFrame(() => {
    const scrollEl = tabsEl.querySelector('.folder-tabs-scroll');
    if (!scrollEl) return;
    const isOverflow = scrollEl.scrollHeight > scrollEl.clientHeight + 2;
    tabsEl.classList.toggle('has-overflow', isOverflow);
    // Count how many real folder tabs are hidden (below the fold)
    if (isOverflow) {
      const label = tabsEl.querySelector('.folder-tabs-toggle-label');
      if (label) {
        if (_folderTabsExpanded) {
          label.textContent = t('sidebar.lessFolders');
        } else {
          const collapsedMax = 94; // matches CSS max-height (3 rows)
          // Only count real folder tabs, exclude the "+" add button
          const tabs = scrollEl.querySelectorAll('.folder-tab:not(.folder-tab-add)');
          let hiddenCount = 0;
          tabs.forEach(tab => {
            if (tab.offsetTop + tab.offsetHeight > collapsedMax) hiddenCount++;
          });
          label.textContent = hiddenCount > 0 ? `+${hiddenCount}` : '';
        }
      }
    }
  });
}

function renderConversationList() {
  const listEl = document.getElementById("convList"),
    statsEl = document.getElementById("sidebarSearchStats");
  if (!sidebarSearchQuery) {
    _lastRenderedSearchQuery = "";   // reset when exiting search mode
    statsEl.classList.remove("visible");
    const all = conversations.filter((c) => c.messages.length > 0 || (c._serverMsgCount || 0) > 0 || c._needsLoad);

    const folders = typeof getFolders === 'function' ? getFolders() : [];
    const _activeFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
    const foldersReady = typeof areFoldersLoaded === 'function' ? areFoldersLoaded() : true;

    /* ── Lightweight hash ── */
    const _quickHash = (arr) => arr.map(c =>
      `${c.id}|${c.title}|${c.updatedAt||""}|${c.id===activeConvId?1:0}|${activeStreams?.has(c.id)?1:0}|${c.activeTaskId||""}|${c._translating?1:0}|${c._memoryPrefetching?1:0}|${c.folderId||""}`
    ).join("\n");
    const folderHash = folders.map(f => `${f.id}|${f.name}|${f.order}|${f.color||''}`).join(",");
    /* ── Render folder tabs (always, regardless of hash — tab visibility may change) ── */
    renderFolderTabs(folders, _activeFolderId, all);

    const hash = `AF${_activeFolderId||''}|FL${foldersReady?1:0}|${_quickHash(all)}|||F${folderHash}`;
    if (hash === _lastConvListHash) return;
    _lastConvListHash = hash;

    /* ── Filter by active folder tab ── */
    let filtered = all;
    if (_activeFolderId) {
      // Specific folder selected — show only its conversations
      const activeFolder = folders.find(f => f.id === _activeFolderId);
      if (!activeFolder) { // folder was deleted while viewing it
        if (typeof setActiveFolderId === 'function') setActiveFolderId(null);
        return;
      }
      filtered = all.filter(c => c.folderId === _activeFolderId);
    } else if (folders.length > 0) {
      // Default "未分类" view — show only conversations NOT in any folder
      const folderIds = new Set(folders.map(f => f.id));
      filtered = all.filter(c => !c.folderId || !folderIds.has(c.folderId));
    } else if (!foldersReady) {
      // Folders not yet loaded — filter out conversations that have a folderId
      // from server settings to avoid flashing them in uncategorized view
      filtered = all.filter(c => !c.folderId);
    }
    // else: folders loaded and empty — show everything (no folders exist)

    let listHtml = "";
    filtered.forEach((c) => {
      listHtml += _buildConvItemHTML(c, escapeHtml(stripNoTranslateTags(c.title)), "");
    });

    /* ── Empty state ── */
    if (filtered.length === 0 && (_activeFolderId || folders.length > 0)) {
      const isUncategorized = !_activeFolderId;
      const emptyIcon = isUncategorized
        ? `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3;margin-bottom:8px"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`
        : `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3;margin-bottom:8px"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
      const emptyText = isUncategorized ? t('sidebar.allCategorized') : t('sidebar.folderEmpty');
      const emptyHint = isUncategorized
        ? t('sidebar.newChatAppear')
        : t('sidebar.clickNewChat');
      listHtml = `<div class="folder-view-empty">${emptyIcon}` +
        `<div style="font-size:12px;color:var(--text-tertiary)">${emptyText}</div>` +
        `<div style="font-size:11px;color:var(--text-tertiary);opacity:0.6;margin-top:4px">${emptyHint}</div>` +
        `</div>`;
    }

    listEl.innerHTML = listHtml;
    /* ★ Keep _lastActiveConvId in sync after a full rebuild so
     * _swapActiveConvItem can do O(1) swaps on subsequent switches. */
    _lastActiveConvId = activeConvId;
  } else {
    const query = sidebarSearchQuery;

    /* ── Guard: skip background re-renders while search results are shown ──
     * Background triggers (60s server poll, streaming saves, cross-tab sync,
     * visibilitychange) call renderConversationList() even during an active
     * search.  Without this guard, every background call would:
     *   1. flash the DOM with title-only partial results
     *   2. fire a NEW /api/conversations/search HTTP request
     *   3. re-render merged results when the response arrives
     * causing the sidebar to visibly "auto-refresh" in a loop.
     * Fix: once search results for a query are rendered, skip re-rendering
     * until the user actually changes the query (which resets this via the
     * input handler calling renderConversationList with a new sidebarSearchQuery). */
    if (query === _lastRenderedSearchQuery) return;
    _lastRenderedSearchQuery = query;

    // Phase 1: instant title matches (local, ~0 ms)
    const titleHits = searchByTitle(query);
    _renderSearchResults(titleHits, query, listEl, statsEl, true);

    // Phase 2: async content/thinking search (server)
    const seq = ++_searchSeq;
    searchByContent(query, seq).then(contentHits => {
      if (contentHits === null) return;           // stale or aborted
      if (sidebarSearchQuery !== query) return;   // user typed more

      // merge: title hits + content hits (deduplicate by conv id)
      const seen = new Set(titleHits.map(h => h.conv.id));
      const merged = [...titleHits];
      for (const h of contentHits) {
        if (!seen.has(h.conv.id)) { merged.push(h); seen.add(h.conv.id); }
      }
      _renderSearchResults(merged, query, listEl, statsEl, false);
    });
  }
}

function _renderSearchResults(results, query, listEl, statsEl, isPartial) {
  statsEl.classList.add("visible");
  const suffix = isPartial ? ' <span class="search-loading">searching…</span>' : "";
  statsEl.innerHTML = `${results.length} result${results.length !== 1 ? "s" : ""}${suffix}`;
  if (results.length === 0 && isPartial) {
    listEl.innerHTML = `<div class="sidebar-search-empty"><div class="sidebar-search-empty-icon"></div>Searching…</div>`;
    _lastConvListHash = "";
    return;
  }
  if (results.length === 0) {
    listEl.innerHTML = `<div class="sidebar-search-empty"><div class="sidebar-search-empty-icon"></div>No matches for "<strong>${escapeHtml(query)}</strong>"</div>`;
    _lastConvListHash = "";
    return;
  }
  const items = results.map(
    ({ conv: c, matchField, matchSnippet, matchRole }) => {
      const tHtml =
        matchField === "title"
          ? highlightMatch(c.title, query)
          : escapeHtml(c.title);
      let snip = "";
      if (matchSnippet) {
        const ico = "";
        /* ID match: no role prefix ("You:"/"Claude:") — snippet is the ID itself. */
        if (matchField === "id") {
          snip = `<div class="conv-item-snippet">${highlightMatch(matchSnippet, query)}</div>`;
        } else {
          const rl = matchRole === "user" ? "You" : "Claude";
          snip = `<div class="conv-item-snippet">${ico} ${rl}: ${highlightMatch(matchSnippet, query)}</div>`;
        }
      }
      return _buildConvItemHTML(c, tHtml, snip);
    },
  );
  const newHtml = items.join("");
  if (newHtml === _lastConvListHash) return;
  _lastConvListHash = newHtml;
  listEl.innerHTML = newHtml;
}

function _buildConvItemHTML(c, titleHtml, snippetHtml) {
  // ★ Separate translating state from streaming for distinct sidebar indicators
  const translating = !!c._translating;
  // ★ Memory-prefetch (cheap-LLM filter) running — distinct amber indicator
  //   so the user knows why the main model hasn't started producing tokens yet.
  const memoryPrefetching = !!c._memoryPrefetching;
  let streaming = activeStreams.has(c.id) || c.activeTaskId;
  if (!streaming) {
    const prefix = c.id + ":";
    for (const k of activeStreams.keys()) { if (k.startsWith(prefix)) { streaming = true; break; } }
  }
  // ★ Detect if conversation is awaiting human input (any round with status=awaiting_human)
  let awaitingHuman = false;
  if (c.messages) {
    for (let i = c.messages.length - 1; i >= 0; i--) {
      const m = c.messages[i];
      if (m.role === 'assistant' && m.toolRounds) {
        for (const r of m.toolRounds) {
          if (r.status === 'awaiting_human') { awaitingHuman = true; break; }
        }
        if (awaitingHuman) break;
      }
    }
  }
  const eid = escapeHtml(c.id);
  const isActive = c.id === activeConvId ? " active" : "";
  const delSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;
  const feishuBadge = c.source === 'feishu' ? `<span class="conv-feishu-badge" title="${t('sidebar.feishuConv')}">Feishu</span>` : '';
  const cpSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
  // ★ Sidebar dot priority: awaiting-human > translating > memory-prefetch > streaming.
  //   Memory-prefetch sits between translating and streaming because it runs
  //   BEFORE the main model starts streaming — distinguishing it from the
  //   answering phase reassures the user that latency is the cheap filter,
  //   not a stuck request.
  let dotHtml = '';
  if (awaitingHuman) {
    dotHtml = `<div class="conv-awaiting-human-dot" title="${t('sidebar.awaitingInput')}"></div>`;
  } else if (translating) {
    dotHtml = `<div class="conv-translating-dot" title="${t('sidebar.translating')}"></div>`;
  } else if (memoryPrefetching) {
    dotHtml = `<div class="conv-memprefetch-dot" title="${t('sidebar.memoryPrefetch')}"></div>`;
  } else if (streaming) {
    dotHtml = '<div class="conv-streaming-dot"></div>';
  }
  let statusTag = '';
  if (translating) {
    statusTag = `<span class="conv-status-tag conv-status-translating">${t('sidebar.translatingTag')}</span>`;
  } else if (memoryPrefetching) {
    statusTag = `<span class="conv-status-tag conv-status-memprefetch">${t('sidebar.memoryPrefetchTag')}</span>`;
  } else if (streaming) {
    statusTag = `<span class="conv-status-tag conv-status-streaming">${t('sidebar.answering')}</span>`;
  }
  const dupSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="14" height="14" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
  const folderSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
  const _isDebug = typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode;
  const copyIdBtn = _isDebug ? `<button class="conv-action-btn conv-copy-id" data-conv-id="${eid}" title="${t('sidebar.copyConvId')}">${cpSvg}</button>` : '';
  const folderClass = c.folderId ? ' in-folder' : '';
  return `<div class="conv-item${isActive}${folderClass}" data-conv-id="${eid}" draggable="true" title="ID: ${eid}">${dotHtml}<div class="conv-text"><div class="conv-title">${feishuBadge}${titleHtml}</div>${snippetHtml || ""}<div class="conv-date">${formatConvTime(c.updatedAt || c.createdAt)}${statusTag}</div></div><div class="conv-actions">${copyIdBtn}<button class="conv-action-btn conv-ref" data-conv-id="${eid}" data-conv-title="${escapeHtml(c.title || 'Untitled')}" title="${t('sidebar.refConv')}">@</button><button class="conv-action-btn conv-folder-assign" data-conv-id="${eid}" title="${t('sidebar.moveToFolder')}">${folderSvg}</button><button class="conv-action-btn conv-dup" data-conv-id="${eid}" title="${t('sidebar.duplicate')}">${dupSvg}</button><button class="conv-action-btn conv-delete" data-conv-id="${eid}" title="${t('sidebar.deleteConv')}">${delSvg}</button></div></div>`;
}

function highlightMatch(text, query) {
  if (!query) return escapeHtml(text);
  const e = escapeHtml(text);
  const q = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return e.replace(
    new RegExp(`(${q})`, "gi"),
    '<span class="sidebar-search-highlight">$1</span>',
  );
}

function _extractText(val) {
  if (typeof val === "string") return val;
  if (Array.isArray(val)) return val.map((v) => (typeof v === "string" ? v : v?.text || "")).join(" ");
  return "";
}

/* ── Two-tier search: instant title match (local) + async content search (server) ── */
let _searchAbort = null;   // AbortController for in-flight search API
let _searchSeq = 0;        // monotonic counter to discard stale results

function searchByTitle(query) {
  if (!query) return [];
  const results = [];
  const seen = new Set();
  /* ── Conv ID match (exact or prefix) ──
   * Conv IDs are lowercase alphanumeric (14 chars) like "mosnzwji2h8kwo".
   * If the user pastes an ID (full or partial ≥4 chars, no spaces), surface
   * that conversation first — lets you jump to a known ID without scrolling.
   * Gate by /^[a-z0-9]+$/ + length ≥ 4 so ordinary search words don't
   * accidentally trigger an ID scan that overlaps with title text. */
  if (/^[a-z0-9]{4,}$/.test(query)) {
    for (const c of conversations) {
      if (c.id && c.id.toLowerCase().includes(query)) {
        results.push({
          conv: c,
          matchField: "id",
          matchSnippet: `ID: ${c.id}`,
          matchRole: null,
        });
        seen.add(c.id);
      }
    }
  }
  for (const c of conversations) {
    if (seen.has(c.id)) continue;
    if ((c.title || "").toLowerCase().includes(query)) {
      results.push({ conv: c, matchField: "title", matchSnippet: null });
      seen.add(c.id);
    }
  }
  return results;
}

async function searchByContent(query, seq) {
  if (_searchAbort) { _searchAbort.abort(); _searchAbort = null; }
  const ac = new AbortController();
  _searchAbort = ac;
  try {
    const hits = await Api.conversations.search(query, { signal: ac.signal });
    if (!Array.isArray(hits)) return [];
    if (seq !== _searchSeq) return null;       // stale — discard
    const convMap = new Map(conversations.map(c => [c.id, c]));
    return hits
      .map(h => {
        const c = convMap.get(h.id);
        if (!c) return null;
        return { conv: c, matchField: h.matchField, matchSnippet: h.matchSnippet, matchRole: h.matchRole };
      })
      .filter(Boolean);
  } catch (e) {
    if (e.name === 'AbortError') return null;  // cancelled — don't render
    console.warn('[search] server error, falling back to local', e);
    return _localContentSearch(query);          // fallback
  } finally {
    if (_searchAbort === ac) _searchAbort = null;
  }
}

/** Local fallback content search (used only if server unreachable) */
function _localContentSearch(query) {
  const results = [];
  for (const c of conversations) {
    if ((c.title || "").toLowerCase().includes(query)) continue; // already in title results
    let found = false;
    for (let i = c.messages.length - 1; i >= 0; i--) {
      const msg = c.messages[i];
      const rawContent = _extractText(msg.content);
      const content = rawContent.toLowerCase();
      if (content.includes(query)) {
        const idx = content.indexOf(query);
        const s = Math.max(0, idx - 30);
        const e = Math.min(content.length, idx + query.length + 50);
        const snip = (s > 0 ? "…" : "") + rawContent.slice(s, e) + (e < content.length ? "…" : "");
        results.push({ conv: c, matchField: "content", matchSnippet: snip, matchRole: msg.role });
        found = true;
        break;
      }
    }
    if (!found) {
      for (let i = c.messages.length - 1; i >= 0; i--) {
        const msg = c.messages[i];
        const rawTh = _extractText(msg.thinking);
        const th = rawTh.toLowerCase();
        if (th.includes(query)) {
          const idx = th.indexOf(query);
          const s = Math.max(0, idx - 30);
          const e = Math.min(th.length, idx + query.length + 50);
          const snip = (s > 0 ? "…" : "") + rawTh.slice(s, e) + (e < th.length ? "…" : "");
          results.push({ conv: c, matchField: "thinking", matchSnippet: snip, matchRole: "assistant" });
          found = true;
          break;
        }
      }
    }
  }
  return results;
}

// ── Shared helpers: streaming bubble & surgical DOM truncation ──

/**
 * Re-resolve an assistant message reference by its stable `_msgId`.
 *
 * The streaming pipeline closes over `assistantMsg` for the lifetime of
 * one SSE connection. Phase-2 reconciliation (`loadConversationMessages`)
 * or any push that reorders `conv.messages` can leave that closure
 * pointing at a detached object — content keeps accumulating into a ref
 * that the renderer never sees, the symptom being "Autopilot content
 * only appears after stop + force-refresh".
 *
 * Use this on every SSE event (or any post-await checkpoint) to rebind
 * the local `assistantMsg` to whatever object currently lives in
 * `conv.messages` for that `_msgId`. Falls back to the closure-bound
 * argument when no id is set yet (very early init or legacy messages).
 *
 * @param {Object} conv  — the conversation object
 * @param {string} msgId — the `_msgId` minted by `_ensureMsgId`
 * @param {Object} fallback — original closure-bound assistantMsg
 * @returns {Object} the live message object (or fallback if not found)
 */
function _resolveAssistantById(conv, msgId, fallback) {
  if (!conv || !msgId) return fallback;
  const msgs = conv.messages;
  if (!Array.isArray(msgs)) return fallback;
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m && m._msgId === msgId) return m;
  }
  return fallback;
}

/**
 * Find the message in conv.messages that carries an `_autopilotPending`
 * payload, regardless of whether it's at the tail. The done-event
 * handler stamps the payload on whatever assistantMsg ref it had at the
 * time; later pushes / reconciliations can move it off the tail before
 * `finishStream` runs.
 *
 * @param {Object} conv
 * @returns {{msg: Object, idx: number}|null}
 */
function _findAutopilotPendingCarrier(conv) {
  if (!conv || !Array.isArray(conv.messages)) return null;
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m && m._autopilotPending) return { msg: m, idx: i };
  }
  return null;
}

