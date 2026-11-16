/** Tofu office scene: the open-plan floor as one painted layer (art-worker.ts) under live DOM actors.
 * People sit at the desks layout.ts allots their team (slots are kept while a person stays), a lit screen
 * marks a running attempt, one bubble above a head marks a decision (?) or an exception (!), and documents
 * wait on the owner's desk. A real report plays one walk: the author leaves its desk, walks the aisles to
 * the owner's office, leaves the document on the desk and walks back (a finished Worker leaves the floor).
 * Every person and document is a button; arrows move focus spatially, Enter opens, hover/focus shows the
 * name. One shared beat restyles all typing hands at once; hidden tabs and reduced motion stop it.
 * Entry point: createOffice. */
import { t } from '../../i18n';
import { ATLAS, PROPS, STATES, type PersonState } from './assets/manifest.generated';
import {
  allocateSlots, BOARD_GRID, fitOffice, nearestInDirection, pathLength, PERSON_H, SEATED_SHOWN, SEATS_PER_POD,
  shelfSlots, TROPHY_H, TROPHY_W, waitingSpots, walkHome, walkOut, walkToOwner,
  type NavDirection, type OfficeLayout, type Point, type Seat, type TeamShape,
} from './layout';
import { firstLine, type DeskDocument, type Office, type Person, type Team } from './model';
import type { ArtReply, ArtRequest } from './art-worker';
// Inlined into this lazy chunk: Vite omits URL-constructed worker bundles from manifest.json, and the
// application publishes only the manifest graph (scripts/vite_asset_manifest.mjs), so a separate worker
// file would 404 in production while the build and canary pass.
import ArtWorker from './art-worker?worker&inline';
import { alarmBox, handOffset, screenBox, skinStyle, watchTheme, worldTheme } from './skin';
import { DIRECTIONS, DOWN, FEET, frame, LEFT, RIGHT, SHEET_SIZE, sheetFor, stillColumn, UP } from './sprites';

export type OpenTarget = { kind: 'person'; id: string } | { kind: 'document'; id: string };
export interface OfficePorts { open(target: OpenTarget): void }
export interface WalkPlan { person: string; document: DeskDocument; leaves: boolean }

/** Ambient beat: half the authored typing cycle; every typing body changes on the same frame. */
export const BEAT_MS = 450;
/** Walking pace in scene units per second, and the walk cycle's frames per second. */
const WALK_SPEED = 180;
const STEP_FPS = 9;
const DROP_PAUSE_MS = 700;
const ARROWS: Record<string, NavDirection> = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' };

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, parent?: HTMLElement): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  parent?.append(node);
  return node;
}
/** DOM writes only on change: rewriting an equal value still invalidates style. */
function setText(node: HTMLElement, value: string) { if (node.textContent !== value) node.textContent = value; }
function setData(node: HTMLElement, key: string, value: string | null) {
  if (value === null) { if (key in node.dataset) delete node.dataset[key]; } else if (node.dataset[key] !== value) node.dataset[key] = value;
}
function setStyle(node: HTMLElement, name: string, value: string) { if (node.style.getPropertyValue(name) !== value) node.style.setProperty(name, value); }
const px = (value: number) => `${value.toFixed(1)}px`;

/** Character sheets fetched and decoded once per URL: a person stays hidden until its sheet can paint, so no
 * one appears as an empty box or as a floating emblem without a body while the art is still arriving. */
const decodedSheets = new Set<string>();
const decodingSheets = new Map<string, Promise<void>>();
function decodeSheet(url: string): Promise<void> {
  let pending = decodingSheets.get(url);
  if (!pending) {
    const image = new Image();
    image.src = url;
    pending = image.decode().catch(() => {}).then(() => { decodedSheets.add(url); });
    decodingSheets.set(url, pending);
  }
  return pending;
}

/** `anchor` is where the person shows now: the seat's desk line, or their spot at the owner's door. */
interface PersonView {
  button: HTMLButtonElement; mark: HTMLElement; screen: HTMLElement; person: Person; seat: Seat | null; anchor: Point | null; key: string;
}
/** The whiteboard and trophies: things that open a person's panel, with their tooltip and focus point. */
interface ExtraView { button: HTMLButtonElement; name: string; detail: string; x: number; y: number }
interface DocumentView { button: HTMLButtonElement; document: DeskDocument }
interface TeamView { banner: HTMLElement; name: HTMLElement; tally: HTMLElement; bar: HTMLElement; routine: HTMLElement }
interface Walker { node: HTMLElement; person: Person; path: Point[]; length: number; started: number; phase: 'in' | 'out'; plan: WalkPlan; onDrop(): void; onDone(): void }

