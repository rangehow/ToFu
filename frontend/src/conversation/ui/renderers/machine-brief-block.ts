/**
 * Machine brief: the text of a `virtual_user` input Turn the project brain
 * wrote (a Coordinator's automatic continuation, a Worker's delegated brief).
 * It is instructions for the agent, not a message for the owner, so it reads
 * as one folded row: a label, the brief's first line and a chevron. Opening it
 * renders the full brief once through the ordinary text renderer; nothing is
 * parsed or rendered while it stays folded.
 * Entry points: isMachineBrief, renderMachineBriefBlock.
 * Dependencies: view-model types, ports, text-blocks, stable-dom, localized-copy, icons.
 */
import type { ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import type { TextBlockViewModel } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { renderTextBlock } from './text-blocks';
import { directClassChild, setStableText } from './stable-dom';
import { textFor } from './localized-copy';
import { disclosureChevron } from './icons';

export function isMachineBrief(turn: ConversationTurnViewModel | undefined): turn is ConversationTurnViewModel {
  return turn?.actor === 'virtual_user' && turn.metadata.origin?.initiator === 'brain';
}

function firstLine(markdown: string): string {
  for (const line of markdown.split('\n')) {
    const text = line.replace(/^[#>*\-\s]+/, '').trim();
    if (text) return text;
  }
  return '';
}

interface BriefState { block: TextBlockViewModel; rendered: TextBlockViewModel | null }
const briefStates = new WeakMap<HTMLElement, BriefState>();

export function renderMachineBriefBlock(
  node: HTMLElement,
  block: TextBlockViewModel,
  ports: ClassicConversationRendererPorts,
  turn: ConversationTurnViewModel,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-machine-brief';
  let fold = directClassChild<HTMLDetailsElement>(node, 'conversation-machine-brief__fold');
  if (!fold) {
    node.replaceChildren();
    fold = document.createElement('details');
    fold.className = 'conversation-machine-brief__fold';
    const head = document.createElement('summary');
    head.className = 'conversation-machine-brief__head';
    const label = document.createElement('span');
    label.className = 'conversation-machine-brief__label';
    const preview = document.createElement('span');
    preview.className = 'conversation-machine-brief__preview';
    head.append(label, preview, disclosureChevron(document, 'conversation-machine-brief__chevron'));
    const body = document.createElement('div');
    body.className = 'conversation-machine-brief__body';
    fold.append(head, body);
    node.append(fold);
    const target = fold;
    fold.addEventListener('toggle', () => {
      const state = briefStates.get(node);
      if (target.open && state && state.rendered !== state.block) {
        renderTextBlock(target.lastElementChild as HTMLElement, state.block, ports, turn);
        state.rendered = state.block;
      }
    });
  }
  const head = fold.firstElementChild as HTMLElement;
  const [label, preview] = Array.from(head.children) as [HTMLElement, HTMLElement];
  setStableText(label, textFor(ports, 'conversation.machineBrief.label', 'Automatic instruction'));
  setStableText(preview, firstLine(block.displayMarkdown ?? block.markdown));
  const state = briefStates.get(node);
  const next: BriefState = { block, rendered: state?.rendered ?? null };
  briefStates.set(node, next);
  if (fold.open && next.rendered !== block) {
    renderTextBlock(fold.lastElementChild as HTMLElement, block, ports, turn);
    next.rendered = block;
  }
}
