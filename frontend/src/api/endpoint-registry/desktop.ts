/** desktop endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports.
 * Only the installer status remains: computer control (nodes, pairing, device
 * waits, diagnostics, SSO relay) has no browser surface since 2026-11-17. */
import type { EndpointPorts } from './contracts';
import type {RequestOptions} from '../transport';
export function createDesktopEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const desktop = {
    /* `arch` is the architecture the CLIENT resolved for itself via
     * navigator.userAgentData.getHighEntropyValues(['architecture']). It is
     * threaded to the server because macOS cannot be narrowed any other way:
     * an Apple Silicon Mac reports "Intel Mac OS X" in its UA, so without this
     * the server must offer BOTH DMGs rather than guess wrong. Omit it and the
     * ambiguous (correct, two-choice) answer comes back. */
    status: (arch = '', options: Pick<RequestOptions, 'signal'> = {}) =>
      get('/api/v4/desktop/status', {onError: 'null', query: arch ? {arch} : {}, ...options}),
  };
  return desktop;
}
