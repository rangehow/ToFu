/** Lazy research composition; Paper loads only for an explicit reader command. */
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './research/components/view';
import './research/pages/session';
import { featureRegistry } from '../feature-registry';
import { openResearchReader } from './research/api/reader';
(featureRegistry as unknown as Record<string, unknown>)._researchReader = openResearchReader;

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('research', name, args, stub);
}
