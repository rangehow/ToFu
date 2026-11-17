/** Lead deliverables in the classic conversation view: the reports and files section of the right-hand panel.
 *
 * Owner request 2026-11-17: the section lives beside the lead's conversation, in the column that shows
 * what the round ran with (the per-turn context rail's card; on a pane too narrow for that column, in
 * the round's context fold), not in the left sidebar. Presence mounts it there while a lead's
 * conversation is open (runtime/sections/presence.js); this module owns only the section.
 *
 * It is what the lead sent the owner: every report (delivery, progress, decision, blocked) with the files
 * its call attached, and files published on their own. It reads the lead's projection page by page,
 * newest first (`leadDeliverables`), and appends older pages on demand, so a list of any length is
 * complete, never truncated; once the items span more than one day they group under day headings. Each
 * row shows a type glyph (deck, PDF, document, report kind...), the whole title (wrapped, never
 * clipped), then its kind in the glyph's ink, project and age (the project moves to the heading when
 * every item shares one), and previews the report's opening paragraph on hover;
 * clicking it opens the deliverable itself through the shared opener, each attached file opens from its
 * chip, and the row's secondary control goes to the lead's message at the exact Turn. The section is
 * open in the rail until the owner folds it, folded by default in the narrow in-flow fold. A
 * `lead_deliverable` project push for the shown lead, a push reconnect and showing another lead re-read
 * the first page; a language switch repaints every label; nothing polls. The server scopes every read
 * to the owner.
 * Entry point: createLeadDeliverables. Dependencies: ports for the read, push and opener; i18n; ui; icons.
 */
import { t } from '../../i18n';
import { createButton } from '../../ui';
import { apiFailure } from '../../api/errors';
import { iconHtml } from '../../icons';
import {
  appendDeliverables, deliverableAge, deliverableCopy, deliverableDayLabel, deliverableDays, deliverablePreview, deliverableProject,
  deliverableVisual, type DeliverableVisual, type LeadDeliverable, type LeadDeliverablePage,
} from './model';
import { type DeliverableOpener } from './open';
import '../../styles/application/36-lead-deliverables.css';

export interface LeadDeliverablesTarget { leadId: string; path: string }
export interface LeadDeliverablesPushFrame { kind?: unknown; conversationId?: unknown }
/** `rail`: the context column's card beside the transcript; `fold`: in flow under the round's header. */
export type LeadDeliverablesPlacement = 'rail' | 'fold';

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
  /** Where the section is mounted; an unfolded/folded choice of the owner wins over the placement's default. */
  place(placement: LeadDeliverablesPlacement): void;
  hide(): void;
  leadId(): string;
  destroy(): void;
}

const GLYPHS: Readonly<Record<DeliverableVisual, string>> = {
  delivery: 'checkCircle', progress: 'chart', decision: 'messageCircle', blocked: 'alertTriangle', report: 'inbox',
  deck: 'slides', pdf: 'file', sheet: 'fileSheet', image: 'image', document: 'file', file: 'fileCode', merge: 'gitMerge',
};

function badgeKey(item: LeadDeliverable, visual: DeliverableVisual) {
  if (item.kind === 'report' && item.reportKind) return `leadDeliverables.reportKind.${item.reportKind}` as const;
  if (visual === 'pdf') return 'leadDeliverables.kind.pdf' as const;
  return `leadDeliverables.kind.${item.kind}` as const;
}

