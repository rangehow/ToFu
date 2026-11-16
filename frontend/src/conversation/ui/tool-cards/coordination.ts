/**
 * Purpose-built presenters for the Coordinator's project tools: delegation,
 * task board, review/merge, waiting on Workers, assignment reads, reports,
 * finish/cancel, conversation control and project messages.
 * Every word comes from `context.t`; every name of project work goes through
 * the work directory, so a card shows task titles and keeps IDs in its
 * technical details. Entry point: COORDINATION_TOOL_PRESENTERS.
 */
import type { I18nKey, I18nParamsFor } from '../../../i18n';

/** A key whose copy takes no parameters, so a table can hold it. */
type PlainKey = { [K in I18nKey]: [I18nParamsFor<K>] extends [never] ? K : never }[I18nKey];

import type {
  ToolChip, ToolChipTone, ToolPresenter, ToolPresenterContext, ToolSummary,
} from './contracts';
import { genericFieldsHtml } from './generic';
import {
  checkRowHtml, chipHtml, collapsibleHtml, escape, fieldHtml, leadHtml, listHtml,
  openConversationHtml, sectionHtml, type MarkKind,
} from './markup';
import { CHECKLIST_ICONS } from '../checklist-card/items';
import { count, isRecord, list, record, text, type ToolView, type UnknownRecord } from './values';

type Labelled = readonly [PlainKey, ToolChipTone];

const EXECUTION_STATUS: Readonly<Record<string, Labelled>> = Object.freeze({
  queued: ['toolCard.status.queued', 'muted'],
  starting: ['toolCard.status.starting', 'info'],
  running: ['toolCard.status.running', 'info'],
  completed: ['toolCard.status.completed', 'ok'],
  failed: ['toolCard.status.failed', 'err'],
  failed_to_start: ['toolCard.status.failedToStart', 'err'],
  cancelled: ['toolCard.status.cancelled', 'muted'],
  interrupted: ['toolCard.status.interrupted', 'warn'],
});

const DELIVERY_STATUS: Readonly<Record<string, Labelled>> = Object.freeze({
  working: ['toolCard.delivery.working', 'muted'],
  pending: ['toolCard.delivery.pending', 'info'],
  delivered: ['toolCard.delivery.delivered', 'ok'],
  blocked: ['toolCard.delivery.blocked', 'err'],
  held: ['toolCard.delivery.held', 'warn'],
});

const VERDICTS: Readonly<Record<string, Labelled>> = Object.freeze({
  accept: ['toolCard.review.accept', 'ok'],
  rework: ['toolCard.review.rework', 'warn'],
  reject: ['toolCard.review.reject', 'err'],
});

const CHECK_MARKS: Readonly<Record<string, readonly [MarkKind, PlainKey]>> = Object.freeze({
  met: ['met', 'toolCard.check.met'],
  unmet: ['unmet', 'toolCard.check.unmet'],
  unknown: ['unknown', 'toolCard.check.unknown'],
});

const REPORT_KINDS: Readonly<Record<string, Labelled>> = Object.freeze({
  progress: ['toolCard.report.progress', 'info'],
  decision: ['toolCard.report.decision', 'warn'],
  blocked: ['toolCard.report.blocked', 'err'],
  delivery: ['toolCard.report.delivery', 'ok'],
});

const FINISH_OUTCOMES: Readonly<Record<string, Labelled>> = Object.freeze({
  complete: ['toolCard.finish.complete', 'ok'],
  needs_user: ['toolCard.finish.needsUser', 'warn'],
});

const CONTROL_ACTIONS: Readonly<Record<string, PlainKey>> = Object.freeze({
  continue: 'toolCard.control.continue',
  checkpoint_resume: 'toolCard.control.checkpointResume',
  regenerate: 'toolCard.control.regenerate',
  stop: 'toolCard.control.stop',
  follow_up: 'toolCard.control.followUp',
  set_model: 'toolCard.control.setModel',
});

