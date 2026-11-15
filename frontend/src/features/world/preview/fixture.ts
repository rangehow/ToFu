/** Fixture source for the office preview: a recorded live snapshot (preview/record_fixture.py output) served
 * as a static WorldLive, optionally scaled to N running Workers, with one scripted report for checking the
 * walk-in. Preview-only: the application reads live data through live.ts. Entry point: fixtureLive. */
import type { ProjectDispatchHistoryRow } from '../../../api/project-dispatch.generated';
import type { CoordinatorConversationPage } from '../../../api/project-coordination.generated';
import { openQuestions, type QuestionTurn } from '../attention';
import type { LiveTeam, SourceState, WorldLive } from '../live';
import type { TeamKind } from '../model';

export interface FixtureTeam {
  kind: TeamKind; key: string; name: string; color: string; leadId: string; order: number;
  team: CoordinatorConversationPage | null; assignments?: ProjectDispatchHistoryRow[] | null;
  progress: LiveTeam['progress']['value']; reports: LiveTeam['reports']['value']; questionTurns: QuestionTurn[] | null;
}
export interface FixtureSnapshot { capturedAt: number; teams: FixtureTeam[] }

const ok = <T,>(value: T | null | undefined): SourceState<T> => ({ value: value ?? null, status: value == null ? 'error' : 'ok', at: 1, inFlight: false, again: false });

function scaled(teams: LiveTeam[], total: number) {
  const projects = teams.filter(team => team.kind === 'project');
  for (let index = 0; index < total; index += 1) {
    const team = projects[index % projects.length];
    const page = team.team.value || { conversations: [], nextCursor: null, coverage: 'complete' as const };
    page.conversations.push({
      conversationId: `worker-scale-${index}`, title: `[LOAD-${index + 1}] Synthetic load Worker ${index + 1}`, turnCount: 4,
      coordinatorId: team.leadId, folderId: null, busy: true,
    });
    team.team = { ...team.team, value: page };
  }
}

export function fixtureLive(snapshot: FixtureSnapshot, onChange: () => void, options: { scale?: number } = {}): WorldLive & { report(team: string): string | null } {
  const teams: LiveTeam[] = snapshot.teams.map(team => ({
    key: team.key, name: team.name, color: team.color, kind: team.kind, leadId: team.leadId, order: team.order,
    team: ok(team.team), assignments: ok(team.kind === 'project' ? team.assignments ?? null : []),
    progress: ok(team.progress), reports: ok(team.reports),
    questions: ok(team.questionTurns ? openQuestions(team.leadId, team.questionTurns) : null),
  }));
  if (options.scale) scaled(teams, options.scale);
  let revision = 9_000_000;
  return {
    state: { teams, started: true, error: '', facts: null, loading: false },
    start: async () => { onChange(); },
    refreshAll: () => onChange(),
    stop: () => {},
    /** Script one real-shaped report: a running Worker's attempt ends and its assignment completes. */
    report(teamKey: string) {
      const team = teams.find(each => each.key === teamKey || each.name === teamKey);
      const busy = team?.team.value?.conversations.find(member => member.busy && member.conversationId !== team.leadId);
      if (!team || !busy) return null;
      team.team = { ...team.team, value: { ...team.team.value!, conversations: team.team.value!.conversations.map(member =>
        member.conversationId === busy.conversationId ? { ...member, busy: undefined } : member) }, at: team.team.at + 1 };
      onChange();
      window.setTimeout(() => {
        const rows = [...(team.assignments.value || [])];
        const index = rows.findIndex(row => row.request.recipientId === busy.conversationId);
        const base = index >= 0 ? rows[index] : null;
        const row = {
          ...(base || { assignmentId: `scripted-${busy.conversationId}`, grantRevision: 1, revoked: false, canonicalDelivery: false, review: null,
            request: { projectPath: team.key, coordinatorId: team.leadId, recipientId: busy.conversationId, brief: busy.title } }),
          execution: { turnId: `turn-${revision}`, attemptId: `attempt-${revision}`, taskId: `task-${revision}`, status: 'completed',
            resultRevision: ++revision, result: { content: `${busy.title}: finished and reported.` } },
        } as ProjectDispatchHistoryRow;
        if (index >= 0) rows[index] = row; else rows.unshift(row);
        team.assignments = { ...team.assignments, value: rows, at: team.assignments.at + 1 };
        onChange();
      }, 400);
      return busy.conversationId;
    },
  };
}
