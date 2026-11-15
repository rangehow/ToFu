/** Typed delivery presenters for the retained artifact panel.
 * Entry points: renderPublishedFile, renderScriptWarning.
 * Dependencies: native UI factories. No path resolution, markup or network IO;
 * only panel-owned published-ID endpoints/actions can initiate delivery.
 */
import type { ArtifactPreviewLink, ArtifactPreviewPage, ArtifactPreviewPages } from '../api/endpoint-registry/artifacts';
import { createButton, createDialog } from '../ui';

const PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
const SAFE_LINK_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);

export interface ArtifactPagePreviewPorts {
  pages(signal: AbortSignal): Promise<ArtifactPreviewPages>;
  page(index: number, signal: AbortSignal): Promise<ArtifactPreviewPage>;
}

export interface ArtifactDeliveryPorts {
  text?(key: string, fallback: string): string;
  download(): void;
  preview?: ArtifactPagePreviewPorts;
}

function formatSize(bytes: unknown): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function extensionOf(filename: string): string {
  const match = /\.([A-Za-z0-9]{1,8})$/.exec(filename);
  return match ? match[1].toUpperCase() : '';
}

/** Renders the attachment card; the returned dispose aborts preview reads and ignores late responses. */
export function renderPublishedFile(host: HTMLElement, value: unknown, ports: ArtifactDeliveryPorts): () => void {
  const doc = host.ownerDocument;
  const text = (key: string, fallback: string) => ports.text?.(key, fallback) ?? fallback;
  const meta = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const filename = typeof meta.filename === 'string' && meta.filename ? meta.filename : '—';
  const previewable = meta.mime === PPTX_MIME && Boolean(ports.preview);

  const card = doc.createElement('article');
  card.className = 'artifact-file-card';
  card.setAttribute('aria-label', text('delivery.file', 'Published attachment'));

  const head = doc.createElement('header');
  head.className = 'artifact-file-card-head';
  const badge = doc.createElement('span');
  badge.className = 'artifact-file-card-badge';
  badge.setAttribute('aria-hidden', 'true');
  badge.textContent = extensionOf(filename) || 'FILE';
  const identity = doc.createElement('div');
  identity.className = 'artifact-file-card-identity';
  const eyebrow = doc.createElement('p');
  eyebrow.className = 'artifact-file-card-eyebrow';
  eyebrow.textContent = text('delivery.file', 'Published attachment');
  const name = doc.createElement('h2');
  name.className = 'artifact-file-card-name';
  name.textContent = filename;
  name.title = filename;
  const summary = doc.createElement('p');
  summary.className = 'artifact-file-card-summary';
  summary.textContent = [extensionOf(filename), formatSize(meta.sizeBytes)].filter(Boolean).join(' · ');
  identity.append(eyebrow, name, summary);
  const download = createButton({ label: text('delivery.download', 'Download original'), variant: 'primary', wrap: true,
    onClick: ports.download }, doc);
  download.classList.add('artifact-file-card-download');
  head.append(badge, identity, download);

  const note = doc.createElement('p');
  note.className = 'artifact-file-card-note';
  note.textContent = previewable
    ? text('delivery.previewHelp', 'Pages are shown as images rendered from the original; links open in a new tab. The file itself is never run in the application.')
    : text('delivery.fileHelp', 'This file is available to download. It is not executed or embedded in the application.');

  const details = doc.createElement('details');
  details.className = 'artifact-file-card-details';
  const detailsLabel = doc.createElement('summary');
  detailsLabel.textContent = text('delivery.details', 'File details');
  const list = doc.createElement('dl');
  for (const [key, fallback, data] of [
    ['delivery.filename', 'Filename', meta.filename],
    ['delivery.mime', 'File type', meta.mime],
    ['delivery.bytes', 'Bytes', meta.sizeBytes],
    ['delivery.hash', 'SHA-256', meta.sha256],
  ]) {
    const term = doc.createElement('dt'); term.textContent = text(String(key), String(fallback));
    const definition = doc.createElement('dd'); definition.textContent = typeof data === 'string' || typeof data === 'number' ? String(data) : '—';
    list.append(term, definition);
  }
  details.append(detailsLabel, list);

  if (!previewable) {
    card.append(head, note, details);
    host.replaceChildren(card);
    return () => {};
  }
  const viewer = mountPagePreview(doc, ports.preview!, text);
  card.append(head, viewer.el, note, details);
  host.replaceChildren(card);
  return viewer.dispose;
}

