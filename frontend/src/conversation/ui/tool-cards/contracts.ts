/**
 * Tool presentation registry contracts: what one tool declares (localized
 * title, one-line summary, expanded view) and the ports it may use.
 * Presenters are pure functions of one projected round plus the work
 * directory; they never read transport, storage or the DOM.
 */
import type { Translator } from '../../../i18n';

import type { ToolCardEnrichmentPorts } from './enrichment';
import type { ToolView } from './values';

export type ToolChipTone = 'ok' | 'warn' | 'err' | 'info' | 'muted';

export interface ToolChip {
  readonly label: string;
  readonly tone: ToolChipTone;
}

/** The collapsed line after the title: plain text plus optional status chips. */
export interface ToolSummary {
  readonly text: string;
  readonly chips?: readonly ToolChip[];
}

/** Identifiers that stay behind the technical-details disclosure. */
export type ToolDebugField = readonly [label: string, value: string];

export interface WorkRecord {
  readonly title: string;
  readonly acceptance: readonly string[];
  readonly conversationId: string;
}

/**
 * Names project work by its human title. Delegations teach it assignment
 * titles and acceptance checks; the conversation catalog names the rest.
 */
export interface WorkDirectory {
  remember(assignmentId: string, work: Partial<WorkRecord>): void;
  /** The assignment was named by a delegation this page has presented. */
  knows(assignmentId: string): boolean;
  /** '' while the assignment's conversation is still being looked up. */
  taskTitle(assignmentId: string, conversationId?: string): string;
  acceptance(assignmentId: string): readonly string[];
  conversationTitle(conversationId: string): string;
}

/**
 * Model-authored free text as the conversation's translation setting shows it:
 * `shown` is the translation when one exists, `original` the authored text
 * then (else ''), `pending` while the translation is still being made.
 */
export interface FreeText {
  readonly shown: string;
  readonly original: string;
  readonly pending: boolean;
}

export interface ToolPresenterContext {
  readonly t: Translator;
  readonly markdown: (source: string) => string;
  readonly work: WorkDirectory;
  readonly freeText: (source: string) => FreeText;
}

export interface ToolPresenter {
  /** Optional narrowing for shared tool names (poll serves several owners). */
  applies?(view: ToolView, context: ToolPresenterContext): boolean;
  title(view: ToolView, context: ToolPresenterContext): string;
  summary(view: ToolView, context: ToolPresenterContext): ToolSummary;
  body(view: ToolView, context: ToolPresenterContext): string;
  debug?(view: ToolView, context: ToolPresenterContext): readonly ToolDebugField[];
  /** A failed call's plain-language reason for a known refusal, else '' (the raw reason stays). */
  failure?(view: ToolView, context: ToolPresenterContext): string;
}

export interface ToolPresentationPorts extends ToolCardEnrichmentPorts {
  readonly translate: Translator;
  readonly markdown: (source: string) => string;
  /** The catalog title, '' or undefined when the catalog does not name it. */
  readonly conversationTitle: (conversationId: string) => string | undefined;
  /** Technical details (IDs, raw result) render only in debug mode. */
  readonly debugEnabled?: () => boolean;
}

/** Row chrome owned by the retained dispatcher (icon, pills, badges, controls). */
export interface ToolCardChrome {
  readonly iconHtml: string;
  readonly rootPillHtml?: string;
  readonly compactionLabelHtml?: string;
  readonly badgeHtml?: string;
  readonly rightControlsHtml?: string;
}
