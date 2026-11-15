/** Pure view model of the Coordinator task history (contract `progressHistoryPage` in
 * contracts/project_coordination_v1.schema.json; docs/PROJECT_COORDINATION.md "Task history").
 *
 * Rows are the loaded tasks in server order, with a heading before each group's first
 * task while every group is shown. Row geometry is two fixed heights, so the visible
 * window is a binary search over prefix offsets and no row is ever measured.
 */
import type {
  CoordinatorProgressHistoryGroup, CoordinatorProgressHistoryPage, CoordinatorProgressHistoryTask,
} from '../api/project-coordination.generated';

export const HISTORY_GROUPS = ['in_progress', 'pending', 'dropped', 'completed', 'cancelled'] as const satisfies
  readonly CoordinatorProgressHistoryGroup[];
export type HistoryFilter = CoordinatorProgressHistoryGroup | 'all';
export type HistoryCounts = CoordinatorProgressHistoryPage['counts'];
export type HistoryRow =
  | { kind: 'group'; id: string; group: CoordinatorProgressHistoryGroup; count: number }
  | { kind: 'task'; id: string; group: CoordinatorProgressHistoryGroup; task: CoordinatorProgressHistoryTask };
export interface RowHeights { group: number; task: number }

export function historyGroup(task: CoordinatorProgressHistoryTask): CoordinatorProgressHistoryGroup {
  return task.dropped ? 'dropped' : task.status;
}

export function historyTotal(counts: HistoryCounts, filter: HistoryFilter): number {
  return filter === 'all' ? HISTORY_GROUPS.reduce((sum, group) => sum + counts[group], 0) : counts[filter];
}

export function historyRows(tasks: readonly CoordinatorProgressHistoryTask[], filter: HistoryFilter,
  counts: HistoryCounts): HistoryRow[] {
  const rows: HistoryRow[] = [];
  let current: CoordinatorProgressHistoryGroup | null = null;
  for (const task of tasks) {
    const group = historyGroup(task);
    if (filter === 'all' && group !== current) {
      rows.push({ kind: 'group', id: `group:${group}`, group, count: counts[group] });
      current = group;
    }
    rows.push({ kind: 'task', id: `task:${task.key}`, group, task });
  }
  return rows;
}

/** Appends a later page, keeping the first occurrence of a key: a task a board write
 * moved between page reads is shown once. */
export function appendPage(tasks: readonly CoordinatorProgressHistoryTask[],
  page: readonly CoordinatorProgressHistoryTask[]): CoordinatorProgressHistoryTask[] {
  const seen = new Set(tasks.map(task => task.key));
  return [...tasks, ...page.filter(task => !seen.has(task.key))];
}

/** offsets[i] is the top of row i; offsets[rows.length] is the list height. */
export function rowOffsets(rows: readonly HistoryRow[], heights: RowHeights): Float64Array {
  const offsets = new Float64Array(rows.length + 1);
  for (let index = 0; index < rows.length; index++) {
    offsets[index + 1] = offsets[index] + (rows[index].kind === 'group' ? heights.group : heights.task);
  }
  return offsets;
}

/** [first, end) of the rows intersecting [top, top + height). */
export function visibleRange(offsets: Float64Array, top: number, height: number): [number, number] {
  const count = offsets.length - 1;
  if (count <= 0 || height <= 0) return [0, 0];
  const bottom = top + height;
  let low = 0, high = count - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (offsets[middle] <= top) low = middle; else high = middle - 1;
  }
  let end = low;
  while (end < count && offsets[end] < bottom) end++;
  return [low, Math.max(end, low + 1)];
}

/** Everything a row shows: an unchanged signature keeps its DOM node across reads.
 * A task on the board is seen at every board write, so its row shows when it last
 * changed, not when it was last seen. */
export function rowSignature(row: HistoryRow): string {
  if (row.kind === 'group') return `${row.group}:${row.count}`;
  const task = row.task;
  return JSON.stringify([task.key, task.title, task.formerTitles, task.status, task.dropped, task.onBoard, task.progress,
    task.dueAt, task.dueOutcome, task.firstSeenAt, task.onBoard ? task.changedAt : task.lastSeenAt]);
}
