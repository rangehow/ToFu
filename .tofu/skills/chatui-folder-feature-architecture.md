---
name: chatui-folder-feature-architecture
description: Conversation folders: tab bar filtering, /api/v1/folders CRUD, pin auto-migration to ⭐ folder
enabled: true
tags: [frontend, sidebar, folders, feature]
created: 2026-04-09T14:14:04Z
updated: 2026-05-29T02:43:33Z
---

# Conversation Folders Feature

## Architecture
- **Backend**: `routes/api_v1/folders.py` — CRUD at `/api/v1/folders` (auth-required, atomic via `lib.json_store.update_json_atomic`), stores in `data/config/folders.json`. *Legacy `/api/folders` removed 2026-05-29.*
- **Data model**: Each folder = `{id, name, color, collapsed, order, createdAt}`. Conv → folder link via `conv.folderId` in settings.
- **Frontend state**: `_folders` array + `_activeFolderId` in `core.js`, loaded at startup alongside conversations
- **`_foldersLoaded` flag**: tracks whether `loadFolders()` has completed. Used by `renderConversationList` to avoid showing all conversations unfiltered before folder data arrives.

## Pinning Replaced by Folders
**Pinning was fully removed** — the pinned zone, freeze border, pin buttons, `togglePinConversation()`, and all related CSS are gone. Instead:
- `_migratePinnedToFolder()` in `core.js` auto-creates a "⭐ 置顶" folder and moves all pinned convs into it on first load
- `_convSorter()` no longer considers `pinned` state — sorts by active status then recency only
- The `pinned`/`pinnedAt` fields are still saved in conv settings for backward compatibility but are functionally ignored

## Folder Tab Bar (Telegram-style, wrapping)
The sidebar has a **wrapping tab bar** between the search wrapper and the conversation list:
- **Layout**: `flex-wrap` CSS with `max-height: 62px` (2 rows) — collapsed by default, expandable
- **Expand/collapse**: When tabs overflow 2 rows, a chevron toggle button (▾) appears at bottom-right with gradient fade. Click to expand all rows. Arrow rotates 180° when expanded.
- **State**: `_folderTabsExpanded` (let in ui.js) tracks expand state, persisted across re-renders
- **"未分类" (Uncategorized) tab** — shows ONLY conversations NOT in any folder (only appears when ≥1 folders exist). Has inbox icon.
- **Folder tabs** — colored pills with dot + name + count badge, click to filter
- **"+" tab** — always visible for folder creation (dashed border, discoverable even with 0 folders)
- **Count badges** — each tab shows conversation count (white-on-accent for active tab)
- **Right-click** a tab → context menu with rename/delete
- **Drag** a conversation onto a tab → assigns it to that folder (dragging to 未分类 removes from folder)
- Tabs auto-hide when search is active

## Key Behavior
- **Default view (未分类)**: Only shows conversations NOT in any folder — folders truly separate conversations
- **No "All" tab** — conversations are either in a folder or uncategorized, never shown in both places
- **New Chat** while viewing a folder tab → auto-assigns to that folder
- **New Chat** while on 未分类 → no folder assignment (stays uncategorized)
- **Empty states**: 未分类 shows "所有对话都已归类", folder shows "文件夹是空的"

## API contract
- `GET    /api/v1/folders` → bare JSON array (NOT enveloped) for frontend `Api.folders.list`.
- `POST   /api/v1/folders` `{name, color?}` → 201 + bare folder dict.
- `PUT    /api/v1/folders/<id>` partial → bare folder dict.
- `DELETE /api/v1/folders/<id>` → `{ok:true}`.
- `POST   /api/v1/folders/reorder` `{order:[id,...]}` → `{ok:true}`.

The bare-dict response shape on create/update is intentional: the
frontend `Object.assign(_folders[idx], updated)` would otherwise leak
an `ok:true` field onto cached folder objects.

## Bug Fix: Force Refresh Shows All Conversations Briefly
**Root cause**: `loadConversationsFromServer()` and `loadFolders()` run in parallel inside `initActiveTasks()`. When `loadConversationsFromServer` finishes first, it calls `renderConversationList()` while `_folders` is still `[]`. The filter code sees `folders.length === 0` and falls through to "show everything" — flashing ALL conversations in the sidebar before folders load and filtering kicks in.

**Fix**: Added `_foldersLoaded` flag (set to true after `loadFolders()` completes) + `areFoldersLoaded()` getter. In `renderConversationList`, when `folders.length === 0 && !foldersReady`, filter out conversations that have a `folderId` from server settings. The hash includes `foldersReady` state to trigger re-render when folders finish loading.

## Sidebar Search
- Search input is **toggled** via a 🔍 button in the sidebar header (not always visible)
- Expandable panel slides down with animation when clicked
- **Ctrl+K** keyboard shortcut toggles search
- **Escape** or ✕ closes and clears
- Search covers ALL conversations regardless of active folder tab

## Key Files
| Component | File | Functions |
|---|---|---|
| Backend API | `routes/api_v1/folders.py` | list/create/update/delete/reorder |
| State & sync | `static/js/core/folders.js` | `loadFolders()`, `createFolder()`, `updateFolder()`, `deleteFolder()`, `setConversationFolder()`, `getFolders()`, `getFolderById()`, `areFoldersLoaded()`, `_migratePinnedToFolder()` |
| Active-folder state | `static/js/core/folders.js` | `setActiveFolderId()`, `getActiveFolderId()` |
| Sidebar rendering | `static/js/ui.js` | `renderFolderTabs(folders, activeFolderId, allConvs)`, `renderConversationList()` — filters by active folder tab |
| Folder UI (picker, dialogs, D&D, tabs) | `static/js/main.js` and `static/js/main/main_folders_mobile.js` | `_showFolderPicker()`, `_promptCreateFolder()`, `_promptRenameFolder()`, `_confirmDeleteFolder()`, `_initFolderDragDrop()`, `_initFolderTabs()`, `_showFolderTabMenu()` |

## Sidebar Layout (top to bottom)
1. Header: `Tofu` | `🔍 🗓️ 📊 ⚙️`
2. Action row: `[+ New Chat]`
3. Search wrapper (hidden by default, slides down on toggle)
4. Folder tabs: `📥未分类(5) | 🔴Work(3) | 🟢Personal(2) | [+]  ▾` (wraps, 2 rows collapsed, expand toggle)
5. Conversations list (filtered by active tab)

