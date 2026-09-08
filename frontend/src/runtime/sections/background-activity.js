/* ===== migrated source: background-activity.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   background-activity.js — "still working" chip in the convStatusStrip

   Answers the question a settled foreground turn leaves behind: are any
   sub-agents (spawn_agents swarms) or detached background commands still
   executing right now? The spawning turn has usually ENDED by the time
   this matters — swarm results arrive later as auto-dispatched inbox
   injects. This chip filters by the active conversation's conv_id while it
   polls the owner-scoped snapshot `GET /api/v1/activity/background`
   and lives in the conversation-status strip directly above the composer
   (same in-flow host as the context gauge, inserted before #turnNav).

   Contract: docs/API_CONTRACT.md §2.19 + routes/api_v1/activity.py. Both
   lists are empty in the steady state — the chip is display:none then
   and the poll backs off to a slow idle cadence.

   Anti-flicker invariants (same discipline as context-bar.js):
   1. The chip node is CREATED ONCE in `_ensureChip()`; steady-state
      updates only set textContent / title / visibility.
   2. The popover list is rebuilt ONLY when its data signature changes —
      a live poll must never collapse a row the user is reading.
   3. ONE chained-setTimeout poll loop (never setInterval): ~3s while
      anything is active, ~10s idle, ticks skipped while the tab is
      hidden (no pending timeout), and an immediate tick on visibility return.

   Public API:
     runtimeScope.updateBackgroundActivity() — force one snapshot tick.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const _TICK_ACTIVE_MS = 3000;
  const _TICK_IDLE_MS = 10000;

  /* _state: cached chip nodes (see context-bar.js for the pattern). */
  let _state = null;
  let _pollTimer = null;
  let _inflight = false;

  let _disposed = false;
  /* Last applied snapshot + its repaint signature. */
  let _last = { swarms: [], backgroundCommands: [] };
  let _lastSig = '';
  let _popover = null;   // { el, bodyNode, sig }

  function _tt(key, fallback, vars) {
    if (typeof t === 'function') return t(key, vars);
    if (!vars) return fallback;
    return String(fallback).replace(/\{(\w+)\}/g,
      (m, name) => (name in vars ? String(vars[name]) : m));
  }

  function _ensureChip() {
    if (_state && _state.el && _state.el.isConnected) return _state;
    const host = document.getElementById('convStatusStrip');
    if (!host) return null;
    const existing = document.getElementById('bgActivityChip');
    const el = existing || document.createElement('aside');
    if (!existing) {
      el.id = 'bgActivityChip';
      el.className = 'bg-activity-chip';
      el.setAttribute('aria-live', 'polite');
      /* Left-to-right strip order: context gauge (prepended), this chip,
       * then the turn-nav right cell. */
      host.insertBefore(el, document.getElementById('turnNav') || null);
    }
    if (!el.querySelector('.bg-act-dot')) {
      const dot = document.createElement('span');
      dot.className = 'bg-act-dot';
      dot.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span');
      label.className = 'bg-act-label';
      el.appendChild(dot);
      el.appendChild(label);
    }
    el.style.display = 'none';
    _state = {
      el,
      labelNode: el.querySelector('.bg-act-label'),
      lastLabel: '',
      lastTitle: '',
      visible: null,
    };
    el.addEventListener('click', _onChipClick);
    return _state;
  }

  /* ── Snapshot → chip ──────────────────────────────────────────── */

  function _summarize(data) {
    const convId = typeof activeConvId !== 'undefined' ? activeConvId : null;
    const belongs = item => !!convId && item.conv_id === convId;
    const swarms = (Array.isArray(data && data.swarms) ? data.swarms : []).filter(belongs);
    const commands = (Array.isArray(data && data.backgroundCommands)
      ? data.backgroundCommands : []).filter(belongs);
    let agentsActive = 0;
    for (const s of swarms) {
      agentsActive += (Number(s.running) || 0) + (Number(s.pending) || 0);
    }
    return { swarms, commands, agentsActive, commandsActive: commands.length };
  }

  function _chipLabel(sum) {
    const parts = [];
    if (sum.agentsActive > 0) {
      parts.push(_tt(
        sum.agentsActive === 1 ? 'bgActivity.chip.agentOne'
                               : 'bgActivity.chip.agentMany',
        sum.agentsActive === 1 ? '{count} sub-agent running'
                               : '{count} sub-agents running',
        { count: sum.agentsActive }));
    }
    if (sum.commandsActive > 0) {
      parts.push(_tt(
        sum.commandsActive === 1 ? 'bgActivity.chip.cmdOne'
                                 : 'bgActivity.chip.cmdMany',
        sum.commandsActive === 1 ? '{count} background command running'
                                 : '{count} background commands running',
        { count: sum.commandsActive }));
    }
    return parts.join(' · ');
  }

  function _chipTitle(sum) {
    const lines = [_tt('bgActivity.title', 'Background activity')];
    for (const s of sum.swarms) {
      lines.push('· ' + _tt('bgActivity.swarmSummary',
        '{running} running · {pending} queued · {completed} done',
        { running: s.running || 0, pending: s.pending || 0,
          completed: s.completed || 0 }));
    }
    for (const c of sum.commands) {
      lines.push('· $ ' + (c.command || ''));
    }
    return lines.join('\n');
  }

  function _apply(sum) {
    const s = _ensureChip();
    if (!s) return;
    const active = sum.agentsActive > 0 || sum.commandsActive > 0;
    if (s.visible !== active) {
      s.el.style.display = active ? '' : 'none';
      s.visible = active;
    }
    if (!active) {
      _closePopover();
      return;
    }
    const label = _chipLabel(sum);
    if (s.lastLabel !== label) {
      s.labelNode.textContent = label;
      s.lastLabel = label;
    }
    const tip = _chipTitle(sum);
    if (s.lastTitle !== tip) {
      s.el.title = tip;
      s.lastTitle = tip;
    }
    if (_popover) _renderPopoverBody(sum);
  }

  /* ── Popover ──────────────────────────────────────────────────── */

  function _fmtElapsed(totalSeconds) {
    const sec = Math.max(0, Math.round(Number(totalSeconds) || 0));
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60);
    const r = sec % 60;
    if (m < 60) return m + 'm ' + String(r).padStart(2, '0') + 's';
    return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }

  function _swarmElapsed(swarm) {
    const created = Number(swarm.created_at) || 0;
    if (!created) return '';
    return _fmtElapsed(Date.now() / 1000 - created);
  }

  function _convLabel(convId) {
    if (!convId) return '';
    if (typeof getConvById === 'function') {
      const conv = getConvById(convId);
      const title = conv && (conv.title || conv.summary);
      if (title) return title;
    }
    return convId;
  }

  function _sigOf(sum) {
    /* Only fields that affect the popover body — chip text follows the
     * same signature cheaply via label/title guards. */
    return JSON.stringify([
      sum.swarms.map(s => [s.swarm_key, s.conv_id, _convLabel(s.conv_id),
        s.created_at, s.running, s.pending, s.completed,
        (s.agents || []).map(a => [a.id, a.role, a.objective, a.status, a.round])]),
      sum.commands.map(c => [c.command_id, c.command, c.status]),
    ]);
  }

  function _closePopover() {
    if (_popover && _popover.el) {
      try { _popover.el.remove(); } catch (e) { /* detached */ }
    }
    _popover = null;
    document.removeEventListener('click', _onDocClick, true);
  }

  function _onDocClick(e) {
    if (_popover && _popover.el && !_popover.el.contains(e.target)
        && _state && _state.el && !_state.el.contains(e.target)) {
      _closePopover();
    }
  }

  function _onChipClick(e) {
    e.stopPropagation();
    _togglePopover();
  }

  function _togglePopover() {
    if (_popover) { _closePopover(); return; }
    const s = _ensureChip();
    if (!s || !s.visible) return;
    const el = document.createElement('div');
    el.id = 'bgActivityPopover';
    el.className = 'bg-act-popover';
    const head = document.createElement('div');
    head.className = 'bg-act-pop-head';
    head.textContent = _tt('bgActivity.title', 'Background activity');
    const body = document.createElement('div');
    body.className = 'bg-act-pop-body';
    el.appendChild(head);
    el.appendChild(body);
    document.body.appendChild(el);
    const r = s.el.getBoundingClientRect();
    el.style.position = 'fixed';
    el.style.left = Math.round(r.left) + 'px';
    /* Opens UPWARD — the strip sits directly above the composer. */
    el.style.bottom = Math.round(window.innerHeight - r.top + 6) + 'px';
    _popover = { el, bodyNode: body, sig: '', swarmClocks: [], commandClocks: [] };
    _renderPopoverBody(_summarize(_last), true);
    document.addEventListener('click', _onDocClick, true);
  }

  function _renderPopoverBody(sum, force) {
    if (!_popover) return;
    const sig = _sigOf(sum);
    if (!force && sig === _popover.sig) {
      // Update only clock leaves: preserve open rows, selection and scroll.
      _popover.swarmClocks.forEach(({ node, index }) => {
        const text = _swarmElapsed(sum.swarms[index]);
        if (node.textContent !== text) node.textContent = text;
      });
      _popover.commandClocks.forEach((node, index) => {
        const text = _fmtElapsed(sum.commands[index].elapsed_s);
        if (node.textContent !== text) node.textContent = text;
      });
      return;
    }
    _popover.swarmClocks = [];
    _popover.commandClocks = [];
    _popover.sig = sig;
    const body = _popover.bodyNode;
    body.textContent = '';

    if (sum.swarms.length) {
      const secTitle = document.createElement('div');
      secTitle.className = 'bg-act-pop-section';
      secTitle.textContent = _tt('bgActivity.sectionAgents', 'Sub-agents');
      body.appendChild(secTitle);
      sum.swarms.forEach((swarm, index) => {
        const row = _swarmRow(swarm);
        const node = row.querySelector('.bg-act-elapsed');
        if (node) _popover.swarmClocks.push({ node, index });
        body.appendChild(row);
      });
    }
    if (sum.commands.length) {
      const secTitle = document.createElement('div');
      secTitle.className = 'bg-act-pop-section';
      secTitle.textContent = _tt('bgActivity.sectionCommands', 'Background commands');
      body.appendChild(secTitle);
      for (const cmd of sum.commands) {
        const row = _commandRow(cmd);
        _popover.commandClocks.push(row.querySelector('.bg-act-elapsed'));
        body.appendChild(row);
      }
    }
  }

  function _swarmRow(swarm) {
    const row = document.createElement('div');
    row.className = 'bg-act-swarm';
    const head = document.createElement('div');
    head.className = 'bg-act-swarm-head';
    const label = document.createElement('span');
    label.className = 'bg-act-swarm-conv';
    label.textContent = _convLabel(swarm.conv_id) || swarm.swarm_key || '';
    head.appendChild(label);
    const summary = document.createElement('span');
    summary.className = 'bg-act-swarm-summary';
    summary.textContent = _tt('bgActivity.swarmSummary',
      '{running} running · {pending} queued · {completed} done',
      { running: swarm.running || 0, pending: swarm.pending || 0,
        completed: swarm.completed || 0 });
    head.appendChild(summary);
    const elapsed = _swarmElapsed(swarm);
    if (elapsed) {
      const el = document.createElement('span');
      el.className = 'bg-act-elapsed';
      el.textContent = elapsed;
      head.appendChild(el);
    }
    row.appendChild(head);
    for (const agent of (swarm.agents || [])) {
      row.appendChild(_agentRow(agent));
    }
    /* Deep-link: the swarm outlives its spawning turn, but its
     * conversation is where the auto-dispatched results will land. */
    if (swarm.conv_id && typeof loadConversation === 'function') {
      row.classList.add('is-clickable');
      row.addEventListener('click', (e) => {
        e.stopPropagation();
        _closePopover();
        loadConversation(swarm.conv_id);
      });
    }
    return row;
  }

  function _agentRow(agent) {
    const row = document.createElement('div');
    row.className = 'bg-act-agent';
    row.dataset.status = agent.status || 'pending';
    const role = document.createElement('span');
    role.className = 'bg-act-agent-role';
    role.textContent = agent.role || 'agent';
    row.appendChild(role);
    const objective = document.createElement('span');
    objective.className = 'bg-act-agent-objective';
    objective.textContent = agent.objective || agent.id || '';
    row.appendChild(objective);
    const status = document.createElement('span');
    status.className = 'bg-act-agent-status';
    let text = agent.status || 'pending';
    if (agent.status === 'running' && Number(agent.round) > 0) {
      text += ' · ' + _tt('bgActivity.agentRound', 'round {round}',
                          { round: agent.round });
    }
    status.textContent = text;
    row.appendChild(status);
    return row;
  }

  function _commandRow(cmd) {
    const row = document.createElement('div');
    row.className = 'bg-act-cmd';
    const text = document.createElement('code');
    text.className = 'bg-act-cmd-text';
    text.textContent = cmd.command || '';
    row.appendChild(text);
    const elapsed = document.createElement('span');
    elapsed.className = 'bg-act-elapsed';
    elapsed.textContent = _fmtElapsed(cmd.elapsed_s);
    row.appendChild(elapsed);
    return row;
  }

  /* ── Poll loop ────────────────────────────────────────────────── */

  function _schedule(ms) {
    _clearPoll();
    if (_disposed || document.hidden || _inflight) return;
    _pollTimer = setTimeout(_tick, ms);
  }

  function _tick() {
    _pollTimer = null;
    if (_disposed || document.hidden || _inflight) return;
    const api = (typeof Api !== 'undefined' && Api && Api.activity) || null;
    if (!api) { _schedule(_TICK_IDLE_MS); return; }
    _inflight = true;
    Promise.resolve().then(() => api.background()).then((data) => {
      _inflight = false;
      if (_disposed) return;
      if (data) {
        _last = data;
        const sum = _summarize(data);
        const sig = _sigOf(sum);
        const active = sum.agentsActive > 0 || sum.commandsActive > 0;
        if (sig !== _lastSig) {
          _lastSig = sig;
          _apply(sum);
        } else if (active) {
          /* Elapsed strings age even when the set is unchanged. */
          _apply(sum);
        }
        _schedule(active ? _TICK_ACTIVE_MS : _TICK_IDLE_MS);
      } else {
        /* Transient read failure — keep the last known state on screen,
         * back off rather than flap the chip. */
        _schedule(_TICK_IDLE_MS);
      }
    }).catch(() => {
      _inflight = false;
      _schedule(_TICK_IDLE_MS);
    });
  }

  function updateBackgroundActivity() {

    if (_disposed) return;
    _closePopover();
    const sum = _summarize(_last);
    _lastSig = _sigOf(sum);
    _apply(sum);
    _schedule(0);
  }

  runtimeScope.updateBackgroundActivity = updateBackgroundActivity;

  function _clearPoll() {
    if (_pollTimer !== null) clearTimeout(_pollTimer);
    _pollTimer = null;
  }

  function _onVisibilityChange() {
    if (document.hidden) _clearPoll();
    else {
      _apply(_summarize(_last));
      _schedule(0);
    }
  }

  function _onReady() {
    // Never clobber an explicit pre-DCL immediate refresh or active flight.
    if (_pollTimer === null) _schedule(1200);
  }

  function _dispose() {
    if (_disposed) return;
    _disposed = true;
    _clearPoll();
    _closePopover();
    document.removeEventListener('visibilitychange', _onVisibilityChange);
    document.removeEventListener('DOMContentLoaded', _onReady);
    if (_state) {
      _state.el.removeEventListener('click', _onChipClick);
      _state.el.remove();
    }
    _state = null;
    _last = { swarms: [], backgroundCommands: [] };
  }

  if (typeof retainedCompositionLifecycle !== 'undefined') {
    retainedCompositionLifecycle.add(_dispose);
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', _onVisibilityChange);
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _onReady, { once: true });
    } else {
      _onReady();
    }
  }
})();