const MESSAGE_KINDS: Readonly<Record<string, PlainKey>> = Object.freeze({
  reference: 'toolCard.message.reference',
  consult: 'toolCard.message.consult',
  report: 'toolCard.message.report',
  question: 'toolCard.message.question',
  assign: 'toolCard.message.assign',
  steer: 'toolCard.message.steer',
});

const MESSAGE_DECISIONS: Readonly<Record<string, Labelled>> = Object.freeze({
  accepted: ['toolCard.message.accepted', 'ok'],
  rejected: ['toolCard.message.rejected', 'err'],
  deferred: ['toolCard.message.deferred', 'muted'],
  conflict: ['toolCard.message.conflict', 'warn'],
});

const TASK_STATES: Readonly<Record<string, readonly [string, PlainKey, string]>> = Object.freeze({
  completed: ['completed', 'toolCard.task.completed', CHECKLIST_ICONS.completed],
  in_progress: ['in_progress', 'toolCard.task.inProgress', CHECKLIST_ICONS.in_progress],
  pending: ['pending', 'toolCard.task.pending', CHECKLIST_ICONS.pending],
  cancelled: ['cancelled', 'toolCard.task.cancelled',
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M8 12h8"/></svg>'],
});

/** A known value's translated chip; an unknown future value shows verbatim, muted. */
function labelled(table: Readonly<Record<string, Labelled>>, value: string, t: ToolPresenterContext['t']): ToolChip | null {
  if (!value) return null;
  const entry = Object.prototype.hasOwnProperty.call(table, value) ? table[value] : null;
  return entry ? { label: t(entry[0]), tone: entry[1] } : { label: value, tone: 'muted' };
}

function chips(...values: readonly (ToolChip | null)[]): readonly ToolChip[] {
  return values.filter((value): value is ToolChip => value !== null);
}

function chipsHtml(values: readonly ToolChip[]): string {
  return values.map(chipHtml).join('');
}

function strings(value: unknown): readonly string[] {
  return list(value).map(text).filter(Boolean);
}

function markdownSection(context: ToolPresenterContext, label: string, source: string, open = false): string {
  return source ? collapsibleHtml(label, context.markdown(source), open) : '';
}

function resultFieldsHtml(view: ToolView, context: ToolPresenterContext, omit: readonly string[]): string {
  if (!view.result) return '';
  const rest = Object.fromEntries(Object.entries(view.result).filter(([key]) => !omit.includes(key)));
  return genericFieldsHtml(rest, context.t, context.markdown);
}

/* ── delegate_project_work ─────────────────────────────────────────── */

function rememberDelegation(view: ToolView, context: ToolPresenterContext): void {
  const result = view.result ?? {};
  const assignmentId = text(result.assignment_id);
  if (!assignmentId) return;
  context.work.remember(assignmentId, {
    title: text(view.args.title),
    acceptance: strings(view.args.acceptance),
    conversationId: text(result.conversation_id),
  });
}

const delegate: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.delegate.title'),
  summary(view, context) {
    rememberDelegation(view, context);
    const status = text(view.result?.status) || text(view.result?.dispatch);
    return { text: text(view.args.title), chips: chips(labelled(EXECUTION_STATUS, status, context.t)) };
  },
  body(view, context) {
    rememberDelegation(view, context);
    const { t } = context;
    const result = view.result ?? {};
    const status = labelled(EXECUTION_STATUS, text(result.status) || text(result.dispatch), t);
    const acceptance = strings(view.args.acceptance)
      .map((check) => checkRowHtml('unknown', t('toolCard.check.notReviewed'), escape(check)));
    const reworkOf = record(view.args.rework_of);
    const reworkTitle = text(reworkOf.assignmentId) ? context.work.taskTitle(text(reworkOf.assignmentId)) : '';
    const dependencies = list(view.args.dependencies).map((item) => context.work.taskTitle(text(record(item).assignmentId)));
    return [
      fieldHtml(t('toolCard.field.task'), `<strong>${escape(text(view.args.title))}</strong>`),
      fieldHtml(t('toolCard.field.startStatus'), status ? chipHtml(status) : ''),
      fieldHtml(t('toolCard.field.reworkOf'), escape(reworkTitle)),
      fieldHtml(t('toolCard.field.dependsOn'), dependencies.map(escape).join('<br>')),
      openConversationHtml(text(result.conversation_id), t('toolCard.delegate.openWorker')),
      sectionHtml(t('toolCard.delegate.acceptance'), listHtml(acceptance)),
      markdownSection(context, t('toolCard.delegate.brief'), text(view.args.brief)),
    ].join('');
  },
  debug: (view, { t }) => [
    [t('toolCard.debug.assignment'), text(view.result?.assignment_id)],
    [t('toolCard.debug.conversation'), text(view.result?.conversation_id)],
    [t('toolCard.debug.idempotencyKey'), text(view.args.idempotency_key)],
  ],
};

