/** Lead deliverables in the classic sidebar: the reports and files section beneath a lead's entry.
 *
 * One section per shown lead (the Global Router's card, a project's Coordinator row). It is what the lead
 * sent the owner: every report (delivery, progress, decision, blocked) with the files its call attached,
 * and files published on their own. It reads the lead's projection page by page, newest first
 * (`leadDeliverables`), and appends older pages on demand, so a list of any length is complete, never
 * truncated. It is open until the owner folds it. Each row shows the kind (a report's own kind; decision
 * and blocked marked for attention), title, project and age; clicking it opens the deliverable itself
 * through the shared opener, each attached file opens from its chip, and the row's secondary control goes
 * to the lead's message at the exact Turn. A `lead_deliverable` project push for the shown lead, a push reconnect and
 * showing another lead re-read the first page; nothing polls. The server scopes every read to the owner.
 * Entry point: createLeadDeliverables. Dependencies: ports for the read, push and opener; i18n; ui.
 */
import { t } from '../../i18n';
import { createButton } from '../../ui';
import { apiFailure } from '../../api/errors';
import { appendDeliverables, deliverableAge, deliverableProject, type LeadDeliverable, type LeadDeliverablePage } from './model';
import { sourceButton, type DeliverableOpener } from './open';
import { folderIcon } from '../project/folder-icons';
import './lead-deliverables.css';

export interface LeadDeliverablesTarget { leadId: string; path: string }
export interface LeadDeliverablesPushFrame { kind?: unknown; conversationId?: unknown }

export interface LeadDeliverablesPorts {
  read(target: LeadDeliverablesTarget, cursor: string | undefined, signal: AbortSignal): Promise<LeadDeliverablePage>;
  /** Project push frames; returns the release. */
  subscribe(handler: (frame: LeadDeliverablesPushFrame) => void): () => void;
  onReconnect(listener: () => void): () => void;
  opener: DeliverableOpener;
  now?(): number;
  readExpanded?(leadId: string): boolean | null;
  writeExpanded?(leadId: string, expanded: boolean): void;
}

export interface LeadDeliverables {
  root: HTMLElement;
  show(target: LeadDeliverablesTarget): void;
  hide(): void;
  leadId(): string;
  destroy(): void;
}