function safeLinkHref(href: unknown): string | null {
  if (typeof href !== 'string') return null;
  try {
    const url = new URL(href);
    return SAFE_LINK_PROTOCOLS.has(url.protocol) ? url.href : null;
  } catch { return null; }
}

function isFraction(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;
}

function renderLink(doc: Document, link: ArtifactPreviewLink): HTMLAnchorElement | null {
  const href = safeLinkHref(link?.href);
  if (!href || ![link.x, link.y, link.w, link.h].every(isFraction) || link.w <= 0 || link.h <= 0) return null;
  const anchor = doc.createElement('a');
  anchor.className = 'artifact-page-preview-link';
  anchor.href = href;
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  anchor.title = href;
  anchor.setAttribute('aria-label', href);
  // Fractions are kept exact; the stylesheet scales them to the page box.
  anchor.style.setProperty('--link-x', String(link.x));
  anchor.style.setProperty('--link-y', String(link.y));
  anchor.style.setProperty('--link-w', String(Math.min(link.w, 1 - link.x)));
  anchor.style.setProperty('--link-h', String(Math.min(link.h, 1 - link.y)));
  return anchor;
}

function mountPagePreview(doc: Document, preview: ArtifactPagePreviewPorts,
  text: (key: string, fallback: string) => string): { el: HTMLElement; dispose: () => void } {
  const controller = new AbortController();
  let disposed = false;
  let pageCount = 0;
  let current = 0;
  let showToken = 0;
  const pages = new Map<number, Promise<ArtifactPreviewPage>>();

  const root = doc.createElement('section');
  root.className = 'artifact-page-preview';
  root.setAttribute('aria-label', text('delivery.previewLabel', 'Page preview'));
  root.dataset.state = 'loading';
  const status = doc.createElement('p');
  status.className = 'artifact-page-preview-status';
  status.setAttribute('role', 'status');
  status.textContent = text('delivery.previewLoading', 'Loading page preview…');
  const stage = doc.createElement('div');
  stage.className = 'artifact-page-preview-stage';
  stage.hidden = true;
  const image = doc.createElement('img');
  image.className = 'artifact-page-preview-image';
  image.decoding = 'async';
  const links = doc.createElement('div');
  links.className = 'artifact-page-preview-links';
  stage.append(image, links);
  const controls = doc.createElement('div');
  controls.className = 'artifact-page-preview-controls';
  controls.hidden = true;
  const previous = createButton({ label: text('delivery.previewPrevious', 'Previous page'), size: 'sm', variant: 'secondary',
    onClick: () => { if (current > 0) void show(current - 1); } }, doc);
  const next = createButton({ label: text('delivery.previewNext', 'Next page'), size: 'sm', variant: 'secondary',
    onClick: () => { if (current < pageCount - 1) void show(current + 1); } }, doc);
  const indicator = doc.createElement('span');
  indicator.className = 'artifact-page-preview-indicator';
  indicator.setAttribute('aria-live', 'polite');
  controls.append(previous, indicator, next);
  root.append(status, stage, controls);

  const load = (index: number): Promise<ArtifactPreviewPage> => {
    let request = pages.get(index);
    if (!request) {
      request = preview.page(index, controller.signal);
      pages.set(index, request);
      // A failed read is not cached, so navigating back retries it.
      request.catch(() => { if (pages.get(index) === request) pages.delete(index); });
    }
    return request;
  };

  const fail = () => {
    root.dataset.state = 'failed';
    stage.hidden = true;
    status.hidden = false;
    status.textContent = text('delivery.previewFailed', 'Page preview is unavailable. You can still download the original.');
  };

  async function show(index: number): Promise<void> {
    const token = ++showToken;
    current = index;
    // Keep only the neighbourhood of the visible page; rendered pages are large data URLs.
    for (const key of [...pages.keys()]) if (Math.abs(key - index) > 1) pages.delete(key);
    indicator.textContent = `${index + 1} / ${pageCount}`;
    previous.disabled = index <= 0;
    next.disabled = index >= pageCount - 1;
    root.dataset.state = 'loading';
    status.hidden = false;
    status.textContent = text('delivery.previewLoading', 'Loading page preview…');
    let page: ArtifactPreviewPage;
    try { page = await load(index); } catch {
      if (!disposed && token === showToken) fail();
      return;
    }
    if (disposed || token !== showToken) return;
    image.src = page.image;
    image.alt = text('delivery.previewPageAlt', 'Page {page} of {total}')
      .replace('{page}', String(index + 1)).replace('{total}', String(pageCount));
    links.replaceChildren(...(Array.isArray(page.links) ? page.links : [])
      .map(link => renderLink(doc, link)).filter((anchor): anchor is HTMLAnchorElement => anchor !== null));
    stage.hidden = false;
    status.hidden = true;
    root.dataset.state = 'ready';
    if (index + 1 < pageCount) void load(index + 1).catch(() => {});
  }

  preview.pages(controller.signal).then(info => {
    if (disposed) return;
    if (!Number.isInteger(info?.pageCount) || info.pageCount < 1) { fail(); return; }
    pageCount = info.pageCount;
    if (info.width > 0 && info.height > 0) stage.style.setProperty('--page-aspect', `${info.width} / ${info.height}`);
    controls.hidden = false;
    void show(0);
  }, () => { if (!disposed) fail(); });

  return {
    el: root,
    dispose: () => {
      if (disposed) return;
      disposed = true;
      controller.abort();
      pages.clear();
    },
  };
}

