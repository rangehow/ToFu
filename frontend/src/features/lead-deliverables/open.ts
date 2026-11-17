/** Opening a lead deliverable: the one open handler of the classic sidebar list and the studio office.
 *
 * `open` shows the deliverable itself in the current mode: a published file, deck or document in the
 * artifact panel (its file preview and deck viewer), a delivery report as the readable report card
 * (the same `report_to_user` card the conversation shows) in a dialog, a merged Worker result at its
 * Turn. `openSource` is the secondary action: the lead's message that delivered it, scrolled to that
 * exact Turn. Modes differ only in `openTurn` (the studio docks the conversation column first).
 * Entry points: createDeliverableOpener, openConversationTurn.
 * Dependencies: the retained runtime's artifact panel, Turn navigation and markdown services (read
 * through feature-registry bindings, never window globals), the tool-card presentation, ui dialog.
 */
import { t } from '../../i18n';
import { createDialog, createButton } from '../../ui';
import { readLiveRuntimeBinding } from '../../feature-registry';
import { createToolCardPresentation } from '../../conversation/ui/tool-cards';
import { deliverableTarget, type LeadDeliverable, type LeadDeliverableTurn } from './model';

export interface DeliverableOpenPorts {
  openArtifact(artifactId: string): void;
  openTurn(conversationId: string, turn: { turnId: string; ordinal?: number }): void;
  markdown(source: string): string;
}

export interface DeliverableOpener {
  /** Show the deliverable itself; false when the item names nothing openable. */
  open(item: LeadDeliverable): boolean;
  /** Go to the lead's message that delivered it; false when that message is unknown. */
  openSource(item: LeadDeliverable): boolean;
}

type OpenAtTurn = (conversationId: string, turn: { turnId: string; ordinal: number }) => Promise<void>;
interface TurnStoreRuntime { ensureRuntimeStore?(conversationId: string): { subscribe(changed: () => void): () => void } }

let pendingReveal: (() => void) | null = null;
/** A Turn without a known ordinal is a recent one: reveal it once the classic Turn store holds it;
 * the subscription ends at the reveal or the next open. */
function revealWhenReady(conversationId: string, turnId: string) {
  const presentation = readLiveRuntimeBinding('ConversationSurfacePresentation') as { revealTurn?(conversation: string, turn: string): boolean } | undefined;
  const turns = readLiveRuntimeBinding('ConversationTurnStore') as TurnStoreRuntime | undefined;
  const reveal = () => Boolean(presentation?.revealTurn?.(conversationId, turnId));
  const store = turns?.ensureRuntimeStore?.(conversationId);
  if (reveal() || !store) return;
  let frame = 0;
  const release = store.subscribe(() => {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; if (reveal()) stop(); });
  });
  const stop = () => { if (frame) cancelAnimationFrame(frame); release(); if (pendingReveal === stop) pendingReveal = null; };
  pendingReveal = stop;
}

/** Open a conversation in the classic column at one Turn: paged in by ordinal when known, else revealed
 * when it arrives. The studio's "show in the conversation" uses this same owner. */
export function openConversationTurn(conversationId: string, turn: { turnId: string; ordinal?: number } | null): void {
  cancelPendingReveal();
  const openAt = readLiveRuntimeBinding('openConversationAtTurn') as OpenAtTurn | undefined;
  if (turn && typeof turn.ordinal === 'number' && typeof openAt === 'function') {
    void openAt(conversationId, { turnId: turn.turnId, ordinal: turn.ordinal });
    return;
  }
  const load = readLiveRuntimeBinding('loadConversation') as ((id: string) => unknown) | undefined;
  load?.(conversationId);
  if (turn) revealWhenReady(conversationId, turn.turnId);
}

export function cancelPendingReveal(): void { pendingReveal?.(); }

