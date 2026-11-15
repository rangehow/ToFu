/** Character sprites for the office: the art manifest's uniform 40 x 60 u cells (feet at 20, 58), the sheet
 * for a person (role, task kind and palette variant, per theme) and the box and background position of one
 * cell of a state. A seated cell shows only its top `shown` share, which ends at the desk line.
 * Entry points: sheetFor, frame, FEET, SHEET_SIZE, DIRECTIONS. Dependency: assets/manifest.generated.ts. */
import type { CharacterModel } from '../office-game/office-character';
import { CELL, PEOPLE, ROWS, STATES, type PersonRole as ArtRole, type PersonState, type WorldTheme } from './assets/manifest.generated';
import { stableHash, type Person } from './model';

export const DOWN = 0, LEFT = 1, RIGHT = 2, UP = 3;
export const DIRECTIONS = ['down', 'left', 'right', 'up'] as const;
export const FEET = { x: CELL.feet[0], y: CELL.feet[1] } as const;
/** Every sheet has the same grid, so one background-size fits all of them (scene units). */
export const SHEET_SIZE = `${CELL.w * CELL.columns}px ${CELL.h * ROWS.length}px`;

/** A Worker's task kind picks its sheet; leads, the Global Router and the Watchdog have their own. */
const KIND_ROLE: Record<CharacterModel, ArtRole> = {
  executor: 'executor', reviewer: 'reviewer', coordinator: 'lead', researcher: 'researcher', engineer: 'engineer',
  writer: 'writer', analyst: 'analyst', general: 'general',
};

export function artRole(person: Pick<Person, 'role' | 'model'>): ArtRole {
  return person.role === 'worker' ? KIND_ROLE[person.model] : person.role;
}

/** The person's sheet URL; the palette variant is stable per person, so a crowd is not a row of clones. */
export function sheetFor(person: Pick<Person, 'id' | 'role' | 'model'>, theme: WorldTheme): string {
  const sheets = PEOPLE[artRole(person)];
  return sheets[stableHash(person.id) % sheets.length].url[theme];
}

export interface Frame { w: number; h: number; position: string }

/** One cell of a state (`column` is a sheet column); `shown` crops it to its top share. */
export function frame(state: PersonState, column: number, shown = 1): Frame {
  return { w: CELL.w, h: CELL.h * shown, position: `${-column * CELL.w}px ${-STATES[state].row * CELL.h}px` };
}

/** The still cell of a state: what reduced motion shows, and what a state without motion always shows. */
export function stillColumn(state: PersonState): number {
  return STATES[state].reduced;
}

/** Head and shoulders of the standing front cell, scaled so the full figure would be `height` tall (panel header). */
export function portraitStyle(person: Pick<Person, 'id' | 'role' | 'model'>, theme: WorldTheme, height: number): string {
  const scale = height / (FEET.y - 6);
  const top = 2, shown = 32;
  const cell = frame('walk-down', stillColumn('walk-down'));
  const [x, y] = cell.position.split(' ').map(value => parseFloat(value) * scale);
  return `background-image:url("${sheetFor(person, theme)}");width:${(CELL.w * scale).toFixed(1)}px;height:${(shown * scale).toFixed(1)}px;`
    + `background-size:${(CELL.w * CELL.columns * scale).toFixed(1)}px ${(CELL.h * ROWS.length * scale).toFixed(1)}px;`
    + `background-position:${x.toFixed(1)}px ${(y - top * scale).toFixed(1)}px`;
}
