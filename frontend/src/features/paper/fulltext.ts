/** Full-document PDF translation entry.
 *
 * Submits an owner-scoped stored PDF to the server route that calls
 * ``lib.paper.builds.pdf_translation.translate_pdf`` and renders only the
 * returned artifact. Transport absence is a first-class state: the panel says
 * the server cannot translate and the action never fires. The translated
 * document is the pipeline's output, not text translated in the browser.
 */
import { featureRegistry } from '../../feature-registry';
import { t, _i18nLang } from '../../i18n';
import { createAlert, createButton, createDropdown, createProgress } from '../../ui';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

type JsonObject = Record<string, unknown>;
interface PaperApi {
  pdfTranslateStatus(): Promise<JsonObject | null>;
  pdfTranslate(body: JsonObject): Promise<Response | null>;
}
interface Ports {
  Api?: { paper?: PaperApi };
  _activePaperId?: string;
  _paperHash?: string;
}
interface Status { configured: boolean; reason: string }

type Phase = 'checking' | 'ready' | 'unconfigured' | 'unavailable'
  | 'running' | 'done' | 'error';

const ports = (): Ports => featureRegistry as unknown as Ports;
const languages = [
  { id: 'zh', label: '中文' },
  { id: 'en', label: 'English' },
  { id: 'ja', label: '日本語' },
];
let mounted: {
  host: HTMLElement; key: string; scope: LifecycleScope;
  paperId: string; hash: string;
} | null = null;
let generation = 0;
let exportUrl = '';

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K, className: string, text = '',
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

export function destroyFulltextRuntime(): void {
  generation += 1;
  mounted?.scope.destroy();
  mounted = null;
  if (exportUrl) {
    URL.revokeObjectURL(exportUrl);
    exportUrl = '';
  }
}

async function failureDetail(response: Response): Promise<string> {
  try {
    const body = await response.json() as JsonObject;
    const error = body.error as JsonObject | string | undefined;
    if (typeof error === 'string' && error.trim()) return error;
    if (error && typeof error === 'object') {
      const detail = error.detail || error.message;
      if (typeof detail === 'string' && detail.trim()) return detail;
    }
    if (typeof body.detail === 'string' && body.detail.trim()) {
      return body.detail;
    }
  } catch { /* non-JSON failure body */ }
  return `HTTP ${response.status}`;
}

