/**
 * Tool presentation registry contracts: what one tool declares (localized
 * title, one-line summary, expanded view) and the ports it may use.
 * Presenters are pure functions of one projected round plus the work
 * directory; they never read transport, storage or the DOM.
 */
import type { Translator } from '../../../i18n';

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
  taskTitle(assignmentId: string, conversationId?: string): string;
  acceptance(assignmentId: string): readonly string[];
  conversationTitle(conversationId: string): string;
}

export interface ToolPresenterContext {
  readonly t: Translator;
  readonly markdown: (source: string) => string;
  readonly work: WorkDirectory;
}

export interface ToolPresenter {
  /** Optional narrowing for shared tool names (poll serves several owners). */
  applies?(view: ToolView, context: ToolPresenterContext): boolean;
  title(view: ToolView, context: ToolPresenterContext): string;
  summary(view: ToolView, context: ToolPresenterContext): ToolSummary;
  body(view: ToolView, context: ToolPresenterContext): string;
  debug?(view: ToolView, context: ToolPresenterContext): readonly ToolDebugField[];
}

export interface ToolPresentationPorts {
  readonly translate: Translator;
  readonly markdown: (source: string) => string;
  readonly conversationTitle: (conversationId: string) => string | undefined;
}

/** Row chrome owned by the retained dispatcher (icon, pills, badges, controls). */
export interface ToolCardChrome {
  readonly iconHtml: string;
  readonly rootPillHtml?: string;
  readonly compactionLabelHtml?: string;
  readonly badgeHtml?: string;
  readonly rightControlsHtml?: string;
}
