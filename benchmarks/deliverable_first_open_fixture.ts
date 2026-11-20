/** Synthetic owner-resident first-open measurement; never imported by the application. */
import { marked } from '#tofu-test-root/node_modules/marked/lib/marked.esm.js';
import DOMPurify from '#tofu-test-root/node_modules/dompurify/dist/purify.es.mjs';
import { createMarkdownRenderer } from '#tofu-test-root/frontend/src/markdown';
import { createArtifactsEndpoints } from '#tofu-test-root/frontend/src/api/endpoint-registry/artifacts';
import { createDeliverableOpener } from '#tofu-test-root/frontend/src/features/lead-deliverables/open';
import { deliverableRow } from '#tofu-test-root/frontend/src/features/lead-deliverables/rows';
import { disposeRichResponse, renderRichResponse } from '#tofu-test-root/frontend/src/rich-response/renderer';
import { escapeHtml } from '#tofu-test-root/frontend/src/html-safety';
import { t, ready, UI_LANGUAGES } from '#tofu-test-root/frontend/src/i18n';
import messages from '#tofu-test-root/frontend/src/i18n/locales/en.json';
import '#tofu-test-root/frontend/src/styles/application/00-design-tokens.css';
import '#tofu-test-root/frontend/src/styles/application/18-artifacts-knowledge-library.css';
import '#tofu-test-root/frontend/src/styles/application/09-tool-cards.css';
import '#tofu-test-root/frontend/src/styles/application/30-ui-kit.css';

const runtimeScope: any = {};
const marks: Record<string, number> = {};
const stamp = (key: string) => { marks[key] = performance.now(); };
const requests: any[] = [];
const pending = new Map<string, { resolve: (response: Response) => void; row: any }>();
let source = '', mode = '';
let parses = 0, sanitizations = 0;
const renderer = createMarkdownRenderer({
  maximumCacheEntries: 300,
  getEnvironment: () => ({ document,
    parser: { parse(text: string) { parses++; return marked.parse(text, { async: false }) as string; } },
    sanitizer: { sanitize(html: string) { sanitizations++; return DOMPurify.sanitize(html); } },
    highlighter: null, mathRenderer: null, basePath: '', language: 'en', projectActive: false }),
  ensureMath: () => {},
});
function renderMarkdown(text: string) {
  stamp('markdownStart');
  const result = renderer.render(text);
  stamp('markdownEnd');
  return result;
}
const request = (path: string) => {
  const key = path.endsWith('/versions') ? 'versions' : path.endsWith('/raw') ? 'raw' : 'meta';
  const row = { key, path, start: performance.now() };
  requests.push(row);
  if (pending.has(key)) throw new Error('Duplicate pending request');
  return new Promise<Response>(resolve => pending.set(key, { resolve, row }));
};
const Api = { artifacts: createArtifactsEndpoints({ request,
  get: async (path: string) => { const response = await request(path); const value = await response.json(); stamp(path.endsWith('/versions') ? 'versionsDecoded' : 'metaDecoded'); return value.data; },
  post: () => { throw new Error('No writes allowed'); }, del: () => { throw new Error('No deletes allowed'); },
  _resolve: () => { throw new Error('No navigation/download allowed'); },
} as any) };

// RETAINED_ARTIFACT_OWNER

const target = () => mode === 'report'
  ? document.querySelector('dialog[open] .ptool-coord-prose')
  : document.querySelector('.artifact-panel:not(.artifact-panel-hidden) article.artifact-md');
const observer = new MutationObserver(() => {
  if (!marks.domReady && target()?.textContent?.includes('FINAL_SENTINEL')) stamp('domReady');
});
observer.observe(document.body, { childList: true, subtree: true });
const snapshot = () => ({ marks: { ...marks }, requests: requests.map(row => ({ ...row })),
  parses, sanitizations, text: target()?.textContent || '',
  pending: [...pending.keys()], nodeCount: target()?.querySelectorAll('*').length || 0,
  structure: { headings: target()?.querySelectorAll('h2').length || 0,
    tables: target()?.querySelectorAll('table').length || 0,
    quotes: target()?.querySelectorAll('blockquote').length || 0 } });
(window as any).fixture = {
  async setup(kind: string, text: string) {
    const originalFetch = window.fetch;
    const catalogUrl = UI_LANGUAGES.find(language => language.code === 'en')!.catalogUrl;
    window.fetch = async input => {
      if (String(input) !== catalogUrl) throw new Error('Only the in-memory English catalog is allowed');
      return new Response(JSON.stringify(messages));
    };
    try { await ready(); } finally { window.fetch = originalFetch; }
    mode = kind; source = text;
    const opener = createDeliverableOpener({
      openArtifact: id => { void runtimeScope.Artifacts.open(id); },
      openTurn: () => { throw new Error('Source navigation forbidden'); }, markdown: renderMarkdown,
    });
    const item: any = { id: kind === 'report' ? 'report:synthetic' : 'artifact:synthetic',
      kind: kind === 'report' ? 'report' : 'document', title: 'Synthetic benign fixture',
      projectPath: '/synthetic-only', createdAt: 1, source: null,
      ...(kind === 'report' ? { summary: text, reportKind: 'delivery' } : { artifactId: 'synthetic' }) };
    document.querySelector('ol')!.append(deliverableRow(item, { now: () => 1,
      opener: () => Promise.resolve(opener) }));
    document.addEventListener('click', () => stamp('click'), { capture: true, once: true });
    const frame = () => {
      if (marks.click && !marks.shellFrame && document.querySelector('.artifact-panel:not(.artifact-panel-hidden)')) stamp('shellFrame');
      const el = target();
      if (marks.domReady && !marks.visibleFrame && el) {
        const box = el.getBoundingClientRect();
        if (box.width && box.height && box.top < innerHeight && box.bottom > 0
          && box.left < innerWidth && box.right > 0 && getComputedStyle(el).visibility !== 'hidden') {
          stamp('visibleFrame');
          requestAnimationFrame(() => stamp('nextFrameOpportunity'));
        }
      }
      if (!marks.nextFrameOpportunity) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  },
  release(key: string) {
    const held = pending.get(key);
    if (!held) throw new Error('Request not pending: ' + key);
    pending.delete(key);
    const body = key === 'raw' ? source : JSON.stringify({ data: key === 'meta'
      ? { id: 'synthetic', conv_id: 'synthetic', format: 'markdown', title: 'Synthetic benign fixture',
        size_bytes: new TextEncoder().encode(source).length, version: 1 }
      : { versions: [{ id: 'synthetic', version: 1 }] } });
    held.row.release = performance.now();
    held.resolve(new Response(body, { status: 200 }));
  }, snapshot,
};