/* ── update_task_progress ──────────────────────────────────────────── */

interface BoardTask { readonly title: string; readonly status: string; readonly progress: string; readonly due: string }

/** The arguments carry the full board; accepted metadata names it when they cannot. */
function boardTasks(view: ToolView): readonly BoardTask[] {
  const fromArgs = list(view.args.tasks).filter(isRecord).map((task) => ({
    title: text(task.title), status: text(task.status), progress: text(task.progress), due: text(task.due),
  }));
  if (fromArgs.length) return fromArgs;
  return list(record(view.meta.todoState).todos).filter(isRecord).map((todo) => ({
    title: text(todo.content), status: text(todo.status), progress: '', due: '',
  }));
}

function boardSentence(view: ToolView): string {
  return text(view.args.summary) || text(record(view.meta.todoState).explanation);
}

const taskBoard: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.board.title'),
  summary(view, { t }) {
    const tasks = boardTasks(view);
    const done = tasks.filter((task) => task.status === 'completed').length;
    const active = tasks.filter((task) => task.status === 'in_progress').length;
    return {
      text: tasks.length ? t('toolCard.board.progress', { done, total: tasks.length }) : t('toolCard.board.empty'),
      chips: active ? [{ label: t('toolCard.board.active', { n: active }), tone: 'info' }] : [],
    };
  },
  body(view, { t }) {
    const tasks = boardTasks(view);
    const rows = tasks.map((task) => {
      const state = TASK_STATES[task.status] ?? TASK_STATES.pending;
      const due = task.due ? `<span class="ptool-coord-due">${escape(t('toolCard.board.due', { due: task.due }))}</span>` : '';
      const progress = task.progress ? `<span class="ptool-coord-progress">${escape(task.progress)}</span>` : '';
      return `<div class="ptool-todo-item ptool-todo-${state[0] === 'cancelled' ? 'pending ptool-coord-task-cancelled' : state[0]}">`
        + `<span class="ptool-todo-mark" role="img" aria-label="${escape(t(state[1]))}" title="${escape(t(state[1]))}">${state[2]}</span>`
        + `<span class="ptool-todo-text"><span class="ptool-coord-task-title">${escape(task.title)}</span>${due}${progress}</span>`
        + '</div>';
    }).join('');
    return leadHtml(boardSentence(view))
      + (rows ? `<div class="ptool-todo-list ptool-coord-board">${rows}</div>`
        : `<div class="ptool-todo-empty">${escape(t('toolCard.board.empty'))}</div>`);
  },
  debug: (view, { t }) => [[t('toolCard.debug.revision'), String(count(view.result?.revision) ?? '')]],
};

/* ── review_project_work ───────────────────────────────────────────── */

function deliveryStatus(view: ToolView): string {
  if (view.result?.canonical_delivery === true) return 'delivered';
  return text(record(view.result?.delivery).status);
}