export function initFulltextTab(force = false): void {
  const host = document.getElementById('paperFulltextContent');
  if (!host) return;
  const paperId = ports()._activePaperId || '';
  const hash = ports()._paperHash || '';
  const key = JSON.stringify([paperId, hash, _i18nLang]);
  if (!force && mounted?.host === host && mounted.key === key && host.isConnected) {
    return;
  }
  destroyFulltextRuntime();
  const token = generation;
  const scope = createLifecycleScope();
  let phase: Phase = 'checking';
  let status: Status = { configured: false, reason: '' };
  let selected = _i18nLang === 'zh' ? 'en' : 'zh';
  let failure = '';
  let pages = 0;
  let busy = false;
  let dropdown: { close(returnFocus?: boolean): void } | null = null;

  const current = (): boolean => !scope.signal.aborted && token === generation
    && (ports()._activePaperId || '') === paperId
    && (ports()._paperHash || '') === hash;
  const root = element('section', 'paper-fulltext');
  host.replaceChildren(root);

  const render = (): void => {
    if (!current()) return;
    dropdown?.close(false);
    dropdown = null;
    root.replaceChildren();
    const header = element('header', 'paper-fulltext-header');
    header.append(
      element('h2', 'paper-fulltext-title', t('paper.tabFullTranslation')),
      element('p', 'paper-fulltext-hint', t('paper.fullTranslationHint')),
    );
    root.append(header);

    const controls = element('div', 'paper-fulltext-controls');
    const language = createDropdown({
      label: t('paper.fullTranslationChoose'),
      items: languages.map(item => ({ id: item.id, label: item.label })),
      disabled: phase !== 'ready' || !paperId,
      onSelect: (lang) => {
        if (!current()) return;
        selected = lang;
        render();
      },
    });
    const start = createButton({
      label: t('paper.fullTranslationStart'),
      variant: 'primary',
      disabled: phase !== 'ready' || !paperId,
      onClick: () => { void run(); },
    });
    dropdown = language;
    controls.append(language.el, start);
    root.append(controls);

    if (!paperId) {
      root.append(createAlert({
        variant: 'info',
        title: t('paper.fullTranslationNeedPdf'),
      }).el);
    } else if (phase === 'checking') {
      root.append(createProgress({
        label: t('paper.fullTranslationChecking'),
      }).el);
    } else if (phase === 'unconfigured') {
      root.append(createAlert({
        variant: 'warning',
        title: t('paper.tabFullTranslation'),
        body: t('paper.fullTranslationUnconfigured'),
      }).el);
    } else if (phase === 'unavailable') {
      root.append(createAlert({
        variant: 'warning',
        title: t('paper.fullTranslationCheckFailed'),
      }).el);
    } else if (phase === 'running') {
      const label = t('paper.fullTranslationRunning');
      root.append(element('p', 'paper-fulltext-status', label));
      root.append(createProgress({ label }).el);
    } else if (phase === 'error') {
      root.append(createAlert({
        variant: 'error',
        title: t('paper.fullTranslationFailed', { reason: failure }),
      }).el);
    } else if (phase === 'done') {
      const result = element('div', 'paper-fulltext-result');
      result.append(
        element('p', 'paper-fulltext-status',
          t('paper.fullTranslationDone', { pages: String(pages) })),
        createButton({
          label: t('paper.fullTranslationDownload'),
          variant: 'secondary',
          onClick: () => {
            if (!exportUrl) return;
            const anchor = document.createElement('a');
            anchor.href = exportUrl;
            anchor.download = `paper-${(hash || paperId).slice(0, 8)}-${selected}.pdf`;
            anchor.click();
          },
        }),
      );
      root.append(result);
    }
  };

  const check = async (): Promise<void> => {
    const api = ports().Api?.paper;
    if (!api) { phase = 'unavailable'; render(); return; }
    try {
      const result = await api.pdfTranslateStatus();
      if (!current()) return;
      if (!result?.ok) { phase = 'unavailable'; render(); return; }
      status = {
        configured: result.configured === true,
        reason: typeof result.reason === 'string' ? result.reason : '',
      };
      phase = status.configured ? 'ready' : 'unconfigured';
    } catch {
      if (!current()) return;
      phase = 'unavailable';
    }
    render();
  };

  const run = async (): Promise<void> => {
    if (busy || phase !== 'ready' || !paperId) return;
    const api = ports().Api?.paper;
    if (!api) { phase = 'unavailable'; render(); return; }
    busy = true; phase = 'running'; failure = ''; render();
    let response: Response | null = null;
    try {
      response = await api.pdfTranslate({
        paper_id: paperId,
        paper_hash: hash,
        target_language: selected,
      });
    } catch (error: unknown) {
      if (!current()) return;
      failure = error instanceof Error ? error.message : String(error);
      phase = 'error'; busy = false; render(); return;
    }
    if (!current()) return;
    if (!response?.ok) {
      failure = response ? await failureDetail(response) : 'no response';
      phase = 'error'; busy = false; render(); return;
    }
    const declaredPages = Number(response.headers.get('X-Tofu-Translation-Pages'));
    let buffer: ArrayBuffer;
    try {
      buffer = await response.arrayBuffer();
    } catch (error: unknown) {
      if (!current()) return;
      failure = error instanceof Error ? error.message : String(error);
      phase = 'error'; busy = false; render(); return;
    }
    if (!current()) return;
    if (!buffer.byteLength) {
      failure = 'empty artifact';
      phase = 'error'; busy = false; render(); return;
    }
    if (exportUrl) URL.revokeObjectURL(exportUrl);
    exportUrl = URL.createObjectURL(new Blob([buffer], { type: 'application/pdf' }));
    pages = Number.isFinite(declaredPages) ? declaredPages : 0;
    phase = 'done'; busy = false; render();
  };

  mounted = { host, key, scope, paperId, hash };
  scope.add(() => dropdown?.close(false));
  scope.add(() => { if (mounted?.scope === scope) mounted = null; });
  scope.listen(window, 'tofu:language-change',
    () => { if (current()) initFulltextTab(true); });
  render();
  if (paperId) void check(); else { phase = 'unavailable'; render(); }
}

Object.assign(featureRegistry, {
  _initFulltextTab: initFulltextTab,
  _destroyFulltextRuntime: destroyFulltextRuntime,
});
