/* ════════════════════════════════════
   upload_preview.js — attachment + tool-content preview modals
   Extracted from upload.js (2026-07). The preview/modal DOM layer:
   previewPendingImage/PdfText, previewMsgPdfText, openImagePreview,
   openTextPreview, closePreview, previewToolContent + the tool-content /
   truncation-bar click delegation. Plain window-scope concatenation (NOT an
   IIFE) — called at runtime from onclick handlers, main.js and chat_render.js;
   load order is free (before main.js). Uses global escapeHtml / getActiveConv /
   getToolRoundsFromMsg / _getToolDisplay / _renderToolGroupsHTML / Icon.
   ════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  ★ Preview functions
// ══════════════════════════════════════════════════════
function previewPendingImage(i) {
  const img = pendingImages[i];
  if (!img || !img.preview) return;
  openImagePreview(img.preview);
}
function previewPendingPdfText(i) {
  const pdf = pendingPdfTexts[i];
  if (!pdf) return;
  const sizeStr =
    pdf.textLength >= 1024
      ? `${(pdf.textLength / 1024).toFixed(1)}KB`
      : `${pdf.textLength} chars`;
  openTextPreview(
    `📄 ${pdf.name}`,
    `${pdf.pages} pages · ${sizeStr}`,
    pdf.text || "",
  );
}
function previewMsgPdfText(msgIdx, pdfIdx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (!msg || !msg.pdfTexts || !msg.pdfTexts[pdfIdx]) return;
  const pdf = msg.pdfTexts[pdfIdx];
  const text =
    pdf.text || "(Text not available — content was truncated for storage)";
  const sizeStr =
    (pdf.textLength || 0) >= 1024
      ? `${((pdf.textLength || 0) / 1024).toFixed(1)}KB`
      : `${pdf.textLength || 0} chars`;
  openTextPreview(
    `📄 ${pdf.name}`,
    `${pdf.pages || "?"} pages · ${sizeStr}`,
    text,
  );
}
function openImagePreview(src) {
  if (!src) return;
  document.getElementById("previewBody").innerHTML =
    `<button class="preview-close-btn" onclick="closePreview()" aria-label="Close">✕</button><img src="${src}" alt="Preview" class="preview-image">`;
  document.getElementById("previewModal").classList.add("open");
}
function openTextPreview(title, meta, text) {
  document.getElementById("previewBody").innerHTML =
    `<button class="preview-close-btn" onclick="closePreview()" aria-label="Close">✕</button><div class="preview-text-panel"><div class="preview-text-header"><span class="preview-text-title">${escapeHtml(title)}</span><span class="preview-text-meta">${escapeHtml(meta)}</span></div><pre class="preview-text-body">${escapeHtml(text)}</pre></div>`;
  document.getElementById("previewModal").classList.add("open");
}
function closePreview() {
  document.getElementById("previewModal").classList.remove("open");
  setTimeout(() => {
    document.getElementById("previewBody").innerHTML = "";
  }, 300);
}

// ── Tool Content Preview (search / fetch / project tools) ──
function previewToolContent(roundNum, toolCallId) {
  const conv = getActiveConv();
  if (!conv) return;
  // Search all assistant messages (not just last) to find the round
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const msg = conv.messages[i];
    if (msg.role !== 'assistant' && msg.role !== 'optimizer') continue;
    const rounds = msg.toolRounds || [];
    const round = rounds.find(r => r.roundNum === roundNum && (toolCallId ? r.toolCallId === toolCallId : true));
    if (round && round.toolContent) {
      const td = typeof _getToolDisplay === 'function' ? _getToolDisplay(round) : { icon: '📄', label: 'Tool' };
      // ★ openTextPreview escapes the title as TEXT, so the icon must be a
      //   plain glyph — never a raw <svg> string. _getToolDisplay returns an
      //   SVG markup string for most tools (MCP, project, timer, …); only a
      //   few use emoji. Including the SVG here leaked literal "<svg …>" into
      //   the preview header. Drop the icon from the escaped title entirely.
      //   For MCP tools round.query already reads "server/tool — resource",
      //   so use it alone instead of the redundant title-cased label.
      const q = (round.query || '').slice(0, 120);
      const isMcp = (round.toolName || '').startsWith('mcp__');
      const title = isMcp ? (q || td.label) : `${td.label}: ${q}`;
      const chars = round.toolContent.length;
      const meta = chars >= 1024 ? `${(chars / 1024).toFixed(1)}KB` : `${chars} chars`;
      openTextPreview(title, meta, round.toolContent);
      return;
    }
  }
}

// Event delegation for tool content preview buttons
document.addEventListener('click', function(e) {
  const btn = e.target.closest('[data-tc-preview]');
  if (!btn) return;
  e.stopPropagation();
  e.preventDefault();
  const rn = parseInt(btn.dataset.tcRn, 10);
  const tcid = btn.dataset.tcTcid || null;
  previewToolContent(rn, tcid);
});

// Event delegation for ptool-truncated "show all" bars (static render path)
document.addEventListener('click', function(e) {
  const trunc = e.target.closest('.ptool-truncated');
  if (!trunc) return;
  const body = trunc.closest('.ptool-panel-body');
  if (!body) { trunc.remove(); return; }
  // Find the message element and its conversation + message data to re-render all rounds
  const msgEl = trunc.closest('.message');
  if (msgEl) {
    const msgIdx = parseInt((msgEl.id || '').replace('msg-', ''), 10);
    const conv = getActiveConv();
    if (conv && conv.messages && conv.messages[msgIdx]) {
      const msg = conv.messages[msgIdx];
      const allRounds = getToolRoundsFromMsg(msg);
      if (allRounds.length > 0) {
        trunc.remove();
        /* Render the full grouped structure (parallel-batch .ptool-turn
         * containers) in one shot via the shared helper so the expanded
         * view matches the streaming/static layout exactly. */
        if (typeof _renderToolGroupsHTML === 'function') {
          body.innerHTML = _renderToolGroupsHTML(allRounds, allRounds);
        } else {
          body.innerHTML = '';
          for (const round of allRounds) {
            const slot = document.createElement('div');
            slot.setAttribute('data-prn', round.roundNum);
            slot.innerHTML = typeof _renderUnifiedToolLine === 'function'
              ? _renderUnifiedToolLine(round, false)
              : `<div class="ptool-line"><span class="ptool-text">${escapeHtml(round.toolName || round.query || '')}</span></div>`;
            body.appendChild(slot);
          }
        }
        return;
      }
    }
  }
  // Fallback: just remove the truncation bar
  trunc.remove();
});
