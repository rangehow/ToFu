/** Status of a Worker row in its Coordinator's sidebar roster, never Project Brain history. */
export interface CoordinatorWorkerActivityFlags {
  streaming?: boolean;
  waiting?: boolean;
  background?: unknown;
  errored?: boolean;
  incomplete?: boolean;
}

export function coordinatorWorkerStatus(
  flags: CoordinatorWorkerActivityFlags,
  hasExecutionEvidence: boolean,
): 'running' | 'done' | 'failed' | 'cancelled' | null {
  if (flags.streaming || flags.waiting || flags.background) return 'running';
  if (flags.errored) return 'failed';
  if (flags.incomplete) return 'cancelled';
  // An unhydrated shell has no Turn evidence; never present it as finished.
  return hasExecutionEvidence ? 'done' : null;
}
