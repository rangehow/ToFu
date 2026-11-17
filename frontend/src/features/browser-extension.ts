/** Demand-loaded browser extension dialog: install, upgrade and connection status. */
import '../styles/application/14-browser-extension.css';
import '../runtime/browser-extension-presenters.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('browser-extension', name, args, stub);
}