export function createLeadDeliverables(ports: LeadDeliverablesPorts): LeadDeliverables {
  const now = ports.now || (() => Date.now());
  const root = document.createElement('section');
  root.className = 'lead-deliverables';
  root.hidden = true;
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'lead-deliverables__toggle';
  const label = document.createElement('span');
  label.className = 'lead-deliverables__label';
  const count = document.createElement('span');
  count.className = 'lead-deliverables__count';
  // The archive toggle's chevron (the date groups' down chevron); CSS turns it right while collapsed.
  const chevron = folderIcon('down');
  chevron.classList.add('lead-deliverables__chevron');
  chevron.setAttribute('width', '12'); chevron.setAttribute('height', '12'); chevron.setAttribute('stroke-width', '2.5');
  toggle.append(chevron, label, count);
  const body = document.createElement('div');
  body.className = 'lead-deliverables__body';
  body.id = `lead-deliverables-${Math.random().toString(36).slice(2)}`;
  toggle.setAttribute('aria-controls', body.id);
  const list = document.createElement('ul');
  list.className = 'lead-deliverables__list';
  list.setAttribute('role', 'list');
  const status = document.createElement('p');
  status.className = 'lead-deliverables__status';
  status.setAttribute('role', 'status');
  const more = createButton({ label: t('leadDeliverables.more'), variant: 'ghost', size: 'sm', onClick: () => void load(false) });
  more.classList.add('lead-deliverables__more');
  const retry = createButton({ label: t('leadDeliverables.retry'), variant: 'ghost', size: 'sm', onClick: () => void load(!items.length) });
  retry.classList.add('lead-deliverables__retry');
  body.append(list, status, more, retry);
  root.append(toggle, body);

  let target: LeadDeliverablesTarget | null = null;
  let items: LeadDeliverable[] = [];
  let cursor: string | null = null;
  let state: 'idle' | 'loading' | 'ok' | 'error' = 'idle';
  let flight: AbortController | null = null;
  let again = false;
  let expanded = false;

  const releasePush = ports.subscribe(frame => {
    if (frame?.kind === 'lead_deliverable' && target && frame.conversationId === target.leadId) void load(true);
  });
  const releaseReconnect = ports.onReconnect(() => { if (target) void load(true); });

  toggle.addEventListener('click', () => {
    expanded = !expanded;
    if (target) ports.writeExpanded?.(target.leadId, expanded);
    paint();
  });

  async function load(first: boolean) {
    if (!target) return;
    if (flight) {
      if (!first) return;
      again = true;
      return;
    }
    const reading = target;
    const controller = new AbortController();
    flight = controller;
    state = 'loading';
    paint();
    try {
      const page = await ports.read(reading, first ? undefined : cursor || undefined, controller.signal);
      if (flight !== controller || target !== reading) return;
      if (first) {
        // A re-read first page keeps the older pages already shown below it; item keys never move,
        // so the cursor after the oldest shown item stays valid.
        if (!items.length) cursor = page.nextCursor;
        items = appendDeliverables(page.items, items);
      } else {
        items = appendDeliverables(items, page.items);
        cursor = page.nextCursor;
      }
      state = 'ok';
    } catch (error) {
      if (controller.signal.aborted || flight !== controller) return;
      state = 'error';
      status.dataset.error = apiFailure(error).message;
    } finally {
      if (flight === controller) {
        flight = null;
        paint();
        if (again) { again = false; void load(true); }
      }
    }
  }

  function row(item: LeadDeliverable): HTMLLIElement {
    const li = document.createElement('li');
    li.className = 'lead-deliverable';
    li.dataset.deliverableId = item.id;
    li.dataset.kind = item.kind;
    if (item.reportKind) li.dataset.reportKind = item.reportKind;
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'lead-deliverable__open';
    const kind = document.createElement('span');
    kind.className = 'lead-deliverable__kind';
    kind.textContent = item.kind === 'report' && item.reportKind
      ? t(`leadDeliverables.reportKind.${item.reportKind}`) : t(`leadDeliverables.kind.${item.kind}`);
    const title = document.createElement('span');
    title.className = 'lead-deliverable__title';
    title.textContent = item.title;
    const meta = document.createElement('span');
    meta.className = 'lead-deliverable__meta';
    const project = deliverableProject(item.projectPath);
    const age = deliverableAge(item.createdAt, now(), document.documentElement.lang);
    meta.textContent = [project, age].filter(Boolean).join(' · ');
    if (item.projectPath) meta.title = item.projectPath;
    if (item.createdAt) open.title = `${item.title}\n${new Date(item.createdAt).toLocaleString(document.documentElement.lang || undefined)}`;
    open.append(kind, title, meta);
    open.addEventListener('click', () => { ports.opener.open(item); });
    li.append(open);
    if (item.files?.length) li.append(files(item));
    const source = sourceButton(item, ports.opener);
    if (source) li.append(source);
    return li;
  }

  function files(item: LeadDeliverable): HTMLElement {
    const group = document.createElement('div');
    group.className = 'lead-deliverable__files';
    group.setAttribute('role', 'list');
    group.setAttribute('aria-label', t('leadDeliverables.files'));
    for (const file of item.files || []) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'lead-deliverable__file';
      chip.setAttribute('role', 'listitem');
      chip.dataset.fileKind = file.kind;
      chip.textContent = file.filename;
      chip.title = file.path || file.filename;
      chip.setAttribute('aria-label', t('leadDeliverables.openFile', { name: file.filename }));
      chip.addEventListener('click', () => {
        ports.opener.open({ id: 'artifact:' + file.artifactId, kind: file.kind, title: file.filename,
          projectPath: item.projectPath, createdAt: item.createdAt, source: item.source, artifactId: file.artifactId });
      });
      group.append(chip);
    }
    return group;
  }

  function paint() {
    label.textContent = t('leadDeliverables.title');
    count.textContent = items.length ? (cursor ? `${items.length}+` : String(items.length)) : '';
    toggle.setAttribute('aria-expanded', String(expanded));
    body.hidden = !expanded;
    root.dataset.state = state;
    if (!expanded) return;
    const keep = new Map<string, HTMLLIElement>();
    for (const node of list.querySelectorAll<HTMLLIElement>(':scope > .lead-deliverable')) keep.set(node.dataset.deliverableId || '', node);
    list.replaceChildren(...items.map(item => keep.get(item.id) || row(item)));
    status.textContent = state === 'loading' && !items.length ? t('leadDeliverables.loading')
      : state === 'error' ? t('leadDeliverables.error')
        : state === 'ok' && !items.length ? t('leadDeliverables.empty') : '';
    status.hidden = !status.textContent;
    more.hidden = !(cursor && state === 'ok');
    retry.hidden = state !== 'error';
  }

  return {
    root,
    show(next) {
      if (target && target.leadId === next.leadId && target.path === next.path) {
        if (root.hidden) root.hidden = false;
        return;
      }
      flight?.abort();
      flight = null;
      again = false;
      target = { ...next };
      items = [];
      cursor = null;
      state = 'idle';
      // Open until the owner folds it: this is where they find what the lead sent them.
      expanded = ports.readExpanded?.(next.leadId) ?? true;
      root.hidden = false;
      root.dataset.leadId = next.leadId;
      list.replaceChildren();
      void load(true);
    },
    hide() { if (!root.hidden) root.hidden = true; },
    leadId: () => (root.hidden ? '' : target?.leadId || ''),
    destroy() {
      flight?.abort();
      releasePush();
      releaseReconnect();
      root.remove();
    },
  };
}
