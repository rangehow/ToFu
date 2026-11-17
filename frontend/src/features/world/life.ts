/** Office life drawn over the floor every frame: what each person is doing, at a glance and in motion.
 * - A name tag per person (a status light and the name; leads edged in their team colour), so the floor reads as a
 *   status board without hovering.
 * - Status in the scene: screen glow that flickers while someone works (red when their team has an exception),
 *   a thought bubble with travelling dots while assigned work waits, a red pulse and a shake for an exception,
 *   steam over an idle desk, confetti when a delivery lands. Raised hands and the cheer are the sheets' own cells.
 * - Ambient life: dust motes drifting in the window light by day.
 * Every motion is a pure function of time and a per-person phase, so reduced motion simply draws time zero.
 * Pure drawing plus a small particle list; entry points: STATUS_COLOR, glow, nameTag, tagBox, packLines, thinking,
 * alarmRing, steam, createParticles. */
import { POD_COLUMNS, SEAT_PITCH } from './layout';
import { rr, type Ctx } from './paint';

export type Status = 'working' | 'waiting' | 'idle' | 'decision' | 'exception' | 'delivering';
/** Status lights. Decision amber, exception red and deliverable gold are the art's reserved signal hues. */
export const STATUS_COLOR: Readonly<Record<Status, string>> = {
  working: '#5fd37a', waiting: '#9aa7ff', idle: '#a89c8a', decision: '#F4B740', exception: '#e5534b', delivering: '#E8B04A',
};
const INK = '#2a1d14';
const CREAM = '#fbf1da';

const glows = new Map<string, HTMLCanvasElement>();
/** A soft round light of `color`, added to what is under it ('lighter'), `r` units in radius. */
export function glow(ctx: Ctx, color: string, x: number, y: number, r: number, alpha: number) {
  let sprite = glows.get(color);
  if (!sprite) {
    sprite = document.createElement('canvas');
    sprite.width = sprite.height = 64;
    const g = sprite.getContext('2d');
    if (g) {
      const gradient = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      gradient.addColorStop(0, color); gradient.addColorStop(0.35, `${color}88`); gradient.addColorStop(1, `${color}00`);
      g.fillStyle = gradient; g.fillRect(0, 0, 64, 64);
    }
    glows.set(color, sprite);
  }
  const operation = ctx.globalCompositeOperation, base = ctx.globalAlpha;
  ctx.globalCompositeOperation = 'lighter';
  ctx.globalAlpha = base * alpha;
  ctx.drawImage(sprite, x - r, y - r, r * 2, r * 2);
  ctx.globalCompositeOperation = operation;
  ctx.globalAlpha = base;
}

/** Smooth noise in 0..1 for flicker: a sum of incommensurate sines (no state, so reduced motion is time zero). */
export function flicker(t: number, phase: number): number {
  return 0.5 + 0.22 * Math.sin(t * 0.0071 + phase) + 0.16 * Math.sin(t * 0.0193 + phase * 2.3) + 0.12 * Math.sin(t * 0.0517 + phase * 0.7);
}

/** Tag text size in scene units. */
const TAG_FONT = 8.5;
let measureCtx: CanvasRenderingContext2D | null = null;
/** Text widths and fitted names per font; only the two fonts in use (Worker and lead tags at the current zoom) are
 * kept, so zooming never grows the cache. */
const byFont = new Map<string, { widths: Map<string, number>; fitted: Map<string, { max: number; text: string }> }>();
function cache(font: string) {
  let entry = byFont.get(font);
  if (!entry) {
    if (byFont.size >= 2) byFont.delete(byFont.keys().next().value!);
    entry = { widths: new Map(), fitted: new Map() };
    byFont.set(font, entry);
  }
  return entry;
}
function measured(font: string, text: string): number {
  const { widths } = cache(font);
  let width = widths.get(text);
  if (width === undefined) {
    measureCtx ??= document.createElement('canvas').getContext('2d');
    if (!measureCtx) return text.length * 5;
    measureCtx.font = font;
    width = measureCtx.measureText(text).width;
    widths.set(text, width);
  }
  return width;
}
function fitted(font: string, text: string, max: number): string {
  const known = cache(font).fitted.get(text);
  if (known?.max === max) return known.text;
  let value = text;
  if (measured(font, text) > max) {
    while (value.length > 1 && measured(font, `${value}…`) > max) value = value.slice(0, -1);
    value = `${value}…`;
  }
  cache(font).fitted.set(text, { max, text: value });
  return value;
}
/** The tag's text size in CSS pixels: the art's 8.5 units at the camera's scale, never below the UI's smallest
 * legible size, so a zoomed-out floor keeps readable names. */
export const tagPixels = (pixel: number) => Math.max(10, TAG_FONT / pixel);
const tagFont = (family: string, lead: boolean, size: number) => `700 ${(lead ? size * 1.1 : size).toFixed(1)}px ${family}`;

