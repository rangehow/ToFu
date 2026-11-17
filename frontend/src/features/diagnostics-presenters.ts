import './inspector/shell.css';
import './inspector/structure.css';
import './inspector/drawer.css';
import './inspector/inline.css';
import './inspector/trace.css';
import './inspector/responsive.css';
/** Demand-loaded Debug and Request Inspector presentation entry. */
import '../runtime/diagnostics-presenters.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('diagnostics-presenters', name, args, stub);
}
