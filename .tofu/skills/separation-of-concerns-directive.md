---
name: separation-of-concerns-directive
description: User directive: strict frontend/backend separation of concerns
enabled: true
tags: [architecture, convention, frontend, backend, separation-of-concerns]
created: 2026-04-14T16:11:41Z
updated: 2026-04-16T07:40:08Z
---

# Separation of Concerns — Frontend vs Backend

**Mandatory directive from user (2026-04-14):**

1. **Do NOT implement logic in the frontend that belongs on the backend.** Business logic, data transformation, validation, and complex decision-making should live in Python (Flask routes / lib modules), not in JavaScript.

2. **When moving logic backend-side, clean up the frontend thoroughly.** Don't leave dead code, unused variables, orphaned event handlers, or vestigial JS functions that are no longer needed. Remove cleanly.

3. **Frontend role**: UI rendering, user interaction, API calls, display formatting only.
4. **Backend role**: Business logic, data processing, validation, persistence, LLM orchestration, security checks.

**Checklist when refactoring:**
- [ ] Identify all frontend code being replaced
- [ ] Implement equivalent backend logic
- [ ] Remove frontend code completely (not just commented out)
- [ ] Verify no dangling references to removed functions/variables
- [ ] Test both the new backend endpoint and the simplified frontend

## Completed Migrations

### Message Queue Decision (2026-04-16)
- **Before**: Frontend `sendMessage()` checked `activeStreams/activeTaskId/_translating` to decide queue vs send, maintained `pendingMessageQueue` Map optimistically, posted to separate `/api/chat/queue` endpoint
- **After**: Frontend always POSTs to `/api/chat/send`. Backend checks for running tasks, returns `{queued: true}` or `{taskId}`. Frontend only syncs UI from server state via `_refreshServerQueue()`.
- Eliminated: frontend queue-decision block (96 lines), `pendingMessageQueue` optimistic writes, `finishStream` gating on `pendingMessageQueue`

