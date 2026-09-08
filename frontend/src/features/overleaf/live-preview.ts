/**
 * Chat-side live PDF preview for LaTeX edits — Overleaf or local.
 *
 * The retained scanner (overleaf-preview-hook.js) hands each newly settled
 * TeX edit round here: an `mcp__overleaf__*` edit compiles the remote
 * project, a local workspace `.tex` edit (the default when the user never
 * mentioned Overleaf) compiles with the host TeX engine. This owner
 * serializes compiles (trailing-latest coalescing so an edit burst
 * compiles once for the newest candidate), renders the staged PDF with
 * the shared Paper pdf.js loader at fit-width, then smooth-scrolls the
 * SyncTeX rect into view with a pulse highlight — the "Overleaf feel"
 * inside the conversation, for any LaTeX paper.
 *
 * Dependencies are injected so the vite spec can drive the whole pipeline
 * with a fake bridge/API/pdfjs under plain node (no real PDF rendering).
 */

import { ensurePdfJs } from '../paper/pdf-viewer';
import { featureRegistry } from '../../feature-registry';
import { t, type Translator } from '../../i18n';

export interface OverleafPreviewCandidate {
  toolCallId: string;
  toolName: string;
  /** 'overleaf' (remote MCP project) or 'local' (workspace .tex file). */
  source?: 'overleaf' | 'local';
  /** Overleaf source: 24-hex project id. */
  projectId?: string;
  /** Local source: the edited .tex path as the model wrote it. */
  path?: string;
  file?: string;
  needle: string;
}

interface PreviewResponse {
  ok?: boolean;
  pdf_url?: string;
  page?: number | null;
  rects?: Array<Record<string, number>>;
}

interface OverleafApi {
  overleaf?: {
    preview(body: Record<string, unknown>): Promise<PreviewResponse>;
  };
  latex?: {
    preview(body: Record<string, unknown>): Promise<PreviewResponse>;
  };
  _resolve?: (url: string) => string;
}

interface PdfViewport { width: number; height: number }
interface PdfPage {
  getViewport(options: { scale: number }): PdfViewport;
  render(options: Record<string, unknown>): { promise: Promise<unknown> };
}
interface PdfDocument {
  numPages: number;
  getPage(page: number): Promise<PdfPage>;
  destroy(): unknown;
}
interface PdfJsLike {
  getDocument(source: string): { promise: Promise<PdfDocument> };
}

interface LivePreviewDeps {
  api(): OverleafApi | undefined;
  loadPdfJs(): Promise<PdfJsLike>;
  showToast(message: string, kind?: string): void;
  translate: Translator;
}

interface PageMeta {
  wrapper: HTMLElement;
  widthPt: number;
  heightPt: number;
}

/** Milliseconds the SyncTeX pulse stays on screen before fading out. */
const HIGHLIGHT_MS = 2600;

function defaultDeps(): LivePreviewDeps {
  return {
    api: () => featureRegistry.Api as OverleafApi | undefined,
    loadPdfJs: () => ensurePdfJs() as unknown as Promise<PdfJsLike>,
    showToast: (message, kind) => {
      const toast = featureRegistry.showToast as
        ((msg: string, kind?: string) => void) | undefined;
      if (typeof toast === 'function') toast(message, kind);
    },
    translate: t,
  };
}

