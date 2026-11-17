/**
 * Coordinator report card: one `report_to_user` message read inside the reply
 * that published it (view-model/report-placement.ts), after the reply's prose.
 * Its head row reads like the reply's process folds: report label, the
 * report's title, its time and a chevron. A delivery, decision or blocked
 * report is created open, a progress report folded; from then on the open
 * state is the reader's. The files the call attached stay in view below the
 * head as chips that open each file in the artifact panel (the surface's
 * `open-artifact` intent). The body reuses the text renderer without a Turn,
 * so it keeps the copy control and translation fold but never a reply's
 * answer toolbar.
 * Entry point: renderCoordinatorReportBlock.
 * Dependencies: view-model types, ports, text-blocks, stable-dom, localized-copy, icons.
 */
import type { CoordinatorReportBlockViewModel, CoordinatorReportFile } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { renderTextBlock } from './text-blocks';
import { directClassChild, setStableAttribute, setStableData, setStableText } from './stable-dom';
import { coordinatorReportLabel, textFor, timestampPresentation } from './localized-copy';
import { disclosureChevron, lineSvgIcon } from './icons';

const FILE_ICON_PATHS: Readonly<Record<CoordinatorReportFile['kind'], ReadonlyArray<string>>> = {
  file: ['M4.5 2.25h4.75L12 5v8.75H4.5V2.25Z', 'M9.25 2.25V5H12'],
  document: ['M4.5 2.25h4.75L12 5v8.75H4.5V2.25Z', 'M9.25 2.25V5H12', 'M6.25 8h3.5M6.25 10.5h3.5'],
  deck: ['M2.75 3.25h10.5v7H2.75v-7Z', 'M8 10.25v2.5M5.75 12.75h4.5'],
};

/** Reports the owner acts on or receives open by default; progress folds. */
const OPEN_BY_DEFAULT = new Set(['delivery', 'decision', 'blocked']);

function renderFiles(node: HTMLElement, files: ReadonlyArray<CoordinatorReportFile>,
  ports: ClassicConversationRendererPorts): void {
  let row = directClassChild<HTMLElement>(node, 'conversation-coordinator-report__files');
  if (!files.length) {
    row?.remove();
    return;
  }
  const document = node.ownerDocument;
  if (!row) {
    row = document.createElement('div');
    row.className = 'conversation-coordinator-report__files';
    node.append(row);
  }
  const label = textFor(ports, 'conversation.coordinatorReport.files', 'Files');
  setStableAttribute(row, 'role', 'list');
  setStableAttribute(row, 'aria-label', label);
  const signature = JSON.stringify(files);
  if (row.dataset.files === signature) return;
  row.dataset.files = signature;
  row.replaceChildren(...files.map((file) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'conversation-coordinator-report__file';
    chip.setAttribute('role', 'listitem');
    chip.dataset.conversationAction = 'open-artifact';
    chip.dataset.operation = file.artifactId;
    chip.dataset.fileKind = file.kind;
    chip.title = file.path || file.filename;
    chip.setAttribute('aria-label', textFor(ports, 'conversation.coordinatorReport.openFile', 'Open {name}',
      { name: file.filename }));
    const name = document.createElement('span');
    name.className = 'conversation-coordinator-report__file-name';
    name.textContent = file.filename;
    chip.append(lineSvgIcon(document, FILE_ICON_PATHS[file.kind]), name);
    return chip;
  }));
}

export function renderCoordinatorReportBlock(
  node: HTMLElement,
  block: CoordinatorReportBlockViewModel,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-coordinator-report';
  setStableData(node, 'reportKind', block.reportKind);
  setStableData(node, 'reportTurnId', block.reportTurnId);
  // Created open or folded by kind; from then on the open state is the
  // reader's, so no repaint writes it.
  let fold = directClassChild<HTMLDetailsElement>(node, 'conversation-coordinator-report__fold');
  if (!fold) {
    fold = document.createElement('details');
    fold.className = 'conversation-coordinator-report__fold';
    fold.open = OPEN_BY_DEFAULT.has(block.reportKind);
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
    node.prepend(fold);
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
  renderFiles(node, block.files ?? [], ports);
}
