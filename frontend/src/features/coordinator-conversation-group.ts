/** Coordinator-started conversations in the existing sidebar, never a session registry.
 * Server-issued _coordinatorSource is the only grouping evidence. Inventory
 * follows the same paged project-conversation endpoint as Manage work.
 */
import { t } from '../i18n';
import { createButton } from '../ui';
import type { CoordinatorConversationPage } from '../api/project-coordination.generated';
import { setStableAttribute, setStableData, setStableHidden, setStableText } from '../conversation/ui/renderers/stable-dom';
import { folderIcon } from './project/folder-icons';
import '../styles/application/33-project-coordination.css';

export interface CoordinatorConversationRow {
  id: string;
  title?: string;
  _coordinatorSource?: string | null;
  _serverTurnCount?: number;
  _serverRev?: number;
  projectPath?: string;
  updatedAt?: number;
  createdAt?: number;
}

export function coordinatorConversationIds(rows: readonly CoordinatorConversationRow[]): ReadonlySet<string> {
  return new Set(rows.filter(row => typeof row._coordinatorSource === 'string' && row._coordinatorSource.length > 0)
    .map(row => row.id));
}

export interface CoordinatorConversationGroupPorts {
  host: HTMLElement;
  rows(): readonly CoordinatorConversationRow[];
  readPage(cursor?: string, signal?: AbortSignal): Promise<CoordinatorConversationPage>;
  adoptPage?(page: CoordinatorConversationPage): void;
  adoptCoordinator?(id: string): void;
  open(id: string): void;
  renderRow(conversation: CoordinatorConversationRow): string;
  activeConversationId(): string;
  isRunning?(conversation: CoordinatorConversationRow): boolean;
  status?(conversation: CoordinatorConversationRow): 'running' | 'done' | 'failed' | 'cancelled' | null;
  readExpanded(coordinatorId: string): boolean | null;
  writeExpanded(coordinatorId: string, expanded: boolean): void;
  /** Bounded DOM: the sidebar list's row-window bound and its prefetch margin. */
  pageSize?: number;
  pagePrefetchPx?: number;
  refresh?(): void;
  refreshActive?(): void;
}

function relativeTime(timestamp: number | undefined): string {
  if (!timestamp) return '';
  const difference = timestamp - Date.now();
  const absolute = Math.abs(difference);
  const [unit, divisor]: [Intl.RelativeTimeFormatUnit, number] = absolute >= 31_536_000_000 ? ['year', 31_536_000_000]
    : absolute >= 2_592_000_000 ? ['month', 2_592_000_000]
      : absolute >= 86_400_000 ? ['day', 86_400_000]
        : absolute >= 3_600_000 ? ['hour', 3_600_000] : ['minute', 60_000];
  return new Intl.RelativeTimeFormat(document.documentElement.lang || undefined, { numeric: 'auto' })
    .format(Math.trunc(difference / divisor), unit);
}

