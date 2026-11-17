import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { presentTurnFinish } from './turn-finish';

function interrupted(settlement: Record<string, unknown>): TurnRecord {
  return { actor: 'assistant', status: 'interrupted', settlement } as unknown as TurnRecord;
}

describe('presentTurnFinish', () => {
  it('says why a restart interruption was not resumed and keeps Continue', () => {
    const finish = presentTurnFinish(interrupted({
      outcome: 'interrupted', cause: 'server_restart',
      error: { kind: 'internal', severity: 'warning', code: 'automatic_resume_paused',
        message: 'Automatic resume is paused: restart storm: the server is in a crash loop',
        automaticResume: { state: 'paused', guard: 'restart_storm', reason: 'restart storm' } },
      resumeOptions: [{ operation: 'continue', anchor: { type: 'replay_only' } }],
    }));
    expect(finish?.label).toBe('Interrupted');
    expect(finish?.detail).toBe('Automatic resume is paused: restart storm: the server is in a crash loop');
    expect(finish?.resumeOptions?.[0]?.operation).toBe('continue');
  });

  it('falls back to the cause when the interruption carries no explanation', () => {
    const finish = presentTurnFinish(interrupted({ outcome: 'interrupted', cause: 'server_restart', error: null }));
    expect(finish?.detail).toBe('server_restart');
  });
});
