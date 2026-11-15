/**
 * Injected localized copy, placeholder fallback, timestamp, duration and
 * diagnostic text formatting. No DOM, transport or catalog.
 * Entry points: textFor, coordinatorReportLabel, timestampPresentation,
 * formatDuration, displayJson.
 * Dependencies: ports.
 */
import type { ClassicConversationRendererPorts } from './ports';

export function textFor(
  ports: Pick<ClassicConversationRendererPorts, 'localizedText'>,
  key: string,
  fallback: string,
  values?: Readonly<Record<string, string | number>>,
): string {
  const localized = ports.localizedText?.(key, fallback, values);
  if (localized != null) return localized;
  if (!values) return fallback;
  /* A port-less fallback must still be renderable — substitute the same
   * {placeholder} vocabulary translateMessage applies. */
  return fallback.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name: string) => (
    Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name] ?? '')
      : token
  ));
}

/* Report kinds of `reportRequest.kind` in project_coordination_v1.schema.json. */
const COORDINATOR_REPORT_KINDS: Readonly<Record<string, readonly [string, string]>> = {
  progress: ['conversation.coordinatorReport.progress', 'Progress'],
  decision: ['conversation.coordinatorReport.decision', 'Decision'],
  blocked: ['conversation.coordinatorReport.blocked', 'Blocked'],
  delivery: ['conversation.coordinatorReport.delivery', 'Delivery'],
};

/** The Coordinator's own report, e.g. "Coordinator report · Progress". */
export function coordinatorReportLabel(
  ports: Pick<ClassicConversationRendererPorts, 'localizedText'>,
  reportKind: string,
): string {
  const title = textFor(ports, 'conversation.coordinatorReport.title', 'Coordinator report');
  const kind = COORDINATOR_REPORT_KINDS[reportKind];
  return kind ? `${title} · ${textFor(ports, kind[0], kind[1])}` : title;
}

export function timestampPresentation(
  timestamp: string | number,
  ports: Pick<ClassicConversationRendererPorts, 'formatTimestamp'>,
): { short: string; exact: string; dateTime: string } {
  const date = new Date(timestamp);
  const valid = Number.isFinite(date.getTime());
  const exact = ports.formatTimestamp?.(timestamp)
    ?? (valid ? date.toLocaleString() : String(timestamp ?? ''));
  return {
    short: valid ? date.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }) : exact,
    exact,
    dateTime: valid ? date.toISOString() : '',
  };
}

export function formatDuration(seconds: number | undefined): string {
  const rounded = Math.max(0, Math.round(seconds ?? 0));
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

export function displayJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value ?? '');
  }
}
