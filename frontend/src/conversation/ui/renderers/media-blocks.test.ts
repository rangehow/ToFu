// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import { renderAttachmentsBlock } from './media-blocks';

/* Unified media attachments (a lead's attached files, channel files) render as
 * a human's attachments do: an image as a thumbnail, any other file as a badge. */

afterEach(() => document.body.replaceChildren());

const ports = {
  renderSafeMarkdownHtml: (text: string) => text,
} as unknown as ClassicConversationRendererPorts;

const attachment = (attachmentId: string, name: string, mimeType: string) => ({
  attachmentId, kind: 'document' as const, name, status: 'ready' as const, mimeType,
  sizeBytes: 2048, sourceUrl: `/api/v4/media/attachments/${attachmentId}/source`,
});

const block = (mediaAttachments: ReturnType<typeof attachment>[]) => ({
  blockId: 'attachments', kind: 'attachments', identitySource: 'contract', source: {},
  images: [], videos: [], pdfTexts: [], mediaAttachments, conversationReferences: [], replyQuotes: [],
}) as unknown as Extract<ConversationBlockViewModel, { kind: 'attachments' }>;

describe('media attachment presentation', () => {
  it('shows an image attachment as a thumbnail and other files as badges', () => {
    const node = document.createElement('div');
    renderAttachmentsBlock(node, block([
      attachment('img1', 'login.png', 'image/png'),
      attachment('bin1', 'model.weights', 'application/octet-stream'),
    ]), ports);
    const thumb = node.querySelector<HTMLButtonElement>('.msg-image-grid .msg-img-thumb');
    expect(thumb?.dataset.conversationAction).toBe('open-media');
    expect(thumb?.dataset.operation).toBe('img1');
    expect(thumb?.querySelector('img')?.getAttribute('src')).toBe('/api/v4/media/attachments/img1/source');
    const badges = [...node.querySelectorAll<HTMLElement>('.pdf-attach-badge')];
    expect(badges.map((badge) => badge.dataset.operation)).toEqual(['bin1']);
    expect(badges[0].title).toBe('model.weights');
  });
});
