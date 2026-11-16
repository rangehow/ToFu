/** Demand-loaded approvals, guidance, apply and cost-popover entries. */
import '../runtime/cost-popover-presenters.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('misc', name, args, stub);
}