export function createCoordinatorConversationGroup(ports: CoordinatorConversationGroupPorts) {
  let coordinatorId = '', expanded = false, loaded = false, failed = false, loading = false, disposed = false;
  let membership = '';
  let flight: AbortController | null = null;
  /* A project can start thousands of Workers. Render a bounded page and reconcile
   * rows by id: one minute label or status change must never replace the list. */
  const pageSize = Math.max(1, ports.pageSize ?? 50);
  const pagePrefetchPx = ports.pagePrefetchPx ?? 600;
  let limit = 0;
  let sentinel: HTMLElement | null = null;
  let windowObserver: IntersectionObserver | null = null;
  const rowNodes = new Map<string, { node: HTMLElement; html: string }>();
  const root = document.createElement('section');
  root.className = 'coordinator-conversations';
  root.dataset.coordinatorId = '';
  root.hidden = true;
  ports.host.append(root);
  const button = createButton({ label: '', variant: 'ghost' });
  button.classList.add('coordinator-conversations-toggle');
  button.setAttribute('aria-expanded', 'false');
  // The date groups' down chevron: CSS turns it to point right while collapsed.
  const chevron = folderIcon('down');
  chevron.classList.add('coordinator-conversations-chevron');
  chevron.setAttribute('width', '12'); chevron.setAttribute('height', '12'); chevron.setAttribute('stroke-width', '2.5');
  const label = document.createElement('span');
  label.className = 'coordinator-conversations-label';
  label.dataset.i18n = 'projectCoordination.conversations';
  label.textContent = t('projectCoordination.conversations');
  const count = document.createElement('span'); count.className = 'coordinator-conversations-count';
  const live = document.createElement('span'); live.className = 'coordinator-conversations-live';
  // A reply awaiting review is marked only on its own row, visible once the group is expanded.
  button.append(chevron, label, count, live);
  const body = document.createElement('div'); body.className = 'coordinator-conversations-body'; body.hidden = true;
  root.append(button, body);

  function members() {
    return ports.rows().filter(row => row._coordinatorSource === coordinatorId)
      .sort((a, b) => (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0));
  }
  /* The inventory only adds members the local catalog lacks, so only a member
   * joining or leaving invalidates it; edits to known members are already local. */
  function membershipOf(id: string) {
    return ports.rows().filter(row => row._coordinatorSource === id).map(row => row.id).sort().join('|');
  }
  function stopWindow() {
    windowObserver?.disconnect(); windowObserver = null;
    sentinel?.remove(); sentinel = null;
  }
  function resetRows() {
    stopWindow(); rowNodes.clear(); body.replaceChildren(); limit = 0;
  }

  /** One row node: the renderer's markup, with its owner-only actions folded away. */
  function mountRow(conversation: CoordinatorConversationRow, html: string): HTMLElement | null {
    const template = document.createElement('template');
    template.innerHTML = html;
    const node = template.content.firstElementChild as HTMLElement | null;
    if (!node) return null;
    node.tabIndex = 0; node.setAttribute('role', 'button');
    node.querySelectorAll('.conv-actions > :not(.conv-copy-id)').forEach(action => action.remove());
    node.querySelector('.conv-coordinator-badge')?.remove();
    const date = node.querySelector('.conv-date');
    if (date) {
      // The row's own background tag (agents or commands still running) stays beside the status.
      const background = date.querySelector('.conv-status-background');
      const time = document.createElement('span');
      time.className = 'coordinator-conversations-time';
      date.replaceChildren(time, ...(background ? [background] : []));
    }
    const open = () => { ports.open(conversation.id); ports.refreshActive?.(); };
    node.addEventListener('click', event => {
      if ((event.target as Element).closest('.conv-actions')) return;
      open();
    });
    node.addEventListener('keydown', event => {
      if (event.target !== node || (event.key !== 'Enter' && event.key !== ' ')) return;
      event.preventDefault(); open();
    });
    return node;
  }

  /** Only the leaves that age or change; equal values are not rewritten. */
  function updateRow(node: HTMLElement, conversation: CoordinatorConversationRow, activeId: string) {
    const active = conversation.id === activeId;
    if (node.classList.contains('active') !== active) node.classList.toggle('active', active);
    const date = node.querySelector('.conv-date');
    if (!date) return;
    const time = date.querySelector<HTMLElement>('.coordinator-conversations-time');
    if (time) setStableText(time, relativeTime(conversation.updatedAt || conversation.createdAt));
    const status = ports.status?.(conversation) ?? null;
    let statusLabel = date.querySelector<HTMLElement>('.coordinator-conversations-status');
    if (!status) { statusLabel?.remove(); return; }
    if (!statusLabel) {
      statusLabel = document.createElement('span');
      if (time) time.after(statusLabel); else date.prepend(statusLabel);
    }
    const className = `coordinator-conversations-status is-${status}`;
    if (statusLabel.className !== className) statusLabel.className = className;
    setStableText(statusLabel, t(`projectCoordination.status.${status}`));
  }

  /** Bottom-sentinel paging, the same bound the sidebar conversation list uses. */
  function ensureWindow(rows: readonly CoordinatorConversationRow[]) {
    if (limit >= rows.length) { stopWindow(); return; }
    if (!sentinel) {
      sentinel = document.createElement('div');
      sentinel.className = 'conv-window-sentinel';
      sentinel.setAttribute('aria-hidden', 'true');
    }
    if (sentinel.parentNode !== body || body.lastElementChild !== sentinel) body.append(sentinel);
    if (windowObserver) return;
    const Observer = body.ownerDocument.defaultView?.IntersectionObserver;
    if (!Observer) { limit = rows.length; sentinel.remove(); sentinel = null; return; }
    windowObserver = new Observer(entries => {
      if (!entries.some(entry => entry.isIntersecting) || !sentinel) return;
      limit += pageSize;
      paint();
      /* A true→true append produces no second callback; re-observe so paging
       * chains until the sentinel is pushed below the prefetch margin. */
      const raf = body.ownerDocument.defaultView?.requestAnimationFrame;
      if (sentinel && raf) raf(() => windowObserver?.observe(sentinel!));
    }, { root: body, rootMargin: `0px 0px ${pagePrefetchPx}px 0px` });
    windowObserver.observe(sentinel);
  }

  /* Every streamed event of any conversation can reach here through the
   * sidebar render: write only what changed, and only for rendered rows. */
  function paint() {
    const rows = members();
    // The count is always the rows the group lists: an empty or still-loading inventory shows nothing.
    setStableHidden(root, !coordinatorId || rows.length === 0);
    if (root.hidden) { setStableHidden(body, true); stopWindow(); return; }
    setStableText(count, String(rows.length));
    const running = rows.filter(row => ports.isRunning?.(row)).length;
    // Background work already counts as running (isRunning); a second tally crowded the label.
    setStableText(live, running ? t('projectCoordination.running', { n: running }) : '');
    setStableAttribute(button, 'aria-expanded', String(expanded));
    button.classList.toggle('is-expanded', expanded);
    setStableAttribute(button, 'aria-label', [`${t('projectCoordination.conversations')} (${rows.length})`,
      live.textContent].filter(Boolean).join(', '));
    setStableHidden(body, !expanded);
    if (!expanded) { stopWindow(); return; }
    const activeId = ports.activeConversationId();
    if (!limit) limit = Math.min(pageSize, rows.length);
    const activeIndex = activeId ? rows.findIndex(row => row.id === activeId) : -1;
    if (activeIndex >= limit) limit = activeIndex + 1;
    if (limit > rows.length) limit = rows.length;
    ensureWindow(rows);
    const visible = rows.slice(0, limit);
    let position = 0;
    for (const row of visible) {
      const html = ports.renderRow(row);
      let record = rowNodes.get(row.id);
      if (!record || record.html !== html) {
        const next = mountRow(row, html);
        if (record) {
          if (next) {
            const focused = record.node.contains(document.activeElement);
            record.node.replaceWith(next);
            if (focused) next.focus({ preventScroll: true });
          } else record.node.remove();
        }
        if (next) { record = { node: next, html }; rowNodes.set(row.id, record); }
        else { rowNodes.delete(row.id); record = undefined; }
      }
      if (!record) continue;
      const before = body.children[position] ?? sentinel ?? null;
      if (before !== record.node) body.insertBefore(record.node, before);
      position += 1;
      updateRow(record.node, row, activeId);
    }
    const shown = new Set(visible.map(row => row.id));
    for (const id of [...rowNodes.keys()]) if (!shown.has(id)) rowNodes.delete(id);
    const keep = new Set([...rowNodes.values()].map(record => record.node));
    for (const child of [...body.children]) {
      if (child !== sentinel && !keep.has(child as HTMLElement)) child.remove();
    }
  }
  async function load() {
    if (loading || loaded || disposed || !coordinatorId) return;
    loading = true;
    failed = false;
    const controller = new AbortController(); flight = controller;
    try {
      let cursor: string | undefined;
      do {
        const page = await ports.readPage(cursor, controller.signal);
        if (disposed || controller.signal.aborted) return;
        ports.adoptCoordinator?.(coordinatorId);
        ports.adoptPage?.(page);
        cursor = page.nextCursor ?? undefined;
      } while (cursor);
      loaded = true;
      // Members this read adopted must not invalidate the read that found them.
      membership = membershipOf(coordinatorId);
      paint();
      ports.refresh?.();
    } catch (error) {
      if (controller.signal.aborted || disposed) return;
      failed = true;
      console.warn('[CoordinatorConversations] inventory read failed', error);
      paint();
    } finally { loading = false; if (flight === controller) flight = null; }
  }
  button.addEventListener('click', () => {
    expanded = !expanded;
    ports.writeExpanded(coordinatorId, expanded);
    paint(); void load();
  });
  return {
    update(id: string) {
      setStableData(root, 'coordinatorId', id);
      const nextMembership = membershipOf(id);
      if (nextMembership !== membership) {
        membership = nextMembership; loaded = false;
      }
      if (id !== coordinatorId) {
        coordinatorId = id; expanded = ports.readExpanded(id) === true;
        loaded = false; failed = false; flight?.abort(); resetRows();
      }
      paint(); void load();
    },
    isCoordinatorConversation(id: string) {
      return Boolean(coordinatorId && ports.rows().some(row => row.id === id && row._coordinatorSource === coordinatorId));
    },
    refresh: paint,
    /** Retries a failed inventory read; `update` already painted this refresh. */
    revalidate() {
      if (failed) void load();
    },
    destroy() { disposed = true; flight?.abort(); stopWindow(); root.remove(); },
  };
}

