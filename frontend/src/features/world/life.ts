/** Office life drawn over the floor every frame: what each person is doing, at a glance and in motion.
 * - A name tag per person (a status light and a short name; leads edged in their team colour), so the floor reads
 *   as a status board without hovering. A Worker's tag never outgrows its desk: it shows the lead clause of the
 *   name (`tagLabels`), cut at a word, and the whole name is in the tooltip and the panel.
 * - Status in the scene: a faint screen glow once the evening dims the room (red for an exception), a thought
 *   bubble with travelling dots while assigned work waits, a red pulse and a shake for an exception, steam over an
 *   idle desk, confetti when a delivery lands. Raised hands and the cheer are the sheets' own cells.
 * Every motion is a pure function of time and a per-person phase, so reduced motion simply draws time zero.
 * Pure drawing plus a small particle list; entry points: STATUS_COLOR, glow, nameTag, tagBox, tagLabels, packLines,
 * thinking, alarmRing, steam, createParticles. */
import { POD_COLUMNS, SEAT_PITCH, TAG_FONT, TAG_GAP, TAG_LEAD_PILL, TAG_MIN_PX, TAG_PILL } from './layout';
import { rr, type Ctx } from './paint';

export type Status = 'working' | 'waiting' | 'idle' | 'decision' | 'exception' | 'delivering';
/** Status lights. Decision amber, exception red and deliverable gold are the art's reserved signal hues. */
export const STATUS_COLOR: Readonly<Record<Status, string>> = {
  working: '#5fd37a', waiting: '#9aa7ff', idle: '#a89c8a', decision: '#F4B740', exception: '#e5534b', delivering: '#E8B04A',
};
const INK = '#2a1d14';
const CREAM = '#fbf1da';

const glows = new Map<string, HTMLCanvasElement>();
/** A soft round light of `color` laid over what is under it, `r` units in radius. Never additive: an added light
 * blooms to white over a lit floor and reads as glare. */
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
  const base = ctx.globalAlpha;
  ctx.globalAlpha = base * alpha;
  ctx.drawImage(sprite, x - r, y - r, r * 2, r * 2);
  ctx.globalAlpha = base;
}

/** Smooth noise in 0..1 for flicker: a sum of incommensurate sines (no state, so reduced motion is time zero). */
export function flicker(t: number, phase: number): number {
  return 0.5 + 0.22 * Math.sin(t * 0.0071 + phase) + 0.16 * Math.sin(t * 0.0193 + phase * 2.3) + 0.12 * Math.sin(t * 0.0517 + phase * 0.7);
}

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
/** `text` cut to `max` pixels: at the last whole word that fits, or by character when not even the first word
 * does. */
function fitted(font: string, text: string, max: number): string {
  const known = cache(font).fitted.get(text);
  if (known?.max === max) return known.text;
  let value = text;
  if (measured(font, text) > max) {
    while (value.length > 1 && measured(font, `${value}…`) > max) value = value.slice(0, -1);
    const word = value.lastIndexOf(' ');
    if (word > 0 && text[value.length] !== ' ') value = value.slice(0, word);
    value = `${value.replace(/[\s:;,.·—–-]+$/u, '')}…`;
  }
  cache(font).fitted.set(text, { max, text: value });
  return value;
}

/** The lead clause of a name: the words before its first colon, dash, bracket or sentence break (an all-capitals
 * marker such as "URGENT:" is skipped), so a tag that must be short still says something whole. */
