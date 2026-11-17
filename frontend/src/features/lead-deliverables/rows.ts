/** One lead deliverable as a row: type glyph, the whole title, then its kind, project and age; the files
 * a report attached as chips.
 *
 * Shared by the task board's expanded tasks (coordinator-task-deliverables.ts). Owner requests 2026-11-17:
 * titles wrap at word boundaries and never clip, the kind reads as a word in the glyph's ink, and hovering
 * previews the report's opening paragraph. Clicking the row opens the deliverable itself through the shared
 * opener; each chip opens its file; the arrow goes to the lead's message at the exact Turn. Report rows read
 * in the conversation's displayed language.
 * Entry point: deliverableRow. Dependencies: i18n, icons, the item model and the opener contract.
 */
import { t } from '../../i18n';
import { iconHtml } from '../../icons';
import {
  deliverableAge, deliverableCopy, deliverablePreview, deliverableProject, deliverableVisual,
  type DeliverableVisual, type LeadDeliverable,
} from './model';
import type { DeliverableOpener } from './open';
import './lead-deliverables.css';

export interface DeliverableRowOptions {
  opener(): Promise<DeliverableOpener>;
  now(): number;
  /** Name the project in the meta line (items of another project than the lead's). */
  showProject?: boolean;
}

const GLYPHS: Readonly<Record<DeliverableVisual, string>> = {
  delivery: 'checkCircle', progress: 'chart', decision: 'messageCircle', blocked: 'alertTriangle', report: 'inbox',
  deck: 'slides', pdf: 'file', sheet: 'fileSheet', image: 'image', document: 'file', file: 'fileCode', merge: 'gitMerge',
};

function badgeKey(item: LeadDeliverable, visual: DeliverableVisual) {
  if (item.kind === 'report' && item.reportKind) return `leadDeliverables.reportKind.${item.reportKind}` as const;
  if (visual === 'pdf') return 'leadDeliverables.kind.pdf' as const;
  return `leadDeliverables.kind.${item.kind}` as const;
}

function fileItem(item: LeadDeliverable, file: NonNullable<LeadDeliverable['files']>[number]): LeadDeliverable {
  return { id: 'artifact:' + file.artifactId, kind: file.kind, title: file.filename, projectPath: item.projectPath,
    createdAt: item.createdAt, source: item.source, artifactId: file.artifactId };
}

export function deliverableRow(item: LeadDeliverable, options: DeliverableRowOptions): HTMLLIElement {
  const locale = document.documentElement.lang || undefined;
  const open = (target: LeadDeliverable) => { void options.opener().then(opener => opener.open(target)); };
  const visual = deliverableVisual(item);
  const li = document.createElement('li');
  li.className = 'lead-deliverable';
  li.dataset.deliverableId = item.id;
  li.dataset.kind = item.kind;
  li.dataset.visual = visual;
  if (item.reportKind) li.dataset.reportKind = item.reportKind;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'lead-deliverable__open';
  // Display title, the original when translated, then the report's opening paragraph.
  button.title = deliverablePreview(item, locale);
  const icon = document.createElement('span');
  icon.className = 'lead-deliverable__icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.innerHTML = iconHtml(GLYPHS[visual]);
  const text = document.createElement('span');
  text.className = 'lead-deliverable__text';
  const title = document.createElement('span');
  title.className = 'lead-deliverable__title';
  title.textContent = deliverableCopy(item).title;
  const meta = document.createElement('span');
  meta.className = 'lead-deliverable__meta';
  const kind = document.createElement('span');
  kind.className = 'lead-deliverable__kind';
  kind.textContent = t(badgeKey(item, visual));
  const project = document.createElement('span');
  project.className = 'lead-deliverable__project';
  project.textContent = deliverableProject(item.projectPath);
  project.hidden = !options.showProject || !item.projectPath;
  if (item.projectPath) project.title = item.projectPath;
  const age = document.createElement('span');
  age.className = 'lead-deliverable__age';
  age.textContent = deliverableAge(item.createdAt, options.now(), locale);
  meta.append(kind, project, age);
  text.append(title, meta);
  button.append(icon, text);
  button.addEventListener('click', () => open(item));
  li.append(button);
  if (item.source) {
    const source = document.createElement('button');
    source.type = 'button';
    source.className = 'lead-deliverable__source';
    source.setAttribute('aria-label', t('leadDeliverables.source'));
    source.title = t('leadDeliverables.source');
    source.innerHTML = iconHtml('arrowRight');
    source.addEventListener('click', event => {
      event.stopPropagation();
      void options.opener().then(opener => opener.openSource(item));
    });
    li.append(source);
  }
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
      chip.innerHTML = iconHtml(file.kind === 'deck' ? 'slides' : 'file');
      const name = document.createElement('span');
      name.textContent = file.filename;
      chip.append(name);
      chip.title = file.path || file.filename;
      chip.setAttribute('aria-label', t('leadDeliverables.openFile', { name: file.filename }));
      chip.addEventListener('click', () => open(fileItem(item, file)));
      group.append(chip);
    }
    li.append(group);
  }
  return li;
}