function reviewExplanation(view: ToolView, context: ToolPresenterContext): string {
  const { t } = context;
  const decision = text(view.args.decision) || text(view.result?.decision);
  if (decision === 'rework') return t('toolCard.review.explainRework');
  if (decision === 'reject') return t('toolCard.review.explainReject');
  if (decision !== 'accept') return t('toolCard.review.explainPending');
  const checkpoint = text(view.args.checkpoint_sha) || text(view.result?.checkpoint_sha);
  if (!checkpoint) return t('toolCard.review.explainNoMerge');
  const delivery = deliveryStatus(view);
  if (delivery === 'delivered') return t('toolCard.review.explainMerged');
  if (delivery === 'blocked' || delivery === 'held') return t('toolCard.review.explainHeld');
  return t('toolCard.review.explainMerging');
}

interface ReviewCheck { readonly check: number; readonly verdict: string; readonly evidence: string }

function reviewChecks(view: ToolView): readonly ReviewCheck[] {
  return list(view.args.acceptance_results).filter(isRecord).map((item, index) => ({
    check: count(item.check) ?? index + 1, verdict: text(item.verdict) || 'unknown', evidence: text(item.evidence),
  }));
}

function checkCounts(view: ToolView): { met: number; total: number } | null {
  const counts = record(view.result?.acceptance_counts);
  const met = count(counts.met);
  if (met !== null) return { met, total: met + (count(counts.unmet) ?? 0) + (count(counts.unknown) ?? 0) };
  const checks = reviewChecks(view);
  return checks.length ? { met: checks.filter((check) => check.verdict === 'met').length, total: checks.length } : null;
}

function mergeChip(view: ToolView, context: ToolPresenterContext): ToolChip | null {
  const decision = text(view.args.decision) || text(view.result?.decision);
  if (decision !== 'accept' || !(text(view.args.checkpoint_sha) || text(view.result?.checkpoint_sha))) return null;
  return labelled(DELIVERY_STATUS, deliveryStatus(view) || 'pending', context.t);
}

const review: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.review.title'),
  summary(view, context) {
    const { t } = context;
    const counts = checkCounts(view);
    return {
      text: context.work.taskTitle(text(view.args.assignment_id) || text(view.result?.assignment_id)),
      chips: chips(
        labelled(VERDICTS, text(view.args.decision) || text(view.result?.decision), t),
        counts ? { label: t('toolCard.review.checksMet', counts), tone: counts.met === counts.total ? 'ok' : 'warn' } : null,
        mergeChip(view, context),
      ),
    };
  },
  body(view, context) {
    const { t } = context;
    const assignmentId = text(view.args.assignment_id) || text(view.result?.assignment_id);
    const criteria = context.work.acceptance(assignmentId);
    const rows = reviewChecks(view).map(({ check, verdict, evidence }) => {
      const [kind, label] = CHECK_MARKS[verdict] ?? CHECK_MARKS.unknown;
      const criterion = criteria[check - 1] || t('toolCard.review.checkNumber', { n: check });
      return checkRowHtml(kind, t(label), escape(criterion), escape(evidence));
    });
    const verdict = labelled(VERDICTS, text(view.args.decision) || text(view.result?.decision), t);
    const merged = deliveryStatus(view) === 'delivered';
    const merge = mergeChip(view, context);
    // pending → merged, with the current step lit; a held/blocked merge shows its own chip.
    const steps = merge && ['pending', 'delivered', 'working'].includes(deliveryStatus(view) || 'pending')
      ? `<span class="ptool-coord-steps">${chipHtml({ label: t('toolCard.delivery.pending'), tone: merged ? 'muted' : 'info' })}<span aria-hidden="true">→</span>${chipHtml({ label: t('toolCard.delivery.delivered'), tone: merged ? 'ok' : 'muted' })}</span>`
      : merge ? chipHtml(merge) : '';
    const reason = text(record(view.result?.delivery).reason);
    return [
      leadHtml(reviewExplanation(view, context)),
      fieldHtml(t('toolCard.field.task'), escape(context.work.taskTitle(assignmentId))),
      fieldHtml(t('toolCard.field.verdict'), verdict ? chipHtml(verdict) : ''),
      fieldHtml(t('toolCard.field.merge'), steps),
      fieldHtml(t('toolCard.field.reason'), escape(reason)),
      sectionHtml(t('toolCard.review.checks'), listHtml(rows)),
      markdownSection(context, t('toolCard.review.note'), text(view.args.note), true),
    ].join('');
  },
  failure: (view, { t }) => (text(view.result?.code) === 'result_revision_changed'
    ? t('toolCard.review.refusedResultChanged') : ''),
  debug: (view, { t }) => [
    [t('toolCard.debug.assignment'), text(view.args.assignment_id) || text(view.result?.assignment_id)],
    [t('toolCard.debug.checkpoint'), text(view.args.checkpoint_sha) || text(view.result?.checkpoint_sha)],
    [t('toolCard.debug.resultRevision'), String(count(view.args.result_revision) ?? count(view.result?.result_revision) ?? '')],
    [t('toolCard.debug.grantRevision'), String(count(view.result?.grant_revision) ?? '')],
  ],
};