/** The tag of `name` centred on (x, y) in scene units at `pixel` units per CSS pixel: its box in scene units
 * (what the scene adds to the person's hit box) and its text. */
export function tagBox(family: string, name: string, lead: boolean, x: number, y: number, pixel: number) {
  const size = tagPixels(pixel), k = size / TAG_FONT;
  const font = tagFont(family, lead, size);
  // The scene packs a desk row's tags onto as many lines as their full names need, so a name is cut only where it
  // would outgrow the whole pod row (a title without a short name); the full text is in the tooltip and panel.
  const room = (SEAT_PITCH * POD_COLUMNS) / pixel;
  const text = fitted(font, name, room - 17 * k);
  const w = (measured(font, text) + 16 * k) * pixel, h = (lead ? 14 : 12) * k * pixel;
  return { x: x - w / 2, y: y - h / 2, w, h, text, font };
}

/** Lines for spans along one row (index-aligned with `spans`): taken left to right, each span goes on the innermost
 * line where it starts at least `gap` after the last span there, so only spans that would touch move outward. */
export function packLines(spans: ReadonlyArray<{ left: number; right: number }>, gap: number): number[] {
  const lines = new Array<number>(spans.length);
  const ends: number[] = [];
  for (const index of spans.map((_, at) => at).sort((a, b) => spans[a].left - spans[b].left)) {
    const span = spans[index];
    let line = ends.findIndex(end => end + gap <= span.left);
    if (line < 0) line = ends.push(span.right) - 1;
    else ends[line] = span.right;
    lines[index] = line;
  }
  return lines;
}

/** A name tag in screen pixels (call under the screen transform; (x, y) is the projected centre): a dark pill with a
 * status light and the name, edged in the team colour for a lead. `pulse` (0..1) breathes the light of someone
 * working or asking. */
