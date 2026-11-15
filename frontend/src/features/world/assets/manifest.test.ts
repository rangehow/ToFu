// @vitest-environment node
/** The art manifest's contract with the office scene (README.md): every listed file exists, atlas rects lie
 * inside the atlas, anchors lie inside their cells and boxes, every role's sheet holds every row, both themes
 * are complete, and every animated state names its still frame for reduced motion. */
import { describe, expect, it, vi } from 'vitest';
import {
  ATLAS, CELL, DOCS, EMBLEMS, LIVE, PEOPLE, PEOPLE_MASTERS, PROP_MASTERS, PROPS, ROWS, STATES, THEMES, type ThemedUrl,
} from './manifest.generated';

const { existsSync, readFileSync } = await vi.importActual<{
  existsSync(path: URL): boolean;
  readFileSync(path: URL, encoding: 'utf8'): string;
  readFileSync(path: URL): Uint8Array;
}>('node:fs');

function themedUrls(): Array<[string, ThemedUrl]> {
  const out: Array<[string, ThemedUrl]> = [['atlas', ATLAS.url]];
  for (const [role, sheets] of Object.entries(PEOPLE)) sheets.forEach((sheet, index) => out.push([`people.${role}.${index}`, sheet.url]));
  for (const [kind, sizes] of Object.entries(DOCS)) { out.push([`docs.${kind}.desk`, sizes.desk.url]); out.push([`docs.${kind}.carry`, sizes.carry.url]); }
  for (const [name, sprite] of Object.entries(EMBLEMS)) out.push([`emblems.${name}`, sprite.url]);
  for (const [name, sprite] of Object.entries(LIVE)) out.push([`live.${name}`, sprite.url]);
  return out;
}

/** Canvas size from a WebP header (VP8X, VP8L or VP8). */
function webpSize(bytes: Uint8Array): [number, number] {
  const text = (start: number, end: number) => String.fromCharCode(...bytes.subarray(start, end));
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  expect(text(0, 4)).toBe('RIFF');
  expect(text(8, 12)).toBe('WEBP');
  const chunk = text(12, 16);
  const uint24 = (at: number) => bytes[at] | (bytes[at + 1] << 8) | (bytes[at + 2] << 16);
  if (chunk === 'VP8X') return [uint24(24) + 1, uint24(27) + 1];
  if (chunk === 'VP8L') { const bits = view.getUint32(21, true); return [(bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1]; }
  return [view.getUint16(26, true) & 0x3fff, view.getUint16(28, true) & 0x3fff];
}

const inside = ([x, y]: readonly number[], [w, h]: readonly number[]) => x >= 0 && y >= 0 && x <= w && y <= h;

describe('office art manifest', () => {
  it('lists light and dark files for every asset, and every file exists', () => {
    const urls = themedUrls();
    expect(urls.length).toBeGreaterThan(40);
    for (const [name, url] of urls) {
      for (const theme of THEMES) {
        expect(url[theme], `${name} ${theme}`).toBeTruthy();
        expect(existsSync(new URL(url[theme])), `${name} ${theme}`).toBe(true);
      }
      expect(url.light, name).not.toBe(url.dark);
    }
    for (const [name, masters] of Object.entries(PROP_MASTERS)) {
      for (const theme of THEMES) expect(existsSync(new URL(masters[theme], import.meta.url)), `${name} ${theme}`).toBe(true);
    }
  });

  it('gives every role a sheet with every row of the uniform cell grid', () => {
    expect(Object.keys(PEOPLE).sort()).toEqual(
      ['analyst', 'engineer', 'executor', 'general', 'lead', 'owner', 'researcher', 'reviewer', 'router', 'watchdog', 'writer']);
    for (const [role, sheets] of Object.entries(PEOPLE)) {
      expect(sheets.length, role).toBeGreaterThan(0);
      sheets.forEach((sheet, index) => {
        expect(sheet.size).toEqual([CELL.w * CELL.columns, CELL.h * ROWS.length]);
        for (const theme of THEMES) {
          // the 2x raster the scene loads, and the vector master it is rendered from
          expect(webpSize(readFileSync(new URL(sheet.url[theme])))).toEqual([sheet.size[0] * 2, sheet.size[1] * 2]);
          const svg = readFileSync(new URL(PEOPLE_MASTERS[role as keyof typeof PEOPLE][index][theme], import.meta.url), 'utf8');
          ROWS.forEach((row, rowIndex) => {
            const y = rowIndex * CELL.h;
            expect(svg.includes(`y="${y}"`) || svg.includes(` ${y})"`), `${role} ${theme} row ${row}`).toBe(true);
          });
        }
      });
    }
    for (const row of ROWS) expect(Object.values(STATES).some(state => ROWS[state.row] === row), row).toBe(true);
  });

  it('keeps anchors inside their cells and boxes', () => {
    expect(inside(CELL.feet, [CELL.w, CELL.h])).toBe(true);
    for (const [name, state] of Object.entries(STATES)) {
      for (const hand of state.hands || []) expect(inside(hand, [CELL.w, CELL.h]), name).toBe(true);
      if (state.hands) expect(state.hands.length, name).toBe(state.columns.length);
      for (const column of state.columns) expect(column < CELL.columns, name).toBe(true);
    }
    const sprites = [...Object.entries(EMBLEMS), ...Object.entries(LIVE),
      ...Object.entries(DOCS).flatMap(([kind, sizes]) => [[`${kind}.desk`, sizes.desk], [`${kind}.carry`, sizes.carry]] as const)];
    for (const [name, sprite] of sprites) expect(inside(sprite.anchor, sprite.size), name).toBe(true);
    for (const [name, prop] of Object.entries(PROPS)) expect(inside(prop.anchor, prop.size), name).toBe(true);
  });

  it('places every prop rect inside the atlas at the atlas scale', () => {
    for (const theme of THEMES) expect(webpSize(readFileSync(new URL(ATLAS.url[theme])))).toEqual([...ATLAS.size]);
    for (const [name, prop] of Object.entries(PROPS)) {
      const [x, y, w, h] = prop.rect;
      expect(x >= 0 && y >= 0 && x + w <= ATLAS.size[0] && y + h <= ATLAS.size[1], name).toBe(true);
      expect([w, h], name).toEqual([prop.size[0] * ATLAS.scale, prop.size[1] * ATLAS.scale]);
    }
  });

  it('names a still frame for every animated state', () => {
    for (const [name, state] of Object.entries(STATES)) {
      const animated = state.columns.length > 1 || Boolean(state.beat) || Boolean(state.fps);
      if (animated) expect(state.columns, name).toContain(state.reduced);
      else expect(state.columns, name).toEqual([state.reduced]);
    }
    expect(EMBLEMS['decision-reduced']).toBeTruthy();
  });
});
