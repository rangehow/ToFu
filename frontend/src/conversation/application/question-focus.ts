/** "Answer in conversation": which ask_human question a conversation opens at, and the composer reply
 * quote that binds the owner's next message to it. A named question (guidance id) is found wherever it
 * is in the loaded Turns; without one, the newest question still open is chosen (a blocking question
 * while its round awaits a human; a non-blocking one until a human Turn follows it, as
 * features/world/attention.ts reads it). Pure; entry point: findQuestionFocus. */
import { guidanceReplyQuote } from '../../rich-response/guidance-values';

export interface QuestionFocusTurn {
  turnId: string;
  ordinal: number;
  actor: string;
  projection?: { toolRounds?: ReadonlyArray<unknown> } | null;
}
export interface QuestionFocus { turnId: string; ordinal: number; guidanceId: string; quote: string }

type RoundRecord = Record<string, unknown>;
const text = (value: unknown) => (typeof value === 'string' ? value.trim() : '');

/** The question as the reply quotes it: a form's questions numbered like the form. */
function questionText(round: RoundRecord): string {
  const batch = Array.isArray(round.guidanceBatchQuestions) ? round.guidanceBatchQuestions : [];
  const numbered = batch.map(item => text((item as RoundRecord | null)?.question)).filter(Boolean)
    .map((question, index) => `${index + 1}. ${question}`);
  return numbered.length ? numbered.join('\n\n') : text(round.guidanceQuestion);
}

export function findQuestionFocus(turns: ReadonlyArray<QuestionFocusTurn>, guidanceId = ''): QuestionFocus | null {
  let laterHuman = false;
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    if (turn.actor === 'human') { laterHuman = true; continue; }
    const rounds = turn.projection?.toolRounds || [];
    for (let at = rounds.length - 1; at >= 0; at -= 1) {
      const round = rounds[at] as RoundRecord | null;
      if (!round || round.toolName !== 'ask_human') continue;
      const id = text(round.guidanceId);
      const question = questionText(round);
      if (!id || !question) continue;
      const open = !round.guidanceWithdrawn
        && (round.guidanceBlocking === false ? !laterHuman : round.status === 'awaiting_human');
      if (guidanceId ? id !== guidanceId : !open) continue;
      return { turnId: turn.turnId, ordinal: turn.ordinal, guidanceId: id, quote: guidanceReplyQuote(id, question) };
    }
  }
  return null;
}
