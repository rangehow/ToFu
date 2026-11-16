/** Off-main-thread painter for the static office layer: receives the OffscreenCanvas once, then repaints it for
 * every new layout, pixel ratio or theme. The atlas URL comes resolved from the main thread with each request;
 * the worker fetches and decodes it once per URL (fetch -> createImageBitmap) and stamps props from it, so no
 * art is decoded on the main thread. While the atlas is still loading, a request paints the procedural base at
 * once (reply `furnished: false`) and the furniture follows when the atlas lands (`furnished: true`), unless a
 * newer request superseded it. Posts back the request id. Entry point: the message handler. Dependency: paint.ts. */
import { paintOffice, type AtlasProp, type PaintInput } from './paint';

export interface ArtRequest {
  id: number; canvas?: OffscreenCanvas; input: PaintInput; pixelScale: number;
  atlas: { url: string; props: Readonly<Record<string, AtlasProp>> };
}
export interface ArtReply { id: number; ok: boolean; furnished: boolean }

const scope = globalThis as typeof globalThis & {
  postMessage(message: ArtReply): void;
  onmessage: ((event: MessageEvent<ArtRequest>) => void) | null;
};
let target: OffscreenCanvas | null = null;
let latest = 0;
let atlasUrl = '';
let atlasImage: Promise<ImageBitmap | null> = Promise.resolve(null);
let atlasDecoded: ImageBitmap | null = null;

function atlas(url: string): Promise<ImageBitmap | null> {
  if (url !== atlasUrl) {
    atlasUrl = url;
    atlasDecoded = null;
    atlasImage = fetch(url)
      .then(response => { if (!response.ok) throw new Error(`atlas ${response.status}`); return response.blob(); })
      .then(blob => createImageBitmap(blob))
      .then(image => { if (atlasUrl === url) atlasDecoded = image; return image; })
      .catch(() => null);
  }
  return atlasImage;
}

function paint(request: ArtRequest, image: ImageBitmap | null) {
  if (!target) { scope.postMessage({ id: request.id, ok: false, furnished: false }); return; }
  try {
    const { layout } = request.input;
    target.width = Math.max(1, Math.round(layout.width * request.pixelScale));
    target.height = Math.max(1, Math.round(layout.height * request.pixelScale));
    const ctx = target.getContext('2d');
    if (!ctx) throw new Error('2d context unavailable');
    ctx.setTransform(request.pixelScale, 0, 0, request.pixelScale, 0, 0);
    ctx.imageSmoothingQuality = 'high';
    paintOffice(ctx, request.input, image ? { image, props: request.atlas.props } : null);
    scope.postMessage({ id: request.id, ok: true, furnished: Boolean(image) });
  } catch {
    scope.postMessage({ id: request.id, ok: false, furnished: false });
  }
}

scope.onmessage = event => {
  const request = event.data;
  if (request.canvas) target = request.canvas;
  latest = request.id;
  const pending = atlas(request.atlas.url);
  if (atlasDecoded) { paint(request, atlasDecoded); return; }
  paint(request, null);
  void pending.then(image => {
    if (request.id !== latest) return;
    if (image) paint(request, image);
    else scope.postMessage({ id: request.id, ok: false, furnished: false });
  });
};
