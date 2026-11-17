/** One lead deliverable as a row: kind, title, project and age; the files a report attached as chips.
 *
 * Shared by the task board's expanded tasks (coordinator-task-deliverables.ts). Clicking the row opens
 * the deliverable itself through the shared opener; each chip opens its file; the secondary control goes
 * to the lead's message at the exact Turn. Report rows read in the conversation's displayed language.
 * Entry point: deliverableRow. Dependencies: i18n, ui, the item model and the opener contract.
 */
import { t } from '../../i18n';
import { createButton } from '../../ui';
import { deliverableAge, deliverableCopy, deliverableProject, type LeadDeliverable } from './model';
import type { DeliverableOpener } from './open';

export interface DeliverableRowOptions {
  opener(): Promise<DeliverableOpener>;
  now(): number;
  /** Show the project in the meta line (items of other projects). */
  showProject?: boolean;
}

function fileItem(item: LeadDeliverable, file: NonNullable<LeadDeliverable['files']>[number]): LeadDeliverable {
  return { id: 'artifact:' + file.artifactId, kind: file.kind, title: file.filename, projectPath: item.projectPath,
    createdAt: item.createdAt, source: item.source, artifactId: file.artifactId };
}

export function deliverableRow(item: LeadDeliverable, options: DeliverableRowOptions): HTMLLIElement {
  const open = (target: LeadDeliverable) => { void options.opener().then(opener => opener.open(target)); };
  const li = document.createElement('li');
  li.className = 'lead-deliverable';
  li.dataset.deliverableId = item.id;
  li.dataset.kind = item.kind;
  if (item.reportKind) li.dataset.reportKind = item.reportKind;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'lead-deliverable__open';
  const kind = document.createElement('span');
  kind.className = 'lead-deliverable__kind';
  kind.textContent = item.kind === 'report' && item.reportKind
    ? t(`leadDeliverables.reportKind.${item.reportKind}`) : t(`leadDeliverables.kind.${item.kind}`);
  const title = document.createElement('span');
  title.className = 'lead-deliverable__title';
  const copy = deliverableCopy(item);
  title.textContent = copy.title;
  const meta = document.createElement('span');
  meta.className = 'lead-deliverable__meta';
  const age = deliverableAge(item.createdAt, options.now(), document.documentElement.lang);
  meta.textContent = [options.showProject ? deliverableProject(item.projectPath) : '', age].filter(Boolean).join(' · ');
  if (item.projectPath) meta.title = item.projectPath;
  // A translated title keeps the original as written in its tooltip.
  const written = copy.title === item.title ? [item.title] : [copy.title, item.title];
  if (item.createdAt) written.push(new Date(item.createdAt).toLocaleString(document.documentElement.lang || undefined));
  button.title = written.join('\n');
  button.append(kind, title, meta);
  button.addEventListener('click', () => open(item));
  li.append(button);
  if (item.files?.length) {
    const group = document.createElement('div');
    group.className = 'lead-deliverable__files';
    group.setAttribute('role', 'list');
    group.setAttribute('aria-label', t('leadDeliverables.files'));
    for (const file of item.files) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'lead-deliverable__file';
      chip.setAttribute('role', 'listitem');
      chip.dataset.fileKind = file.kind;
      chip.textContent = file.filename;
      chip.title = file.path || file.filename;
      chip.setAttribute('aria-label', t('leadDeliverables.openFile', { name: file.filename }));
      chip.addEventListener('click', () => open(fileItem(item, file)));
      group.append(chip);
    }
    li.append(group);
  }
  if (item.source) {
    const source = createButton({ label: t('leadDeliverables.source'), variant: 'ghost', size: 'sm', onClick: event => {
      event.stopPropagation();
      void options.opener().then(opener => opener.openSource(item));
    } });
    source.classList.add('lead-deliverable__source');
    li.append(source);
  }
  return li;
}
