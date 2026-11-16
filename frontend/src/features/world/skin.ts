/** Office art skin: the theme the art follows and the art URLs the scene's CSS reads.
 * Theme: [data-theme] dark or missing -> dark, light or tofu -> light, watched on <html> with a MutationObserver.
 * Sprite URLs for documents, emblems, screens, notes and trophies are published once per theme as custom
 * properties on the world element, so a theme change restyles every sprite with a handful of writes; people
 * sheets are per person (sprites.ts). Lit screens sit on the monitors of the desk props stamped by the art worker.
 * Entry points: worldTheme, watchTheme, skinStyle, screenBox, alarmBox, handOffset. Dependency: assets/manifest.generated.ts. */
import { DOCS, EMBLEMS, LIVE, PROPS, STATES, type PersonState, type WorldTheme } from './assets/manifest.generated';
import type { Rect, Seat } from './layout';

export type { WorldTheme };

export function worldTheme(root: HTMLElement = document.documentElement): WorldTheme {
  const value = root.dataset.theme;
  return value === 'light' || value === 'tofu' ? 'light' : 'dark';
}

/** Calls `onChange` when the effective art theme changes; returns the disconnect. */
export function watchTheme(onChange: (theme: WorldTheme) => void): () => void {
  const root = document.documentElement;
  let theme = worldTheme(root);
  const observer = new MutationObserver(() => {
    const next = worldTheme(root);
    if (next !== theme) { theme = next; onChange(next); }
  });
  observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  return () => observer.disconnect();
}

const url = (value: string) => `url("${value}")`;

/** Custom properties for the world element (27-world-office.css reads them). */
export function skinStyle(theme: WorldTheme): Record<string, string> {
  return {
    '--art-doc-report': url(DOCS.report.desk.url[theme]),
    '--art-doc-deliverable': url(DOCS.deliverable.desk.url[theme]),
    '--art-doc-pile': url(DOCS['report-pile'].desk.url[theme]),
    '--art-alarm': url(LIVE['alarm-glow'].url[theme]),
    '--art-carry-report': url(DOCS.report.carry.url[theme]),
    '--art-carry-deliverable': url(DOCS.deliverable.carry.url[theme]),
    '--art-emblem-decision': url(EMBLEMS.decision.url[theme]),
    '--art-emblem-decision-still': url(EMBLEMS['decision-reduced'].url[theme]),
    '--art-emblem-exception': url(EMBLEMS.exception.url[theme]),
    '--art-screen-front': url(LIVE['screen-front-on'].url[theme]),
    '--art-screen-glow': url(LIVE['screen-back-glow'].url[theme]),
    '--art-note-pending': url(LIVE['note-todo'].url[theme]),
    '--art-note-active': url(LIVE['note-doing'].url[theme]),
    '--art-note-done': url(LIVE['note-done'].url[theme]),
    '--art-note-blocked': url(LIVE['note-blocked'].url[theme]),
    '--art-trophy': url(LIVE['trophy-cup'].url[theme]),
  };
}

export interface Box { x: number; y: number; w: number; h: number }

/** Where a seat's lit screen goes: on the front of a desk-up monitor, or as a glow around the back of a
 * desk-down, lead or Watchdog monitor, from the stamped prop's "screen" slot (relative to its anchor). */
export function screenBox(seat: Seat, station: Rect | null): Box {
  const desk = seat.desk;
  if (seat.facing === 'up') {
    const [sx, sy] = PROPS['desk-up'].slots!.screen;
    const sprite = LIVE['screen-front-on'];
    return { x: desk.x + sx - sprite.anchor[0], y: desk.y + sy - sprite.anchor[1], w: sprite.size[0], h: sprite.size[1] };
  }
  const [sx, sy, sw, sh] = station ? PROPS['watchdog-station'].slots!.screen
    : desk.w > 60 ? PROPS['lead-desk'].slots!.screen : PROPS['desk-down'].slots!.screen;
  const origin = station ? { x: station.x, y: station.y } : desk;
  const glow = LIVE['screen-back-glow'];
  return { x: origin.x + sx + sw / 2 - glow.anchor[0], y: origin.y + sy + sh / 2 - glow.anchor[1], w: glow.size[0], h: glow.size[1] };
}

/** The Watchdog beacon's glow over the station's "alarm" slot (slots are relative to the station's origin). */
export function alarmBox(station: Rect): Box {
  const [sx, sy] = PROPS['watchdog-station'].slots!.alarm;
  const glow = LIVE['alarm-glow'];
  return { x: station.x + sx - glow.anchor[0], y: station.y + sy - glow.anchor[1], w: glow.size[0], h: glow.size[1] };
}

/** Where a carried document's box goes inside a person's cell (the hand anchor minus the document's anchor). */
export function handOffset(state: PersonState, column: number): { x: number; y: number } {
  const hands = STATES[state].hands;
  const hand = hands ? hands[Math.min(column, hands.length - 1)] : [0, 0];
  const anchor = DOCS.report.carry.anchor;
  return { x: hand[0] - anchor[0], y: hand[1] - anchor[1] };
}
