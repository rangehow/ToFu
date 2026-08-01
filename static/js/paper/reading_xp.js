/* ═══════════════════════════════════════════════════════════════════
   paper/reading_xp.js — Reading-experience rail (design
   docs/PAPER_READING_EXPERIENCE_DESIGN.md P0)

   Turns the insight second-pass from a single end-of-report block into
   ANCHORED cards distributed through the reading flow:

     • connections / provocations carrying a resolved ``anchor_idx`` land as
       cards right after that section's heading (the backend resolved the
       model's nomination against the report's real h2/h3 sequence — the
       index here enumerates the same sequence, so the two never disagree);
     • thesis / opinion / open problems / UNANCHORED items rebuild the
       classic 💡 section at the end (nothing the pass produced is lost);
     • a 📦 recap card closes the report (thesis + top connections + the
       first open problem) — the "what you're taking away" moment;
     • the finish tag shows the TOTAL cost (body + second passes) with a
       per-pass breakdown tooltip (cost visibility, design §3.3).

   Seams into report.js (all optional-chained so a stale bundle degrades to
   the legacy appended-markdown behaviour):
     • window._paperXpAfterRender(article, container, view) — end of
       _renderFinalReport (covers cached + re-rendered reports);
     • window._paperXpHandleInsightEvent(s, ev, view) — the v2 'insight'
       event (structured items) → distribute into the LIVE article;
     • window._paperXpApplyMetaEvent(s, ev, view) — the 'report_meta' event
       (secondPasses landed after `done`) → hot-update the finish tag.

   All state is `var` on window — no top-level let/const (bundle concat).
   ═══════════════════════════════════════════════════════════════════ */

/** Grounded ref → arXiv link HTML (mirrors lib/paper/insight_engine/_render._ref_md). */
function _xpRefLink(card) {
  if (!card || !card.arxiv_id) return '';
  var url = card.abs_url || ('https://arxiv.org/abs/' + card.arxiv_id);
  var title = card.title || card.arxiv_id;
  return ' (<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener">' +
    escapeHtml(title) + '</a>)';
}

function _xpCard(cls, icon, title, innerHtml) {
  return '<div class="paper-xp-card ' + cls + '" role="note">' +
    '<div class="paper-xp-card-head">' + icon +
    '<span class="paper-xp-card-title">' + escapeHtml(title) + '</span></div>' +
    '<div class="paper-xp-card-body">' + innerHtml + '</div></div>';
}

function _xpConnectionCardHtml(item) {
  var label = (typeof t === 'function') ? t('paper.xpConnTitle') : 'Connections to your reading';
  var body = '<div class="paper-xp-conn">' + escapeHtml(item.text || '') +
    _xpRefLink(item.paper) + '</div>';
  return _xpCard('xp-conn', '🔗', label, body);
}

function _xpProvocationCardHtml(item) {
  var label = (typeof t === 'function') ? t('paper.xpProvTitle') : 'Pause and think';
  var text = (typeof item === 'string') ? item : (item.text || '');
  return _xpCard('xp-prov', '💭', label, '<div class="paper-xp-prov">' + escapeHtml(text) + '</div>');
}

/** Remove every xp node we previously inserted (idempotent re-distribution). */
function _xpClear(article) {
  if (!article || !article.querySelectorAll) return;
  var nodes = article.querySelectorAll('.paper-xp-card, .paper-xp-section, .paper-xp-recap');
  for (var i = 0; i < nodes.length; i++) {
    if (nodes[i].parentNode) nodes[i].parentNode.removeChild(nodes[i]);
  }
}

/** Distribute the insight payload through the rendered article.
 *  `view._xpInsight` = {items, markdown}. Safe to call repeatedly. */