/** The classic application's ports; a mode replaces only what it shows differently. */
export function runtimeOpenPorts(overrides: Partial<DeliverableOpenPorts> = {}): DeliverableOpenPorts {
  return {
    openArtifact(artifactId) {
      const artifacts = readLiveRuntimeBinding('Artifacts') as { open?(id: string): unknown } | undefined;
      void artifacts?.open?.(artifactId);
    },
    openTurn: openConversationTurn,
    markdown(source) {
      const render = readLiveRuntimeBinding('renderMarkdown') as ((text: string) => string) | undefined;
      if (typeof render === 'function') return render(source);
      const node = document.createElement('p');
      node.textContent = source;
      return node.outerHTML;
    },
    ...overrides,
  };
}

function reportCard(item: LeadDeliverable, ports: DeliverableOpenPorts): HTMLElement {
  const cards = createToolCardPresentation({ translate: t, markdown: source => ports.markdown(source), conversationTitle: () => undefined });
  const host = document.createElement('div');
  host.className = 'lead-deliverable-report';
  host.innerHTML = cards.renderCardHtml({
    toolName: 'report_to_user', status: 'done', roundNum: 0,
    toolArgs: { title: item.title, message: item.summary || '', kind: item.reportKind || 'delivery' },
    toolContent: JSON.stringify({ conversationId: item.source?.conversationId || '', turnId: item.source?.turnId || '', published: true }),
  }, { iconHtml: '' });
  host.querySelector('details')?.setAttribute('open', '');
  return host;
}

/** The files a report attached, each opening the file itself. */
function reportFiles(item: LeadDeliverable, open: (artifactId: string) => void): HTMLElement | null {
  if (!item.files?.length) return null;
  const group = document.createElement('div');
  group.className = 'lead-deliverable-report__files';
  group.setAttribute('role', 'list');
  group.setAttribute('aria-label', t('leadDeliverables.files'));
  for (const file of item.files) {
    const chip = createButton({ label: file.filename, variant: 'secondary', size: 'sm', onClick: () => open(file.artifactId) });
    chip.setAttribute('role', 'listitem');
    chip.title = file.path || file.filename;
    group.append(chip);
  }
  return group;
}

export function createDeliverableOpener(ports: DeliverableOpenPorts): DeliverableOpener {
  function openSource(item: LeadDeliverable): boolean {
    if (!item.source) return false;
    ports.openTurn(item.source.conversationId, { turnId: item.source.turnId, ordinal: item.source.ordinal });
    return true;
  }

  function showReport(item: LeadDeliverable) {
    const body = reportCard(item, ports);
    const files = reportFiles(item, artifactId => {
      dialog.close(null);
      ports.openArtifact(artifactId);
    });
    if (files) body.append(files);
    const dialog = createDialog({
      title: item.title, body,
      actions: [
        ...(item.source ? [{ id: 'source', label: t('leadDeliverables.source') }] : []),
        { id: 'close', label: t('leadDeliverables.close'), variant: 'primary' as const },
      ],
    });
    dialog.el.classList.add('lead-deliverable-dialog');
    void dialog.open().then(action => { if (action === 'source') openSource(item); });
  }

  return {
    open(item) {
      const target = deliverableTarget(item);
      if (!target) return false;
      if (target.kind === 'artifact') ports.openArtifact(target.artifactId);
      else if (target.kind === 'report') showReport(target.item);
      else ports.openTurn(target.turn.conversationId, { turnId: target.turn.turnId, ordinal: target.turn.ordinal });
      return true;
    },
    openSource,
  };
}

/** The source control for places that render their own row (the sidebar list). */
export function sourceButton(item: LeadDeliverable, opener: DeliverableOpener): HTMLButtonElement | null {
  if (!item.source) return null;
  const button = createButton({ label: t('leadDeliverables.source'), variant: 'ghost', size: 'sm', onClick: event => {
    event.stopPropagation();
    opener.openSource(item);
  } });
  button.classList.add('lead-deliverable__source');
  return button;
}

export type { LeadDeliverableTurn };