export function createOverleafLivePreview(
  overrides: Partial<LivePreviewDeps> = {},
): { observeRounds(candidates: OverleafPreviewCandidate[]): void } {
  const deps = { ...defaultDeps(), ...overrides };

  let panel: HTMLElement | null = null;
  let bodyEl: HTMLElement | null = null;
  let statusEl: HTMLElement | null = null;
  let fileEl: HTMLElement | null = null;
  let pages: PageMeta[] = [];
  let doc: PdfDocument | null = null;
  let inFlight = false;
  let pending: OverleafPreviewCandidate | null = null;
  let lastCandidate: OverleafPreviewCandidate | null = null;
  let renderToken = 0;
  let highlightTimer: ReturnType<typeof setTimeout> | null = null;

  function setStatus(text: string, busy: boolean): void {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.classList.toggle('olp-status-busy', busy);
  }

  function ensurePanel(): void {
    if (panel) return;
    const el = document.createElement('div');
    el.id = 'overleafLivePanel';
    el.className = 'olp-panel';
    el.setAttribute('role', 'complementary');
    el.setAttribute('aria-label', deps.translate('overleaf.livePreviewTitle'));

    const header = document.createElement('div');
    header.className = 'olp-header';

    const title = document.createElement('span');
    title.className = 'olp-title';
    title.textContent = deps.translate('overleaf.livePreviewTitle');

    fileEl = document.createElement('span');
    fileEl.className = 'olp-file';

    statusEl = document.createElement('span');
    statusEl.className = 'olp-status';

    const refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.className = 'olp-icon-btn olp-refresh';
    refresh.title = deps.translate('overleaf.refresh');
    refresh.setAttribute('aria-label', deps.translate('overleaf.refresh'));
    refresh.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
    refresh.addEventListener('click', () => {
      if (lastCandidate) {
        pending = lastCandidate;
        void pump();
      }
    });

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'olp-icon-btn olp-close';
    close.title = deps.translate('overleaf.close');
    close.setAttribute('aria-label', deps.translate('overleaf.close'));
    close.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    close.addEventListener('click', closePanel);

    header.append(title, fileEl, statusEl, refresh, close);
    bodyEl = document.createElement('div');
    bodyEl.className = 'olp-body';
    const empty = document.createElement('div');
    empty.className = 'olp-empty';
    empty.textContent = deps.translate('overleaf.emptyState');
    bodyEl.append(empty);
    el.append(header, bodyEl);
    document.body.append(el);
    panel = el;
    /* Slide-in on the next frame so the transition plays. */
    requestAnimationFrame(() => el.classList.add('olp-panel-open'));
  }

  function closePanel(): void {
    renderToken += 1;
    if (highlightTimer) {
      clearTimeout(highlightTimer);
      highlightTimer = null;
    }
    try { doc?.destroy(); } catch { /* already destroyed */ }
    doc = null;
    pages = [];
    pending = null;
    panel?.remove();
    panel = null;
    bodyEl = null;
    statusEl = null;
    fileEl = null;
  }

  /** Fit-width scale shared by every page (uniform pt width for a paper). */
  function computeScale(widthPt: number): number {
    const container = bodyEl ? bodyEl.clientWidth - 24 : 600;
    return Math.max(0.25, Math.min(3, container / Math.max(1, widthPt)));
  }

  async function renderPdf(
    url: string,
    token: number,
  ): Promise<void> {
    const pdfjs = await deps.loadPdfJs();
    if (token !== renderToken) return;
    const nextDoc = await pdfjs.getDocument(url).promise;
    if (token !== renderToken) {
      try { nextDoc.destroy(); } catch { /* superseded */ }
      return;
    }
    try { doc?.destroy(); } catch { /* stale doc */ }
    doc = nextDoc;
    if (!bodyEl) return;
    bodyEl.textContent = '';
    pages = [];
    const first = await nextDoc.getPage(1);
    const scale = computeScale(first.getViewport({ scale: 1 }).width);
    for (let n = 1; n <= nextDoc.numPages; n += 1) {
      if (token !== renderToken) return;
      const page = n === 1 ? first : await nextDoc.getPage(n);
      const viewport = page.getViewport({ scale });
      const wrapper = document.createElement('div');
      wrapper.className = 'olp-page';
      wrapper.dataset.page = String(n);
      const canvas = document.createElement('canvas');
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      canvas.className = 'olp-canvas';
      wrapper.append(canvas);
      bodyEl.append(wrapper);
      pages.push({
        wrapper,
        widthPt: viewport.width / scale,
        heightPt: viewport.height / scale,
      });
      /* eslint-disable-next-line no-await-in-loop -- bounded page count */
      await page.render({
        canvasContext: canvas.getContext('2d'),
        viewport,
      }).promise;
    }
  }

  /**
   * Scroll the SyncTeX rect into the middle of the panel and pulse it.
   * Rects are PostScript points with `v` measured from the page top — the
   * same axis as the rendered canvas, so px = pt × scale.
   */
  function scrollToRect(response: PreviewResponse): boolean {
    const rects = Array.isArray(response.rects) ? response.rects : [];
    const pageNum = typeof response.page === 'number'
      ? response.page
      : (typeof rects[0]?.page === 'number' ? rects[0].page : null);
    if (!pageNum || !bodyEl) return false;
    const meta = pages[pageNum - 1];
    if (!meta) return false;
    const scale = meta.wrapper.clientWidth > 0
      ? meta.wrapper.clientWidth / meta.widthPt
      : computeScale(meta.widthPt);
    const rect = rects.find((r) => Number(r.page) === pageNum) ?? rects[0];
    const rectTopPx = rect ? Number(rect.v) * scale : 0;
    const target = meta.wrapper.offsetTop + rectTopPx
      - Math.max(0, (bodyEl.clientHeight / 2) - 40);
    bodyEl.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
    if (!rect) return true;
    const mark = document.createElement('div');
    mark.className = 'olp-highlight';
    mark.style.left = `${Number(rect.h) * scale}px`;
    mark.style.top = `${rectTopPx}px`;
    mark.style.width = `${Math.max(24, Number(rect.width) * scale)}px`;
    mark.style.height = `${Math.max(10, Number(rect.height) * scale)}px`;
    meta.wrapper.append(mark);
    requestAnimationFrame(() => mark.classList.add('olp-highlight-on'));
    if (highlightTimer) clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => {
      mark.classList.remove('olp-highlight-on');
      mark.classList.add('olp-highlight-out');
      setTimeout(() => mark.remove(), 400);
    }, HIGHLIGHT_MS);
    return true;
  }

  async function run(candidate: OverleafPreviewCandidate): Promise<void> {
    const api = deps.api();
    const isLocal = candidate.source === 'local';
    const previewApi = isLocal ? api?.latex : api?.overleaf;
    if (!api || !previewApi) {
      deps.showToast(deps.translate('overleaf.compileFailed'), 'error');
      return;
    }
    ensurePanel();
    const label = isLocal
      ? (candidate.path || '')
      : (candidate.file || candidate.projectId || '');
    if (fileEl) {
      fileEl.textContent = label;
      fileEl.title = label;
    }
    setStatus(deps.translate('overleaf.compiling'), true);
    const body: Record<string, unknown> = isLocal
      ? { path: candidate.path }
      : { project_id: candidate.projectId };
    if (!isLocal && candidate.file) body.file = candidate.file;
    if (candidate.needle) body.needle = candidate.needle;
    const response = await previewApi.preview(body);
    if (!response?.ok || typeof response.pdf_url !== 'string') {
      throw new Error('preview rejected');
    }
    const token = renderToken;
    setStatus(deps.translate('overleaf.rendering'), true);
    const resolved = typeof api._resolve === 'function'
      ? api._resolve(response.pdf_url)
      : response.pdf_url;
    await renderPdf(resolved, token);
    if (token !== renderToken) return;
    const jumped = scrollToRect(response);
    setStatus(jumped ? '' : deps.translate('overleaf.rendered'), false);
  }

  /* Trailing-latest pump: at most one compile in flight; a burst of edits
   * collapses to the newest candidate instead of queuing stale PDFs. */
  async function pump(): Promise<void> {
    if (inFlight) return;
    inFlight = true;
    try {
      while (pending) {
        const candidate = pending;
        pending = null;
        lastCandidate = candidate;
        try {
          await run(candidate);
        } catch (error: unknown) {
          setStatus(deps.translate('overleaf.compileFailed'), false);
          deps.showToast(
            `${deps.translate('overleaf.compileFailed')}: ${
              error instanceof Error ? error.message : String(error)}`,
            'error',
          );
        }
      }
    } finally {
      inFlight = false;
    }
  }

  return {
    observeRounds(candidates: OverleafPreviewCandidate[]): void {
      if (!Array.isArray(candidates) || candidates.length === 0) return;
      const latest = candidates[candidates.length - 1];
      if (!latest) return;
      const isLocal = latest.source === 'local';
      const valid = isLocal
        ? typeof latest.path === 'string' && latest.path.trim() !== ''
        : typeof latest.projectId === 'string'
          && /^[0-9a-f]{24}$/.test(latest.projectId);
      if (!valid) return;
      pending = latest;
      void pump();
    },
  };
}
