/** Lazy LaTeX live-preview domain: an agent TeX edit — Overleaf project
 * or local workspace .tex file — recompiles the paper and the chat-side
 * PDF panel scrolls to the edit. */
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { featureRegistry } from '../feature-registry';
import { createOverleafLivePreview } from './overleaf/live-preview';

const preview = createOverleafLivePreview();
(featureRegistry as unknown as Record<string, unknown>)._overleafPreviewRounds =
  preview.observeRounds;

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('overleaf', name, args, stub);
}
