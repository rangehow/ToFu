/**
 * Responsibility: probe Sidecar readiness and own the persistent storage
 * warning (since when, the cause, and whether only an explicit restart
 * recovers) plus its bounded, visibility-aware recovery poll.
 * Triggers: the startup check and every response that reports unavailable
 * storage; the health payload is storage-free, so it answers while fenced.
 * Entry point: createStorageAvailabilityMonitor. Dependencies: injected
 * health, copy, DOM, scheduler, visibility, and logging ports only.
 */

import type {
  AvailabilityHealthProbeResponse,
  AvailabilityLogger,
  AvailabilitySchedule,
} from './availability-monitor-ports';

export interface StorageAvailabilityCopy {
  unavailableTitle(): string;
  unavailableDescription(): string;
  /** The fence's wall-clock start, as local time. */
  unavailableSince(epochMs: number): string;
  /** Storage stopped recovering by itself; only an explicit restart helps. */
  restartRequired(): string;
  dismiss(): string;
}

export interface StorageAvailabilityPorts {
  readonly document: Document;
  readonly schedule: AvailabilitySchedule;
  readonly copy: StorageAvailabilityCopy;
  readonly log: AvailabilityLogger;
  warningIconHtml(): string;
  isVisible(): boolean;
  probeHealth(
    timeoutMs: number,
  ): Promise<AvailabilityHealthProbeResponse | null>;
  /** While the cold-start boot splash owns the not-ready window, this
   * banner stays out of its way (the splash carries the richer per-phase
   * truth and the retry path). Post-ready storage blips are unaffected:
   * the splash is gone by then and answers false. */
  isSuppressed?(): boolean;
}

export interface StorageAvailabilityMonitor {
  check(): Promise<void>;
  destroy(): void;
}

export const STORAGE_AVAILABILITY_POLICY = Object.freeze({
  probeTimeoutMs: 3_000,
  recoveryPollMs: 15_000,
});

const STORAGE_WARNING_ID = 'storage-warning-banner';
const COPY_KEYS = Object.freeze({
  unavailableTitle: 'conn.storageUnavailableTitle',
  unavailableDescription: 'conn.storageUnavailableDesc',
  unavailableSince: 'conn.storageUnavailableSince',
  restartRequired: 'conn.storageRestartRequired',
  dismiss: 'conn.dismiss',
});

const FALLBACK_COPY = Object.freeze({
  unavailableTitle: '存储服务暂时不可用',
  unavailableDescription: '持久化操作已安全暂停，服务器正在自动恢复存储连接。',
  restartRequired: '存储已停止自动恢复，需要显式重启 Tofu（serverctl restart）。',
  dismiss: '关闭',
});

interface StorageReading {
  readonly ready: boolean;
  readonly sinceMs: number | null;
  readonly cause: string;
  readonly restartRequired: boolean;
}

