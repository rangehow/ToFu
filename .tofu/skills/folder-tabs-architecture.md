---
name: folder-tabs-architecture
description: Folder tab bar (always visible even with 0 folders), search moved to header toggle button (Ctrl+K), expandable search panel
enabled: true
tags: [frontend, folders, sidebar, architecture]
created: 2026-04-09T16:03:08Z
updated: 2026-04-09T16:14:04Z
---

# Folder Tabs + Search Toggle Architecture

## Layout
```
┌───────────────────────────┐
│ Tofu          🔍 🗓️📊⚙️  │  ← header (search toggle added)
├───────────────────────────┤
│ [+ New Chat]              │  ← action row
├───────────────────────────┤
│ 🔍 Search...           ✕  │  ← search panel (hidden by default, slides down)
├───────────────────────────┤
│ [+]  or  All│🔴Work│[+]  │  ← folder tabs (ALWAYS visible, even with 0 folders)
├───────────────────────────┤
│  conversations...         │
└───────────────────────────┘
```

## Search
- **Trigger**: 🔍 button in sidebar header OR `Ctrl+K` shortcut
- **Panel**: `#sidebarSearchWrapper` — hidden by default (`display:none`), slides down with `searchSlideDown` animation
- **Close**: ✕ button calls `closeSidebarSearch()`, Escape key also closes, `Ctrl+K` toggles
- **Active state**: `#sidebarSearchToggle.active` highlights the search icon
- **Functions**: `toggleSidebarSearch()`, `closeSidebarSearch()`, `initSidebarSearch()` (input debounce)
- Search auto-exits folder view (searches all conversations)

## Folder Tabs
- **Always visible** — even with 0 folders, shows just the `[+]` add button for discoverability
- "All" tab only shows when there are ≥1 folders (otherwise redundant)
- `renderFolderTabs()` in `ui.js` — no more `display:none` when empty
- Click tab → filter sidebar. Right-click → rename/delete context menu
- Drag conversation onto tab → move to folder
- `+` tab opens create folder dialog

## Key Files
| Component | File | Functions |
|---|---|---|
| HTML structure | `index.html` | `#sidebarSearchToggle` button, `#sidebarSearchWrapper`, `#folderTabs` |
| Search toggle | `static/js/main.js` | `toggleSidebarSearch()`, `closeSidebarSearch()`, `initSidebarSearch()` |
| Tab rendering | `static/js/ui.js` | `renderFolderTabs()` (always shows, even with 0 folders) |
| Tab interactions | `static/js/main.js` | `_initFolderTabs()`, `_showFolderTabMenu()`, `_initFolderDragDrop()` |
| CSS | `static/styles.css` | `.sidebar-search-wrapper` (expandable), `.folder-tabs`, `.folder-tab` |

