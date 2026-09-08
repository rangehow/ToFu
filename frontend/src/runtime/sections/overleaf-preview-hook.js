/* ===== migrated source: overleaf-preview-hook.js ===== */
/* ===== LaTeX live preview — retained scanner =====
 *
 * renderToolRoundsHTML calls _overleafPreviewObserveRounds on every paint.
 * The scan is pure and cheap; only a NEWLY settled TeX edit round during a
 * LIVE stream loads the lazy preview chunk (through the routed feature
 * stub). History re-renders (isStreaming=false) never compile.
 *
 * Two edit sources feed the same preview panel:
 *   - Overleaf MCP edits (mcp__overleaf__edit_file / rewrite_file /
 *     update_section) → remote compile via /api/v1/overleaf/preview.
 *   - LOCAL workspace .tex edits (write_file / edit_file / apply_diff(s) /
 *     insert_content(s)) → local compile via /api/v1/latex/preview. This is
 *     the default when the user never mentioned Overleaf.
 */

const _OVERLEAF_EDIT_TOOL_NAMES = new Set([
  'mcp__overleaf__edit_file',
  'mcp__overleaf__rewrite_file',
  'mcp__overleaf__update_section',
]);
const _LOCAL_TEX_EDIT_TOOL_NAMES = new Set([
  'write_file',
  'edit_file',
  'apply_diff',
  'apply_diffs',
  'insert_content',
  'insert_contents',
]);
const _TEX_SUFFIX_RE = /\.(tex|ltx)$/i;
/* Dedupe is bounded: toolCallIds are append-only per tab session. */
const _overleafPreviewSeen = new Set();
const _OVERLEAF_PREVIEW_SEEN_MAX = 256;
const _OVERLEAF_NEEDLE_MAX = 1200;

function _overleafPreviewRoundBlocked(round) {
  /* Only a settled success compiles: mid-run rounds have no stable content
   * yet; a rejected/refused edit changed nothing on disk or the remote. */
  return !round || round._rejected || round.rejection || round.refusal
    || round.status !== 'done';
}

function _overleafPreviewMarkSeen(toolCallId) {
  _overleafPreviewSeen.add(toolCallId);
  if (_overleafPreviewSeen.size > _OVERLEAF_PREVIEW_SEEN_MAX) {
    const keep = Array.from(_overleafPreviewSeen).slice(-128);
    _overleafPreviewSeen.clear();
    for (const id of keep) _overleafPreviewSeen.add(id);
  }
}

function _overleafPreviewParseArgs(round) {
  let args = round && round.toolArgs;
  if (typeof args === 'string') {
    try { args = JSON.parse(args); } catch (_) { args = null; }
  }
  return args && typeof args === 'object' ? args : null;
}

function _overleafRoundCandidate(round) {
  const name = round && typeof round.toolName === 'string' ? round.toolName : '';
  if (!_OVERLEAF_EDIT_TOOL_NAMES.has(name)) return null;
  if (_overleafPreviewRoundBlocked(round)) return null;
  const id = typeof round.toolCallId === 'string' ? round.toolCallId : '';
  if (!id || _overleafPreviewSeen.has(id)) return null;
  const args = _overleafPreviewParseArgs(round);
  if (!args) return null;
  const projectId = typeof args.project_id === 'string' ? args.project_id : '';
  if (!/^[0-9a-f]{24}$/.test(projectId)) return null;
  const file = typeof args.file_path === 'string' ? args.file_path : '';
  /* The SyncTeX needle: edit_file → the inserted text, update_section →
   * its title, rewrite_file → the new content head (server bounds it). */
  let needle = '';
  if (typeof args.new_string === 'string' && args.new_string.trim()) {
    needle = args.new_string;
  } else if (typeof args.section_title === 'string') {
    needle = args.section_title;
  } else if (typeof args.content === 'string') {
    needle = args.content.slice(0, 800);
  }
  return {
    toolCallId: id,
    toolName: name,
    source: 'overleaf',
    projectId,
    file,
    needle: needle.slice(0, _OVERLEAF_NEEDLE_MAX),
  };
}

/* Model-emitted args are verbatim (the repair layer renames only its
 * execution copy), so tolerate the foreign-harness aliases here too. */
function _localTexStr(value) {
  return typeof value === 'string' ? value : '';
}

function _localTexPushCandidate(out, id, name, rawPath, needle) {
  const path = _localTexStr(rawPath).trim();
  if (!path || !_TEX_SUFFIX_RE.test(path)) return;
  out.push({
    toolCallId: id,
    toolName: name,
    source: 'local',
    path,
    needle: _localTexStr(needle).slice(0, _OVERLEAF_NEEDLE_MAX),
  });
}

function _localTexRoundCandidates(round) {
  const name = round && typeof round.toolName === 'string' ? round.toolName : '';
  if (!_LOCAL_TEX_EDIT_TOOL_NAMES.has(name)) return null;
  if (_overleafPreviewRoundBlocked(round)) return null;
  const id = typeof round.toolCallId === 'string' ? round.toolCallId : '';
  if (!id || _overleafPreviewSeen.has(id)) return null;
  const args = _overleafPreviewParseArgs(round);
  if (!args) return null;
  const out = [];
  if (name === 'write_file') {
    _localTexPushCandidate(out, id, name,
      _localTexStr(args.path) || _localTexStr(args.file_path),
      _localTexStr(args.content) || _localTexStr(args.file_text));
  } else if (name === 'apply_diff') {
    _localTexPushCandidate(out, id, name,
      _localTexStr(args.path) || _localTexStr(args.file_path),
      _localTexStr(args.replace) || _localTexStr(args.new_string));
  } else if (name === 'insert_content') {
    _localTexPushCandidate(out, id, name,
      _localTexStr(args.path) || _localTexStr(args.file_path),
      _localTexStr(args.content));
  } else {
    /* edit_file / apply_diffs / insert_contents: batch edits=[{path,…}]. */
    const edits = Array.isArray(args.edits) ? args.edits : [];
    for (const edit of edits) {
      if (!edit || typeof edit !== 'object') continue;
      _localTexPushCandidate(out, id, name,
        _localTexStr(edit.path) || _localTexStr(edit.file_path),
        _localTexStr(edit.replace) || _localTexStr(edit.new_string)
          || _localTexStr(edit.content));
    }
  }
  return out.length ? out : null;
}

function _overleafPreviewObserveRounds(rounds, isStreaming) {
  if (!isStreaming || !Array.isArray(rounds)) return;
  let latest = null;
  for (const round of rounds) {
    const overleaf = _overleafRoundCandidate(round);
    const locals = overleaf ? null : _localTexRoundCandidates(round);
    const candidates = overleaf ? [overleaf] : locals;
    if (!candidates || !candidates.length) continue;
    _overleafPreviewMarkSeen(candidates[0].toolCallId);
    /* A batch edit touching several .tex files previews the last one — the
     * trailing-latest pump collapses bursts to the newest anyway. */
    latest = candidates[candidates.length - 1];
  }
  if (!latest) return;
  const entry = runtimeScope._overleafPreviewRounds;
  if (typeof entry !== 'function') return;
  try { entry([latest]); } catch (e) {
    console.warn('[OverleafPreview] observe dispatch failed:', e && e.message);
  }
}
