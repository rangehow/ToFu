/** Read-only summaries used before the Coordinator board view is loaded. */
import { t } from '../i18n';
import type { CoordinatorProgressBoard, CoordinatorProgressTask, CoordinatorProgressTranslation } from '../api/project-coordination.generated';

type Status = CoordinatorProgressTask['status'];

export function coordinatorProgressTally(board: CoordinatorProgressBoard) {
  const count = (status: Status) => board.tasks.filter(task => task.status === status).length;
  const limit = (task: CoordinatorProgressTask) => task.blocker?.kind === 'needs_external_resource'
    && 'unavailable' in task.blocker && task.blocker.unavailable === true;
  const waiting = board.tasks.filter(task => task.status === 'pending' && task.reason && !limit(task)).length;
  return { active: count('in_progress'), pending: count('pending') - waiting, waiting,
    finished: count('completed') + count('cancelled'), total: board.tasks.length };
}

/** One-line state for the sidebar entry; null when the board says nothing yet. */
export function coordinatorProgressHeadline(board: CoordinatorProgressBoard | null): string | null {
  if (!board || board.tasks.length === 0) return null;
  const tally = coordinatorProgressTally(board);
  if (tally.active > 0) return t('coordinatorProgress.sidebar.active', { count: tally.active });
  if (tally.waiting > 0) return t('coordinatorProgress.sidebar.waiting', { count: tally.waiting });
  if (tally.pending > 0) return t('coordinatorProgress.sidebar.pending', { count: tally.pending });
  return t('coordinatorProgress.sidebar.allDone');
}

/** A board text as shown: its display translation when the board carries one. */
export function coordinatorProgressText(translation: CoordinatorProgressTranslation | undefined, text: string): string {
  return translation?.texts.find(entry => entry.source === text)?.text ?? text;
}

