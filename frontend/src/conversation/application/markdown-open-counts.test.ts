// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { createMarkdownRenderer } from '../../markdown';
import { createConversationSurfaceController } from './conversation-surface-controller';
import { createTurnState } from '../domain/turn-store';
import { createClassicConversationRenderers } from '../ui/classic-conversation-renderers';

it('counts terminal Markdown projection on normal first open and A→B→A without confusing warm reuse with duplicate work', () => {
  const parsed: string[] = [], sanitized: string[] = [], requested: string[] = [];
  const parser = { parse(source: string) { parsed.push(source); return marked.parse(source, { async: false }) as string; } };
  const sanitizer = { sanitize(html: string, config: { ALLOW_DATA_ATTR: false }) {
    sanitized.push(html); return DOMPurify.sanitize(html, config);
  } };
  const markdown = createMarkdownRenderer({ maximumCacheEntries: 300,
    getEnvironment: () => ({ document, parser, sanitizer, highlighter: null, mathRenderer: null,
      basePath: '/fixture', language: 'en', projectActive: false }), ensureMath: () => {} });
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  let active = 'A';
  const controller = createConversationSurfaceController({ isActive: id => id === active,
    getContainer: () => container, schedule: render => { frames.push(render); return () => {}; },
    nativeRenderers: createClassicConversationRenderers({ renderSafeMarkdownHtml(source, options) {
      requested.push(source); return markdown.render(source, options);
    } }),
  });
  const source = (id: string) => `# Synthetic ${id}\n\n**Immutable prose** [web](https://example.invalid/read) [file](/workspace/fixture.md) [asset](/api/v4/artifacts/fixture/download)\n\n<script>bad()</script><span data-tofu-action="bad()" onclick="bad()">safe</span>`;
  const state = (id: string, content: string, revision = 1) => {
    const graph = createTurnState(id);
    graph.turnsById.answer = { turnId: 'answer', presentationId: 'answer', conversationId: id,
      laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply', runId: '', status: 'completed',
      currentAttemptId: 'attempt', projectionRevision: revision, createdAt: 1, updatedAt: revision,
      settlement: {}, projection: { content } };
    graph.laneOrder.main = ['answer']; return graph;
  };
  const graphs = { A: state('A', source('A')), B: state('B', source('B')) };
  const observations: Array<{ requests: number; parses: number; sanitizations: number }> = [];
  const outputs: string[] = [];
  try {
    controller.render({ id: 'A' }, createTurnState('A'));
    frames.splice(0).forEach(render => render());
    expect(requested).toEqual([]);
    expect(parsed).toEqual([]);
    for (const id of ['A', 'B', 'A'] as const) {
      active = id;
      const before = [requested.length, parsed.length, sanitized.length];
      expect(controller.render({ id }, graphs[id])).toBe(true);
      frames.splice(0).forEach(render => render());
      const body = container.querySelector<HTMLElement>('.conversation-block--text .md-content')!;
      expect(body.textContent).toContain(`Synthetic ${id}`);
      expect(body.querySelector('script,[onclick],[data-tofu-action="bad()"]')).toBeNull();
      expect(body.querySelector('a')?.getAttribute('rel')).toBe('noopener noreferrer');
      expect(body.querySelector('.md-inaccessible-attachment')?.textContent).toContain('/workspace/fixture.md');
      expect(body.querySelector('a[href="/fixture/api/v4/artifacts/fixture/download"]')).not.toBeNull();
      outputs.push(body.innerHTML);
      const settled = [requested.length, parsed.length, sanitized.length];
      controller.render({ id }, graphs[id]);
      frames.splice(0).forEach(render => render());
      expect([requested.length, parsed.length, sanitized.length]).toEqual(settled);
      observations.push({ requests: requested.length - before[0], parses: parsed.length - before[1],
        sanitizations: sanitized.length - before[2] });
    }
    expect(observations).toEqual([
      { requests: 1, parses: 1, sanitizations: 1 },
      { requests: 1, parses: 1, sanitizations: 1 },
      { requests: 1, parses: 0, sanitizations: 0 },
    ]);
    expect(outputs[2]).toBe(outputs[0]);
    expect(parsed).toEqual([source('A'), source('B')]);
    expect(new Set(sanitized).size).toBe(2);
    controller.render({ id: 'A' }, state('A', '# Changed same Turn', 2));
    frames.splice(0).forEach(render => render());
    expect(parsed).toHaveLength(3);
    expect(container.textContent).toContain('Changed same Turn');
    expect(container.textContent).not.toContain('Immutable prose');
    console.info('synthetic canonical Markdown opens', JSON.stringify(observations));
  } finally { controller.dispose(); container.remove(); markdown.dispose(); }
});

it('keeps genuine content and render policy/environment changes separate from equivalent work', () => {
  const parse = vi.fn((source: string) => marked.parse(source, { async: false }) as string);
  const sanitize = vi.fn((html: string, config: { ALLOW_DATA_ATTR: false }) => DOMPurify.sanitize(html, config));
  const parser = { parse }, sanitizer = { sanitize };
  let basePath = '/A', projectActive = false;
  const markdown = createMarkdownRenderer({ maximumCacheEntries: 300,
    getEnvironment: () => ({ document, parser, sanitizer, highlighter: null, mathRenderer: null,
      basePath, projectActive }), ensureMath: () => {} });
  const source = '[asset](/api/v4/artifacts/fixture/download) <b>bold</b>';
  try {
    const raw = markdown.render(source);
    expect(markdown.render(source)).toBe(raw);
    expect(parse).toHaveBeenCalledTimes(1);
    const escaped = markdown.render(source, { escapeRawHtml: true });
    expect(escaped).not.toBe(raw);
    expect(escaped).toContain('&lt;b&gt;');
    expect(parse).toHaveBeenCalledTimes(2);
    basePath = '/B';
    expect(markdown.render(source)).toContain('href="/B/api/');
    expect(parse).toHaveBeenCalledTimes(3);
    projectActive = true;
    markdown.render(source);
    expect(parse).toHaveBeenCalledTimes(4);
    markdown.render(source + ' changed');
    expect(parse).toHaveBeenCalledTimes(5);
    expect(sanitize).toHaveBeenCalledTimes(5);
    expect(sanitize.mock.calls.every(([, config]) => config.ALLOW_DATA_ATTR === false)).toBe(true);
  } finally { markdown.dispose(); }
});