function objectOrNull(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

/** Project /api/health's storage block: the runtime snapshot plus, while an
 * attached process is fenced, the storage host's own terminal verdict. */
function readStoragePayload(payload: unknown): StorageReading {
  const storage = objectOrNull(objectOrNull(payload)?.storage);
  const host = objectOrNull(storage?.host);
  const hostFailed = host?.state === 'failed';
  const text = (value: unknown): string => (
    typeof value === 'string' ? value.trim() : '');
  const since = Number(storage?.unavailable_since);
  return {
    ready: storage?.ready === true,
    sinceMs: Number.isFinite(since) && since > 0 ? Math.round(since * 1_000) : null,
    cause: (hostFailed ? text(host?.error) : '') || text(storage?.last_error),
    restartRequired: hostFailed || storage?.degraded === true,
  };
}

export function createStorageAvailabilityMonitor(
  ports: StorageAvailabilityPorts,
): StorageAvailabilityMonitor {
  const browserDocument = ports.document;
  let recoveryPollTimer: number | null = null;
  let destroyed = false;
  let generation = 0;
  let probeInFlight: Promise<StorageReading | null> | null = null;

  const copyOrFallback = (
    read: () => string,
    key: string,
    fallback: string,
  ): string => {
    try {
      const value = read();
      if (value && value !== key) return value;
    } catch (error: unknown) {
      ports.log.debug('[StorageAvailability] copy lookup failed', error);
    }
    return fallback;
  };

  const stopRecoveryPoll = (): void => {
    if (recoveryPollTimer === null) return;
    ports.schedule.clearInterval(recoveryPollTimer);
    recoveryPollTimer = null;
  };

  const clearWarning = (recovered: boolean): void => {
    const banner = browserDocument.getElementById(STORAGE_WARNING_ID);
    if (banner) {
      banner.remove();
      if (recovered) {
        ports.log.info(
          '[StorageAvailability] Sidecar ready again; warning cleared',
        );
      }
    }
    stopRecoveryPoll();
  };

  const showWarning = (): void => {
    if (destroyed || browserDocument.getElementById(STORAGE_WARNING_ID)) return;
    const banner = browserDocument.createElement('div');
    banner.id = STORAGE_WARNING_ID;
    banner.setAttribute('role', 'alert');
    banner.setAttribute('aria-live', 'assertive');
    banner.style.cssText =
      'position:fixed;top:0;left:0;right:0;z-index:10000;' +
      'background:#dc2626;color:#fff;padding:10px 16px;font-size:14px;' +
      'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3);' +
      'display:flex;align-items:center;justify-content:center;gap:8px;';

    const icon = browserDocument.createElement('span');
    icon.style.display = 'inline-flex';
    icon.innerHTML = ports.warningIconHtml();
    const message = browserDocument.createElement('span');
    const title = browserDocument.createElement('strong');
    title.textContent = copyOrFallback(
      ports.copy.unavailableTitle,
      COPY_KEYS.unavailableTitle,
      FALLBACK_COPY.unavailableTitle,
    );
    const detail = browserDocument.createElement('span');
    detail.className = 'saw-detail';
    detail.style.opacity = '.92';
    message.append(
      title,
      ` — ${copyOrFallback(
        ports.copy.unavailableDescription,
        COPY_KEYS.unavailableDescription,
        FALLBACK_COPY.unavailableDescription,
      )}`,
      detail,
    );
    const dismiss = browserDocument.createElement('button');
    dismiss.type = 'button';
    dismiss.textContent = copyOrFallback(
      ports.copy.dismiss,
      COPY_KEYS.dismiss,
      FALLBACK_COPY.dismiss,
    );
    dismiss.style.cssText =
      'background:rgba(255,255,255,.2);border:none;color:#fff;padding:4px 10px;' +
      'border-radius:4px;cursor:pointer;font-size:13px;margin-left:12px;' +
      'white-space:nowrap;';
    dismiss.addEventListener('click', () => clearWarning(false));
    banner.append(icon, message, dismiss);
    browserDocument.body.prepend(banner);
  };

  const renderDetail = (reading: StorageReading): void => {
    const detail = browserDocument.getElementById(STORAGE_WARNING_ID)
      ?.querySelector('.saw-detail');
    if (!detail) return;
    const parts: string[] = [];
    const sinceMs = reading.sinceMs;
    if (sinceMs !== null) {
      parts.push(copyOrFallback(
        () => ports.copy.unavailableSince(sinceMs),
        COPY_KEYS.unavailableSince,
        new Date(sinceMs).toLocaleTimeString(),
      ));
    }
    if (reading.cause) parts.push(reading.cause);
    if (reading.restartRequired) {
      parts.push(copyOrFallback(
        ports.copy.restartRequired,
        COPY_KEYS.restartRequired,
        FALLBACK_COPY.restartRequired,
      ));
    }
    detail.textContent = parts.length ? ` (${parts.join(' · ')})` : '';
  };

  const readStorageState = (
    expectedGeneration: number,
    context: string,
  ): Promise<StorageReading | null> => {
    if (probeInFlight) return probeInFlight;
    const request = (async (): Promise<StorageReading | null> => {
      try {
        const response = await ports.probeHealth(
          STORAGE_AVAILABILITY_POLICY.probeTimeoutMs,
        );
        if (destroyed || generation !== expectedGeneration) return null;
        if (!response?.ok) return null;
        if (typeof response.json !== 'function') {
          throw new Error('health response has no JSON reader');
        }
        return readStoragePayload(await response.json());
      } catch (error: unknown) {
        ports.log.debug(
          `[StorageAvailability] ${context} failed (unreachable or bad payload)`,
          error,
        );
        return null;
      }
    })();
    probeInFlight = request;
    void request.then(() => {
      if (probeInFlight === request) probeInFlight = null;
    });
    return request;
  };

  const startRecoveryPoll = (): void => {
    if (destroyed || recoveryPollTimer !== null) return;
    recoveryPollTimer = ports.schedule.setInterval(() => {
      if (!browserDocument.getElementById(STORAGE_WARNING_ID)) {
        stopRecoveryPoll();
        return;
      }
      if (!ports.isVisible()) return;
      const expectedGeneration = generation;
      void readStorageState(expectedGeneration, 'recovery poll').then((reading) => {
        if (reading === null || destroyed || generation !== expectedGeneration) return;
        if (reading.ready) clearWarning(true);
        else renderDetail(reading);
      });
    }, STORAGE_AVAILABILITY_POLICY.recoveryPollMs);
  };

  const check = async (): Promise<void> => {
    if (destroyed) return;
    if (ports.isSuppressed?.() === true) return;
    const expectedGeneration = generation;
    const reading = await readStorageState(expectedGeneration, 'availability check');
    if (destroyed || generation !== expectedGeneration || reading === null) return;
    if (reading.ready) {
      clearWarning(true);
      return;
    }
    if (ports.isSuppressed?.() === true) return;
    showWarning();
    renderDetail(reading);
    startRecoveryPoll();
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    generation += 1;
    clearWarning(false);
  };

  return Object.freeze({ check, destroy });
}