/* ── poll (Coordinator Workers) ───────────────────────────────────── */

const ACTIVE_EXECUTION = new Set(['queued', 'starting', 'running']);

function polledWorkers(view: ToolView): readonly UnknownRecord[] {
  return list(view.result?.workers).filter(isRecord);
}

function workerTitle(worker: UnknownRecord, context: ToolPresenterContext): string {
  return context.work.taskTitle(text(worker.assignment_id), text(worker.conversation_id));
}

const pollWorkers: ToolPresenter = {
  applies(view, context) {
    if (polledWorkers(view).length) return true;
    const ids = strings(view.args.ids);
    return ids.length > 0 && ids.every((id) => context.work.knows(id));
  },
  title: (_view, { t }) => t('toolCard.poll.title'),
  summary(view, { t }) {
    const workers = polledWorkers(view);
    if (!workers.length) return { text: t('toolCard.poll.waitingFor', { n: strings(view.args.ids).length }) };
    const running = workers.filter((worker) => ACTIVE_EXECUTION.has(text(worker.status))).length;
    return {
      text: t('toolCard.poll.counts', { done: workers.length - running, running }),
      chips: text(view.result?.wait) === 'pending_input'
        ? [{ label: t('toolCard.poll.newInput'), tone: 'info' }] : [],
    };
  },
  body(view, context) {
    const { t } = context;
    const workers = polledWorkers(view);
    const finished = workers.filter((worker) => !ACTIVE_EXECUTION.has(text(worker.status))).map((worker) => {
      const status = labelled(EXECUTION_STATUS, text(worker.status), t);
      const delivery = labelled(DELIVERY_STATUS, worker.canonical_delivery === true ? 'delivered' : text(record(worker.delivery).status), t);
      const excerpt = text(worker.result_excerpt);
      return `<li class="ptool-coord-worker"><span class="ptool-coord-worker-title">${escape(workerTitle(worker, context))}</span>${chipsHtml(chips(status, delivery))}`
        + `${excerpt ? collapsibleHtml(t('toolCard.poll.result'), context.markdown(excerpt)) : ''}</li>`;
    });
    const running = workers.filter((worker) => ACTIVE_EXECUTION.has(text(worker.status))).map((worker) => {
      const status = labelled(EXECUTION_STATUS, text(worker.status), t);
      return `<li class="ptool-coord-worker"><span class="ptool-coord-worker-title">${escape(workerTitle(worker, context))}</span>${status ? chipHtml(status) : ''}</li>`;
    });
    const waited = workers.length ? [] : strings(view.args.ids)
      .map((id) => `<li class="ptool-coord-worker"><span class="ptool-coord-worker-title">${escape(context.work.taskTitle(id))}</span></li>`);
    const inputs = list(view.result?.waiting).filter(isRecord).map((input) => `<li>${escape(text(input.preview))}</li>`);
    return [
      text(view.result?.wait) === 'pending_input' ? leadHtml(t('toolCard.poll.explainInput')) : '',
      inputs.length ? `<ul class="ptool-coord-quotes">${inputs.join('')}</ul>` : '',
      sectionHtml(t('toolCard.poll.finished', { n: finished.length }), listHtml(finished, 'ptool-coord-workers')),
      sectionHtml(t('toolCard.poll.running', { n: running.length }), listHtml(running, 'ptool-coord-workers')),
      sectionHtml(t('toolCard.poll.waitedOn', { n: waited.length }), listHtml(waited, 'ptool-coord-workers')),
    ].join('');
  },
  debug: (view, { t }) => strings(view.args.ids).map((id) => [t('toolCard.debug.assignment'), id] as const),
};

