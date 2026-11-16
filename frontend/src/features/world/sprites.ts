/** Character sprites for the office: the art manifest's uniform 40 x 60 u cells (feet at 20, 58), the sheet for
 * a person (role, task kind and palette variant, per theme) and the panel header's portrait cut from it.
 * Entry points: sheetFor, artRole, FEET, portraitStyle. Dependency: assets/manifest.generated.ts. */
import type { CharacterModel } from '../office-game/office-character';
import { CELL, PEOPLE, ROWS, STATES, type PersonRole as ArtRole, type WorldTheme } from './assets/manifest.generated';
import { stableHash } from './hash';
import type { Person } from './model';

export const FEET = { x: CELL.feet[0], y: CELL.feet[1] } as const;

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

/** Head and shoulders of the standing front cell, scaled so the full figure would be `height` tall (panel header). */
export function portraitStyle(person: Pick<Person, 'id' | 'role' | 'model'>, theme: WorldTheme, height: number): string {
  const scale = height / (FEET.y - 6);
  const top = 2, shown = 32;
  const still = STATES['walk-down'];
  const x = -still.reduced * CELL.w * scale, y = -still.row * CELL.h * scale;
  return `background-image:url("${sheetFor(person, theme)}");width:${(CELL.w * scale).toFixed(1)}px;height:${(shown * scale).toFixed(1)}px;`
    + `background-size:${(CELL.w * CELL.columns * scale).toFixed(1)}px ${(CELL.h * ROWS.length * scale).toFixed(1)}px;`
    + `background-position:${x.toFixed(1)}px ${(y - top * scale).toFixed(1)}px`;
}
