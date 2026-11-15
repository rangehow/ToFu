/** clientCrash endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createClientCrashEndpoints(ports: EndpointPorts) {
  const { post } = ports;
  const clientCrash = {
    // Unclean-exit report of this tab's previous page instance
    // (contracts/client_crash_v1.schema.json clientCrashReport). Throws ApiError
    // so the journal keeps the report for the next boot when delivery fails.
    report: (payload: JsonObject) => post<JsonObject>('/api/v4/client/crash-report', payload, { onError: 'throw' }),
  };
  return clientCrash;
}
