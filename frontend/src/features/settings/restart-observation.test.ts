import { describe, expect, it } from 'vitest';
import { projectRestartHealth, restartPhaseLabel, restartPhaseProgress, restartWaitNotice } from './restart-observation';

describe('restart health authority', () => {
  it('does not mistake the accepting process or an early listener for completion', () => {
    expect(projectRestartHealth({ok:true, bootId:'old', boot:{status:'ready'}}, 'old')).toEqual({kind:'old'});
    expect(projectRestartHealth({ok:true, bootId:'new', boot:{status:'running'}}, 'old')).toMatchObject({kind:'booting'});
    expect(projectRestartHealth({ok:true, bootId:'new', boot:{status:'ready'}}, 'old')).toMatchObject({kind:'ready'});
  });
  it('surfaces a new boot failure and adopts a boot already running on click', () => {
    const boot = {status:'failed', error:'storage corrupt'};
    expect(projectRestartHealth({ok:true, bootId:'new', boot}, 'old')).toEqual({kind:'failed',boot});
    expect(projectRestartHealth({ok:true, bootId:'new', boot:{status:'running'}}, null)).toMatchObject({kind:'booting'});
    expect(projectRestartHealth(null, 'old')).toEqual({kind:'unavailable'});
  });
  it('keeps older health endpoints usable', () => {
    expect(projectRestartHealth({ok:true}, null)).toMatchObject({kind:'ready'});
  });
  it('reports an outage only after nothing has answered for the slow window', () => {
    expect(restartWaitNotice(60, 60, 180)).toBeNull();
    expect(restartWaitNotice(400, 0, 180)).toBe('slow');
    expect(restartWaitNotice(400, 179, 180)).toBe('slow');
    expect(restartWaitNotice(400, 181, 180)).toBe('unreachable');
  });
  it('projects the running phase without extrapolating to a later phase', () => {
    expect(restartPhaseProgress({status:'running',phaseTotal:9,currentPhase:{index:3,key:'storage_sidecar'}})).toEqual({key:'storage_sidecar',pct:20+76*2/9});
    expect(restartPhaseProgress({status:'running',phaseTotal:9,currentPhase:{index:10,key:'readiness'}})).toBeNull();
  });
  it('never paints a phase key the catalog does not know', () => {
    const catalog: Record<string, string> = {'update.phase.storage_sidecar': '正在启动存储 sidecar…'};
    const translate = (key: string) => catalog[key] ?? key;
    expect(restartPhaseLabel('storage_sidecar', 'Storage sidecar', translate))
      .toBe('正在启动存储 sidecar…');
    // A phase added on the server before the browser catalog: the server's
    // label is degraded copy, the raw i18n key is a broken UI.
    expect(restartPhaseLabel('execution_service', 'Execution service', translate))
      .toBe('Execution service');
    expect(restartPhaseLabel('execution_service', '', translate)).toBe('update.phase.execution_service');
    expect(restartPhaseLabel('', 'Execution service', translate)).toBe('Execution service');
  });
});