/* ── get_project_assignment ───────────────────────────────────────── */

const assignmentRead: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.assignment.title'),
  summary: (view, context) => ({ text: context.work.taskTitle(text(view.args.assignment_id)) }),
  body(view, context) {
    const { t } = context;
    const assignmentId = text(view.args.assignment_id);
    const value = view.result ?? {};
    const request = record(value.request);
    const acceptance = strings(request.acceptance);
    if (acceptance.length) context.work.remember(assignmentId, { acceptance });
    const execution = record(value.execution);
    const status = labelled(EXECUTION_STATUS, text(execution.status), t);
    const review = record(value.review);
    const verdict = labelled(VERDICTS, text(review.decision), t);
    const checks = (acceptance.length ? acceptance : context.work.acceptance(assignmentId))
      .map((check) => checkRowHtml('unknown', t('toolCard.check.notReviewed'), escape(check)));
    return [
      leadHtml(t('toolCard.assignment.explain')),
      fieldHtml(t('toolCard.field.task'), `<strong>${escape(context.work.taskTitle(assignmentId))}</strong>`),
      fieldHtml(t('toolCard.field.status'), status ? chipHtml(status) : ''),
      fieldHtml(t('toolCard.field.verdict'), verdict ? chipHtml(verdict) : ''),
      view.partial ? leadHtml(t('toolCard.assignment.partial')) : '',
      sectionHtml(t('toolCard.delegate.acceptance'), listHtml(checks)),
      markdownSection(context, t('toolCard.delegate.brief'), text(request.brief)),
    ].join('');
  },
  debug: (view, { t }) => [[t('toolCard.debug.assignment'), text(view.args.assignment_id)]],
};

/* ── report_to_user ───────────────────────────────────────────────── */

const report: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.report.title'),
  summary: (view, { t }) => ({
    text: text(view.args.title),
    chips: chips(labelled(REPORT_KINDS, text(view.args.kind), t)),
  }),
  body(view, context) {
    const { t } = context;
    const message = text(view.args.message);
    return [
      fieldHtml(t('toolCard.field.published'), view.result?.published === true ? escape(t('toolCard.report.published')) : ''),
      message ? `<div class="ptool-coord-prose md-content">${context.markdown(message)}</div>` : '',
    ].join('');
  },
  debug: (view, { t }) => [
    [t('toolCard.debug.turn'), text(view.result?.turnId)],
    [t('toolCard.debug.idempotencyKey'), text(view.args.idempotency_key)],
  ],
};

/* ── finish_project_request / cancel_project_work ─────────────────── */

const finish: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.finish.title'),
  summary: (view, { t }) => {
    const outcome = labelled(FINISH_OUTCOMES, text(view.args.outcome), t);
    return { text: outcome?.label ?? '', chips: [] };
  },
  body: (view, context) => [
    leadHtml(text(view.args.outcome) === 'needs_user'
      ? context.t('toolCard.finish.explainNeedsUser') : context.t('toolCard.finish.explainComplete')),
    resultFieldsHtml(view, context, ['next_step']),
  ].join(''),
  debug: (view, { t }) => [[t('toolCard.debug.turn'), text(view.args.report_turn_id)]],
};