function leadClause(name: string): { head: string; rest: string } {
  const match = /^(.+?)(?:\s*:\s+|\s*：\s*|\s+[—–-]\s+|\s+\(|[;；。]\s*|\.\s+)(.+)$/su.exec(name.trim());
  if (!match) return { head: name.trim(), rest: '' };
  if (/^[A-Z]+$/.test(match[1])) return leadClause(match[2]);
  return { head: match[1], rest: match[2] };
}

/** What each tag of a team says, index-aligned with `names`: the lead clause of each name, or the clause after it
 * where two or more names share their lead ("Multi-device: tool routing" and "Multi-device: node program" read
 * "tool routing" and "node program"). */
export function tagLabels(names: ReadonlyArray<string>): string[] {
  const clauses = names.map(leadClause);
  const count = new Map<string, number>();
  for (const clause of clauses) count.set(clause.head, (count.get(clause.head) ?? 0) + 1);
  return clauses.map(clause => ((count.get(clause.head) ?? 0) > 1 && clause.rest ? leadClause(clause.rest).head : clause.head));
}
/** The tag's text size in CSS pixels: the art's TAG_FONT units at the camera's scale, never below the UI's
 * smallest legible size, so a zoomed-out floor keeps readable names (layout.ts keeps the floor for them). */
export const tagPixels = (pixel: number) => Math.max(TAG_MIN_PX, TAG_FONT / pixel);
const tagFont = (family: string, lead: boolean, size: number) => `700 ${(lead ? size * 1.1 : size).toFixed(1)}px ${family}`;

/** The tag of `name` centred on (x, y) in scene units at `pixel` units per CSS pixel: its box in scene units
 * (what the scene adds to the person's hit box) and its text. A Worker's tag is at most two desks wide, so the tags
 * of a desk row take at most two lines, neighbours alternating where they would touch; a lead's head desk has the
 * pod row's width. */
export function tagBox(family: string, name: string, lead: boolean, x: number, y: number, pixel: number) {
  const size = tagPixels(pixel), k = size / TAG_FONT;
  const font = tagFont(family, lead, size);
  // Two tags two desks apart on one line keep TAG_GAP between them; one pixel more absorbs rounding.
  const room = lead ? (SEAT_PITCH * POD_COLUMNS) / pixel : (SEAT_PITCH * 2) / pixel - TAG_GAP - 1;
  const text = fitted(font, name, room - 15 * k);
  const w = (measured(font, text) + 14 * k) * pixel, h = (lead ? TAG_LEAD_PILL : TAG_PILL) * k * pixel;
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

/** A name tag in screen pixels (call under the screen transform; (x, y) is the projected centre): a pill with a
 * status light and the name, edged in the team colour for a lead. In the light theme the pill is cream with ink
 * text, so a busy floor is not peppered with dark labels; in the dark theme it is walnut with cream text. `pulse`
 * (0..1) breathes the light of someone asking the owner, the one state that waits on them. */
export function nameTag(ctx: Ctx, family: string, name: string, status: Status, lead: boolean, team: string,
  x: number, y: number, pulse: number, pixel: number, light: boolean) {
  const k = tagPixels(pixel) / TAG_FONT;
  const world = tagBox(family, name, lead, 0, 0, pixel);
  const w = world.w / pixel, h = world.h / pixel;
  const box = { x: Math.round(x - w / 2), y: Math.round(y - h / 2), w, h };
  ctx.fillStyle = light ? 'rgba(74, 46, 24, 0.16)' : 'rgba(8, 5, 3, 0.3)'; rr(ctx, box.x, box.y + 1, box.w, box.h, box.h / 2); ctx.fill();
  ctx.fillStyle = light ? 'rgba(255, 252, 245, 0.95)' : 'rgba(34, 24, 17, 0.9)'; rr(ctx, box.x, box.y, box.w, box.h, box.h / 2); ctx.fill();
  if (lead) { ctx.strokeStyle = team; ctx.lineWidth = 1.4; ctx.stroke(); }
  else { ctx.strokeStyle = light ? 'rgba(90, 62, 40, 0.2)' : 'rgba(255, 236, 206, 0.1)'; ctx.lineWidth = 1; ctx.stroke(); }
  const color = STATUS_COLOR[status];
  const dot = { x: box.x + 6 * k, y: box.y + box.h / 2 };
  if (pulse > 0) { ctx.fillStyle = color; ctx.globalAlpha *= 0.35 * pulse; ctx.beginPath(); ctx.arc(dot.x, dot.y, 3.8 * k, 0, Math.PI * 2); ctx.fill(); ctx.globalAlpha /= 0.35 * pulse; }
  ctx.fillStyle = color; ctx.beginPath(); ctx.arc(dot.x, dot.y, 2.2 * k, 0, Math.PI * 2); ctx.fill();
  if (light) { ctx.strokeStyle = 'rgba(42, 29, 20, 0.28)'; ctx.lineWidth = 0.8; ctx.stroke(); }
  ctx.fillStyle = light ? INK : CREAM; ctx.font = world.font; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(world.text, box.x + 10 * k, box.y + box.h / 2 + 0.5);
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

interface Particle { x: number; y: number; vx: number; vy: number; spin: number; turn: number; color: string; age: number; life: number }
const CONFETTI = ['#E8B04A', '#5fd37a', '#7cc4ff', '#ff8fb1', '#fbf1da', '#b48cff'];

/** Confetti bursts when a delivery lands; stepped with the scene. */
export function createParticles() {
  let list: Particle[] = [];
  let seed = 1;
  const random = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  return {
    burst(x: number, y: number) {
      for (let i = 0; i < 64; i += 1) {
        const angle = -Math.PI / 2 + (random() - 0.5) * 2.6, speed = 110 + random() * 170;
        list.push({ x, y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed, spin: random() * 6, turn: (random() - 0.5) * 14,
          color: CONFETTI[i % CONFETTI.length], age: 0, life: 1300 + random() * 700 });
      }
    },
    step(dt: number) {
      const s = dt / 1000;
      for (const p of list) {
        p.age += dt;
        p.vy += 260 * s; p.vx *= Math.pow(0.35, s); p.vy *= Math.pow(0.6, s); p.spin += p.turn * s;
        p.x += p.vx * s; p.y += p.vy * s;
      }
      list = list.filter(p => p.age < p.life);
    },
    get busy() { return list.length > 0; },
    draw(ctx: Ctx) {
      const base = ctx.globalAlpha;
      for (const p of list) {
        ctx.globalAlpha = base * Math.min(1, (p.life - p.age) / 400);
        ctx.fillStyle = p.color;
        const w = 4.6 * Math.abs(Math.cos(p.spin)) + 0.8;
        ctx.fillRect(p.x - w / 2, p.y - 1.6, w, 3.2);
      }
      ctx.globalAlpha = base;
    },
    clear() { list = []; },
  };
}
