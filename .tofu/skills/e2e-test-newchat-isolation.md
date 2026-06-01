---
name: e2e-test-newchat-isolation
description: Bug fix: Playwright E2E tests must call newChat() before sending messages to avoid polluting the currently-open production conversation
enabled: true
tags: [testing, e2e, playwright, bug-fix]
created: 2026-04-06T09:34:36Z
updated: 2026-04-06T09:34:36Z
---

# E2E Test Conversation Isolation

## Bug Pattern
Playwright visual E2E tests in `tests/test_visual_e2e.py` send test messages (e.g. "Hello, this is a test message!", "What is 2+2?", "Test sidebar entry") which land in **whatever conversation is currently open** when the page loads.

Since the `live_server` fixture uses the **production database** (not an isolated test DB), and the `page` fixture navigates to the live server which loads the most recently active conversation, test messages get appended to real user conversations.

The `page` fixture cleanup only deletes **newly created** conversation IDs (comparing before/after snapshots). Since the test appended to an existing conversation instead of creating a new one, the cleanup never fires.

## Fix
Every test that calls `_send_message()` **MUST** first call:
```python
page.evaluate("newChat()")
time.sleep(0.3)
```
This ensures a fresh conversation is created, which the fixture cleanup will properly delete.

## Impact
Without this fix:
- Test messages appear in real conversations
- The polluted conversation's `updated_at` gets bumped, pushing it to the top of the sidebar
- Users see random test messages like "Hello, this is a test message!" in their work conversations

## Files
- `tests/test_visual_e2e.py` — All test classes that send messages
- `tests/conftest.py` — The `page` fixture with cleanup logic