function _paperXpDistribute(article, view) {
  if (!article || !view) return;
  var payload = view._xpInsight;
  var items = payload && payload.items;
  _xpClear(article);
  if (!items || typeof items !== 'object') return;
  var zh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
  var headings = article.querySelectorAll('h2, h3');

  // ── Anchored cards: insert right after the resolved section heading. ──
  var endConns = [];
  var conns = Array.isArray(items.connections) ? items.connections : [];
  for (var i = 0; i < conns.length; i++) {
    var c = conns[i];
    if (!c || !(c.text || '').trim()) continue;
    var idx = (typeof c.anchor_idx === 'number') ? c.anchor_idx : null;
    if (idx !== null && headings[idx]) {
      headings[idx].insertAdjacentHTML('afterend', _xpConnectionCardHtml(c));
    } else {
      endConns.push(c);
    }
  }
  var endProvs = [];
  var provs = Array.isArray(items.provocations) ? items.provocations : [];
  for (var j = 0; j < provs.length; j++) {
    var p = provs[j];
    var ptext = (typeof p === 'string') ? p : (p && p.text) || '';
    if (!ptext.trim()) continue;
    var pidx = (p && typeof p === 'object' && typeof p.anchor_idx === 'number')
      ? p.anchor_idx : null;
    if (pidx !== null && headings[pidx]) {
      headings[pidx].insertAdjacentHTML('afterend', _xpProvocationCardHtml(p));
    } else {
      endProvs.push(ptext);
    }
  }

  // ── End section: everything unanchored + thesis/opinion/open problems. ──
  var H = zh
    ? { sec: '## 💡 洞见与灵感', thesis: '这篇论文的赌注', conns: '与你读过的工作的联系',
        opinion: '一个观点', open: '值得你周一动手的开放问题', prov: '挑衅式追问' }
    : { sec: '## 💡 Insight & Ideas', thesis: 'The Bet', conns: 'Connections to Your Reading',
        opinion: 'A Take', open: 'Open Problems Worth Your Monday', prov: 'Provocations' };
  var md = [];
  var thesis = (items.thesis || '').trim();
  var opinion = (items.opinion || '').trim();
  var ops = (Array.isArray(items.open_problems) ? items.open_problems : [])
    .filter(function (o) { return o && (o.text || '').trim(); });
  var hasEnd = thesis || opinion || endConns.length || ops.length || endProvs.length;
  if (hasEnd) {
    md.push(H.sec, '');
    if (thesis) {
      md.push('### ' + H.thesis, '',
        (zh ? '> 关键结论：' : '> Key takeaway: ') + thesis, '');
    }
    if (endConns.length) {
      md.push('### ' + H.conns, '');
      for (var k = 0; k < endConns.length; k++) {
        md.push('- ' + endConns[k].text.trim() +
          (endConns[k].paper && endConns[k].paper.arxiv_id
            ? ' ([' + (endConns[k].paper.title || endConns[k].paper.arxiv_id) + '](' +
              (endConns[k].paper.abs_url || ('https://arxiv.org/abs/' + endConns[k].paper.arxiv_id)) + '))'
            : ''));
      }
      md.push('');
    }
    if (opinion) md.push('### ' + H.opinion, '', opinion, '');
    if (ops.length) {
      md.push('### ' + H.open, '');
      for (var m = 0; m < ops.length; m++) {
        md.push('- ' + ops[m].text.trim() +
          (ops[m].grounded_by && ops[m].grounded_by.arxiv_id
            ? ' ([' + (ops[m].grounded_by.title || ops[m].grounded_by.arxiv_id) + '](' +
              (ops[m].grounded_by.abs_url || ('https://arxiv.org/abs/' + ops[m].grounded_by.arxiv_id)) + '))'
            : ''));
      }
      md.push('');
    }
    if (endProvs.length) {
      md.push('### ' + H.prov, '');
      for (var n = 0; n < endProvs.length; n++) md.push('- ' + endProvs[n].trim());
      md.push('');
    }
    var sec = document.createElement('div');
    sec.className = 'paper-xp-section';
    sec.innerHTML = (typeof renderMarkdown === 'function')
      ? renderMarkdown(md.join('\n')) : escapeHtml(md.join('\n'));
    article.appendChild(sec);
  }

  // ── Recap card: what you're taking away. Always at the very end. ──
  if (thesis || conns.length || ops.length) {
    var recapTitle = (typeof t === 'function') ? t('paper.xpRecapTitle') : 'What you are taking away';
    var rows = [];
    if (thesis) rows.push('<div class="paper-xp-recap-row"><b>' +
      escapeHtml(zh ? '赌注' : 'The bet') + ':</b> ' + escapeHtml(thesis) + '</div>');
    var top = conns.slice(0, 2);
    for (var r = 0; r < top.length; r++) {
      rows.push('<div class="paper-xp-recap-row">🔗 ' + escapeHtml(top[r].text || '') + '</div>');
    }
    if (ops.length) {
      rows.push('<div class="paper-xp-recap-row">🧭 ' + escapeHtml(ops[0].text || '') + '</div>');
    }
    var recap = document.createElement('div');
    recap.className = 'paper-xp-recap';
    recap.setAttribute('role', 'note');
    recap.innerHTML = '<div class="paper-xp-recap-head">📦 ' + escapeHtml(recapTitle) + '</div>' +
      rows.join('');
    article.appendChild(recap);
  }
}

