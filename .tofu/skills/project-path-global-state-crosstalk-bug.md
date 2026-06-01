---
name: project-path-global-state-crosstalk-bug
description: Bug fix: conv.projectPath diverges from projectState UI singleton — new conv has undefined projectPath (shows project in UI, sends empty to backend), _restoreConvProject/_saveConvToolState/_loadProjectStatus never synced conv.projectPath, inline saves in loadConversation/newChat missed projectPath
enabled: true
tags: [javascript, frontend, bug-fix, project-path, crosstalk, global-state, per-conversation, state-divergence, projectState, conv.projectPath]
created: 2026-03-30T05:55:59Z
updated: 2026-03-30T09:45:13Z
---

# Project Path State Divergence Bug (v2)

## Root Cause
`projectState` (global UI singleton) and `conv.projectPath` (per-conv truth) can diverge because:

1. **New conv has no projectPath**: `sendMessage()` creates `{ id, title, messages:[] }` — no `projectPath` property. If `newChat()` was called with `hasInput=true`, `_clearProjectStateLocal()` is skipped → UI shows project B → but new conv has `projectPath = undefined` → `_getConvProjectPath(conv) = ""` → backend gets no project.

2. **`_restoreConvProject` success path didn't write `conv.projectPath`**: It only reads `conv.projectPath` to call `/api/project/set`, then calls `_applyProjectData()` which updates `projectState` but NOT `conv.projectPath`.

3. **`_saveConvToolState` didn't sync projectPath**: Saves all tool toggles (searchMode, fetchEnabled, etc.) to conv but skipped projectPath — managed separately by `_saveConvProjectPath()` which is only called from the project modal.

4. **Inline saves in `loadConversation`/`newChat` missed projectPath**: When switching convs, `prevConv` gets tool toggles saved inline but not `projectPath`.

## Bug Scenario
1. User on Conv A with project A active
2. Types text in input → clicks "New Chat" → `hasInput=true` → project UI NOT cleared
3. `sendMessage()` creates new conv → `conv.projectPath = undefined`
4. `startAssistantResponse` → `_pp = _getConvProjectPath(conv) = ""` → backend: no project tools
5. **User sees project A in UI but backend has no project!**

## Fix (5 locations)

### 1. `_saveConvToolState()` — sync from projectState
```javascript
// Only when projectState is actively showing a project
// Do NOT clear when projectState.active is false (async gap during _restoreConvProject)
if (projectState.active && projectState.path) {
    conv.projectPath = projectState.path;
    // + sync multi-root paths
}
```

### 2. `sendMessage()` — new conv inherits projectPath
```javascript
conv = { id, title: "New Chat", messages: [], ... };
if (projectState.active && projectState.path) {
    conv.projectPath = projectState.path;
    conv.projectPaths = [projectState.path, ...extras];
}
```

### 3. `_restoreConvProject()` — write back on success
```javascript
if (resp.ok) {
    _applyProjectData(data);
    conv.projectPath = data.path || savedPath;  // ← NEW
}
```

### 4. `loadConversation()` inline save — sync prevConv
```javascript
if (projectState.active && projectState.path) {
    prevConv.projectPath = projectState.path;
}
```

### 5. `newChat()` inline save — sync prevConv
Same as #4.

### 6. `loadProjectStatus()` — sync on both status-match and restore paths

## Critical Timing Note
`_restoreConvProject` temporarily clears `projectState` during its async fetch. If `_saveConvToolState` runs during this gap (e.g. user toggles a tool), we must NOT clear `conv.projectPath` — only sync when `projectState.active && projectState.path`. Explicit clearing is handled by `clearProject()` → `_saveConvProjectPath("")`.

