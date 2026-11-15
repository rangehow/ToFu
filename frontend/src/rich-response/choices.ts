/** Native ask_human choice controls. Interaction ports are bound by the conversation
 * owner, never by model IDs, URLs or actions. Server projections own answer state;
 * this module owns only transient selection, one request and its disposal fence.
 */
import { createButton, createChoice } from '../ui';

export interface ChoiceInteraction {
  mode?: 'choice' | 'free_text' | 'batch';
  questions?: import('./guidance-values').GuidanceQuestion[];
  interactionId: string;
  question: string;
  options: string[];

  descriptions?: string[];
  state: 'pending' | 'answered' | 'stale';
  response?: string;
  /** Owner-provided status line: a pending note is information, not an unconfirmed answer. */
  note?: string;
}
export interface ChoicePorts {

  reportError?(error: unknown): void;
  load(interactionId: string, signal: AbortSignal): Promise<ChoiceInteraction>;
  /** Resolves only after the existing conversation command durably accepts. */
  submit(interactionId: string, response: string, commandId: string, signal: AbortSignal): Promise<ChoiceInteraction>;
  commandId(): string;
  text?(key: string, fallback: string): string;
}
import type { choice_prompt } from './contract.generated';
export type ChoiceBlock = Pick<choice_prompt, 'interactionId'>;

/** Owned question prose: sanitized host Markdown when supplied, else plain text. */
export function setGuidanceQuestion(target: HTMLElement, source: string, renderMarkdown?: (source: string) => string): void {
  if (target.dataset.source === source) return;
  target.dataset.source = source;
  if (renderMarkdown) target.innerHTML = renderMarkdown(source);
  else target.textContent = source;
}

