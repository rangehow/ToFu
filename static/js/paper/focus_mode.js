/* ═══════════════════════════════════════════════════════════════════
   paper/focus_mode.js — Focus mode (design P4 沉浸)

   One paragraph at a time: everything else dims to a whisper, j/k walk the
   block flow, Esc (or the toolbar button) exits. Pure DOM/CSS, zero model —
   immersion here is subtraction, not features.
   ═══════════════════════════════════════════════════════════════════ */

var _paperFocus = { on: false, current: -1, blocks: [] };

function _focusBlocks(article) {
  var out = [];
  var kids = article ? article.children : [];
  for (var i = 0; i < kids.length; i++) {
    var tag = kids[i].tagName || '';
    if (/^(P|H1|H2|H3|UL|OL|BLOCKQUOTE|TABLE|PRE|DIV)$/.test(tag)) {
      // Skip layout furniture, keep content blocks (xp cards ARE content).
      if (kids[i].classList.contains('paper-report-finish-tag')) continue;
      if (kids[i].classList.contains('paper-read-time')) continue;
      out.push(kids[i]);
    }
  }
  return out;
}

function _focusPaint() {
  var blocks = _paperFocus.blocks;
  for (var i = 0; i < blocks.length; i++) {
    blocks[i].classList.toggle('paper-focus-current', i === _paperFocus.current);
  }
  if (_paperFocus.current >= 0 && blocks[_paperFocus.current]
      && typeof blocks[_paperFocus.current].scrollIntoView === 'function') {
    blocks[_paperFocus.current].scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

function _focusSetContainer(on) {
  var container = document.getElementById('paperReportContent');
  if (container) container.classList.toggle('paper-focus-on', on);
  var btns = document.querySelectorAll('.paper-focus-btn');
  for (var i = 0; i < btns.length; i++) {
    btns[i].classList.toggle('is-on', on);
    btns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

function _paperFocusModeToggle() {
  var container = document.getElementById('paperReportContent');
  var article = container && container.querySelector('.paper-report-article');
  if (!_paperFocus.on && !article) return;
  _paperFocus.on = !_paperFocus.on;
  if (_paperFocus.on) {
    _paperFocus.blocks = _focusBlocks(article);
    // Start at the block nearest the current scroll position.
    var top = container.scrollTop;
    var idx = 0;
    for (var i = 0; i < _paperFocus.blocks.length; i++) {
      if (_paperFocus.blocks[i].offsetTop <= top + 40) idx = i;
      else break;
    }
    _paperFocus.current = idx;
  } else {
    _paperFocus.current = -1;
    for (var j = 0; j < _paperFocus.blocks.length; j++) {
      _paperFocus.blocks[j].classList.remove('paper-focus-current');
    }
  }
  _focusSetContainer(_paperFocus.on);
  if (_paperFocus.on) _focusPaint();
}

function _focusStep(dir) {
  var n = _paperFocus.blocks.length;
  if (!n) return;
  _paperFocus.current = Math.max(0, Math.min(n - 1, _paperFocus.current + dir));
  _focusPaint();
}

/** Called by reading_xp's after-render seam: a rebuild invalidated the block
 *  list — refresh it and keep the current position when focus mode is on. */
function _paperFocusAfterRender(article, container, view) {
  if (!_paperFocus.on) return;
  var prev = Math.max(0, _paperFocus.current);
  _paperFocus.blocks = _focusBlocks(article);
  _paperFocus.current = Math.min(prev, Math.max(0, _paperFocus.blocks.length - 1));
  _focusSetContainer(true);
  _focusPaint();
}

if (typeof window !== 'undefined') {
  window._paperFocusModeToggle = _paperFocusModeToggle;
  window._paperFocusAfterRender = _paperFocusAfterRender;
  if (!window._paperFocusKeysWired) {
    window._paperFocusKeysWired = true;
    document.addEventListener('keydown', function (ev) {
      if (!_paperFocus.on) return;
      // Never hijack typing.
      var t = ev.target;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || '')) return;
      if (t && t.isContentEditable) return;
      if (ev.key === 'j' || ev.key === 'ArrowDown') { _focusStep(1); ev.preventDefault(); }
      else if (ev.key === 'k' || ev.key === 'ArrowUp') { _focusStep(-1); ev.preventDefault(); }
      else if (ev.key === 'Escape') { _paperFocusModeToggle(); }
    });
  }
}
