/** Demand-loaded Project workspace presentation entry. */
import './project-presenters.css';
import '../runtime/project-presenters.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('project', name, args, stub);
}
