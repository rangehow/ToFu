/**
 * Coordinator report card: one `report_to_user` message read inside the reply
 * that published it (view-model/report-placement.ts). It reads as one folded
 * row in the grammar of the reply's process folds: report label, the report's
 * title, its time and a chevron. The reader opens it; the body reuses the text
 * renderer without a Turn, so it keeps the copy control and translation fold
 * but never a reply's answer toolbar.
 * Entry point: renderCoordinatorReportBlock.
 * Dependencies: view-model types, ports, text-blocks, stable-dom, localized-copy, icons.
 */
import type { CoordinatorReportBlockViewModel } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { renderTextBlock } from './text-blocks';
import { directClassChild, setStableAttribute, setStableData, setStableText } from './stable-dom';
import { coordinatorReportLabel, timestampPresentation } from './localized-copy';
import { disclosureChevron } from './icons';

export function renderCoordinatorReportBlock(
  node: HTMLElement,
  block: CoordinatorReportBlockViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-coordinator-report';
  setStableData(node, 'reportKind', block.reportKind);
  setStableData(node, 'reportTurnId', block.reportTurnId);
  // Created folded; from then on the open state is the reader's, so no repaint
  // writes it.
  let fold = directClassChild<HTMLDetailsElement>(node, 'conversation-coordinator-report__fold');
  if (!fold) {
    fold = document.createElement('details');
    fold.className = 'conversation-coordinator-report__fold';
    const head = document.createElement('summary');
    head.className = 'conversation-coordinator-report__head';
    const label = document.createElement('span');
    label.className = 'conversation-coordinator-report__label';
    const title = document.createElement('span');
    title.className = 'conversation-coordinator-report__title';
    const time = document.createElement('time');
    time.className = 'conversation-coordinator-report__time';
    head.append(label, title, time, disclosureChevron(document, 'conversation-coordinator-report__chevron'));
    // The text renderer owns its node's class, so the body is found by role.
    const body = document.createElement('div');
    body.dataset.coordinatorReportBody = '';
    fold.append(head, body);
    node.append(fold);
  }
  const head = fold.firstElementChild as HTMLElement;
  const [label, title, time] = Array.from(head.children) as [HTMLElement, HTMLElement, HTMLTimeElement];
  setStableText(label, coordinatorReportLabel(ports, block.reportKind));
  setStableText(title, block.title);
  const presentation = timestampPresentation(block.createdAt, ports);
  setStableText(time, presentation.short);
  setStableAttribute(time, 'datetime', presentation.dateTime || null);
  setStableAttribute(time, 'title', presentation.exact || null);
  renderTextBlock(fold.lastElementChild as HTMLElement, block.text, ports);
}