/* ── Seams (consumed by report.js) ────────────────────────────────────── */

/** End-of-render seam: distribute when a payload is attached to the view. */
function _paperXpAfterRender(article, container, view) {
  try {
    if (view && view._xpInsight) _paperXpDistribute(article, view);
  } catch (e) {
    console.warn('[Paper:XP] after-render distribute failed (non-fatal):', e);
  }
}

/** Live v2 'insight' event: stash the structured payload and distribute into
 *  the already-rendered article. Returns true when the event was handled as
 *  v2 (caller then SKIPS the legacy markdown-append path). */
function _paperXpHandleInsightEvent(s, ev, view) {
  if (!ev || !ev.items || typeof ev.items !== 'object') return false;
  s._insightRunning = false;
  if (s._insightApplied) return true;   // cursor replay guard
  s._insightApplied = true;
  s.insightText = ev.insight || '';
  s._xpInsight = { items: ev.items, markdown: ev.insight || '' };
  if (view) view._xpInsight = s._xpInsight;
  try {
    var container = view && document.getElementById(view.containerId);
    var article = container && container.querySelector('.paper-report-article');
    if (article) _paperXpDistribute(article, view);
  } catch (e) {
    console.warn('[Paper:XP] live distribute failed (non-fatal):', e);
  }
  return true;
}

/** 'report_meta' event: secondPasses landed after `done` — hot-update the
 *  finish tag in place (rebuild it from the fresh meta and swap the node). */
function _paperXpApplyMetaEvent(s, ev, view) {
  if (!ev || ev.type !== 'report_meta' || !ev.meta) return false;
  s.meta = ev.meta;
  if (view) view.meta = ev.meta;
  try {
    var container = view && document.getElementById(view.containerId);
    var old = container && container.querySelector('.paper-report-finish-tag');
    if (old && typeof _renderReportFinishTag === 'function') {
      var html = _renderReportFinishTag(ev.meta);
      if (html) {
        var wrap = document.createElement('div');
        wrap.innerHTML = html;
        if (wrap.firstChild) old.parentNode.replaceChild(wrap.firstChild, old);
      }
    }
  } catch (e) {
    console.warn('[Paper:XP] report_meta finish-tag refresh failed (non-fatal):', e);
  }
  return true;
}

/** Total-cost breakdown for the finish tag tooltip: "报告 ¥A + 洞察 ¥B + …".
 *  Returns '' when there are no passes (the tag then shows the body cost). */
function _paperXpCostBreakdown(meta) {
  if (!meta || !meta.secondPasses) return '';
  var parts = [];
  var fmt = function (v) {
    if (typeof v !== 'number' || v <= 0) return null;
    return (typeof formatCny === 'function') ? formatCny(v) : ('¥' + v.toFixed(4));
  };
  var base = fmt(meta.costCny);
  var _tt = (typeof t === 'function') ? t : function (k) { return k; };
  if (base) parts.push(_tt('paper.xpCostBody') + ' ' + base);
  var names = { insight: 'paper.xpPassInsight', termfill: 'paper.xpPassTermfill',
                checkpoints: 'paper.xpPassCheckpoints', deepen: 'paper.xpPassDeepen' };
  var sp = meta.secondPasses || {};
  for (var key in sp) {
    var c = fmt(sp[key] && sp[key].costCny);
    if (c) parts.push(_tt(names[key] || key) + ' ' + c);
  }
  return parts.join(' + ');
}

if (typeof window !== 'undefined') {
  window._paperXpAfterRender = _paperXpAfterRender;
  window._paperXpHandleInsightEvent = _paperXpHandleInsightEvent;
  window._paperXpApplyMetaEvent = _paperXpApplyMetaEvent;
  window._paperXpDistribute = _paperXpDistribute;
  window._paperXpCostBreakdown = _paperXpCostBreakdown;
}
