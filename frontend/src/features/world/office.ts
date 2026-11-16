/** Tofu office: the open-plan floor on the shared game stage (features/office-game/engine). Mounts the stage with
 * camera gestures (drag to pan, wheel or pinch to zoom, +/-/0 keys), spatial arrow-key focus over people and
 * documents, and a camera that fits the whole view, so a docked panel is an inset that crops the floor and the
 * camera pans to the chosen person instead of the floor shrinking. Hosts the world scene (scene.ts) with its art
 * (art.ts), follows the art theme, and gives the workspace (app.ts) its office: a new projection, report walks,
 * selection and focus, the dock inset, and what lies on the owner's desk.
 * Entry point: createOffice. */
import { t } from '../../i18n';
import type { Insets } from '../office-game/engine/camera';
import { mountStage } from '../office-game/engine/stage';
import { createWorldArt } from './art';
import type { Office, Person } from './model';
import { createWorldScene, type OpenTarget, type WalkPlan } from './scene';
import { watchTheme, worldTheme } from './skin';

export interface OfficePorts { open(target: OpenTarget): void }

function stateLabel(person: Person, waiting: boolean): string {
  if (person.emphasis === 'decision') return t(waiting ? 'world.state.atDoor' : 'world.state.asking');
  if (person.emphasis === 'exception') return t('world.state.exception');
  return t(person.state === 'working' ? 'world.state.working' : person.state === 'waiting' ? 'world.state.waiting' : 'world.state.idle');
}

export function createOffice(ports: OfficePorts) {
  const viewport = document.createElement('div');
  viewport.className = 'w-office';
  const fontFamily = getComputedStyle(document.documentElement).getPropertyValue('--font-ui').trim() || 'sans-serif';
  const stage = mountStage(viewport, { label: t('world.stage'), fontFamily, gestures: 'camera', navigation: 'targets' });
  viewport.setAttribute('role', 'region');
  stage.camera.fit = 'contain';
  stage.camera.fitTo = 'view';
  // data-art="ready": the theme's atlas and sprites have decoded and the furniture is drawn.
  const art = createWorldArt(() => {
    if (art.theme()) viewport.dataset.art = 'ready';
    stage.wake();
  });
  const scene = createWorldScene({
    open: target => ports.open(target),
    wake: () => stage.wake(),
    camera: stage.camera,
    fontFamily,
    art,
    text: {
      get ownerPlate() { return t('world.office.owner'); },
      get watchdogPlate() { return t('world.role.watchdog'); },
      person(person, waiting) {
        const state = stateLabel(person, waiting);
        return { label: `${person.name}${person.detail ? ` · ${person.detail}` : ''} · ${state}`, state };
      },
      document: kind => t(kind === 'deliverable' ? 'world.doc.deliverable' : 'world.doc.report'),
      pile: count => t('world.doc.pile', { count }),
      board: (done, total) => t('world.board.title', { done, total }),
      trophy: title => t('world.trophy.label', { title }),
      get trophyDetail() { return t('world.trophy.detail'); },
      trophyMore: count => t('world.trophy.more', { count }),
      routine: (count, more) => t(more ? 'world.team.routineMore' : 'world.team.routine', { count }),
    },
  });
  scene.setTheme(worldTheme());
  stage.setScene(scene.scene);
  const stopTheme = watchTheme(next => scene.setTheme(next));
  // Focus returns to whoever had it when a panel closes; a pointer opens a panel without focusing anyone.
  let lastFocused = '';
  let lastSelected = '';
  viewport.addEventListener('focusin', event => {
    const id = (event.target as HTMLElement | null)?.dataset?.target;
    if (id) lastFocused = id;
  });

  return {
    element: viewport,
    /** A new projection; `settling` while the office's first reads have not all answered. */
    update(office: Office, settling: boolean) { scene.update(office, settling); },
    resize(width: number, height: number) { scene.setViewport(width, height); },
    walk(plan: WalkPlan, done: () => void, dropped?: () => void) { scene.walk(plan, done, dropped); },
    /** Documents actually on the owner's desk (one still being carried is not there yet). */
    documentCounts: () => scene.documentCounts(),
    isWalking: (id: string) => scene.isWalking(id),
    focus(id: string) { stage.resumeFollow(); stage.focusTarget(id); },
    /** The person whose panel is open: ringed, and the camera brings them into the frame. */
    select(id: string | null) {
      if (id) { lastSelected = id; stage.resumeFollow(); }
      scene.select(id);
    },
    restoreFocus() { const id = lastFocused || lastSelected; if (id) stage.focusTarget(id); },
    /** Screen space a docked panel covers: the camera keeps the scale and pans within what stays visible. */
    setInsets(insets: Insets) { stage.setInsets(insets); },
    layout: () => scene.layout(),
    inspect: () => ({ ...scene.inspect(), camera: { ...stage.camera, insets: { ...stage.camera.insets } }, metrics: stage.metrics() }),
    destroy() {
      stopTheme();
      stage.dispose();
      scene.dispose();
      art.dispose();
      viewport.remove();
    },
  };
}
export type OfficeScene = ReturnType<typeof createOffice>;
