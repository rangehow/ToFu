/** Lead office: the team view beneath a lead's sidebar entry, in place of the
 * list of conversations it started (that list stays below as the entry's
 * archive). A project coordinator's floor shows its own office and one floor
 * per Worker desk; the main coordinator's headquarters shows its office, one
 * floor per project with that project's coordinator, then its own Worker desks.
 * The conversation stays the main view: a room opens its conversation there, a
 * project floor opens that project's folder and coordinator, and the room of the
 * open conversation is framed, so the office reads as the list it replaces.
 *
 * Owns the office root (presence places it beneath the lead's entry), the game
 * stage (tap-only, so the sidebar keeps scrolling; sized to the tower's aspect),
 * the assignment page read (the one Manage work reads) and, at the headquarters,
 * the owner's project summary pages. Binding, board and report facts arrive from
 * the coordination surface that reads them; live run flags and the open
 * conversation from the loaded catalog. Reads are event-driven: on show, on a
 * project push, and when the lead's or a desk's live state changes; one flight
 * at a time; no polling or timers.
 * Entry: createLeadOffice.
 */
import { createButton } from '../../ui';
import { t } from '../../i18n';
import { apiFailure } from '../../api/errors';
import type { ProjectDispatchPage } from '../../api/project-dispatch.generated';
import type { CoordinatorProgressBoard, CoordinatorReportState } from '../../api/project-coordination.generated';
import { mountStage, type Stage } from '../office-game/engine/stage';
import { clampCamera } from '../office-game/engine/camera';
import {
  projectLeadOffice, type LiveStatus, type OfficeAltitude, type OfficeFolder,
  type OfficeProject, type OfficeProjectFacts, type OfficeView,
} from './projection';
import { createLeadOfficeScene, type LeadOfficeScene, type LeadOfficeSceneText, type OfficeActivation } from './scene';
import './office.css';

export interface LeadOfficeContext {
  altitude: OfficeAltitude;
  /** Project root of the lead (the router home for the main coordinator). */
  path: string;
  leadId: string;
  board: CoordinatorProgressBoard | null;
  report: CoordinatorReportState | undefined;
}
export interface LeadOfficeProjectsPage { projects: OfficeProjectFacts[]; nextCursor: string | null; }
export interface LeadOfficePorts {
  assignments(path: string, signal: AbortSignal): Promise<ProjectDispatchPage>;
  projects(cursor: string | undefined, signal: AbortSignal): Promise<LeadOfficeProjectsPage>;
  folders(): OfficeFolder[];
  live(conversationId: string): LiveStatus;
  /** Running worker chats the lead started, from the loaded catalog. */
  runningWorkers(leadId: string): { conversationId: string; title: string }[];
  /** A conversation's own title from the loaded catalog ('' when unknown or untitled). */
  title(conversationId: string): string;
  /** The lead conversation's own live flags, from the loaded catalog. */
  leadActivity(leadId: string): { streaming: boolean; awaitingHuman: boolean };
  /** The conversation open in the main view ('' when none). */
  currentConversationId(): string;
  subscribe(refresh: () => void): () => void;
  navigate(conversationId: string): void;
  /** Opens the conversation at its newest open ask_human question with the composer replying to it
   * (the same handler as the studio's "answer in conversation"). */
  openQuestion(conversationId: string): void;
  /** Shows the project's folder and opens its coordinator. */
  openProject(path: string, conversationId: string): void;
}

const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';

