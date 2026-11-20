/** Session-free task board pinned atop the bound Coordinator conversation.
 *
 * Reads as a todo list of the Coordinator-authored board (contract
 * `progressBoard` in contracts/project_coordination_v1.schema.json): one line
 * per task with a visible status, open work first and finished work folded. Waiting conditions and cancellation reasons are visible text. Pending by
 * itself only means the Coordinator has not marked the task started. A display translation
 * (`progressTranslation`) replaces each text it covers, and an "Original"
 * toggle shows the board as written. The head is one line (title, state,
 * summary, update time) that stays readable while the card is folded.
 * Exact positive assignment receipts from the latest and explicitly identified older requests may accompany
 * authored statuses; missing receipts remain unknown, never absence or completion.
 * Questions are ask_human cards in the chat. Tasks a later board omits
 * stay reachable through the footer's 历史任务 ("Task history") button
 * (coordinator-task-history.ts). Rendering reads nothing, acknowledges nothing
 * and starts no work.
 *
 * Each task expands to its own reports and files (owner request 2026-11-17):
 * with the deliverables helper attached (coordinator-task-deliverables.ts,
 * loaded on demand), a task row is a disclosure showing how many items name
 * that task, and opening it lists them. Tasks that left the board but have
 * deliverables keep their historical state, grouped separately from current
 * work. A linked delivery report and a board completion are distinct facts.
 * Unlinked items stay reachable under "Other reports and files".
 *
 * The card persists across renders: the owner's footer control (a Router's
 * Worker model) stays attached, and the fold, scroll and focus survive live
 * board updates.
 */
import { createButton, createDisclosureSummary } from '../ui';
import { t } from '../i18n';
import type {
  CoordinatorProgressBoard, CoordinatorProgressTask, CoordinatorProgressTranslation, CoordinatorProgressState,
} from '../api/project-coordination.generated';
import type { TaskDeliverables, TaskDeliverableSummary } from './coordinator-task-deliverables';
import { folderIcon } from './project/folder-icons';
import { coordinatorProgressHeadline, coordinatorProgressTally } from './coordinator-progress-summary';
import { deliverableAge } from './lead-deliverables/model';

/** The helper's key of the other group (coordinator-task-deliverables.ts OTHER_TASKS), kept here so the
 * card does not load the helper module. */
const OTHER_TASKS = 'other';

export type CoordinatorProgressView =
  | { kind: 'board'; board: CoordinatorProgressBoard; translation?: CoordinatorProgressTranslation;
      /** History identity of each board task, in board order (progressState.taskKeys). */
      taskKeys?: readonly string[]; workEvidence?: CoordinatorProgressState['workEvidence'] }
  | { kind: 'empty' }
  | { kind: 'unavailable' };

export interface CoordinatorProgressPorts {
  /** Initial fold of the card and of its finished tasks; the owner keeps both across cards. */
  open: boolean;
  finishedOpen: boolean;
  toggle(open: boolean): void;
  toggleFinished(open: boolean): void;
  /** Owner control kept attached at the start of the footer for the card's lifetime. */
  footer?: HTMLElement;
  now?(): number;
}

/** The owner's current callbacks, bound again on every render. */
export interface CoordinatorProgressActions {
  /** Opens every task the board ever showed; never opened implicitly. */
  history(): void;
  retry(): void;
  openWorker?(conversationId: string): void;
}

export interface CoordinatorProgressCard {
  el: HTMLDetailsElement;
  render(view: CoordinatorProgressView, actions: CoordinatorProgressActions): void;
  /** Make tasks expandable to their reports and files. */
  attachDeliverables(deliverables: TaskDeliverables): void;
  /** Repaint the last view, e.g. after the deliverables helper changed. */
  repaint(): void;
  invalidateEvidence(): boolean;
}

type Status = CoordinatorProgressTask['status'];
type RowStatus = Status | 'waiting' | 'limited' | 'dropped' | 'unknown';

const STATUS_LABEL = {
  in_progress: 'coordinatorProgress.status.in_progress',
  pending: 'coordinatorProgress.status.pending',
  waiting: 'coordinatorProgress.status.waiting',
  limited: 'coordinatorProgress.status.limited',
  completed: 'coordinatorProgress.status.completed',
  cancelled: 'coordinatorProgress.status.cancelled',
  dropped: 'coordinatorHistory.group.dropped',
  unknown: 'coordinatorProgress.status.unknown',
} as const satisfies Record<RowStatus, string>;

