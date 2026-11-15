/**
 * Value normalization shared by tool presenters: argument/result parsing and
 * small typed readers. Pure; no DOM, no translation, no browser state.
 * Entry points: readToolView, record, text, list, count.
 */

export type UnknownRecord = Readonly<Record<string, unknown>>;

const EMPTY: UnknownRecord = Object.freeze({});

export function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : EMPTY;
}

export function isRecord(value: unknown): value is UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function list(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

export function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function parseObject(source: string): UnknownRecord | null {
  try {
    const parsed: unknown = JSON.parse(source);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** A spilled result arrives as one envelope line followed by its summary body. */
function parseResultText(source: string): { value: UnknownRecord | null; partial: boolean; body: string } {
  const trimmed = source.trim();
  if (!trimmed) return { value: null, partial: false, body: '' };
  const whole = parseObject(trimmed);
  if (whole && !(whole.status === 'partial' && typeof whole.artifactRef === 'string')) {
    return { value: whole, partial: false, body: trimmed };
  }
  const firstBreak = trimmed.indexOf('\n');
  const envelope = parseObject(firstBreak < 0 ? trimmed : trimmed.slice(0, firstBreak));
  if (envelope && envelope.status === 'partial') {
    const rest = firstBreak < 0 ? '' : trimmed.slice(firstBreak + 1);
    const bodyStart = rest.indexOf('\n');
    const body = (/^summary \(/.test(rest) && bodyStart >= 0 ? rest.slice(bodyStart + 1) : rest).trim();
    return { value: parseObject(body), partial: true, body };
  }
  return { value: null, partial: false, body: trimmed };
}

export interface ToolView {
  readonly name: string;
  readonly status: string;
  readonly args: UnknownRecord;
  /** Parsed JSON object result, when the result is one. */
  readonly result: UnknownRecord | null;
  /** Result text as delivered (the spilled summary for a partial result). */
  readonly resultText: string;
  /** The result was spilled and only its beginning is available. */
  readonly partial: boolean;
  /** First result metadata entry attached by the backend. */
  readonly meta: UnknownRecord;
  readonly round: UnknownRecord;
}

export function readToolView(roundValue: unknown): ToolView {
  const round = record(roundValue);
  let args: unknown = round.toolArgs;
  if (typeof args === 'string') args = parseObject(args);
  const meta = record(Array.isArray(round.results) ? round.results[0] : null);
  const content = typeof round.toolContent === 'string' ? round.toolContent : '';
  const snapshot = record(round._snapshotToolResult);
  const source = content || (typeof snapshot.preview === 'string' ? snapshot.preview : '');
  const parsed = parseResultText(source);
  return {
    name: text(round.toolName),
    status: text(round.status),
    args: record(args),
    result: parsed.value,
    resultText: parsed.body,
    partial: parsed.partial || Boolean(!content && snapshot.preview),
    meta,
    round,
  };
}