export function createLeadOffice(ports: LeadOfficePorts) {
  let context: LeadOfficeContext | null = null, identity = '';
  let assignments: ProjectDispatchPage | null | undefined;
  let projects: OfficeProjectFacts[] | null | undefined;
  let failure = '';
  let flight: AbortController | null = null, pending = false, generation = 0;
  let view: OfficeView | null = null, viewKey = '', liveKey = '';
  let stage: Stage | null = null, scene: LeadOfficeScene | null = null;
  let unsubscribe: (() => void) | undefined;

  const root = document.createElement('section');
  root.className = 'lead-office';
  root.hidden = true;
  const stageHost = document.createElement('div'); stageHost.className = 'lead-office-stage';
  const status = document.createElement('div'); status.className = 'lead-office-status';
  status.setAttribute('role', 'status'); status.hidden = true;
  root.append(stageHost, status);

  const roleName = () => t(context?.altitude === 'headquarters' ? 'globalRouter.entry' : 'projectCoordination.coordinator');
  const projectFacts = (project: OfficeProject): string[] => {
    if (!project.coordinatorId) return [t('leadOffice.project.unavailable')];
    if (project.workers === null) return [t('leadOffice.project.error')];
    const facts = [
      project.workers ? t('leadOffice.count.working', { count: project.workers }) : '',
      project.questions ? t('leadOffice.count.questions', { count: project.questions }) : '',
      project.report ? t('leadOffice.count.newReport') : '',
      project.overdue ? t('leadOffice.count.overdue', { count: project.overdue }) : '',
    ].filter(Boolean);
    return facts.length ? facts : [t('leadOffice.project.idle')];
  };
  const leadDetails = (lead: { report: string; board: { done: number; total: number } | null }) => [
    lead.report ? t('leadOffice.report', { title: lead.report }) : '',
    lead.board ? t('world.team.board', { done: lead.board.done, total: lead.board.total }) : '',
  ].filter(Boolean);
  const text: LeadOfficeSceneText = {
    lead: lead => [t('leadOffice.lead.label', { role: roleName(), state: t(`leadOffice.lead.${lead.state}`) }),
      lead.report ? t('leadOffice.report', { title: lead.report }) : '', t('leadOffice.openChat')].filter(Boolean).join(' · '),
    get leadName() { return roleName(); },
    leadState: lead => t(`leadOffice.lead.${lead.state}`),
    leadDetails,
    desk: desk => t('leadOffice.desk.label', { title: desk.title, state: t(`leadOffice.desk.${desk.state}`) }),
    deskState: desk => t(`leadOffice.floor.${desk.state}`),
    deskDetail: desk => t(`leadOffice.desk.${desk.state}`),
    project: project => t('leadOffice.project.label', { name: project.name, details: projectFacts(project).join(' · ') }),
    projectFacts,
    get open() { return t('leadOffice.openChat'); },
  };

  function current(own: number) { return own === generation && !!context; }
  function activate(target: OfficeActivation) {
    if (target.kind === 'project') ports.openProject(target.path, target.conversationId);
    else if (!target.asking) ports.navigate(target.conversationId);
    if (target.asking) ports.openQuestion(target.conversationId);
  }

  function mountCanvas() {
    if (stage || !context) return;
    const fontFamily = window.getComputedStyle(root).fontFamily || 'sans-serif';
    scene = createLeadOfficeScene({ wake: () => stage?.wake(), activate, text, fontFamily });
    if (view) scene.update(view);
    stageHost.style.aspectRatio = `${scene.scene.width} / ${scene.scene.height}`;
    stage = mountStage(stageHost, { label: t('leadOffice.stageLabel'), gestures: 'tap', fontFamily });
    stage.camera.fit = 'contain';
    stage.setScene(scene.scene);
  }
  function unmountStage() {
    stage?.dispose(); stage = null; scene?.dispose(); scene = null;
    stageHost.style.removeProperty('aspect-ratio');
  }
  /** Hand the scene the view; a tower that gained or lost a floor resizes its host and camera. */
  function repaint() {
    if (!scene || !view || !stage) return;
    scene.update(view);
    const { width, height } = scene.scene;
    if (stage.camera.worldWidth !== width || stage.camera.worldHeight !== height) {
      stageHost.style.aspectRatio = `${width} / ${height}`;
      stage.camera.worldWidth = width; stage.camera.worldHeight = height; clampCamera(stage.camera);
    }
    stage.wake();
  }

  function paintStatus() {
    const unavailable = view?.coverage === 'unavailable' || view?.projectsCoverage === 'unavailable';
    status.hidden = !unavailable;
    if (!unavailable) { status.replaceChildren(); return; }
    const message = document.createElement('span');
    message.textContent = failure ? t('leadOffice.readFailedCause', { cause: failure }) : t('leadOffice.readFailed');
    status.replaceChildren(message, createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: () => read() }));
  }
  function projectView() {
    if (!context) return;
    const next = projectLeadOffice({
      altitude: context.altitude, leadId: context.leadId, lead: ports.leadActivity(context.leadId),
      board: context.board, report: context.report, assignments, live: ports.live,
      running: ports.runningWorkers(context.leadId), title: ports.title, current: ports.currentConversationId(),
      projects: context.altitude === 'headquarters' ? projects : undefined, folders: ports.folders(),
    });
    const key = JSON.stringify(next);
    if (key === viewKey) return;
    viewKey = key; view = next;
    root.dataset.altitude = context.altitude;
    root.setAttribute('aria-label', t(context.altitude === 'headquarters' ? 'leadOffice.headquartersTitle' : 'leadOffice.floorTitle'));
    root.setAttribute('aria-busy', String(next.coverage === 'loading' || next.projectsCoverage === 'loading'));
    paintStatus(); repaint();
  }
  /** Live states that can change what the assignment page says: the lead's or a
   * desk's run state, or a Worker of this lead starting. A change re-reads once. */
  function liveSignature(): string {
    if (!context) return '';
    const lead = ports.leadActivity(context.leadId);
    const desks = (view?.desks || []).map(desk => `${desk.conversationId}:${ports.live(desk.conversationId) || ''}`);
    const running = ports.runningWorkers(context.leadId).map(worker => worker.conversationId).sort();
    return JSON.stringify([lead.streaming, lead.awaitingHuman, desks, running]);
  }
  async function readProjects(signal: AbortSignal) {
    const all: OfficeProjectFacts[] = [], cursors = new Set<string>();
    let cursor: string | undefined;
    do {
      const page = await ports.projects(cursor, signal);
      all.push(...page.projects);
      cursor = page.nextCursor || undefined;
      if (cursor && cursors.has(cursor)) break;
      if (cursor) cursors.add(cursor);
    } while (cursor);
    return all;
  }
  function read() {
    if (!context) return;
    if (flight) { pending = true; return; }
    const own = generation, scope = context, controller = new AbortController();
    flight = controller; pending = false;
    const reads: Promise<void>[] = [ports.assignments(scope.path, controller.signal).then(page => {
      if (current(own)) { assignments = page; failure = ''; }
    }, error => {
      if (!current(own) || isAbort(error)) return;
      // A failed refresh keeps the last confirmed page; only a first failure is unavailable.
      if (assignments === undefined) assignments = null;
      failure = apiFailure(error).message;
    })];
    if (scope.altitude === 'headquarters') reads.push(readProjects(controller.signal).then(list => {
      if (current(own)) projects = list;
    }, error => {
      if (!current(own) || isAbort(error)) return;
      if (projects === undefined) projects = null;
      failure = apiFailure(error).message;
    }));
    void Promise.all(reads).finally(() => {
      if (flight === controller) flight = null;
      if (!current(own)) return;
      // The signature covers the desks this page projects, so it follows the projection.
      projectView();
      liveKey = liveSignature();
      if (pending) read();
    });
  }

  function reset() {
    generation++; flight?.abort(); flight = null; pending = false;
    unsubscribe?.(); unsubscribe = undefined;
    assignments = undefined; projects = undefined; failure = '';
    view = null; viewKey = ''; liveKey = '';
  }

  return {
    root,
    /** The lead whose team is shown ('' when hidden). */
    leadId: () => context?.leadId || '',
    /** A lead to present: a new one resets and reads; the same one takes fresh facts. */
    show(next: LeadOfficeContext) {
      const id = JSON.stringify([next.altitude, next.path, next.leadId]);
      if (id !== identity) {
        // A replaced lead's floors and walkers never carry over into another tower.
        reset(); unmountStage(); identity = id; context = next;
        root.hidden = false;
        unsubscribe = ports.subscribe(() => read());
        projectView(); mountCanvas();
        read();
      } else { context = next; projectView(); }
    },
    hide() {
      if (!context) return;
      reset(); context = null; identity = '';
      unmountStage(); status.hidden = true; status.replaceChildren(); root.hidden = true;
    },
    /** Every conversation list render: the open conversation or live flags may have moved. */
    refresh() {
      if (!context) return;
      projectView();
      const signature = liveSignature();
      if (view && view.coverage !== 'loading' && signature !== liveKey) { liveKey = signature; read(); }
    },
    destroy() { reset(); context = null; identity = ''; unmountStage(); root.remove(); },
  };
}
export type LeadOffice = ReturnType<typeof createLeadOffice>;
