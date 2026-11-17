/** Devices shell ports: binds the one shared devices store to the application's push transport.
 * Entry point: getDevicesStore. Dependencies: retained push bindings through the feature registry.
 */
import { featureRegistry, readLiveRuntimeBinding } from '../../feature-registry';
import { createDevicesStore, type DevicesStore, type PushPorts } from './live';

type Handler = (frame: unknown) => void;
type Subscribe = (channel: string, taskId: string, handler: Handler) => void;

function pushPorts(): PushPorts | null {
  const subscribe = readLiveRuntimeBinding('pushSubscribe') as Subscribe | undefined;
  const unsubscribe = readLiveRuntimeBinding('pushUnsubscribe') as Subscribe | undefined;
  const reconnect = readLiveRuntimeBinding('pushOnReconnect') as ((fn: () => void) => () => void) | undefined;
  const latency = readLiveRuntimeBinding('pushOnLatency') as
    ((fn: (reading: { connected?: boolean }) => void) => () => void) | undefined;
  const isOurs = (featureRegistry as unknown as { _frameIsOurs?: (userId: unknown) => boolean })._frameIsOurs;
  if (typeof subscribe !== 'function' || typeof unsubscribe !== 'function') return null;
  return {
    subscribe(handler) {
      subscribe('notify', '*', handler);
      return () => unsubscribe('notify', '*', handler);
    },
    onReconnect: listener => (typeof reconnect === 'function' ? reconnect(listener) : () => {}),
    onConnection: typeof latency === 'function'
      ? listener => latency(reading => listener(reading.connected !== false))
      : undefined,
    isOurs: typeof isOurs === 'function' ? isOurs : undefined,
  };
}

let store: DevicesStore | null = null;

export function getDevicesStore(): DevicesStore {
  if (!store) store = createDevicesStore(pushPorts());
  return store;
}
