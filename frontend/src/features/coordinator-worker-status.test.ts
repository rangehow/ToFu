import { expect, it } from 'vitest';
import { coordinatorWorkerStatus } from './coordinator-worker-status';

it('derives running, failure, cancellation and execution-evidence status for Worker rows', () => {
  expect(coordinatorWorkerStatus({ streaming: true }, false)).toBe('running');
  expect(coordinatorWorkerStatus({ waiting: true }, false)).toBe('running');
  expect(coordinatorWorkerStatus({ background: { queuedOnly: true } }, false)).toBe('running');
  expect(coordinatorWorkerStatus({ errored: true }, true)).toBe('failed');
  expect(coordinatorWorkerStatus({ incomplete: true }, true)).toBe('cancelled');
  expect(coordinatorWorkerStatus({}, true)).toBe('done');
  expect(coordinatorWorkerStatus({}, false)).toBeNull();
});