export function renderChoices(block: ChoiceBlock, ports: ChoicePorts | undefined, doc: Document = document,
  renderMarkdown?: (source: string) => string) {
  const node = doc.createElement('form');
  node.className = 'rich-choices';
  const question = doc.createElement('div'); question.className = 'rich-guidance-question';
  const group = doc.createElement('fieldset');
  const legend = doc.createElement('legend');
  const status = doc.createElement('p'); status.className = 'rich-guidance-status';
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const text = (key: string, fallback: string) => ports?.text?.(key, fallback) ?? fallback;
  legend.textContent = text('rich.choose', 'Choose one'); group.append(legend);
  const submit = createButton({ label: text('rich.submit', 'Submit answer'), variant: 'primary', type: 'submit', size: 'sm', disabled: true }, doc);
  submit.dataset.richControl = 'choices-submit';
  const reload = createButton({ label: text('rich.reload', 'Reload answer'), variant: 'ghost', size: 'sm' }, doc);
  reload.dataset.richControl = 'choices-reload';
  const toolbar = doc.createElement('div'); toolbar.className = 'ui-toolbar rich-guidance-actions'; toolbar.append(submit, reload);
  node.append(question, group, status, toolbar);
  let disposed = false;
  let flight: AbortController | null = null;
  let interaction: ChoiceInteraction | null = null;
  let selected = '';
  let command: { response: string; id: string } | null = null;
  const inputs: HTMLInputElement[] = [];
  // Declared options never bound the owner: the last card always takes an own answer.
  const own = createChoice({ type: 'radio', name: 'choice', label: text('rich.ownAnswer', 'None of these? Write your own answer') }, doc);
  own.el.classList.add('rich-choice-option', 'rich-choice-own');
  own.input.dataset.richControl = 'choice:own';
  const ownBody = doc.createElement('span'); ownBody.className = 'rich-choice-option__text';
  ownBody.append(...[...own.el.childNodes].filter(child => child !== own.input));
  own.el.append(ownBody);
  const ownText = doc.createElement('textarea');
  ownText.className = 'ui-input rich-choice-own-input'; ownText.rows = 2; ownText.maxLength = 32768;
  ownText.placeholder = text('rich.ownAnswerPlaceholder', 'Type your answer…');
  ownText.dataset.richControl = 'choice:own-text';
  ownText.setAttribute('aria-label', text('rich.ownAnswer', 'None of these? Write your own answer'));
  const ownCard = doc.createElement('div'); ownCard.className = 'rich-choice-own-card'; ownCard.append(own.el, ownText);
  const response = () => own.input.checked ? ownText.value.trim() : inputs.find(input => input.checked)?.value ?? '';

  let descriptionsIdentity = '';
  const setDisabled = () => {
    group.disabled = Boolean(flight) || interaction?.state !== 'pending';
    submit.disabled = group.disabled || !selected;
    reload.disabled = Boolean(flight) || !ports;
    node.setAttribute('aria-busy', String(Boolean(flight)));
  };
  const valid = (value: ChoiceInteraction) => value.interactionId === block.interactionId
    && typeof value.question === 'string' && value.question.length <= 32768
    && ['pending', 'answered', 'stale'].includes(value.state)
    && Array.isArray(value.options) && value.options.length >= 1 && value.options.length <= 32
    && value.options.every(option => typeof option === 'string' && option.length > 0 && option.length <= 2000)
    && new Set(value.options).size === value.options.length

    && (!value.descriptions || (Array.isArray(value.descriptions) && value.descriptions.length === value.options.length
      && value.descriptions.every(item => typeof item === 'string' && item.length <= 8192)))
    && (value.state !== 'answered' || typeof value.response === 'string');
  const apply = (value: ChoiceInteraction) => {
    if (!valid(value)) throw new Error('Invalid interaction projection');
    interaction = value;
    setGuidanceQuestion(question, value.question, renderMarkdown);
    // Reuse controls on reload so native keyboard focus is not lost.
    const descriptions = JSON.stringify(value.descriptions ?? []);
    if (descriptions !== descriptionsIdentity || inputs.length !== value.options.length || inputs.some((input, index) => input.value !== value.options[index])) {
      descriptionsIdentity = descriptions;
      group.replaceChildren(legend); inputs.length = 0;
      ownCard.remove();
      value.options.forEach((option, index) => {
        const { el, input } = createChoice({ type: 'radio', name: 'choice', label: option, value: option }, doc);
        el.classList.add('rich-choice-option');
        input.dataset.richControl = `choice:${index}`;
        group.append(el); inputs.push(input);
        // The explanation belongs to its option: one card, one click target.
        const body = doc.createElement('span'); body.className = 'rich-choice-option__text';
        body.append(...[...el.childNodes].filter(child => child !== input));
        el.append(body);
        const description = value.descriptions?.[index];
        if (description) {
          const explanation = doc.createElement('span'); explanation.className = 'rich-choice-description';
          explanation.id = `rich-choice-description-${doc.defaultView?.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)}`;
          explanation.textContent = description; input.setAttribute('aria-describedby', explanation.id);
          body.append(explanation);
        }
      });
      group.append(ownCard);
    }
    if (value.state === 'answered') {
      const answer = value.response ?? '';
      const declared = value.options.includes(answer);
      inputs.forEach(input => { input.checked = declared && input.value === answer; });
      own.input.checked = !declared;
      if (!declared) ownText.value = answer;
    } else if (!own.input.checked && !value.options.includes(selected)) {
      inputs.forEach(input => { input.checked = false; });
    }
    selected = response();
    status.textContent = value.state === 'answered'
      ? `${value.note ?? text('rich.answerSaved', 'Answer saved')}: ${value.response}`
      : value.state === 'stale' ? text('rich.answerStale', 'This question is no longer answerable. Reload to check its current state.') : value.note ?? '';
  };
  const run = async (kind: 'load' | 'submit') => {
    if (disposed || flight || !ports || (kind === 'submit' && (interaction?.state !== 'pending' || !selected))) return;
    const controller = new AbortController(); flight = controller;
    status.textContent = kind === 'load' ? text('rich.loadingChoices', 'Loading question…') : text('rich.submitting', 'Saving answer…');
    setDisabled();
    try {
      if (kind === 'submit' && (!command || command.response !== selected)) command = { response: selected, id: ports.commandId() };
      const value = kind === 'load' ? await ports.load(block.interactionId, controller.signal)
        : await ports.submit(block.interactionId, selected, command!.id, controller.signal);
      if (disposed || controller.signal.aborted) return;
      apply(value);
      if (kind === 'submit' && value.state !== 'answered' && !value.note) {
        status.textContent = text('rich.answerUnconfirmed', 'Answer not confirmed. Reload before retrying.');
        interaction = { ...value, state: 'stale' };
      }
    } catch (error) {
      ports.reportError?.(error);
      if (!disposed && !controller.signal.aborted) {

        if (kind === 'load') interaction = null;
        status.textContent = text('rich.answerRejected', 'Answer unavailable or rejected. Reload or retry.');
      }
    } finally {
      if (!disposed && flight === controller) { flight = null; setDisabled(); }
    }
  };
  const change = () => {
    selected = response();
    setDisabled();
  };
  // Typing an own answer selects it; Ctrl/Cmd+Enter submits like the other guidance inputs.
  const typeOwn = () => { if (!own.input.checked) own.input.checked = true; change(); };
  const ownKey = (event: KeyboardEvent) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void run('submit'); }
  };
  const send = (event: Event) => { event.preventDefault(); void run('submit'); };
  const refresh = () => { void run('load'); };
  group.addEventListener('change', change); node.addEventListener('submit', send); reload.addEventListener('click', refresh);
  ownText.addEventListener('input', typeOwn); ownText.addEventListener('keydown', ownKey);
  if (ports) void run('load');
  else { status.textContent = text('rich.choicesUnavailable', 'This question is unavailable in this context.'); setDisabled(); }
  return { node, dispose() {
    if (disposed) return;
    disposed = true; flight?.abort(); flight = null;
    group.disabled = true; submit.disabled = true; reload.disabled = true;
    group.removeEventListener('change', change); node.removeEventListener('submit', send); reload.removeEventListener('click', refresh);
    ownText.removeEventListener('input', typeOwn); ownText.removeEventListener('keydown', ownKey);
  } };
}
