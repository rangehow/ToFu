/** Office art for the canvas scene: the props atlas, one sheet per person's role variant, and the small sprites
 * (documents, emblems, lit screens, whiteboard notes, trophies, the alarm beacon), each decoded once per theme.
 * Webp art is fetched and decoded off the main thread (createImageBitmap); SVG sprites are rasterized once at
 * the art's own scale, so a frame only blits bitmaps. A theme change keeps drawing the previous theme's art
 * until the new theme's atlas, sprites and the sheets of everyone on the floor are all decoded, then switches at
 * once: the office is never half reskinned. Other sheets load when a person first needs one; until then the
 * scene does not draw that person.
 * `onLoad` asks for a frame whenever something new can be drawn.
 * Entry point: createWorldArt. Dependency: assets/manifest.generated.ts. */
import { ATLAS, DOCS, EMBLEMS, LIVE, type WorldTheme } from './assets/manifest.generated';

/** Pixels per scene unit in the shipped art (the atlas is a 2x WebP; sheets and sprites follow it). */
export const ART_SCALE = ATLAS.scale;

export interface SpriteArt { size: readonly number[]; anchor: readonly number[] }
const SPRITES = {
  'doc-report': DOCS.report.desk, 'doc-deliverable': DOCS.deliverable.desk, 'doc-pile': DOCS['report-pile'].desk,
  'carry-report': DOCS.report.carry, 'carry-deliverable': DOCS.deliverable.carry,
  'emblem-decision': EMBLEMS.decision, 'emblem-decision-still': EMBLEMS['decision-reduced'], 'emblem-exception': EMBLEMS.exception,
  'screen-front': LIVE['screen-front-on'], 'screen-glow': LIVE['screen-back-glow'], alarm: LIVE['alarm-glow'],
  'note-pending': LIVE['note-todo'], 'note-in_progress': LIVE['note-doing'], 'note-completed': LIVE['note-done'], 'note-blocked': LIVE['note-blocked'],
  trophy: LIVE['trophy-cup'],
} as const;
export type SpriteName = keyof typeof SPRITES;
export const spriteArt = (name: SpriteName): SpriteArt => SPRITES[name];

type Source = CanvasImageSource & { width: number; height: number };

async function decodeBitmap(url: string): Promise<Source | null> {
  try {
    const response = await fetch(url);
    if (!response.ok) return null;
    return await createImageBitmap(await response.blob());
  } catch {
    return null;
  }
}

async function rasterize(url: string, size: readonly number[]): Promise<Source | null> {
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(size[0] * ART_SCALE));
    canvas.height = Math.max(1, Math.round(size[1] * ART_SCALE));
    canvas.getContext('2d')?.drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas;
  } catch {
    return null;
  }
}

export function createWorldArt(onLoad: () => void) {
  const decoded = new Map<string, Source>();
  const pending = new Map<string, Promise<Source | null>>();
  let wanted: WorldTheme | null = null;
  let shown: WorldTheme | null = null;
  let disposed = false;

  function load(url: string, decode: () => Promise<Source | null>): Promise<Source | null> {
    let job = pending.get(url);
    if (!job) {
      job = decode().then(source => {
        if (source && !disposed) decoded.set(url, source);
        return source;
      });
      pending.set(url, job);
    }
    return job;
  }

  /** The theme's atlas, every sprite and `sheets`; the theme is shown once all of them are decoded. */
  function loadTheme(theme: WorldTheme, sheets: ReadonlyArray<string>) {
    const jobs: Promise<unknown>[] = [load(ATLAS.url[theme], () => decodeBitmap(ATLAS.url[theme]))];
    for (const art of Object.values(SPRITES)) jobs.push(load(art.url[theme], () => rasterize(art.url[theme], art.size)));
    for (const url of sheets) jobs.push(load(url, () => decodeBitmap(url)));
    void Promise.all(jobs).then(() => {
      if (disposed || wanted !== theme) return;
      shown = theme;
      onLoad();
    });
  }

  return {
    /** Show `theme` once its art and `sheets` (the people on the floor, in that theme) are decoded. */
    setTheme(theme: WorldTheme, sheets: ReadonlyArray<string> = []) {
      if (wanted === theme) return;
      wanted = theme;
      loadTheme(theme, sheets);
    },
    /** The theme whose art is drawn now (the previous one while the next decodes); null before any is ready. */
    theme: () => shown,
    atlas(): Source | null { return shown ? decoded.get(ATLAS.url[shown]) ?? null : null; },
    sprite(name: SpriteName): Source | null { return shown ? decoded.get(SPRITES[name].url[shown]) ?? null : null; },
    /** A person's sheet by URL, loading it on first request. */
    sheet(url: string): Source | null {
      const source = decoded.get(url);
      if (!source && !pending.has(url)) void load(url, () => decodeBitmap(url)).then(result => { if (result && !disposed) onLoad(); });
      return source ?? null;
    },
    dispose() { disposed = true; decoded.clear(); pending.clear(); },
  };
}
export type WorldArt = ReturnType<typeof createWorldArt>;
