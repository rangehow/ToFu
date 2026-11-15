// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { renderPublishedFile, renderScriptWarning } from './artifact-delivery';
import type { ArtifactPagePreviewPorts } from './artifact-delivery';
import type { ArtifactPreviewLink, ArtifactPreviewPage, ArtifactPreviewPages } from '../api/endpoint-registry/artifacts';

const PPTX = { filename: 'deck.pptx', mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', sizeBytes: 9, sha256: 'b'.repeat(64) };
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const info = (pageCount: number): ArtifactPreviewPages => ({ version: 1, artifactId: 'art1', sha256: 'b'.repeat(64), pageCount, width: 960, height: 540 });
const pageOf = (index: number, links: ArtifactPreviewLink[] = []): ArtifactPreviewPage => ({
  version: 1, index, pageCount: 3, width: 960, height: 540, image: `data:image/png;base64,PAGE${index}`, links });

function deferred<T>() {
  let resolve!: (value: T) => void; let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function mountDeck(preview: ArtifactPagePreviewPorts) {
  const host = document.createElement('div'); const download = vi.fn();
  const dispose = renderPublishedFile(host, PPTX, { download, preview });
  const viewer = host.querySelector<HTMLElement>('.artifact-page-preview')!;
  const [previous, next] = Array.from(viewer.querySelectorAll<HTMLButtonElement>('button'));
  const downloadButton = host.querySelector<HTMLButtonElement>('.artifact-file-card-download')!;
  return { host, download, dispose, viewer, previous, next, downloadButton,
    image: () => viewer.querySelector('img')!,
    indicator: () => viewer.querySelector('.artifact-page-preview-indicator')!.textContent };
}

describe('presentation page preview', () => {
  it('renders the first page above the details with exact fractional, safe link boxes', async () => {
    const page = vi.fn((index: number) => Promise.resolve(pageOf(index, index ? [] : [
      { href: 'https://example.com/a', x: 0.1, y: 0.07, w: 0.25, h: 0.125 },
      { href: 'mailto:team@example.com', x: 0.5, y: 0.5, w: 0.2, h: 0.1 },
      { href: 'javascript:alert(1)', x: 0, y: 0, w: 1, h: 1 },
      { href: 'data:text/html,<script>x</script>', x: 0, y: 0, w: 1, h: 1 },
      { href: 'http://example.org/', x: 0.2, y: 2, w: 0.1, h: 0.1 },
    ])));
    const deck = mountDeck({ pages: () => Promise.resolve(info(3)), page });
    expect(deck.viewer.dataset.state).toBe('loading');
    expect(deck.viewer.textContent).toContain('Loading page preview');
    const card = deck.host.querySelector('.artifact-file-card')!;
    expect(Array.from(card.children, el => el.className)).toEqual(
      ['artifact-file-card-head', 'artifact-page-preview', 'artifact-file-card-note', 'artifact-file-card-details']);
    expect(card.querySelector('.artifact-file-card-note')!.textContent).toContain('links open in a new tab');
    await settle();
    expect(deck.viewer.dataset.state).toBe('ready');
    expect(deck.image().getAttribute('src')).toBe('data:image/png;base64,PAGE0');
    expect(deck.image().alt).toBe('Page 1 of 3');
    expect(deck.indicator()).toBe('1 / 3');
    const anchors = Array.from(deck.viewer.querySelectorAll<HTMLAnchorElement>('a'));
    expect(anchors.map(a => a.getAttribute('href'))).toEqual(['https://example.com/a', 'mailto:team@example.com']);
    const [first] = anchors;
    expect(['--link-x', '--link-y', '--link-w', '--link-h'].map(name => first.style.getPropertyValue(name)))
      .toEqual(['0.1', '0.07', '0.25', '0.125']);
    for (const anchor of anchors) {
      expect(anchor.target).toBe('_blank'); expect(anchor.rel).toBe('noopener noreferrer');
      expect(anchor.title).toBe(anchor.getAttribute('href')); expect(anchor.tabIndex).toBe(0);
    }
    expect(deck.viewer.querySelector('[href^="javascript"],[href^="data"]')).toBeNull();
    // The next page is prefetched exactly once.
    expect(page.mock.calls.map(call => call[0])).toEqual([0, 1]);
    deck.dispose();
  });

  it('navigates one page at a time with bounded previous/next controls', async () => {
    const page = vi.fn((index: number) => Promise.resolve(pageOf(index)));
    const deck = mountDeck({ pages: () => Promise.resolve(info(3)), page });
    await settle();
    expect(deck.previous.disabled).toBe(true); expect(deck.next.disabled).toBe(false);
    deck.next.click(); expect(deck.indicator()).toBe('2 / 3'); await settle();
    expect(deck.image().getAttribute('src')).toBe('data:image/png;base64,PAGE1');
    expect(deck.image().alt).toBe('Page 2 of 3');
    deck.next.click(); await settle();
    expect(deck.indicator()).toBe('3 / 3'); expect(deck.next.disabled).toBe(true);
    deck.next.click(); await settle(); expect(deck.indicator()).toBe('3 / 3');
    deck.previous.click(); await settle();
    expect(deck.indicator()).toBe('2 / 3'); expect(deck.image().getAttribute('src')).toBe('data:image/png;base64,PAGE1');
    expect(page.mock.calls.map(call => call[0])).toEqual([0, 1, 2]);
    expect(deck.viewer.querySelectorAll('img')).toHaveLength(1);
    deck.dispose();
  });

  it('shows an explicit failure state and keeps the download usable', async () => {
    const deck = mountDeck({ pages: () => Promise.reject(new Error('preview_unsupported')), page: vi.fn() });
    await settle();
    expect(deck.viewer.dataset.state).toBe('failed');
    expect(deck.viewer.textContent).toContain('Page preview is unavailable');
    expect(deck.viewer.querySelector('img')!.closest('[hidden]')).not.toBeNull();
    deck.downloadButton.click(); expect(deck.download).toHaveBeenCalledOnce();

    const failing = mountDeck({ pages: () => Promise.resolve(info(2)), page: () => Promise.reject(new Error('preview_render_failed')) });
    await settle();
    expect(failing.viewer.dataset.state).toBe('failed');
    failing.downloadButton.click(); expect(failing.download).toHaveBeenCalledOnce();

    let failSecond = true;
    const partial = mountDeck({ pages: () => Promise.resolve(info(2)),
      page: index => index === 1 && failSecond ? Promise.reject(new Error('preview_render_failed')) : Promise.resolve(pageOf(index)) });
    await settle();
    partial.next.click(); await settle();
    expect(partial.viewer.dataset.state).toBe('failed'); expect(partial.indicator()).toBe('2 / 2');
    expect(partial.image().closest('[hidden]')).not.toBeNull();
    failSecond = false; partial.previous.click(); await settle(); partial.next.click(); await settle();
    expect(partial.viewer.dataset.state).toBe('ready');
    expect(partial.image().getAttribute('src')).toBe('data:image/png;base64,PAGE1');
    partial.dispose();
  });

  it('dispose aborts in-flight reads and late responses do not touch the DOM', async () => {
    const pages = deferred<ArtifactPreviewPages>();
    let signal: AbortSignal | undefined;
    const page = vi.fn();
    const deck = mountDeck({ pages: received => { signal = received; return pages.promise; }, page });
    const before = deck.host.innerHTML;
    deck.dispose();
    expect(signal!.aborted).toBe(true);
    pages.resolve(info(3)); await settle();
    expect(deck.host.innerHTML).toBe(before); expect(page).not.toHaveBeenCalled();

    const first = deferred<ArtifactPreviewPage>();
    const signals: AbortSignal[] = [];
    const late = mountDeck({ pages: () => Promise.resolve(info(3)), page: (_index, received) => { signals.push(received); return first.promise; } });
    await settle();
    const pending = late.host.innerHTML;
    late.dispose();
    expect(signals.every(s => s.aborted)).toBe(true);
    first.resolve(pageOf(0)); await settle();
    expect(late.host.innerHTML).toBe(pending);
    expect(late.image().hasAttribute('src')).toBe(false);
  });
});

describe('published file panel presenters', () => {
  it('uses inert metadata and a native factory download action, never model markup', () => {
    const host = document.createElement('div'); const download = vi.fn();
    renderPublishedFile(host, { filename: '<img src=x onerror=alert(1)>.pdf', mime: 'application/pdf', sizeBytes: 42, sha256: 'a'.repeat(64) }, { download });
    expect(host.querySelector('img,iframe,script,[onerror]')).toBeNull();
    expect(host.textContent).toContain('<img src=x onerror=alert(1)>.pdf');
    const control = host.querySelector('button')!;
    expect(control.type).toBe('button'); expect(control.classList.contains('ui-btn')).toBe(true);
    expect(download).not.toHaveBeenCalled(); control.click(); expect(download).toHaveBeenCalledOnce();
    expect(host.querySelectorAll('dd')).toHaveLength(4);
  });
  it('renders a card with the file name, a readable size and a primary download action', () => {
    const host = document.createElement('div'); const download = vi.fn();
    renderPublishedFile(host, { filename: '实习岗位.pptx', mime: 'application/pdf', sizeBytes: 788042, sha256: 'c'.repeat(64) },
      { download, text: (key, fallback) => key === 'delivery.download' ? '下载原文件' : fallback });
    const card = host.querySelector('.artifact-file-card')!;
    expect(host.children).toHaveLength(1);
    expect(card.querySelector('.artifact-file-card-name')!.textContent).toBe('实习岗位.pptx');
    expect(card.querySelector('.artifact-file-card-badge')!.textContent).toBe('PPTX');
    expect(card.querySelector('.artifact-file-card-summary')!.textContent).toBe('PPTX · 770 KB');
    const action = card.querySelector<HTMLButtonElement>('.artifact-file-card-head .artifact-file-card-download')!;
    expect(action.textContent).toBe('下载原文件');
    expect(action.classList.contains('ui-btn--primary')).toBe(true);
    action.click(); expect(download).toHaveBeenCalledOnce();
    const details = card.querySelector<HTMLDetailsElement>('details.artifact-file-card-details')!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain('c'.repeat(64));
  });
  it('leaves non-presentation attachments without a preview and never asks for one', () => {
    const host = document.createElement('div');
    const preview = { pages: vi.fn(), page: vi.fn() };
    const dispose = renderPublishedFile(host, { filename: 'a.pdf', mime: 'application/pdf' }, { download: vi.fn(), preview });
    expect(Array.from(host.querySelector('.artifact-file-card')!.children, el => el.className))
      .toEqual(['artifact-file-card-head', 'artifact-file-card-note', 'artifact-file-card-details']);
    expect(host.textContent).toContain('It is not executed or embedded');
    expect(host.querySelector('.artifact-page-preview')).toBeNull();
    expect(preview.pages).not.toHaveBeenCalled(); expect(preview.page).not.toHaveBeenCalled();
    dispose();
  });
  it('requires an explicit native action and explains code and network risks', () => {
    const host = document.createElement('div'); const allow = vi.fn();
    renderScriptWarning(host, allow);
    expect(host.textContent).toMatch(/untrusted code/); expect(host.textContent).toMatch(/network requests/);
    const button = host.querySelector('button')!;
    expect(allow).not.toHaveBeenCalled(); button.click(); expect(allow).toHaveBeenCalledOnce();
    expect(host.querySelector('[role=note]')).not.toBeNull();
  });
});