/** A Coordinator with its own pinned entry; its group follows `host`. */
export interface PinnedCoordinator { id: string; path: string; host: HTMLElement }

/**
 * One group per Coordinator in the shown sidebar view. A pinned Coordinator
 * (the shown project's binding, the Global Router, the Watchdog) groups beneath
 * its entry. Any other conversation of the view that started conversations is a
 * replaced Coordinator: it gets one clickable row directly above the list, and
 * the list omits it (`hasHistoryRow`). Each mounted record owns every node it
 * inserted and is rebuilt when its anchor changes, so a Coordinator classified
 * before its binding settled never leaves a second row behind.
 */
export function createCoordinatorConversationGroups(ports: {
  /** The conversation list; replaced Coordinators' rows sit directly above it. */
  listHost: HTMLElement;
  /** Pinned Coordinators; an id without a path reserves it without a group. */
  pinned(): ReadonlyArray<PinnedCoordinator>;
  /** False while the shown project's binding is unresolved: its current Coordinator cannot be told from a replaced one yet. */
  historySettled(): boolean;
  projectPath(conversation: CoordinatorConversationRow): string;
  rows(): readonly CoordinatorConversationRow[];
  readPage(path: string, cursor?: string, signal?: AbortSignal): Promise<CoordinatorConversationPage>;
  adoptPage?(page: CoordinatorConversationPage): void;
  adoptCoordinator?(id: string, path: string): void;
  open(id: string): void;
  renderRow(conversation: CoordinatorConversationRow): string;
  activeConversationId(): string;
  isRunning?(conversation: CoordinatorConversationRow): boolean;
  status?(conversation: CoordinatorConversationRow): 'running' | 'done' | 'failed' | 'cancelled' | null;
  pageSize?: number;
  pagePrefetchPx?: number;
  refresh?(): void;
  refreshActive?(): void;
}) {
  interface Wanted { anchor: HTMLElement | null; path: string; coordinator?: CoordinatorConversationRow }
  interface Mounted {
    anchor: HTMLElement | null;
    row: HTMLElement | null;
    rowHtml: string;
    host: HTMLElement;
    group: ReturnType<typeof createCoordinatorConversationGroup>;
  }
  const mounted = new Map<string, Mounted>();
  let shown: readonly CoordinatorConversationRow[] = [];
  function unmount(id: string) {
    const record = mounted.get(id);
    if (!record) return;
    record.group.destroy(); record.host.remove(); record.row?.remove();
    mounted.delete(id);
  }
  function open(id: string) { ports.open(id); ports.refreshActive?.(); }
  function mountRow(id: string): HTMLElement {
    const row = document.createElement('div');
    row.className = 'coordinator-history-entry';
    row.addEventListener('click', event => {
      if ((event.target as Element).closest('.conv-actions')) return;
      if ((event.target as Element).closest('.conv-item')) open(id);
    });
    row.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const item = (event.target as Element).closest('.conv-item');
      if (!item || event.target !== item) return;
      event.preventDefault(); open(id);
    });
    ports.listHost.before(row);
    return row;
  }
  function paintRow(record: Mounted, coordinator: CoordinatorConversationRow) {
    const row = record.row!;
    const html = ports.renderRow(coordinator);
    if (html !== record.rowHtml) {
      const focused = row.contains(document.activeElement);
      row.innerHTML = html; record.rowHtml = html;
      const item = row.querySelector<HTMLElement>('.conv-item');
      if (item) {
        item.querySelectorAll('.conv-actions > :not(.conv-copy-id)').forEach(action => action.remove());
        item.querySelector('.conv-coordinator-badge')?.remove();
        item.tabIndex = 0; item.setAttribute('role', 'button');
        if (focused) item.focus({ preventScroll: true });
      }
    }
    row.querySelector('.conv-item')?.classList.toggle('active', coordinator.id === ports.activeConversationId());
  }
  return {
    /** Reconcile against the conversations of the shown sidebar view; a
     * presence-only refresh (a pinned entry changed) reuses the last view. */
    refresh(view: readonly CoordinatorConversationRow[] = shown) {
      shown = view;
      const rows = ports.rows();
      const sources = new Set(rows.map(row => row._coordinatorSource).filter((id): id is string => !!id));
      const pins = ports.pinned().filter(pin => pin.id);
      const pinned = new Set(pins.map(pin => pin.id));
      const wanted = new Map<string, Wanted>();
      // A pinned Coordinator mounts before any Worker is local: its paged inventory supplies cold history.
      for (const pin of pins) if (pin.path && !wanted.has(pin.id)) wanted.set(pin.id, { anchor: pin.host, path: pin.path });
      if (ports.historySettled()) {
        for (const row of view) {
          // A conversation another Coordinator started belongs to that group, never a row of its own.
          if (!sources.has(row.id) || row._coordinatorSource || pinned.has(row.id) || wanted.has(row.id)) continue;
          wanted.set(row.id, { anchor: null, path: ports.projectPath(row), coordinator: row });
        }
      }
      for (const [id, record] of mounted) {
        if (!wanted.has(id) || wanted.get(id)!.anchor !== record.anchor) unmount(id);
      }
      for (const [id, next] of wanted) {
        let record = mounted.get(id);
        if (!record) {
          const row = next.anchor ? null : mountRow(id);
          const host = document.createElement('div'); host.className = 'coordinator-conversations-host';
          (next.anchor ?? row!).after(host);
          const path = next.path;
          record = {
            anchor: next.anchor, row, rowHtml: '', host,
            group: createCoordinatorConversationGroup({
              host, rows: ports.rows,
              readPage: path ? (cursor, signal) => ports.readPage(path, cursor, signal)
                : async () => ({ conversations: [], nextCursor: null, coverage: 'complete' as const }),
              adoptPage: ports.adoptPage,
              adoptCoordinator: coordinator => ports.adoptCoordinator?.(coordinator, path),
              open: ports.open, renderRow: ports.renderRow,
              activeConversationId: ports.activeConversationId,
              isRunning: ports.isRunning, status: ports.status,
              pageSize: ports.pageSize, pagePrefetchPx: ports.pagePrefetchPx,
              readExpanded: key => localStorage.getItem(`tofu.coordinator.conversations.expanded.${key}`) === 'true' ? true : null,
              writeExpanded: (key, value) => localStorage.setItem(`tofu.coordinator.conversations.expanded.${key}`, String(value)),
              refresh: ports.refresh, refreshActive: ports.refreshActive,
            }),
          };
          mounted.set(id, record);
        }
        if (record.row && next.coordinator) paintRow(record, next.coordinator);
        record.group.update(id);
        record.group.revalidate();
      }
    },
    /** True when a replaced Coordinator's row is painted above the list instead of in it. */
    hasHistoryRow(id: string) { return Boolean(mounted.get(id)?.row); },
    isCoordinatorConversation(id: string) {
      return [...mounted.values()].some(record => record.group.isCoordinatorConversation(id));
    },
    destroy() { for (const id of [...mounted.keys()]) unmount(id); },
  };
}