export function createLeadDeliverables(ports: LeadDeliverablesPorts): LeadDeliverables {
  const now = ports.now || (() => Date.now());
  const locale = () => document.documentElement.lang || undefined;
  const root = document.createElement('section');
  root.className = 'lead-deliverables';
  root.hidden = true;
  root.dataset.conversationPart = 'lead-deliverables';
  const heading = document.createElement('div');
  heading.className = 'lead-deliverables__heading';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'lead-deliverables__toggle';
  const glyph = document.createElement('span');
  glyph.className = 'lead-deliverables__glyph';
  glyph.setAttribute('aria-hidden', 'true');
  glyph.innerHTML = iconHtml('inbox');
  const label = document.createElement('span');
  label.className = 'lead-deliverables__label';
  const count = document.createElement('span');
  count.className = 'lead-deliverables__count';
  // The one project every listed item belongs to, named once here instead of on every row.
  const scope = document.createElement('span');
  scope.className = 'lead-deliverables__scope';
  const caret = document.createElement('span');
  caret.className = 'lead-deliverables__caret';
  caret.setAttribute('aria-hidden', 'true');
  toggle.append(glyph, label, count, scope, caret);
  heading.append(toggle);
  const body = document.createElement('div');
  body.className = 'lead-deliverables__body';
  body.id = `lead-deliverables-${Math.random().toString(36).slice(2)}`;
  toggle.setAttribute('aria-controls', body.id);
  const days = document.createElement('div');
  days.className = 'lead-deliverables__days';
  const status = document.createElement('p');
  status.className = 'lead-deliverables__status';
  status.setAttribute('role', 'status');
  const more = createButton({ label: t('leadDeliverables.more'), variant: 'ghost', size: 'sm', onClick: () => void load(false) });
  more.classList.add('lead-deliverables__more');
  const retry = createButton({ label: t('leadDeliverables.retry'), variant: 'ghost', size: 'sm', onClick: () => void load(!items.length) });
  retry.classList.add('lead-deliverables__retry');
  body.append(days, status, more, retry);
  root.append(heading, body);

  let target: LeadDeliverablesTarget | null = null;
  let items: LeadDeliverable[] = [];
  let cursor: string | null = null;
  let state: 'idle' | 'loading' | 'ok' | 'error' = 'idle';
  let flight: AbortController | null = null;
  let again = false;
  let expanded = false;
  let placement: LeadDeliverablesPlacement = 'rail';
  const rows = new Map<string, HTMLLIElement>();

  const releasePush = ports.subscribe(frame => {
    if (frame?.kind === 'lead_deliverable' && target && frame.conversationId === target.leadId) void load(true);
  });
  const releaseReconnect = ports.onReconnect(() => { if (target) void load(true); });
  // Rows are kept across paints, so a language switch rebuilds them and relabels the fixed controls.
  const relabel = () => {
    more.textContent = t('leadDeliverables.more');
    retry.textContent = t('leadDeliverables.retry');
    rows.clear();
    paint();
  };
  window.addEventListener('tofu:language-change', relabel);

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
    const visual = deliverableVisual(item);
    const li = document.createElement('li');
    li.className = 'lead-deliverable';
    li.dataset.deliverableId = item.id;
    li.dataset.kind = item.kind;
    li.dataset.visual = visual;
    if (item.reportKind) li.dataset.reportKind = item.reportKind;
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'lead-deliverable__open';
    open.title = deliverablePreview(item, locale());
    const icon = document.createElement('span');
    icon.className = 'lead-deliverable__icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = iconHtml(GLYPHS[visual]);
    const text = document.createElement('span');
    text.className = 'lead-deliverable__text';
    const title = document.createElement('span');
    title.className = 'lead-deliverable__title';
    const copy = deliverableCopy(item);
    title.textContent = copy.title;
    const meta = document.createElement('span');
    meta.className = 'lead-deliverable__meta';
    const kind = document.createElement('span');
    kind.className = 'lead-deliverable__kind';
    kind.textContent = t(badgeKey(item, visual));
    const project = document.createElement('span');
    project.className = 'lead-deliverable__project';
    project.textContent = deliverableProject(item.projectPath);
    project.hidden = !item.projectPath;
    if (item.projectPath) project.title = item.projectPath;
    const age = document.createElement('span');
    age.className = 'lead-deliverable__age';
    age.textContent = deliverableAge(item.createdAt, now(), locale());
    meta.append(kind, project, age);
    text.append(title, meta);
    open.append(icon, text);
    open.addEventListener('click', () => { ports.opener.open(item); });
    li.append(open);
    if (item.source) {
      const source = document.createElement('button');
      source.type = 'button';
      source.className = 'lead-deliverable__source';
      source.setAttribute('aria-label', t('leadDeliverables.source'));
      source.title = t('leadDeliverables.source');
      source.innerHTML = iconHtml('arrowRight');
      source.addEventListener('click', event => {
        event.stopPropagation();
        ports.opener.openSource(item);
      });
      li.append(source);
    }
    if (item.files?.length) li.append(files(item));
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
      const name = document.createElement('span');
      name.textContent = file.filename;
      chip.innerHTML = iconHtml(file.kind === 'deck' ? 'slides' : 'file');
      chip.append(name);
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
    count.hidden = !items.length;
    const paths = new Set(items.map(item => item.projectPath));
    const shared = paths.size === 1 ? items[0].projectPath : '';
    const scopeName = document.createElement('span');
    scopeName.textContent = shared ? deliverableProject(shared) : '';
    scope.innerHTML = shared ? iconHtml('folder') : '';
    scope.append(scopeName);
    scope.title = shared;
    scope.hidden = !shared;
    // Rows name their project only while the list spans more than one.
    root.dataset.sharedProject = String(Boolean(shared));
    toggle.setAttribute('aria-expanded', String(expanded));
    body.hidden = !expanded;
    root.dataset.state = state;
    root.dataset.placement = placement;
    if (!expanded) return;
    const grouped = deliverableDays(items);
    // Day headings only once there is more than one day to tell apart.
    const headed = grouped.length > 1;
    const keep = new Set(items.map(item => item.id));
    for (const id of [...rows.keys()]) if (!keep.has(id)) rows.delete(id);
    days.replaceChildren(...grouped.map(day => {
      const group = document.createElement('section');
      group.className = 'lead-deliverables__day';
      if (headed && day.start) {
        const heading = document.createElement('h4');
        heading.className = 'lead-deliverables__day-label';
        heading.textContent = deliverableDayLabel(day.start, now(), locale());
        group.append(heading);
      }
      const list = document.createElement('ul');
      list.className = 'lead-deliverables__list';
      list.setAttribute('role', 'list');
      list.append(...day.items.map(item => {
        let node = rows.get(item.id);
        if (!node) { node = row(item); rows.set(item.id, node); }
        return node;
      }));
      group.append(list);
      return group;
    }));
    status.textContent = state === 'loading' && !items.length ? t('leadDeliverables.loading')
      : state === 'error' ? t('leadDeliverables.error')
        : state === 'ok' && !items.length ? t('leadDeliverables.empty') : '';
    status.dataset.tone = state === 'error' ? 'error' : state === 'loading' ? 'loading' : 'quiet';
    status.hidden = !status.textContent;
    more.hidden = !(cursor && state === 'ok');
    retry.hidden = state !== 'error';
  }

  function chosen(): boolean | null {
    return target ? ports.readExpanded?.(target.leadId) ?? null : null;
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
      rows.clear();
      state = 'idle';
      // Open in the rail until the owner folds it: this is where they find what the lead sent them.
      expanded = chosen() ?? placement === 'rail';
      root.hidden = false;
      root.dataset.leadId = next.leadId;
      days.replaceChildren();
      void load(true);
    },
    place(next) {
      if (placement === next) return;
      placement = next;
      expanded = chosen() ?? placement === 'rail';
      paint();
    },
    hide() { if (!root.hidden) root.hidden = true; },
    leadId: () => (root.hidden ? '' : target?.leadId || ''),
    destroy() {
      flight?.abort();
      releasePush();
      releaseReconnect();
      window.removeEventListener('tofu:language-change', relabel);
      root.remove();
    },
  };
}