const cancel: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.cancel.title'),
  summary: (view, context) => ({
    text: context.work.taskTitle(text(view.args.assignment_id)),
    chips: chips(labelled(EXECUTION_STATUS, view.result?.revoked === true ? 'cancelled' : text(view.result?.execution_status), context.t)),
  }),
  body(view, context) {
    const { t } = context;
    return [
      leadHtml(view.result?.revoked === true ? t('toolCard.cancel.explain') : ''),
      fieldHtml(t('toolCard.field.task'), escape(context.work.taskTitle(text(view.args.assignment_id)))),
      fieldHtml(t('toolCard.field.status'), chipsHtml(chips(labelled(EXECUTION_STATUS, text(view.result?.execution_status), t)))),
    ].join('');
  },
  debug: (view, { t }) => [[t('toolCard.debug.assignment'), text(view.args.assignment_id)]],
};

/* ── control_conversation ─────────────────────────────────────────── */

function controlAction(view: ToolView, t: ToolPresenterContext['t']): string {
  const action = text(view.args.action);
  return Object.prototype.hasOwnProperty.call(CONTROL_ACTIONS, action) ? t(CONTROL_ACTIONS[action]) : action;
}

const control: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.control.title'),
  summary: (view, context) => ({
    text: [controlAction(view, context.t), context.work.conversationTitle(text(view.args.conversation_id))]
      .filter(Boolean).join(' · '),
  }),
  body(view, context) {
    const { t } = context;
    const conversationId = text(view.args.conversation_id);
    return [
      fieldHtml(t('toolCard.field.action'), escape(controlAction(view, t))),
      fieldHtml(t('toolCard.field.conversation'), escape(context.work.conversationTitle(conversationId))),
      fieldHtml(t('toolCard.field.model'), escape(text(view.args.model))),
      fieldHtml(t('toolCard.field.reason'), escape(text(view.args.reason))),
      markdownSection(context, t('toolCard.control.instruction'), text(view.args.instruction), true),
      openConversationHtml(conversationId, t('toolCard.control.open')),
    ].join('');
  },
  debug: (view, { t }) => [
    [t('toolCard.debug.conversation'), text(view.args.conversation_id)],
    [t('toolCard.debug.turn'), text(view.args.turn_id)],
    [t('toolCard.debug.command'), text(view.result?.command_id)],
  ],
};

/* ── project messages ─────────────────────────────────────────────── */

function messageKind(value: unknown, t: ToolPresenterContext['t']): string {
  const kind = text(value);
  return Object.prototype.hasOwnProperty.call(MESSAGE_KINDS, kind) ? t(MESSAGE_KINDS[kind]) : kind;
}

const sendMessage: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.message.sendTitle'),
  summary: (view, context) => ({
    text: context.t('toolCard.message.to', { recipient: context.work.conversationTitle(text(view.args.conversation_id)) }),
    chips: [{ label: messageKind(view.args.kind || 'reference', context.t), tone: 'muted' }],
  }),
  body(view, context) {
    const content = text(view.args.content);
    return [
      fieldHtml(context.t('toolCard.field.recipient'), escape(context.work.conversationTitle(text(view.args.conversation_id)))),
      content ? `<div class="ptool-coord-prose md-content">${context.markdown(content)}</div>` : '',
    ].join('');
  },
  debug: (view, { t }) => [
    [t('toolCard.debug.conversation'), text(view.args.conversation_id)],
    [t('toolCard.debug.message'), text(record(view.result?.message).id)],
    [t('toolCard.debug.idempotencyKey'), text(view.args.idempotency_key)],
  ],
};