export function createOffice(ports: OfficePorts) {
  const viewport = el('div', 'w-office');
  viewport.setAttribute('role', 'region');
  viewport.setAttribute('aria-label', t('world.stage'));
  const world = el('div', 'w-world', viewport);
  const canvas = el('canvas', 'w-art', world);
  canvas.setAttribute('aria-hidden', 'true');
  const layer = el('div', 'w-layer', world);
  const ownerPlate = el('div', 'w-plate w-owner-plate', layer);
  const watchdogPlate = el('div', 'w-plate', layer);
  watchdogPlate.hidden = true;
  // The Watchdog's beacon, lit only while an incident is open (the Watchdog's exception).
  const alarm = el('div', 'w-alarm', layer);
  alarm.hidden = true;
  alarm.setAttribute('aria-hidden', 'true');
  const tip = el('div', 'w-tip', layer);
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  const tipName = el('b', '', tip);
  const tipDetail = el('span', '', tip);

  const people = new Map<string, PersonView>();
  const documents = new Map<string, DocumentView>();
  const teams = new Map<string, TeamView>();
  const slots = new Map<string, Map<string, number>>();
  const walking = new Map<string, Walker>();
  const pendingDocuments = new Set<string>();
  const extras = new Map<string, ExtraView>();
  const trophyButtons = new Map<string, HTMLButtonElement>();
  let trophyMore: HTMLElement | null = null;
  const board = el('button', 'w-board', layer);
  board.type = 'button';
  board.tabIndex = -1;
  board.hidden = true;
  board.dataset.extra = 'board';
  board.addEventListener('click', () => { if (current?.whiteboard) ports.open({ kind: 'person', id: current.whiteboard.owner }); });
  board.addEventListener('pointerenter', () => showTip('board'));
  board.addEventListener('pointerleave', () => hideTip());
  let layout: OfficeLayout | null = null;
  let layoutKey = '';
  let viewportWidth = 0;
  let viewportHeight = 0;
  let scale = 1;
  let current: Office | null = null;
  let focusId = '';
  let selectedId = '';
  let worker: Worker | null = null;
  let artId = 0;
  let walkFrame = 0;
  let beat = 0;
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const beatTimer = window.setInterval(() => {
    if (document.hidden || reduced()) return;
    beat ^= 1;
    world.dataset.beat = String(beat);
  }, BEAT_MS);
  // Art theme: sprite URLs live in custom properties on the world; a theme change repaints the floor and
  // re-skins every person (sheets are per person) and walker.
  let theme = worldTheme();
  const applySkin = () => { for (const [name, value] of Object.entries(skinStyle(theme))) setStyle(world, name, value); };
  applySkin();
  world.dataset.sprites = 'ready';
  const stopTheme = watchTheme(next => {
    theme = next;
    applySkin();
    layoutKey = '';
    for (const view of people.values()) view.key = '';
    for (const walker of walking.values()) walker.node.style.backgroundImage = `url("${sheetFor(walker.person, theme)}")`;
    if (current) update(current);
  });

  function paint() {
    if (!layout || !current) return;
    const pixelScale = scale * (window.devicePixelRatio || 1);
    const zones = current.teams.map(team => ({ key: team.key, color: team.color, kind: team.kind }));
    const request: ArtRequest = {
      id: ++artId, input: { layout, zones, hour: new Date().getHours(), theme }, pixelScale,
      atlas: { url: ATLAS.url[theme], props: PROPS },
    };
    if (!worker) {
      worker = new ArtWorker();
      // 'base' shows the procedural floor at once; 'ready' once the furniture is painted too.
      worker.onmessage = (event: MessageEvent<ArtReply>) => {
        if (event.data.id === artId && event.data.ok) world.dataset.art = event.data.furnished ? 'ready' : 'base';
      };
      const offscreen = canvas.transferControlToOffscreen();
      worker.postMessage({ ...request, canvas: offscreen }, [offscreen]);
    } else {
      worker.postMessage(request);
    }
  }

  function shapes(office: Office): TeamShape[] {
    return office.teams.map(team => {
      const held = slots.get(team.key);
      const highest = held && held.size ? Math.max(...[...held.entries()].filter(([id]) => team.workers.some(each => each.id === id)).map(([, slot]) => slot), -1) : -1;
      return { key: team.key, kind: team.kind, seats: Math.max(team.workers.length, highest + 1) };
    });
  }

  function relayout(office: Office) {
    const teamShapes = shapes(office);
    // The hour picks the window light, so the art repaints once when it changes.
    const key = `${viewportWidth}x${viewportHeight}|${new Date().getHours()}|${teamShapes.map(shape => `${shape.key}:${shape.kind}:${Math.ceil(shape.seats / SEATS_PER_POD)}`).join(',')}`;
    if (key === layoutKey && layout) return false;
    layoutKey = key;
    ({ layout, scale } = fitOffice(viewportWidth, viewportHeight, teamShapes));
    setStyle(world, 'width', px(layout.width * scale));
    setStyle(world, 'height', px(layout.height * scale));
    setStyle(canvas, 'width', px(layout.width * scale));
    setStyle(canvas, 'height', px(layout.height * scale));
    setStyle(layer, 'width', px(layout.width));
    setStyle(layer, 'height', px(layout.height));
    setStyle(layer, 'transform', `scale(${scale})`);
    const room = layout.owner.room;
    setStyle(ownerPlate, 'left', px(room.x + 14));
    setStyle(ownerPlate, 'top', px(room.y + room.h - 30));
    setText(ownerPlate, t('world.office.owner'));
    const station = layout.watchdog?.station;
    watchdogPlate.hidden = !station;
    if (station) {
      setStyle(watchdogPlate, 'left', px(station.x + 8));
      setStyle(watchdogPlate, 'top', px(station.y + station.h - 22));
      setText(watchdogPlate, t('world.role.watchdog'));
      const beacon = alarmBox(station);
      setStyle(alarm, 'left', px(beacon.x));
      setStyle(alarm, 'top', px(beacon.y));
    }
    paint();
    return true;
  }

  function teamView(team: Team): TeamView {
    let view = teams.get(team.key);
    if (!view) {
      const banner = el('div', 'w-banner', layer);
      const name = el('b', 'w-banner-name', banner);
      const tally = el('span', 'w-banner-tally', banner);
      const bar = el('i', 'w-banner-bar', tally);
      const routine = el('span', 'w-banner-routine', banner);
      view = { banner, name, tally, bar, routine };
      teams.set(team.key, view);
    }
    return view;
  }

  function personView(person: Person): PersonView {
    let view = people.get(person.id);
    if (!view) {
      const screen = el('i', 'w-screen', layer);
      const button = el('button', 'w-person', layer);
      button.type = 'button';
      button.tabIndex = -1;
      button.dataset.person = person.id;
      const mark = el('span', 'w-mark', button);
      mark.setAttribute('aria-hidden', 'true');
      button.addEventListener('click', () => ports.open({ kind: 'person', id: person.id }));
      button.addEventListener('pointerenter', () => showTip(person.id));
      button.addEventListener('pointerleave', () => hideTip());
      button.classList.add('w-arrive');
      button.addEventListener('animationend', () => button.classList.remove('w-arrive'), { once: true });
      view = { button, mark, screen, person, seat: null, anchor: null, key: '' };
      people.set(person.id, view);
    }
    view.person = person;
    return view;
  }

  function stateLabel(person: Person, waiting = false): string {
    if (person.emphasis === 'decision') return t(waiting ? 'world.state.atDoor' : 'world.state.asking');
    if (person.emphasis === 'exception') return t('world.state.exception');
    return t(person.state === 'working' ? 'world.state.working' : person.state === 'waiting' ? 'world.state.waiting' : 'world.state.idle');
  }

  /** Seated: a running attempt types (work A/B on the shared beat), anyone else sits back (idle cell). */
  function placePerson(view: PersonView, seat: Seat) {
    const person = view.person;
    const busy = person.state === 'working';
    const work = `work-${seat.facing}` as PersonState;
    const state = busy ? work : `idle-${seat.facing}` as PersonState;
    const still = frame(state, stillColumn(state), SEATED_SHOWN);
    const typing = frame(work, STATES[work].columns[1], SEATED_SHOWN);
    const sheet = sheetFor(person, theme);
    const key = `seat|${sheet}|${state}|${seat.x}|${seat.cut}`;
    const button = view.button;
    if (view.key !== key) {
      view.key = key;
      button.style.cssText = `left:${px(seat.x - still.w / 2)};top:${px(seat.cut - still.h)};width:${px(still.w)};height:${px(still.h)};`
        + `background-image:url("${sheet}");background-size:${SHEET_SIZE};--still:${still.position};--beat:${typing.position}`;
      const screen = screenBox(seat, person.role === 'watchdog' ? layout?.watchdog?.station ?? null : null);
      view.screen.style.cssText = `left:${px(screen.x)};top:${px(screen.y)};width:${px(screen.w)};height:${px(screen.h)}`;
    }
    view.seat = seat;
    view.anchor = { x: seat.x, y: seat.cut };
    setData(button, 'waiting', null);
    setData(button, 'facing', seat.facing);
    setData(view.screen, 'facing', seat.facing);
    decorate(view, busy);
  }

  /** A person with a real open owner question stands at the owner's door, facing it; the desk stays theirs. */
  function placeWaiting(view: PersonView, seat: Seat, spot: Point) {
    const person = view.person;
    const still = frame('wait-up', stillColumn('wait-up'));
    const shift = frame('wait-up', STATES['wait-up'].columns[1]);
    const hand = handOffset('wait-up', STATES['wait-up'].columns.indexOf(stillColumn('wait-up')));
    const sheet = sheetFor(person, theme);
    const key = `wait|${sheet}|${spot.x}|${spot.y}`;
    const button = view.button;
    if (view.key !== key) {
      view.key = key;
      button.style.cssText = `left:${px(spot.x - FEET.x)};top:${px(spot.y - FEET.y)};width:${px(still.w)};height:${px(still.h)};`
        + `background-image:url("${sheet}");background-size:${SHEET_SIZE};--still:${still.position};--beat:${shift.position};`
        + `--hand-x:${px(hand.x)};--hand-y:${px(hand.y)}`;
      const screen = screenBox(seat, person.role === 'watchdog' ? layout?.watchdog?.station ?? null : null);
      view.screen.style.cssText = `left:${px(screen.x)};top:${px(screen.y)};width:${px(screen.w)};height:${px(screen.h)}`;
    }
    view.seat = seat;
    view.anchor = spot;
    setData(button, 'waiting', '');
    setData(view.screen, 'facing', seat.facing);
    decorate(view, false);
  }

  function decorate(view: PersonView, busy: boolean) {
    const person = view.person;
    const button = view.button;
    const walkingNow = walking.has(person.id);
    const sheet = sheetFor(person, theme);
    if (decodedSheets.has(sheet)) setData(button, 'loading', null);
    else {
      setData(button, 'loading', '');
      void decodeSheet(sheet).then(() => { if (sheetFor(view.person, theme) === sheet) setData(button, 'loading', null); });
    }
    setData(button, 'busy', busy ? '' : null);
    setData(button, 'role', person.role);
    setData(button, 'assignment', person.assignment?.assignmentId ?? null);
    setData(button, 'emphasis', person.emphasis);
    setData(button, 'walking', walkingNow ? '' : null);
    setData(view.screen, 'on', busy && !walkingNow ? '' : null);
    setText(view.mark, person.emphasis === 'decision' ? '?' : person.emphasis === 'exception' ? '!' : '');
    const label = `${person.name}${person.detail ? ` · ${person.detail}` : ''} · ${stateLabel(person, 'waiting' in button.dataset)}`;
    if (button.getAttribute('aria-label') !== label) button.setAttribute('aria-label', label);
    button.tabIndex = person.id === focusId ? 0 : -1;
    button.toggleAttribute('aria-current', person.id === selectedId);
  }

  function placeBoard(office: Office) {
    const value = office.whiteboard;
    if (!layout || !value) { board.hidden = true; extras.delete('board'); return; }
    const rect = layout.owner.whiteboard;
    board.hidden = false;
    setStyle(board, 'left', px(rect.x));
    setStyle(board, 'top', px(rect.y));
    setStyle(board, 'width', px(rect.w));
    setStyle(board, 'height', px(rect.h));
    const tasks = value.board.tasks;
    const capacity = BOARD_GRID.columns * BOARD_GRID.rows;
    const shown = tasks.length > capacity ? tasks.slice(0, capacity - 1) : tasks;
    const signature = `${shown.map(task => task.status).join(',')}|${tasks.length}`;
    if (board.dataset.signature !== signature) {
      board.dataset.signature = signature;
      board.replaceChildren(...shown.map(task => {
        const note = document.createElement('i');
        note.className = 'w-note';
        note.dataset.status = task.status;
        return note;
      }));
      if (tasks.length > shown.length) el('b', 'w-note w-note-more', board).textContent = `+${tasks.length - shown.length}`;
    }
    const name = t('world.board.title', { done: value.board.done, total: value.board.total });
    if (board.getAttribute('aria-label') !== name) board.setAttribute('aria-label', name);
    board.tabIndex = focusId === 'board' ? 0 : -1;
    extras.set('board', { button: board, name, detail: firstLine(value.board.summary), x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 });
  }

  function placeTrophies(office: Office) {
    if (!layout) return;
    const slots = shelfSlots(layout.owner.shelf);
    const all = office.trophies;
    const shown = all.length > slots.length ? all.slice(0, slots.length - 1) : all;
    const keep = new Set(shown.map(trophy => trophy.id));
    for (const [id, button] of trophyButtons) if (!keep.has(id)) { button.remove(); trophyButtons.delete(id); extras.delete(id); }
    shown.forEach((trophy, index) => {
      let button = trophyButtons.get(trophy.id);
      if (!button) {
        button = el('button', 'w-trophy', layer);
        button.type = 'button';
        button.tabIndex = -1;
        button.dataset.extra = trophy.id;
        button.addEventListener('click', () => ports.open({ kind: 'document', id: trophy.id }));
        button.addEventListener('pointerenter', () => showTip(trophy.id));
        button.addEventListener('pointerleave', () => hideTip());
        trophyButtons.set(trophy.id, button);
      }
      const slot = slots[index];
      setStyle(button, 'left', px(slot.x));
      setStyle(button, 'top', px(slot.y));
      const label = t('world.trophy.label', { title: trophy.title });
      if (button.getAttribute('aria-label') !== label) button.setAttribute('aria-label', label);
      button.tabIndex = focusId === trophy.id ? 0 : -1;
      extras.set(trophy.id, { button, name: trophy.title, detail: t('world.trophy.detail'), x: slot.x + TROPHY_W / 2, y: slot.y + TROPHY_H / 2 });
    });
    const more = all.length - shown.length;
    if (more > 0) {
      trophyMore ||= el('span', 'w-trophy-more', layer);
      const slot = slots[slots.length - 1];
      setStyle(trophyMore, 'left', px(slot.x));
      setStyle(trophyMore, 'top', px(slot.y));
      setText(trophyMore, `+${more}`);
      trophyMore.title = t('world.trophy.more', { count: more });
    } else if (trophyMore) {
      trophyMore.remove();
      trophyMore = null;
    }
  }

  function placeTeam(team: Team) {
    if (!layout) return;
    const zone = layout.zones.find(each => each.key === team.key);
    const view = teamView(team);
    if (!zone) { view.banner.hidden = true; return; }
    view.banner.hidden = false;
    setStyle(view.banner, 'left', px(zone.banner.x));
    setStyle(view.banner, 'top', px(zone.banner.y));
    setStyle(view.banner, '--c', team.color);
    setText(view.name, team.kind === 'router' ? team.lead.name : team.name);
    const board = team.board;
    view.tally.hidden = !board || !board.total;
    if (board && board.total) {
      setStyle(view.bar, '--done', String(board.done / board.total));
      view.tally.title = t('world.team.board', { done: board.done, total: board.total });
      setData(view.tally, 'label', `${board.done}/${board.total}`);
    }
    view.routine.hidden = team.routine === 0;
    setText(view.routine, team.history ? t('world.team.routineMore', { count: team.routine }) : t('world.team.routine', { count: team.routine }));
    view.routine.title = t('world.team.routineTitle', { count: team.routine });
    setData(view.banner, 'stale', team.failed ? '' : null);
  }

  function seatFor(team: Team, person: Person): Seat | null {
    if (!layout) return null;
    if (person.role === 'watchdog') return layout.watchdog?.seat ?? null;
    const zone = layout.zones.find(each => each.key === team.key);
    if (!zone) return null;
    if (person === team.lead) return zone.lead;
    const slot = slots.get(team.key)?.get(person.id);
    return slot === undefined ? null : zone.seats[slot] ?? null;
  }

  /** Lead reports and deliverables lie on the desk one by one (at most one of each per lead); Workers' results
   * stack in the in-tray, newest on top, so any number of them stays one reachable pile. */
  function placeDocuments(office: Office) {
    if (!layout) return;
    const onDesk = office.documents.filter(document => !pendingDocuments.has(document.id));
    const results = onDesk.filter(document => document.ref.source === 'worker-result');
    const shown = onDesk.filter(document => document.ref.source !== 'worker-result');
    const keep = new Set(shown.map(document => document.id));
    for (const [id, view] of documents) if (!keep.has(id)) { view.button.remove(); documents.delete(id); }
    const tray = layout.owner.tray;
    placePile(results, tray.x + 4, tray.y + 6);
    const deliverables = shown.filter(document => document.kind === 'deliverable');
    const reports = shown.filter(document => document.kind === 'report');
    const place = (document: DeskDocument, x: number, y: number, index: number) => {
      let view = documents.get(document.id);
      if (!view) {
        const button = el('button', 'w-doc', layer);
        button.type = 'button';
        button.tabIndex = -1;
        button.dataset.document = document.id;
        button.addEventListener('click', () => ports.open({ kind: 'document', id: document.id }));
        button.addEventListener('pointerenter', () => showTip(document.id));
        button.addEventListener('pointerleave', () => hideTip());
        button.classList.add('w-drop');
        button.addEventListener('animationend', () => button.classList.remove('w-drop'), { once: true });
        view = { button, document };
        documents.set(document.id, view);
      }
      view.document = document;
      setData(view.button, 'kind', document.kind);
      setStyle(view.button, 'left', px(x));
      setStyle(view.button, 'top', px(y));
      setStyle(view.button, 'z-index', String(10 + index));
      const label = `${t(document.kind === 'deliverable' ? 'world.doc.deliverable' : 'world.doc.report')} · ${document.title}`;
      if (view.button.getAttribute('aria-label') !== label) view.button.setAttribute('aria-label', label);
      view.button.tabIndex = document.id === focusId ? 0 : -1;
    };
    // One fan, papers first and gold folders on top: each document starts past the previous one's centre,
    // so every document's centre stays uncovered and clickable however many leads report.
    [...reports, ...deliverables].forEach((document, index) =>
      place(document, tray.x + 24 + index * 15, tray.y + 4 + (index % 2) * 3, index));
  }

  let pile: HTMLButtonElement | null = null;
  function placePile(results: ReadonlyArray<DeskDocument>, x: number, y: number) {
    const top = results[results.length - 1];
    if (!top) { pile?.remove(); pile = null; extras.delete('pile'); return; }
    if (!pile) {
      const button = el('button', 'w-doc w-pile', layer);
      button.type = 'button';
      button.tabIndex = -1;
      button.dataset.extra = 'pile';
      button.dataset.kind = 'report';
      button.addEventListener('click', () => ports.open({ kind: 'document', id: button.dataset.document! }));
      button.addEventListener('pointerenter', () => showTip('pile'));
      button.addEventListener('pointerleave', () => hideTip());
      pile = button;
    }
    setData(pile, 'document', top.id);
    setData(pile, 'pile', String(results.length));
    setStyle(pile, 'left', px(x));
    setStyle(pile, 'top', px(y));
    const name = t('world.doc.pile', { count: results.length });
    const label = `${name} · ${top.title}`;
    if (pile.getAttribute('aria-label') !== label) pile.setAttribute('aria-label', label);
    pile.tabIndex = focusId === 'pile' ? 0 : -1;
    extras.set('pile', { button: pile, name, detail: top.title, x: x + 8, y: y + 8 });
  }

  function update(office: Office) {
    current = office;
    if (!viewportWidth) return;
    for (const team of office.teams) slots.set(team.key, allocateSlots(slots.get(team.key) || new Map(), team.workers.map(worker => worker.id)));
    relayout(office);
    const present = new Set<string>();
    // Oldest open question first, nearest the door.
    const askers = [...new Set([...office.attention.decisions].sort((a, b) => a.since - b.since)
      .map(item => item.source.conversationId).filter((id): id is string => Boolean(id) && office.people.has(id!)))];
    const spots = layout ? waitingSpots(layout) : [];
    for (const team of office.teams) {
      placeTeam(team);
      for (const person of [team.lead, ...team.workers]) {
        const seat = seatFor(team, person);
        if (!seat) continue;
        present.add(person.id);
        const view = personView(person);
        const queue = person.emphasis === 'decision' ? askers.indexOf(person.id) : -1;
        if (queue >= 0 && queue < spots.length && !walking.has(person.id)) placeWaiting(view, seat, spots[queue]);
        else placePerson(view, seat);
      }
    }
    for (const [id, view] of people) {
      if (present.has(id) || walking.has(id)) continue;
      if (document.activeElement === view.button) viewport.focus({ preventScroll: true });
      view.button.remove(); view.screen.remove(); people.delete(id);
    }
    for (const [key, view] of teams) if (!office.teams.some(team => team.key === key)) { view.banner.remove(); teams.delete(key); }
    placeDocuments(office);
    placeBoard(office);
    placeTrophies(office);
    if (!focusId || (!people.has(focusId) && !documents.has(focusId) && !extras.has(focusId))) {
      const first = office.teams[0]?.lead.id;
      if (first && people.has(first)) { focusId = first; people.get(first)!.button.tabIndex = 0; }
    }
    const watchdog = office.teams.find(team => team.kind === 'watchdog')?.lead;
    const dark = !layout?.watchdog || watchdog?.emphasis !== 'exception';
    if (alarm.hidden !== dark) alarm.hidden = dark;
    world.dataset.emphasis = String(emphasisCount());
  }

  function emphasisCount(): number {
    let count = 0;
    for (const view of people.values()) if (view.person.emphasis) count += 1;
    for (const view of documents.values()) if (view.document.kind === 'deliverable') count += 1;
    return count;
  }

  function showTip(id: string) {
    const personView = people.get(id);
    const documentView = documents.get(id);
    const extra = extras.get(id);
    if (!personView && !documentView && !extra) return;
    const target = (personView?.button || documentView?.button || extra!.button);
    if (personView) {
      setText(tipName, personView.person.name);
      const state = stateLabel(personView.person, 'waiting' in personView.button.dataset);
      setText(tipDetail, `${personView.person.detail ? `${personView.person.detail} · ` : ''}${state}`);
    } else if (documentView) {
      setText(tipName, t(documentView.document.kind === 'deliverable' ? 'world.doc.deliverable' : 'world.doc.report'));
      setText(tipDetail, documentView.document.title);
    } else {
      setText(tipName, extra!.name);
      setText(tipDetail, extra!.detail);
    }
    tip.hidden = false;
    // Keep the tooltip inside the floor: a person by the right wall gets it shifted left, not cut off.
    const half = tip.offsetWidth / 2 + 4;
    const centre = target.offsetLeft + target.offsetWidth / 2;
    setStyle(tip, 'left', px(layout ? Math.min(Math.max(centre, half), layout.width - half) : centre));
    setStyle(tip, 'top', px(target.offsetTop - 8));
  }
  function hideTip() { tip.hidden = true; }

  function targets() {
    const list: Array<{ id: string; x: number; y: number; button: HTMLButtonElement }> = [];
    for (const [id, view] of people) if (view.anchor && !walking.has(id)) list.push({ id, x: view.anchor.x, y: view.anchor.y - 10, button: view.button });
    for (const [id, view] of documents) list.push({ id, x: view.button.offsetLeft + 8, y: view.button.offsetTop + 8, button: view.button });
    for (const [id, extra] of extras) list.push({ id, x: extra.x, y: extra.y, button: extra.button });
    return list;
  }

  function focusTarget(id: string, scroll = true) {
    const button = people.get(id)?.button || documents.get(id)?.button || extras.get(id)?.button;
    if (!button) return;
    const previous = people.get(focusId)?.button || documents.get(focusId)?.button || extras.get(focusId)?.button;
    if (previous && previous !== button) previous.tabIndex = -1;
    focusId = id;
    button.tabIndex = 0;
    button.focus({ preventScroll: true });
    if (scroll) button.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: reduced() ? 'auto' : 'smooth' });
    showTip(id);
  }

  viewport.addEventListener('keydown', event => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const direction = ARROWS[event.key];
    if (!direction) return;
    const list = targets();
    const from = list.find(item => item.id === focusId) || list[0];
    if (!from) return;
    const next = nearestInDirection(from, list.filter(item => item !== from), direction);
    if (next) focusTarget(next.id);
    event.preventDefault();
  });
  viewport.addEventListener('focusin', event => {
    const target = event.target as HTMLElement;
    const id = target.dataset.person || target.dataset.extra || target.dataset.document;
    if (id && id !== focusId) { focusId = id; showTip(id); }
    else if (id) showTip(id);
  });
  viewport.addEventListener('focusout', () => hideTip());

  function walkerNode(person: Person): HTMLElement {
    const node = el('div', 'w-walker', layer);
    const cell = frame('walk-down', 0);
    node.style.cssText = `width:${px(cell.w)};height:${px(cell.h)};background-image:url("${sheetFor(person, theme)}");background-size:${SHEET_SIZE}`;
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function stepWalker(walker: Walker, now: number) {
    const distance = Math.min(walker.length, ((now - walker.started) / 1000) * WALK_SPEED);
    let travelled = 0;
    let point = walker.path[walker.path.length - 1];
    let direction = walker.phase === 'in' ? UP : DOWN;
    for (let index = 1; index < walker.path.length; index += 1) {
      const a = walker.path[index - 1], b = walker.path[index];
      const segment = Math.abs(b.x - a.x) + Math.abs(b.y - a.y);
      if (travelled + segment >= distance) {
        const share = segment ? (distance - travelled) / segment : 1;
        point = { x: a.x + (b.x - a.x) * share, y: a.y + (b.y - a.y) * share };
        direction = Math.abs(b.x - a.x) > Math.abs(b.y - a.y) ? (b.x > a.x ? RIGHT : LEFT) : (b.y > a.y ? DOWN : UP);
        break;
      }
      travelled += segment;
    }
    const arrived = distance >= walker.length;
    const carrying = 'carry' in walker.node.dataset;
    const state = `${carrying ? 'carry' : 'walk'}-${DIRECTIONS[arrived && walker.phase === 'in' ? UP : direction]}` as PersonState;
    const columns = STATES[state].columns;
    const index = arrived ? columns.indexOf(stillColumn(state)) : Math.floor(((now - walker.started) / 1000) * STEP_FPS) % columns.length;
    const style = walker.node.style;
    style.backgroundPosition = frame(state, columns[index]).position;
    style.transform = `translate(${px(point.x - FEET.x)},${px(point.y - FEET.y)})`;
    if (carrying) {
      const hand = handOffset(state, index);
      setStyle(walker.node, '--hand-x', px(hand.x));
      setStyle(walker.node, '--hand-y', px(hand.y));
      setData(walker.node, 'layer', STATES[state].docLayer ?? null);
    }
    return arrived;
  }

  function tick(now: number) {
    walkFrame = 0;
    for (const [id, walker] of walking) {
      const arrived = stepWalker(walker, now);
      if (!arrived) continue;
      if (walker.phase === 'in') {
        if (now - walker.started < walker.length / WALK_SPEED * 1000 + DROP_PAUSE_MS) continue;
        walker.onDrop();
        walker.phase = 'out';
        const seat = people.get(id)?.seat;
        walker.path = walker.plan.leaves || !seat || !layout ? walkOut(layout!) : walkHome(layout, seat);
        walker.length = pathLength(walker.path);
        walker.started = now;
      } else {
        walker.node.remove();
        walking.delete(id);
        walker.onDone();
      }
    }
    if (walking.size) walkFrame = requestAnimationFrame(tick);
  }

  /** Plays one real report: the author walks in, leaves the document, walks back or out. */
  function walk(plan: WalkPlan, done: () => void, dropped?: () => void) {
    const view = people.get(plan.person);
    pendingDocuments.add(plan.document.id);
    const finish = () => {
      pendingDocuments.delete(plan.document.id);
      if (current) placeDocuments(current);
      world.dataset.emphasis = String(emphasisCount());
    };
    if (!view?.seat || !layout || reduced() || walking.has(plan.person) || 'waiting' in view.button.dataset) { finish(); done(); return; }
    const path = walkToOwner(layout, view.seat);
    const node = walkerNode(view.person);
    node.dataset.carry = plan.document.kind;
    node.dataset.person = plan.person;
    node.dataset.document = plan.document.id;
    const walker: Walker = {
      node, person: view.person, path, phase: 'in', plan, started: performance.now(),
      length: pathLength(path),
      onDrop: () => { delete node.dataset.carry; finish(); dropped?.(); },
      onDone: () => { if (current) update(current); done(); },
    };
    walking.set(plan.person, walker);
    world.dataset.walking = String(walking.size);
    setData(view.button, 'walking', '');
    setData(view.screen, 'on', null);
    if (!walkFrame) walkFrame = requestAnimationFrame(tick);
  }

  return {
    element: viewport,
    update,
    resize(width: number, height: number) {
      if (!width || (width === viewportWidth && height === viewportHeight)) return;
      viewportWidth = width;
      viewportHeight = height;
      layoutKey = '';
      if (current) update(current);
    },
    walk,
    /** Documents actually on the owner's desk (one still being carried is not there yet). */
    documentCounts() {
      let reports = Number(pile?.dataset.pile || 0), deliverables = 0;
      for (const view of documents.values()) if (view.document.kind === 'deliverable') deliverables += 1; else reports += 1;
      return { reports, deliverables };
    },
    isWalking: (id: string) => walking.has(id),
    focus: (id: string) => focusTarget(id),
    select(id: string | null) {
      selectedId = id || '';
      for (const [personId, view] of people) view.button.toggleAttribute('aria-current', personId === selectedId);
      if (id) people.get(id)?.button.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: reduced() ? 'auto' : 'smooth' });
    },
    restoreFocus() { const id = focusId; if (id) focusTarget(id, false); },
    emphasisCount,
    layout: () => layout,
    destroy() {
      window.clearInterval(beatTimer);
      stopTheme();
      if (walkFrame) cancelAnimationFrame(walkFrame);
      worker?.terminate(); worker = null;
      viewport.remove();
    },
  };
}
export type OfficeScene = ReturnType<typeof createOffice>;