const OPEN: ReadonlySet<RowStatus> = new Set(['in_progress', 'pending', 'waiting', 'limited', 'dropped', 'unknown']);
const SVG_NS = 'http://www.w3.org/2000/svg';

function updatedLabel(updatedAt: number, now: number): string {
  const when = new Date(updatedAt);
  const sameDay = when.toDateString() === new Date(now).toDateString();
  const time = new Intl.DateTimeFormat(document.documentElement.lang || undefined, sameDay
    ? { hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(when);
  const age = deliverableAge(updatedAt, now, document.documentElement.lang);
  return t('coordinatorProgress.updated', { time: age ? `${time} · ${age}` : time });
}

/* Open work is a ring, half filled while in progress; finished work is a
 * filled disc with its mark cut out, a check when done and a cross when dropped. */
export function coordinatorProgressGlyph(status: RowStatus): HTMLSpanElement {
  const mark = document.createElement('span');
  mark.className = 'coordinator-progress-glyph';
  mark.setAttribute('role', 'img');
  mark.setAttribute('aria-label', t(STATUS_LABEL[status]));
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const shape = (name: string, attributes: Record<string, string>) => {
    const node = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    svg.append(node);
  };
  if (OPEN.has(status)) {
    shape('circle', { cx: '8', cy: '8', r: '6.25', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5' });
    if (status === 'in_progress') shape('path', { d: 'M8 4.25a3.75 3.75 0 0 1 0 7.5z', fill: 'currentColor' });
    if (status === 'waiting' || status === 'limited' || status === 'dropped') shape('path', {
      d: status === 'waiting' ? 'M8 4.5v3.75l2.5 1.5' : status === 'limited' ? 'M5 6.5h6M5 9.5h6' : 'M5 8h6',
      fill: 'none', stroke: 'currentColor', 'stroke-width': '1.4', 'stroke-linecap': 'round',
    });
  } else {
    shape('circle', { cx: '8', cy: '8', r: '7', fill: 'currentColor' });
    shape('path', {
      class: 'coordinator-progress-glyph-cut', fill: 'none', 'stroke-width': '1.6',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      d: status === 'completed' ? 'M5 8.25l2 2 4-4.25' : 'M5.75 5.75l4.5 4.5m0-4.5-4.5 4.5',
    });
  }
  mark.append(svg);
  return mark;
}

/** Expandable row content: the task's line becomes a disclosure button with its item count. */
function expandable(row: HTMLLIElement, line: Node[], key: string, label: string, qualifiers: string[], deliverables: TaskDeliverables) {
  const count = deliverables.count(key);
  if (!count) {
    row.append(...line);
    return;
  }
  const open = deliverables.expanded(key);
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'coordinator-progress-task-toggle';
  toggle.setAttribute('aria-expanded', String(open));
  toggle.dataset.task = key;
  const badge = document.createElement('span');
  badge.className = 'coordinator-progress-task-count';
  badge.textContent = String(count);
  badge.title = t('coordinatorProgress.deliverables', { count });
  toggle.setAttribute('aria-label', [label, ...qualifiers, badge.title].join(' · '));
  // The archive toggle's chevron; CSS turns it right while folded.
  const chevron = folderIcon('down');
  chevron.classList.add('coordinator-progress-task-chevron');
  chevron.setAttribute('width', '12'); chevron.setAttribute('height', '12'); chevron.setAttribute('stroke-width', '2.5');
  toggle.append(...line, badge, chevron);
  toggle.addEventListener('click', () => deliverables.toggle(key));
  // Reading starts as the pointer or focus arrives, so the list is usually there by the click.
  toggle.addEventListener('pointerenter', () => deliverables.prefetch(key));
  toggle.addEventListener('focus', () => deliverables.prefetch(key));
  row.append(toggle);
  row.dataset.expandable = 'true';
  if (open) {
    const region = deliverables.region(key);
    region.setAttribute('aria-label', label);
    row.append(region);
  }
}

type Blocker = NonNullable<CoordinatorProgressTask['blocker']>;

/** A terminal limit: the external resource cannot be obtained now. It is never a wait. */
export function coordinatorTaskLimit(blocker?: Blocker): boolean {
  return blocker?.kind === 'needs_external_resource' && 'unavailable' in blocker && blocker.unavailable === true;
}

/** The typed wait or limit of a pending row in the human's words; an id alone stays in the status badge. */
export function coordinatorBlockerText(blocker?: Blocker): string | undefined {
  if (!blocker) return undefined;
  if (blocker.kind === 'needs_external_resource') {
    return 'unavailable' in blocker
      ? `${blocker.resource} — ${blocker.limit}`
      : `${blocker.resource} — ${blocker.check}${'ownerAction' in blocker ? ` · ${blocker.ownerAction}` : ''}`;
  }
  if (blocker.kind === 'needs_owner_decision') return blocker.question;
  if (blocker.kind === 'next_action') return blocker.action;
  return undefined;
}

export function coordinatorTaskStatus(status: Status, reason?: string, blocker?: Blocker): Status | 'waiting' | 'limited' {
  if (coordinatorTaskLimit(blocker)) return 'limited';
  if (status !== 'pending') return status;
  return reason || blocker ? 'waiting' : status;
}

/** Never use an ordinary progress update as a removal or cancellation reason. */
export function coordinatorTaskExplanation(status: RowStatus, reason?: string, progress?: string,
  blocker?: Blocker): string | undefined {
  if (status === 'limited') {
    const limit = [coordinatorBlockerText(blocker), reason].filter(Boolean).join(' — ');
    return t('coordinatorProgress.limitReason', { reason: limit });
  }
  const wait = status === 'waiting' ? [coordinatorBlockerText(blocker), reason].filter(Boolean).join(' — ') : '';
  if (wait) return t('coordinatorProgress.waitReason', { reason: wait });
  if (status === 'dropped') return t('coordinatorHistory.removalUnknown');
  if (status === 'cancelled') return reason
    ? t('coordinatorProgress.cancelReason', { reason }) : t('coordinatorProgress.cancelUnknown');
  if (status === 'pending') return [t('coordinatorProgress.startUnknown'), progress].filter(Boolean).join(' ');
  return progress;
}

function taskRow(title: string, status: RowStatus | undefined, deliverables: TaskDeliverables | null,
  { key, progress, reason, blocker, written, hasDeliveryReport }: { key?: string; progress?: string; reason?: string; blocker?: Blocker; written?: string; hasDeliveryReport?: boolean } = {},
): HTMLLIElement {
  const row = document.createElement('li');
  row.className = 'coordinator-progress-task';
  row.dataset.status = status ?? 'other';
  if (key) row.dataset.taskKey = key;
  const text = document.createElement('span');
  text.className = 'coordinator-progress-text';
  const heading = document.createElement('span');
  heading.className = 'coordinator-progress-task-line';
  const name = document.createElement('span');
  name.className = 'coordinator-progress-task-title';
  name.textContent = title;
  if (written && written !== title) name.title = written;
  heading.append(name);
  const qualifiers: string[] = [];
  if (status) {
    const badge = document.createElement('span');
    badge.className = 'coordinator-progress-task-status';
    badge.textContent = t(STATUS_LABEL[status]);
    qualifiers.push(badge.textContent);
    heading.append(badge);
    if (status === 'completed' && hasDeliveryReport !== undefined) {
      const evidence = document.createElement('span');
      evidence.className = 'coordinator-progress-task-evidence';
      evidence.textContent = t(hasDeliveryReport ? 'coordinatorProgress.deliveryLinked' : 'coordinatorProgress.noDeliveryReport');
      qualifiers.push(evidence.textContent);
      heading.append(evidence);
    }
  }
  text.append(heading);
  const explanation = status ? coordinatorTaskExplanation(status, reason, progress, blocker) : progress;
  if (explanation) {
    const note = document.createElement('span');
    note.className = 'coordinator-progress-task-note';
    note.textContent = explanation;
    if (status === 'in_progress') note.title = explanation;
    else if (status === 'completed') row.title = explanation;
    else qualifiers.push(explanation);
    text.append(note);
  }
  const mark = status && status !== 'unknown' ? coordinatorProgressGlyph(status) : document.createElement('span');
  if (!mark.className) mark.className = 'coordinator-progress-glyph coordinator-progress-glyph--history';
  // The visible text and the disclosure's accessible name both state the status.
  mark.setAttribute('aria-hidden', 'true');
  mark.removeAttribute('role');
  mark.removeAttribute('aria-label');
  const line = [mark, text];
  if (key && deliverables) expandable(row, line, key, title, qualifiers, deliverables);
  else row.append(...line);
  return row;
}

function taskList(rows: HTMLLIElement[]): HTMLOListElement {
  const list = document.createElement('ol');
  list.className = 'coordinator-progress-tasks';
  list.append(...rows);
  return list;
}

function taskGroup(className: string, label: string, rows: HTMLLIElement[], open: boolean,
  remember: (open: boolean) => void): HTMLDetailsElement {
  const fold = document.createElement('details');
  fold.className = `coordinator-progress-group ${className}`;
  fold.open = open;
  fold.addEventListener('toggle', () => remember(fold.open));
  fold.append(createDisclosureSummary(document, label), taskList(rows));
  return fold;
}

export function createCoordinatorProgress(ports: CoordinatorProgressPorts): CoordinatorProgressCard {
  let finishedOpen = ports.finishedOpen, original = false, earlierOpen = false, droppedOpen = false;
  let last: [CoordinatorProgressView, CoordinatorProgressActions] | null = null;
  let deliverables: TaskDeliverables | null = null;

  const card = document.createElement('details');
  card.className = 'coordinator-progress';
  card.open = ports.open;
  card.addEventListener('toggle', () => ports.toggle(card.open));
  const head = createDisclosureSummary(document);
  head.classList.add('coordinator-progress-head');
  const body = document.createElement('div');
  body.className = 'coordinator-progress-body';
  const content = document.createElement('div');
  content.className = 'coordinator-progress-content';
  // Footer nodes are never moved by a render, so a focused or open control stays live.
  const actions = document.createElement('div');
  actions.className = 'coordinator-progress-actions';
  const commands = document.createElement('span');
  commands.className = 'coordinator-progress-commands';
  const originalToggle = createButton({ label: t('chat.bilingualOriginal'), size: 'sm', variant: 'ghost', onClick: () => {
    original = !original;
    if (last) render(...last);
  } });
  const commandSlot = document.createElement('span');
  commandSlot.className = 'coordinator-progress-command-slot';
  commands.append(originalToggle, commandSlot);
  if (ports.footer) actions.append(ports.footer);
  actions.append(commands);
  body.append(content, actions);
  card.append(head, body);

  function taskGroups(tasks: readonly CoordinatorProgressTask[], keys: readonly string[], say: (text: string) => string) {
    const current = new Set(keys);
    const earlier = deliverables?.tasks().filter(task => !current.has(task.key)) ?? [];
    const rows: Record<RowStatus, HTMLLIElement[]> = {
      in_progress: [], pending: [], waiting: [], limited: [], completed: [], cancelled: [], dropped: [], unknown: [],
    };
    const reportByKey = new Map(deliverables?.tasks().map(task => [task.key, task.hasDeliveryReport]));
    tasks.forEach((task, index) => {
      const key = keys[index];
      const status = coordinatorTaskStatus(task.status, task.reason, task.blocker);
      rows[status].push(taskRow(say(task.title), status, deliverables, {
        key, progress: task.progress ? say(task.progress) : undefined, reason: task.reason ? say(task.reason) : undefined,
        blocker: task.blocker, hasDeliveryReport: reportByKey.get(key),
      }));
    });
    const archived = (task: TaskDeliverableSummary) => {
      const status = task.archivedGroup ?? 'unknown';
      rows[status].push(taskRow(original ? task.title : task.translation?.title ?? task.title, status, deliverables, {
        key: task.key, written: task.title, hasDeliveryReport: task.hasDeliveryReport,
        reason: original ? task.reason : task.translation?.reason ?? task.reason,
      }));
    };
    earlier.forEach(archived);
    const nodes: Node[] = [];
    const open = [...rows.in_progress, ...rows.waiting, ...rows.limited, ...rows.pending];
    if (open.length) nodes.push(taskList(open));
    if (rows.dropped.length) {
      const label = t('coordinatorHistory.chip', { label: t(STATUS_LABEL.dropped), n: rows.dropped.length });
      nodes.push(taskGroup('coordinator-progress-dropped', label, rows.dropped, droppedOpen, open => { droppedOpen = open; }));
    }
    const finished = [...rows.completed, ...rows.cancelled];
    if (finished.length) nodes.push(taskGroup('coordinator-progress-finished', t('coordinatorProgress.finished', { count: finished.length }),
      finished, finishedOpen, open => { finishedOpen = open; ports.toggleFinished(open); }));
    if (rows.unknown.length) nodes.push(taskGroup('coordinator-progress-earlier', t('coordinatorProgress.earlier', { count: rows.unknown.length }),
      rows.unknown, earlierOpen, open => { earlierOpen = open; }));
    if (deliverables?.count(OTHER_TASKS)) {
      const list = taskList([taskRow(t('coordinatorProgress.other'), undefined, deliverables, { key: OTHER_TASKS })]);
      list.classList.add('coordinator-progress-other');
      nodes.push(list);
    }
    return { nodes, translated: earlier.some(task => task.translation &&
      (task.translation.title !== task.title || (task.translation.reason && task.translation.reason !== task.reason))) };
  }

  function render(view: CoordinatorProgressView, actionPorts: CoordinatorProgressActions) {
    last = [view, actionPorts];
    card.dataset.state = view.kind;
    const title = document.createElement('span');
    title.className = 'coordinator-progress-title';
    title.textContent = t('coordinatorProgress.title');
    const headNodes: Node[] = [title];
    const headline = (text: string) => {
      const line = document.createElement('span');
      line.className = 'coordinator-progress-summary';
      line.textContent = text;
      headNodes.push(line);
      return line;
    };
    const contentNodes: Node[] = [];
    const commandNodes: Node[] = [];
    let translated = false;

    if (view.kind === 'unavailable') {
      headline(t('coordinatorProgress.unavailable')).setAttribute('role', 'status');
      commandNodes.push(createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: actionPorts.retry }));
    } else if (view.kind === 'empty') {
      headline(t('coordinatorProgress.empty'));
    } else {
      const { board, translation } = view;
      translated = !!translation?.texts.some(entry => entry.text !== entry.source);
      const shown = translated && !original && translation
        ? new Map(translation.texts.map(entry => [entry.source, entry.text])) : null;
      const say = (text: string) => shown?.get(text) ?? text;
      const state = coordinatorProgressHeadline(board);
      if (state) {
        const tally = document.createElement('span');
        tally.className = 'coordinator-progress-tally';
        const { active, pending, waiting } = coordinatorProgressTally(board);
        tally.textContent = [state,
          ...(active && waiting ? [t('coordinatorProgress.sidebar.waiting', { count: waiting })] : []),
          ...((active || waiting) && pending ? [t('coordinatorProgress.sidebar.pending', { count: pending })] : []),
        ].join(' · ');
        headNodes.push(tally);
      }
      headline(say(board.summary));
      const updated = document.createElement('time');
      updated.className = 'coordinator-progress-updated';
      updated.dateTime = new Date(board.updatedAt).toISOString();
      updated.textContent = updatedLabel(board.updatedAt, ports.now?.() ?? Date.now());
      headNodes.push(updated);

      const keys = view.taskKeys?.length === board.tasks.length ? view.taskKeys : [];
      const groups = taskGroups(board.tasks, keys, say);
      const seenAssignments = new Set<string>();
      const receipts = view.workEvidence ? [view.workEvidence, ...(view.workEvidence.additionalRequests ?? [])]
        .flatMap(request => request.rows.filter(receipt => {
          if (seenAssignments.has(receipt.assignmentId)) return false;
          seenAssignments.add(receipt.assignmentId);
          return true;
        }).map(receipt => ({ ...receipt, requestId: request.requestId, sourceTurnId: request.sourceTurnId }))) : [];
      const coverage = document.createElement('small');
      coverage.className = 'coordinator-progress-evidence-scope';
      // A read request proves dispatch data was readable; only an unread one may claim it is missing.
      coverage.append(document.createTextNode(receipts.length
        ? `${t('projectCoordination.graph.assignmentCoverage')} ${t('projectCoordination.graph.bounded')}`
        : view.workEvidence?.requestId ? t('projectCoordination.graph.progressUnknown')
          : t('projectCoordination.graph.assignmentsUnavailable')));
      const scope = document.createElement('code');
      if (view.workEvidence?.requestId) scope.dataset.requestId = view.workEvidence.requestId;
      scope.textContent = JSON.stringify({ scope: 'latest_request', coverage: 'unknown',
        requestId: view.workEvidence?.requestId ?? null, sourceTurnId: view.workEvidence?.sourceTurnId ?? null });
      coverage.append(document.createTextNode(' '), scope);
      contentNodes.push(...groups.nodes, coverage);
      // A per-task line is the contrast against observed receipts; with none the
      // coverage line below states it once instead of on every row.
      const receiptRows = receipts.length
        ? groups.nodes.flatMap(node => node instanceof Element ? [...node.querySelectorAll<HTMLLIElement>('.coordinator-progress-task')] : [])
        : [];
      for (const row of receiptRows) {
        const key = row.dataset.taskKey;
        if (!key) continue;
        const taskReceipts = receipts.filter(receipt => receipt.taskKeys.includes(key));
        const facts = document.createElement('span');
        facts.className = 'coordinator-progress-live-receipt';
        if (!taskReceipts.length) facts.textContent = t('projectCoordination.graph.progressUnknown');
        for (const receipt of taskReceipts) {
          const fact = document.createElement('span');
          fact.dataset.assignmentId = receipt.assignmentId;
          if (receipt.requestId) fact.dataset.requestId = receipt.requestId;
          const labels = {
            queued: 'projectCoordination.graph.status.queued', active: 'projectCoordination.graph.status.running',
            awaiting_review: 'projectCoordination.graph.dependency.waiting_review',
            awaiting_delivery: 'projectCoordination.graph.dependency.waiting_delivery',
            delivered: 'projectCoordination.graph.deliveryConfirmed', held: 'projectCoordination.graph.status.blocked',
            ended: 'projectCoordination.graph.status.unknown', unknown: 'projectCoordination.graph.status.unknown',
          } as const;
          fact.textContent = `${t('projectCoordination.graph.assignmentCoverage')} · ${t(labels[receipt.phase])}`;
          const identity = document.createElement('code');
          if (receipt.requestId) identity.dataset.requestId = receipt.requestId;
          identity.textContent = JSON.stringify({ requestId: receipt.requestId, sourceTurnId: receipt.sourceTurnId,
            assignmentId: receipt.assignmentId, phase: receipt.phase });
          fact.append(document.createTextNode(' '), identity);
          facts.append(fact);
          if (actionPorts.openWorker) facts.append(createButton({
            label: t('projectCoordination.openWorker'), size: 'sm', variant: 'ghost',
            onClick: () => actionPorts.openWorker?.(receipt.conversationId),
          }));
        }
        row.append(facts);
      }
      translated ||= groups.translated;
    }
    if (view.kind === 'empty') {
      const groups = taskGroups([], [], text => text);
      contentNodes.push(...groups.nodes);
      translated = groups.translated;
    }
    commandNodes.push(createButton({ label: t('coordinatorProgress.history'), size: 'sm', variant: 'ghost', onClick: actionPorts.history }));
    originalToggle.textContent = t('chat.bilingualOriginal');
    originalToggle.hidden = !translated;
    originalToggle.setAttribute('aria-pressed', String(translated && original));
    // A task toggle is rebuilt by every paint; keep keyboard focus on the same task's toggle.
    const focusedTask = document.activeElement instanceof HTMLElement && content.contains(document.activeElement)
      ? document.activeElement.closest<HTMLElement>('.coordinator-progress-task-toggle')?.dataset.task : undefined;
    head.replaceChildren(...headNodes);
    content.replaceChildren(...contentNodes);
    if (focusedTask) {
      content.querySelector<HTMLElement>(`.coordinator-progress-task-toggle[data-task="${focusedTask}"]`)?.focus();
    }
    commandSlot.replaceChildren(...commandNodes);
  }

  return {
    el: card, render,
    attachDeliverables(helper) {
      deliverables = helper;
      if (last) render(...last);
    },
    repaint() { if (last) render(...last); },
    invalidateEvidence() {
      if (last?.[0].kind !== 'board' || !last[0].workEvidence) return false;
      last = [{ ...last[0], workEvidence: undefined }, last[1]];
      for (const receipt of content.querySelectorAll('.coordinator-progress-live-receipt')) receipt.remove();
      const coverage = content.querySelector('.coordinator-progress-evidence-scope');
      if (coverage) coverage.textContent = t('projectCoordination.graph.assignmentsUnavailable');
      return true;
    },
  };
}