const readMessages: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.message.readTitle'),
  summary(view, { t }) {
    const direction = text(view.args.direction) === 'outgoing' ? t('toolCard.message.outgoing') : t('toolCard.message.incoming');
    const total = count(view.result?.total_messages);
    return { text: total === null ? direction : `${direction} · ${t('toolCard.message.count', { n: total })}` };
  },
  body(view, context) {
    const rows = list(view.result?.messages).filter(isRecord).map((message) => {
      const sender = context.work.conversationTitle(text(message.sender));
      const decision = labelled(MESSAGE_DECISIONS, text(message.decision), context.t);
      return `<li class="ptool-coord-message"><span class="ptool-coord-worker-title">${escape(sender)}</span>`
        + `${chipHtml({ label: messageKind(message.kind, context.t), tone: 'muted' })}${decision ? chipHtml(decision) : ''}`
        + `<div class="ptool-coord-prose md-content">${context.markdown(text(message.content))}</div></li>`;
    });
    return rows.length ? listHtml(rows, 'ptool-coord-messages') : leadHtml(context.t('toolCard.message.none'));
  },
};

const considerMessage: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.message.considerTitle'),
  summary: (view, { t }) => ({ text: '', chips: chips(labelled(MESSAGE_DECISIONS, text(view.args.decision), t)) }),
  body: (view, context) => markdownSection(context, context.t('toolCard.message.note'), text(view.args.note), true),
  debug: (view, { t }) => [[t('toolCard.debug.message'), text(view.args.message_id)]],
};

/* ── Worker listing and the remaining coordination reads ──────────── */

const workers: ToolPresenter = {
  title: (_view, { t }) => t('toolCard.workers.title'),
  summary: (view, { t }) => ({ text: view.result ? t('toolCard.workers.count', { n: list(view.result.workers).length }) : '' }),
  body(view, context) {
    const rows = list(view.result?.workers).filter(isRecord).map((worker) => {
      const status = labelled(EXECUTION_STATUS, text(worker.status), context.t);
      return `<li class="ptool-coord-worker"><span class="ptool-coord-worker-title">${escape(workerTitle(worker, context))}</span>${status ? chipHtml(status) : ''}</li>`;
    });
    return rows.length ? listHtml(rows, 'ptool-coord-workers') : leadHtml(context.t('toolCard.workers.none'));
  },
};

function titledOnly(key: PlainKey, summary?: (view: ToolView, context: ToolPresenterContext) => ToolSummary): ToolPresenter {
  return {
    title: (_view, { t }) => t(key),
    summary: summary ?? (() => ({ text: '' })),
    body: (view, context) => resultFieldsHtml(view, context, ['next_step']),
  };
}

const assignmentSummary = (view: ToolView, context: ToolPresenterContext): ToolSummary => ({
  text: context.work.taskTitle(text(view.args.assignment_id)),
});

export const COORDINATION_TOOL_PRESENTERS: Readonly<Record<string, ToolPresenter>> = Object.freeze({
  delegate_project_work: delegate,
  update_task_progress: taskBoard,
  review_project_work: review,
  poll: pollWorkers,
  get_project_assignment: assignmentRead,
  report_to_user: report,
  finish_project_request: finish,
  cancel_project_work: cancel,
  control_conversation: control,
  send_project_message: sendMessage,
  get_project_messages: readMessages,
  consider_project_message: considerMessage,
  get_project_workers: workers,
  await_project_workers: titledOnly('toolCard.await.title', (view, { t }) => ({
    text: t('toolCard.poll.waitingFor', { n: strings(view.args.assignment_ids).length }),
  })),
  request_user_decision: titledOnly('toolCard.decision.title', (view) => ({ text: text(view.args.recommendation) })),
  get_conversation_status: titledOnly('toolCard.conversationStatus.title', (view, context) => ({
    text: context.work.conversationTitle(text(view.args.conversation_id)),
  })),
  create_conversation: titledOnly('toolCard.create.title', (view) => ({ text: text(view.args.title) })),
  dispatch_project_assignment: titledOnly('toolCard.dispatch.title', assignmentSummary),
});

