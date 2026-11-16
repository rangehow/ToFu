/** The one ask_human answer control. Every surface that lets the owner answer a question mounts it: the
 * classic conversation's question card (through the rich-response renderer's choice/form prompt blocks),
 * the studio person panel's question card and the sidebar's lead office (which opens the conversation at
 * that card). The interaction ports bind it to one (conversation, Turn, question) and carry the existing
 * answer path: `answer_guidance` for a waiting question, a quoted chat reply for a non-blocking one
 * (conversation/application/choice-interaction.ts). This module only picks the view for the question kind.
 * Entry points: guidancePromptBlock, mountGuidanceAnswer. */
import { renderChoices, type ChoicePorts, type ChoiceRenderOptions } from './choices';
import { renderGuidanceForm } from './guidance-form';
import type { choice_prompt, form_prompt } from './contract.generated';

export type GuidanceAnswerMode = 'choice' | 'free_text' | 'batch';

export function isGuidanceAnswerMode(value: unknown): value is GuidanceAnswerMode {
  return value === 'choice' || value === 'free_text' || value === 'batch';
}

/** The rich-response block that renders one question: options as a choice prompt, text and forms as a form. */
export function guidancePromptBlock(mode: GuidanceAnswerMode, interactionId: string, title: string): choice_prompt | form_prompt {
  return mode === 'choice'
    ? { type: 'choice_prompt', id: 'human-choice', title, interactionId }
    : { type: 'form_prompt', id: 'human-choice', title, interactionId };
}

export interface GuidanceAnswerMount { node: HTMLElement; dispose(): void }

/** Mounts the control for a question whose kind is not known yet (a surface that holds only its id):
 * the first load decides the view, and that view's own first load reuses it rather than reading again. */
export function mountGuidanceAnswerFor(doc: Document, interactionId: string, ports: ChoicePorts | undefined,
  renderMarkdown?: (source: string) => string, options: ChoiceRenderOptions = {}): GuidanceAnswerMount {
  const host = doc.createElement('div');
  host.className = 'guidance-answer-host';
  host.setAttribute('aria-busy', 'true');
  const status = doc.createElement('p');
  status.className = 'rich-guidance-status';
  status.setAttribute('role', 'status');
  status.textContent = ports?.text?.('rich.loadingChoices', 'Loading question…') ?? 'Loading question…';
  host.append(status);
  if (!ports) {
    status.textContent = 'This question is unavailable in this context.';
    host.removeAttribute('aria-busy');
    return { node: host, dispose() {} };
  }
  const controller = new AbortController();
  let inner: GuidanceAnswerMount | null = null;
  let disposed = false;
  const first = ports.load(interactionId, controller.signal);
  first.then(value => {
    if (disposed) return;
    let primed: Promise<typeof value> | null = Promise.resolve(value);
    const once: ChoicePorts = { ...ports, load: (id, signal) => {
      const ready = primed;
      primed = null;
      return ready && id === interactionId ? ready : ports.load(id, signal);
    } };
    inner = mountGuidanceAnswer(doc, isGuidanceAnswerMode(value.mode) ? value.mode : 'choice', interactionId, once, renderMarkdown, options);
    host.replaceChildren(inner.node);
    host.removeAttribute('aria-busy');
  }, error => {
    if (disposed) return;
    ports.reportError?.(error);
    status.textContent = ports.text?.('rich.answerRejected', 'Answer unavailable or rejected. Reload or retry.')
      ?? 'Answer unavailable or rejected. Reload or retry.';
    host.removeAttribute('aria-busy');
  });
  return { node: host, dispose() {
    if (disposed) return;
    disposed = true;
    controller.abort();
    inner?.dispose();
  } };
}

export function mountGuidanceAnswer(doc: Document, mode: GuidanceAnswerMode, interactionId: string, ports: ChoicePorts | undefined,
  renderMarkdown?: (source: string) => string, options: ChoiceRenderOptions = {}): GuidanceAnswerMount {
  const block = guidancePromptBlock(mode, interactionId, '');
  const view = block.type === 'choice_prompt'
    ? renderChoices(block, ports, doc, renderMarkdown, options)
    : renderGuidanceForm(doc, block, ports, renderMarkdown);
  view.node.classList.add('guidance-answer');
  view.node.dataset.guidanceId = interactionId;
  return { node: view.node, dispose: () => view.dispose() };
}
