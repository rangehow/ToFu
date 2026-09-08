/** Live paper cards and durable run history; reuses authenticated API ports. */
import { escapeHtml } from '../../../html-safety';
import { featureRegistry } from '../../../feature-registry';
import { _i18nLang } from '../../../i18n';
import type { ResearchStream } from '../state/stream';
const label = (zh: string, en: string) => _i18nLang === 'zh' ? zh : en;
export function paperCards(stream: ResearchStream): string {
  const papers = stream.papers || [];
  const heading = label('正在建立你的文献库', 'Your research library');
  return '<section class="rs-library"><header><div><span class="rs-eyebrow">' + label('文献 · 实时入库', 'LITERATURE · LIVE') + '</span><h2>' + heading + '</h2></div><strong>' + papers.length + ' <small>' + label('篇已处理', 'processed') + '</small></strong></header>'
    + (papers.length ? '<div class="rs-paper-grid">' + papers.map((p, i) => {
      const id = String(p.arxivId || '');
      const failed = p.status === 'error' || p.status === 'failed';
      const state = failed ? label('采集失败', 'Failed') : p.status === 'cached' ? label('已在文献库', 'In library') : label('已采集', 'Captured');
      return '<article class="rs-paper-card" id="rs-paper-' + i + '"><div class="rs-paper-meta"><span>' + escapeHtml(id || label('文献', 'Paper')) + '</span><b>' + state + '</b></div><h3>' + escapeHtml(String(p.title || id || label('正在读取标题', 'Reading title'))) + '</h3><p>' + escapeHtml(String(p.error || (Number(p.textLength) > 0 ? label('已提取原文，可用于后续综述', 'Source text available for survey') : label('元信息已保留，全文状态待确认', 'Metadata retained; full text not confirmed')))) + '</p>'
        + (!failed && id ? '<button class="rs-btn is-secondary" type="button" data-research-paper="' + escapeHtml(id) + '">' + label('打开论文', 'Open paper') + ' ↗</button>' : '') + '</article>';
    }).join('') + '</div>' : '<div class="rs-library-empty"><span>⌕</span><h3>' + label('检索中的论文将在这里逐篇出现', 'Papers will appear here as they are collected') + '</h3><p>' + label('保留标题、处理状态和原文入口，不再只有计数。', 'Titles, processing status and reading links—not just a counter.') + '</p></div>') + '</section>';
}
export function bindPaperCards(host: HTMLElement): void {
  host.querySelectorAll<HTMLButtonElement>('[data-research-paper]').forEach(button => {
    button.onclick = () => { const registry = featureRegistry as unknown as { _researchReader?: (id: string) => Promise<void> }; void registry._researchReader?.(button.dataset.researchPaper || ''); };
  });
}

