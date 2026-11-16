/** Fixture source for the office preview: a recorded live snapshot (preview/record_fixture.py output) served
 * as a static WorldLive, optionally scaled to N running Workers, with one scripted report for checking the
 * walk-in, and answer ports for the leads' recorded questions (the production choice-interaction adapter over
 * the fixture's Turns: an answer settles the question in the fixture, as the server would). Preview-only: the
 * application reads live data through live.ts. Entry point: fixtureLive. */
import { createChoiceInteractionPorts } from '../../../conversation/application/choice-interaction';
import type { ChoicePorts } from '../../../rich-response/choices';
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

export function fixtureLive(snapshot: FixtureSnapshot, onChange: () => void, options: { scale?: number } = {}): WorldLive & {
  report(team: string): string | null;
  answerPorts(conversationId: string, turnId: string): ChoicePorts | undefined;
  replies: Array<{ conversationId: string; guidanceId: string; quote: string; responseText: string }>;
} {
  const teams: LiveTeam[] = snapshot.teams.map(team => ({
    key: team.key, name: team.name, color: team.color, kind: team.kind, leadId: team.leadId, order: team.order,
    team: ok(team.team), assignments: ok(team.kind === 'project' ? team.assignments ?? null : []),
    progress: ok(team.progress), reports: ok(team.reports),
    questions: ok(team.questionTurns ? openQuestions(team.leadId, team.questionTurns) : null),
  }));
  if (options.scale) scaled(teams, options.scale);
  let revision = 9_000_000;
  const replies: Array<{ conversationId: string; guidanceId: string; quote: string; responseText: string }> = [];
  const questionTeam = (conversationId: string) => snapshot.teams.find(team => team.leadId === conversationId && team.questionTurns);
  const settle = (fixture: FixtureTeam) => {
    const team = teams.find(each => each.key === fixture.key)!;
    team.questions = { ...ok(openQuestions(fixture.leadId, fixture.questionTurns!)), at: team.questions.at + 1 };
    onChange();
  };
  const turnOf = (fixture: FixtureTeam, turnId: string) => {
    const turn = fixture.questionTurns!.find(each => each.turnId === turnId);
    if (!turn) throw new Error('No such Turn');
    return { status: 'running', settlement: null, projectionRevision: 1, ...turn, conversationId: fixture.leadId };
  };
  return {
    replies,
    answerPorts(conversationId, turnId) {
      const fixture = questionTeam(conversationId);
      if (!fixture) return undefined;
      return createChoiceInteractionPorts({
        conversationId, turnId, commandId: () => `preview-${++revision}`,
        readTurn: async () => turnOf(fixture, turnId),
        createAttempt: async (_conversation, _turn, body) => {
          const turn = fixture.questionTurns!.find(each => each.turnId === turnId)!;
          const rounds = (turn.projection?.toolRounds || []).map(raw => {
            const round = raw as Record<string, unknown>;
            return round.guidanceId === body.guidanceId
              ? { ...round, status: 'done', guidanceResponse: body.humanResponse, guidanceAnswerCommandId: body.commandId } : round;
          });
          turn.projection = { ...turn.projection, toolRounds: rounds };
          window.setTimeout(() => settle(fixture), 0);
          return { attempt: { turnId, commandId: body.commandId, operation: 'answer_guidance' }, turn: turnOf(fixture, turnId) };
        },
        replyInChat: async request => {
          replies.push(request);
          const last = fixture.questionTurns!.reduce((top, turn) => Math.max(top, turn.ordinal), 0);
          fixture.questionTurns!.push({ turnId: `reply-${++revision}`, ordinal: last + 1, actor: 'human', createdAt: Date.now() });
          window.setTimeout(() => settle(fixture), 0);
          return 'sent';
        },
      });
    },
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