export function nameTag(ctx: Ctx, family: string, name: string, status: Status, lead: boolean, team: string,
  x: number, y: number, pulse: number, pixel: number) {
  const k = tagPixels(pixel) / TAG_FONT;
  const world = tagBox(family, name, lead, 0, 0, pixel);
  const w = world.w / pixel, h = world.h / pixel;
  const box = { x: Math.round(x - w / 2), y: Math.round(y - h / 2), w, h };
  // Worker tags are lighter than leads' (no drop shadow, a little translucent), so a full floor reads as people
  // first and labels second; the text keeps its full contrast.
  if (lead) { ctx.fillStyle = 'rgba(12, 8, 5, 0.28)'; rr(ctx, box.x + 0.6, box.y + 1, box.w, box.h, box.h / 2); ctx.fill(); }
  ctx.fillStyle = lead ? 'rgba(30, 21, 15, 0.92)' : 'rgba(30, 21, 15, 0.8)'; rr(ctx, box.x, box.y, box.w, box.h, box.h / 2); ctx.fill();
  if (lead) { ctx.strokeStyle = team; ctx.lineWidth = 1.3; ctx.stroke(); }
  const color = STATUS_COLOR[status];
  const dot = { x: box.x + 6.5 * k, y: box.y + box.h / 2 };
  if (pulse > 0) { ctx.fillStyle = color; ctx.globalAlpha *= 0.35 * pulse; ctx.beginPath(); ctx.arc(dot.x, dot.y, 3.8 * k, 0, Math.PI * 2); ctx.fill(); ctx.globalAlpha /= 0.35 * pulse; }
  ctx.fillStyle = color; ctx.beginPath(); ctx.arc(dot.x, dot.y, 2.3 * k, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = CREAM; ctx.font = world.font; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(world.text, box.x + 10.5 * k, box.y + box.h / 2 + 0.5);
}

/** A thought bubble with three dots that light up in turn (assigned work waiting), its tail at (x, y). */
export function thinking(ctx: Ctx, x: number, y: number, t: number) {
  const w = 20, h = 11, left = x - w / 2 + 4, top = y - h - 5;
  ctx.fillStyle = 'rgba(12, 8, 5, 0.25)'; rr(ctx, left + 0.6, top + 1, w, h, 5.5); ctx.fill();
  ctx.fillStyle = '#fbf7ff'; rr(ctx, left, top, w, h, 5.5); ctx.fill();
  ctx.strokeStyle = '#5a5f8a'; ctx.lineWidth = 0.9; ctx.stroke();
  ctx.beginPath(); ctx.arc(x + 1, y - 3, 1.8, 0, Math.PI * 2); ctx.fillStyle = '#fbf7ff'; ctx.fill(); ctx.stroke();
  const lit = Math.floor(t / 360) % 4;
  for (let k = 0; k < 3; k += 1) {
    ctx.fillStyle = k === lit ? '#5a63c8' : '#b9bde6';
    ctx.beginPath(); ctx.arc(left + 5 + k * 5, top + h / 2 - (k === lit ? 0.8 : 0), 1.5, 0, Math.PI * 2); ctx.fill();
  }
}

/** A red ring pulsing out on the floor under someone with an exception. */
export function alarmRing(ctx: Ctx, x: number, y: number, t: number) {
  const k = (t % 1400) / 1400;
  const base = ctx.globalAlpha;
  ctx.strokeStyle = STATUS_COLOR.exception; ctx.lineWidth = 1.6;
  ctx.globalAlpha = base * (1 - k) * 0.9;
  ctx.beginPath(); ctx.ellipse(x, y, 10 + 12 * k, 3.5 + 4 * k, 0, 0, Math.PI * 2); ctx.stroke();
  ctx.globalAlpha = base * 0.35;
  ctx.fillStyle = STATUS_COLOR.exception; ctx.beginPath(); ctx.ellipse(x, y, 11, 3.8, 0, 0, Math.PI * 2); ctx.fill();
  ctx.globalAlpha = base;
}

/** Two wisps of steam rising from a cup at (x, y). */
export function steam(ctx: Ctx, x: number, y: number, t: number) {
  const base = ctx.globalAlpha;
  ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.1; ctx.lineCap = 'round';
  for (let k = 0; k < 2; k += 1) {
    const age = ((t / 1600) + k * 0.5) % 1;
    ctx.globalAlpha = base * 0.55 * Math.sin(age * Math.PI);
    const sx = x - 1.5 + k * 3, sy = y - age * 9;
    ctx.beginPath(); ctx.moveTo(sx, sy);
    ctx.quadraticCurveTo(sx + 2 * Math.sin(age * 6 + k), sy - 3, sx, sy - 6); ctx.stroke();
  }
  ctx.globalAlpha = base; ctx.lineCap = 'butt';
}

interface Particle { x: number; y: number; vx: number; vy: number; spin: number; turn: number; color: string; age: number; life: number; kind: 'confetti' | 'mote' }
const CONFETTI = ['#E8B04A', '#5fd37a', '#7cc4ff', '#ff8fb1', '#fbf1da', '#b48cff'];

/** Confetti bursts (a delivery landed) and dust motes in the window light; stepped with the scene. */
export function createParticles() {
  let list: Particle[] = [];
  let seed = 1;
  const random = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  return {
    burst(x: number, y: number) {
      for (let i = 0; i < 64; i += 1) {
        const angle = -Math.PI / 2 + (random() - 0.5) * 2.6, speed = 110 + random() * 170;
        list.push({ x, y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed, spin: random() * 6, turn: (random() - 0.5) * 14,
          color: CONFETTI[i % CONFETTI.length], age: 0, life: 1300 + random() * 700, kind: 'confetti' });
      }
    },
    /** Keeps `count` motes drifting inside `areas` (window shafts); none when `count` is 0. */
    motes(areas: ReadonlyArray<{ x: number; y: number; w: number; h: number }>, count: number) {
      const motes = list.filter(each => each.kind === 'mote').length;
      for (let i = motes; i < count && areas.length; i += 1) {
        const area = areas[Math.floor(random() * areas.length)];
        list.push({ x: area.x + random() * area.w, y: area.y + random() * area.h, vx: (random() - 0.5) * 6, vy: -2 - random() * 4,
          spin: 0, turn: 0, color: '#fff3cf', age: 0, life: 5000 + random() * 5000, kind: 'mote' });
      }
      if (!count) list = list.filter(each => each.kind !== 'mote');
    },
    step(dt: number) {
      const s = dt / 1000;
      for (const p of list) {
        p.age += dt;
        if (p.kind === 'confetti') { p.vy += 260 * s; p.vx *= Math.pow(0.35, s); p.vy *= Math.pow(0.6, s); p.spin += p.turn * s; }
        else { p.vx += (Math.sin(p.age / 900 + p.x) * 3) * s; }
        p.x += p.vx * s; p.y += p.vy * s;
      }
      list = list.filter(p => p.age < p.life);
    },
    get busy() { return list.some(p => p.kind === 'confetti'); },
    get size() { return list.length; },
    draw(ctx: Ctx) {
      const base = ctx.globalAlpha;
      for (const p of list) {
        const fade = p.kind === 'mote' ? Math.sin((p.age / p.life) * Math.PI) * 0.7 : Math.min(1, (p.life - p.age) / 400);
        ctx.globalAlpha = base * fade;
        ctx.fillStyle = p.color;
        if (p.kind === 'mote') { ctx.fillRect(p.x, p.y, 1.3, 1.3); continue; }
        const w = 4.6 * Math.abs(Math.cos(p.spin)) + 0.8;
        ctx.fillRect(p.x - w / 2, p.y - 1.6, w, 3.2);
      }
      ctx.globalAlpha = base;
    },
    clear() { list = []; },
  };
}
