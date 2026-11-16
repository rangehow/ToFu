/** Composer send/translation receipts. Insertion sends retain their original
 * message here without manufacturing an assistant Turn. Browser elapsed time
 * is waiting, never provider execution time. No transport or durable writes. */
import { createDemandScopedPresentationTicker } from './demand-scoped-presentation-ticker';

export type TranslationWait = {
  conversationId: string; startedAt: number; kind: 'send' | 'turn';
  status: 'pending' | 'completed' | 'failed' | 'cancelled';
  elapsedMs?: number; model?: string;
} & SendReceiptPresentation;
export interface SendReceiptPresentation {
  /** Optional original insertion text; never a translated or durable Turn. */
  message?: string;
}
export interface TranslationStatusPorts {
  document: Document;
  activeConversationId(): string | null;
  now(): number;
  label(key: string): string;
}

export function createComposerTranslationStatus(ports: TranslationStatusPorts) {
  const pending = new Set<TranslationWait>();
  let last: TranslationWait | undefined;
  let row: HTMLElement | null = null;
  let disposed = false;
  const win = ports.document.defaultView!;
  const ticker = createDemandScopedPresentationTicker({
    schedule: {
      setTimeout: (callback, delay) => win.setTimeout(callback, delay),
      clearTimeout: handle => win.clearTimeout(handle as number),
    },
    visibility: {
      isHidden: () => ports.document.hidden,
      subscribe: listener => {
        ports.document.addEventListener('visibilitychange', listener);
        return () => ports.document.removeEventListener('visibilitychange', listener);
      },
    },
    tick: () => { render(); return visible()?.status === 'pending'; },
  });
  function visible() {
    const id = ports.activeConversationId();
    const candidates = [...pending].filter(item => item.conversationId === id);
    return candidates.at(-1) ?? (last?.conversationId === id ? last : undefined);
  }
  function render() {
    if (disposed) return;
    const item = visible();
    if (!item) { row?.remove(); row = null; return; }
    const host = ports.document.querySelector('.input-box');
    if (!host) return;
    if (!row) {
      row = ports.document.createElement('div');
      row.className = 'composer-translation-status';
      row.setAttribute('role', 'status');
      // The ticking wait is not a new live-region utterance each second.
      const detail = ports.document.createElement('span');
      detail.className = 'composer-translation-detail';
      detail.setAttribute('aria-live', 'off');
      row.append(detail);
      host.append(row);
    }
    row.dataset.state = item.status;
    // Pending work needs no phase label; only an unresolved outcome is named.
    const outcomeText = item.status === 'failed' ? ports.label('translationWait.failed')
      : item.status === 'cancelled' ? ports.label('preview.cancelled') : '';
    let outcome = row.querySelector<HTMLElement>('.composer-translation-outcome');
    if (outcomeText) {
      if (!outcome) {
        outcome = ports.document.createElement('span');
        outcome.className = 'composer-translation-outcome';
        row.prepend(outcome);
      }
      if (outcome.textContent !== outcomeText) outcome.textContent = outcomeText;
    } else outcome?.remove();
    const detail = row.querySelector<HTMLElement>('.composer-translation-detail')!;
    const seconds = Math.max(0, (item.elapsedMs ?? ports.now() - item.startedAt) / 1000);
    const model = item.model || ports.label(item.status === 'pending'
      ? 'translationWait.modelPending' : 'translationWait.modelUnknown');
    const insertion = item.message !== undefined;
    detail.textContent = (insertion && !item.model ? '' : model + ' · ')
      + ports.label('translationWait.waited') + ' ' + seconds.toFixed(1) + 's';
    row.classList.toggle('has-message', insertion);
    let message = row.querySelector<HTMLElement>('.composer-translation-message');
    if (insertion && item.message) {
      if (!message) {
        message = ports.document.createElement('span');
        message.className = 'composer-translation-message';
        row.append(message);
      }
      if (message.textContent !== item.message) message.textContent = item.message;
      message.title = item.message;
    } else message?.remove();
    row.title = ports.label('translationWait.explanation');
  }
  function refresh() {
    render();
    if (visible()?.status === 'pending') ticker.demand();
    else ticker.stop();
  }
  function begin(conversationId: string, kind: TranslationWait['kind'], presentation: SendReceiptPresentation = {}) {
    const item: TranslationWait = { conversationId, kind, startedAt: ports.now(), status: 'pending', ...presentation };
    if (!disposed) { pending.add(item); last = undefined; refresh(); }
    return item;
  }
  function update(item: TranslationWait, presentation: SendReceiptPresentation) {
    if (disposed || !pending.has(item)) return;
    Object.assign(item, presentation);
    refresh();
  }
  function finish(item: TranslationWait, status: Exclude<TranslationWait['status'], 'pending'>, model?: string) {
    if (!pending.delete(item)) return;
    item.status = status;
    item.elapsedMs = Math.max(0, ports.now() - item.startedAt);
    item.model = model;
    // Successful attribution belongs to the durable message, not the composer.
    // Other in-flight work still wins visible() and retains its demand ticker;
    // success must not erase a failure/cancellation belonging to another wait.
    if (status !== 'completed') last = item;
    refresh();
  }
  function destroy() {
    disposed = true; ticker.stop(); pending.clear(); last = undefined;
    row?.remove(); row = null;
  }
  return { begin, update, finish, refresh, destroy };
}
