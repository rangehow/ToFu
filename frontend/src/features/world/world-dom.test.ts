// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import type { ApiTransport } from '../../api/transport';
import { readOnlyTransport } from './live';
import { renderText } from './text';

describe('owner-facing text projection', () => {
  it('builds structure from markdown and never interprets markup', () => {
    const root = document.createElement('div');
    renderText('**Only you** can `hope chpr`\n\n- one <img src=x onerror=alert(1)>\n- two\n\n2. second\n3. third\n\n```\n<script>x</script>\n```', root);
    expect(root.querySelector('strong')?.textContent).toBe('Only you');
    expect(root.querySelector('code')?.textContent).toBe('hope chpr');
    expect([...root.querySelectorAll('ul > li')].map(item => item.textContent)).toEqual(['one <img src=x onerror=alert(1)>', 'two']);
    expect((root.querySelector('ol') as HTMLOListElement).start).toBe(2);
    expect(root.querySelector('pre')?.textContent).toBe('<script>x</script>');
    expect(root.querySelector('img, script')).toBeNull();
  });
});

describe('read-only transport', () => {
  it('refuses every non-GET before it reaches the network and forwards GETs unchanged', async () => {
    class FakeError extends Error { constructor(message: string, readonly detail: Record<string, unknown>) { super(message); } }
    const request = vi.fn(async () => ({ ok: true }));
    const guarded = readOnlyTransport({ request, ApiError: FakeError } as unknown as ApiTransport);
    await expect(guarded.request('/api/v4/project/coordination/reports/read', { method: 'POST', json: {} })).rejects.toMatchObject({ detail: { code: 'read_only' } });
    await expect(guarded.request('/api/v4/folders', { method: 'delete' })).rejects.toBeInstanceOf(FakeError);
    expect(request).not.toHaveBeenCalled();
    await expect(guarded.request('/api/v4/folders', {})).resolves.toEqual({ ok: true });
    expect(request).toHaveBeenCalledWith('/api/v4/folders', {});
  });
});
