---
name: spa-page-switch-state-persistence
description: Pattern for SPA page-switch state persistence: keep SSE readers/background streams alive in module-scoped vars, restore UI from accumulated state on re-enter instead of always resetting to initial state
enabled: true
tags: [javascript, spa, state-persistence, page-switch, sse, pattern, background-stream]
created: 2026-03-29T04:45:47Z
updated: 2026-04-08T08:12:25Z
---

# SPA Page-Switch State Persistence

## Problem
In single-page apps with tab navigation, switching tabs often calls `loadPage()` 
which resets all UI state (e.g., `_showPhase('setup')`). This destroys:
- Active SSE/fetch streams that are reading in the background  
- Accumulated progress logs
- Canvas charts, timeline entries, etc.

## Solution Pattern

### 1. Module-scoped persistent state
```javascript
var _simState = 'setup';           // Current phase
var _activeFetchReader = null;     // ReadableStreamDefaultReader
var _fetchLogHistory = [];         // Accumulated log entries
var _fetchPhaseProgress = {};      // {phase: {done, total, msg}} for progress bar replay
var _fetchComplete = false;
var _simResultData = null;         // Final result object
```

### 2. Load function restores instead of resetting
```javascript
function loadSimulator() {
    if (_simState === 'fetch') {
        _showPhase('fetch');
        _replayFetchLogs();        // Re-render accumulated logs + progress bars to DOM
        if (_fetchComplete) showProceedButton();
    } else if (_simState === 'results' && _simResultData) {
        _showPhase('results');
        _showResults(_simResultData);
    } else {
        _showPhase('setup');       // Only reset when truly in setup state
    }
}
```

### 3. Event handlers always update persistent state
```javascript
function _handleFetchEvent(evt) {
    // Always store in persistent history (even if DOM is hidden)
    _fetchLogHistory.push({ time: now, msg: evt.message });
    // Also persist per-phase progress for bar replay
    _fetchPhaseProgress[evt.phase] = { done: evt.done, total: evt.total, msg: evt.message };
    
    // Render to DOM (works even when hidden since elements still exist)
    var el = $('simFetchLog');
    if (el) { /* append to DOM */ }
}
```

### 4. Replay function for re-entering the page
```javascript
function _replayFetchLogs() {
    var logEl = $('simFetchLog');
    if (!logEl) return;
    logEl.innerHTML = '';
    _fetchLogHistory.forEach(function(entry) {
        // Re-create DOM elements from stored data
    });
    // Restore per-phase progress bars
    Object.keys(_fetchPhaseProgress).forEach(function(phase) {
        var p = _fetchPhaseProgress[phase];
        _setFetchProgress(phase, p.done, p.total, p.msg);
    });
    _updateOverallProgress();
}
```

### 5. Only reset state on explicit "go back" 
```javascript
function goBackToSetup() {
    _simState = 'setup';
    _fetchLogHistory = [];
    _fetchPhaseProgress = {};
    if (_activeFetchReader) { _activeFetchReader.cancel(); _activeFetchReader = null; }
    _showPhase('setup');
}
```

### 6. ⚠️ Event type disambiguation
When using thread+queue SSE (where event_type is a callback param, not a named SSE field),
**always inject _type into the data** at the route level:

```python
def on_event(event_type, event_data):
    event_data['_type'] = event_type
    event_q.put(event_data)
```

Then dispatch in frontend by `evt._type`, not by data shape heuristics. Shape-based
detection fails when different event types share common fields (e.g. both `sim_start`
and `sim_complete` have `session_id`).

## Key Insight
The SSE ReadableStreamDefaultReader continues reading in the background
even when its target DOM elements are hidden. The module-scoped variables
accumulate all events. When the user switches back, we replay from state
rather than re-requesting.