export function renderScriptWarning(host: HTMLElement, allow: () => void,
  translate?: (key: string, fallback: string) => string,
  options: { interactiveDownload?: () => void; scriptsAllowed?: boolean } = {}): () => void {
  const doc = host.ownerDocument;
  const text = (key: string, fallback: string) => translate?.(key, fallback) ?? fallback;
  let disposed = false;
  let closeConfirmation: (() => void) | null = null;
  const banner = doc.createElement('div'); banner.className = 'artifact-script-banner'; banner.setAttribute('role', 'note');
  const message = doc.createElement('span');
  const risk = text('delivery.interactiveRisk', 'Opening the original interactive document can execute untrusted code, make network requests, or send data. Download it only if you trust the source.');
  message.textContent = options.scriptsAllowed ? risk : text('delivery.scriptRisk', 'Scripts are blocked. Running this document executes untrusted code and may make network requests or send data. Only enable it if you trust the source.');
  const control = createButton({ label: text('delivery.runScripts', 'Run scripts (code / network risk)'), size: 'sm', wrap: true, onClick: allow }, doc);
  control.dataset.artAction = 'allow-scripts';
  // The typed owner handles the click; retained delegation must not activate it twice.
  control.addEventListener('click', event => event.stopPropagation());
  banner.append(message);
  if (!options.scriptsAllowed) banner.append(control);
  if (options.interactiveDownload) {
    const original = createButton({ label: text('delivery.interactiveDownload', 'Download original interactive document'), size: 'sm', wrap: true,
      onClick: async () => {

        if (disposed || closeConfirmation) return;
        const dialog = createDialog({ title: text('delivery.interactiveConfirm', 'Download interactive code?'), body: risk,
          actions: [{ id: 'cancel', label: text('delivery.cancel', 'Cancel') },
            { id: 'download', label: text('delivery.interactiveProceed', 'Download with code / network risk'), variant: 'danger' }] });
        doc.body.append(dialog.el);
        closeConfirmation = () => { dialog.close(); dialog.el.remove(); };
        try {
          if (await dialog.open() === 'download' && !disposed && banner.isConnected) options.interactiveDownload?.();
        } finally { dialog.el.remove(); closeConfirmation = null; }
      } }, doc);
    original.addEventListener('click', event => event.stopPropagation());
    banner.append(original);
  }
  host.append(banner);
  return () => { disposed = true; closeConfirmation?.(); banner.remove(); };
}
