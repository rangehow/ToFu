---
name: folder-tabs-architecture
description: Folders now render as a VERTICAL PROJECT RAIL (left column of .sidebar-body), not a wrapping pill bar; collapsible icon-strip, zero-folder degradation
enabled: true
tags: [frontend, folders, sidebar, architecture]
created: 2026-04-09T16:03:08Z
updated: 2026-07-07T11:14:14Z
---

# Project Rail (folders) Architecture — 2026-07-07 redesign

The wrapping pill tab-bar was REPLACED by a **vertical project rail** (a left
column inside the sidebar). The old ragged flex-wrap pills + `+N` expand
toggle are GONE.

## Layout (index.html)
```
.sidebar
  .sidebar-header (Tofu | search/theme/debug/settings)
  .sidebar-action-row ([+ New Chat])
  .sidebar-search-wrapper (hidden, slides down)
  .folder-quickadd  #folderQuickAdd  ← "+ New folder", ONLY shown when 0 folders
  .sidebar-body  #sidebarBody  (display:flex row)
     nav.project-rail  #folderTabs   ← the rail (left column)
     .conversations-list #convList   ← filtered list (flex)
```

## Rail render (`_renderFolderTabsInner` in static/js/ui/conversation_list.js)
Emits: `.project-rail-head` (title `t('sidebar.projects')` + `.project-rail-collapse` chevron) → `.project-rail-list` (未分类 row + one `.folder-tab` row per folder) → footer `.folder-tab.folder-tab-add`.
- **Row DOM contract UNCHANGED** from the pill era: `.folder-tab[data-folder-id]` (empty string = 未分类) + inner `.folder-tab-dot` + `.folder-tab-name` + `.folder-tab-count`. This is deliberate so all delegated handlers (click/context-menu/long-press/drag-drop) + the longpress test keep working.
- **`.folder-tab-dot` is a monogram "app tile"** (2026-07-07 pass 2), NOT a small dot. A colored 20px rounded-square (30px in the collapsed strip / drawer) carrying `data-initial` = a **1–2 char monogram** from `_folderMonogram(name)` (multi-word → initials "Machine Learning"→ML; single word → first two letters "chatui"→CH; CJK → first two chars raw; empty → •) + a `data-mono-len` font-size hint. Shown in BOTH the labeled rail and the collapsed strip (the lone-letter recognition complaint). The inbox (未分类) + add tiles are the SAME 20/30px footprint but a neutral `--bg-hover` surface + `inset 0 0 0 1px --border` ring with a centered glyph → they read as "system" chips, distinct from the colored project tiles. Active-when-collapsed uses a 2px-gap accent RING on the tile (`box-shadow:0 0 0 2px --bg-tertiary,0 0 0 4px --accent`), not the left `::before` bar (which is hidden when centered). Folder color rides the inline `style="background:<color>"` computed by `_folderColor(f)`: an explicit `f.color` is honored verbatim; an UNCOLORED folder gets a STABLE per-key HSL (hash of `f.id||f.name` mod 360, fixed `52% 55%` pastel) instead of the shared `var(--accent)` — so N uncolored projects are visually distinct (pure fn → same key = same color across renders/sessions). Empty key → `var(--accent)`. Inbox/add tiles use theme tokens so tofu adapts with no extra rule.
- **Split-hash fast path PRESERVED**: struct/content/active hashes; re-selecting the active project swaps `.active` in place, no rebuild.

## Zero-folder degradation (owner-mandated)
`folders.length===0` → rail rendered empty + `.sidebar.has-rail` REMOVED → single-column list, exactly as before. Rail materializes only once ≥1 folder exists. `#folderQuickAdd` is the only entry point in that state (hidden by `.sidebar.has-rail .folder-quickadd{display:none}`).

## Collapsible icon rail
`.project-rail-collapse` → `_toggleProjectRail()` (main_folders_mobile.js) toggles `.sidebar.rail-collapsed` + persists `localStorage['tofu_project_rail_collapsed']` ('1'/'0'). Renderer reads it back via `_readRailCollapsed()` every render (self-heals). Collapsed = ~52px icon-only (colored initial in the dot, name/count hidden).

## Width (CSS vars, DERIVED — no magic numbers)
`.sidebar{--project-rail-w:150px;--project-rail-w-collapsed:52px;--sidebar-list-min:280px}`. `.sidebar.has-rail` width = `calc(rail + list-min)`; `.rail-collapsed` uses the collapsed var. Push, never overlay.

## Responsive + paper mode
- Drawer viewport `@media(max-width:768px),(max-width:1024px) and (pointer:coarse)` (matches core.js isDrawerViewport/TOFU_BP + paper predicate): rail ALWAYS icon-only via `!important`; NO horizontal chip fallback. The two drawer `.sidebar{...}` blocks reset the derived width so it doesn't widen the fixed 82vw/360px drawer.
- Paper mode: `.sidebar.paper-active` hides `.sidebar-body` + `.folder-quickadd` (both desktop + mobile paper blocks).

## Interactions (main_folders_mobile.js `_initFolderTabs`)
Single delegated click handler: `.project-rail-collapse` → toggle; `.folder-tab-add` → `_promptCreateFolder`; else `setActiveFolderId`. Right-click / 500ms touch long-press → `_showFolderTabMenu` (rename/delete). Drag a `.conv-item` onto a `.folder-tab` → `setConversationFolder`. `#folderQuickAdd` click → `_promptCreateFolder`.

## i18n keys
`sidebar.projects`, `sidebar.collapseRail`, `sidebar.expandRail` (zh/en). REMOVED the now-dead `sidebar.moreFolders`/`sidebar.lessFolders`. `sidebar.uncategorized`/`sidebar.newFolder` still used.

## Tests
- `tests/test_frontend_project_rail.py` — render/filter/fast-path/zero-folder/drag/collapse-persist + 3 neuters. jsdom evals the real conversation_list.js + main_folders_mobile.js.
- `tests/test_frontend_folder_longpress.py` — long-press/click-swallow/drag/delete-dialog. NOTE: its harness must stub `isDrawerViewport`/`isMobileViewport`/`isTabletDrawerViewport` (the load-time IIFEs call them) or it throws before running.

## Unchanged
Backend `/api/v1/folders` CRUD, data model (`{id,name,color,collapsed,order}` + `conv.folderId`), `core/folders.js` state, pin→⭐ migration, drag-drop assignment semantics, 未分类 = conversations in no folder.

