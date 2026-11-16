/** Tofu office: the game-led landing workspace inside the application shell. Composes the live read-only
 * sources, the office projection and its report events, the open-plan scene, the HUD and the person panel,
 * with one requestAnimationFrame-coalesced render per change. The only interaction is choosing someone to
 * talk to: a person or a document opens that person's panel (brief first, then the application's own chat
 * through the shell). Esc closes the panel; arrows and Enter work in the office.
 * Entry point: mountWorld (returns teardown and the route hook). */
import { t } from '../../i18n';
import { projectBrief, type TurnRef } from './brief';
import { reportEvents } from './events';
import { createHud, type PushState } from './hud';
import { createWorldLive, type LiveTeam, type PushPort, type WorldLive } from './live';
import { busyConversations, factQuestions, openAlarms, type FactsView } from './facts';
import {
  busyOf, memberPerson, newestAssignments, projectOffice, type DeskDocument, type Office, type Person, type PersonLabels, type TeamInput,
} from './model';
import { assignmentStatus } from '../project-coordination-graph';
import { createOffice, type OpenTarget } from './office';
import { createPanel } from './panel';

export type ConversationDock = 'closed' | 'brief' | 'chat';
/** What the world asks of the application shell; it never reaches into chat chrome itself. */
export interface WorldShell {
  /** Show this person's real conversation in the application's chat column, optionally at one Turn. */
  openChat(conversationId: string, turn: TurnRef | null): void;
  dock(mode: ConversationDock): void;
  classic(): void;
  push: PushPort | null;
}
export interface WorldOptions { live?: (onChange: () => void) => WorldLive; now?: () => number }

/** One team's projection input. Office facts, where the server has them, are the authority for who is
 * busy, which owner questions are open (the text still comes from the lead's tail) and open incidents. */
const sourcesOf = (team: LiveTeam) => [team.team, team.assignments, team.progress, team.reports, team.questions];

function input(team: LiveTeam, facts: FactsView | null, busy: ReadonlySet<string> | null, alarms: number | null): TeamInput {
  const sources = sourcesOf(team);
  return {
    key: team.key, kind: team.kind, name: team.name, color: team.color, leadId: team.leadId,
    team: team.team.value, assignments: team.assignments.value, progress: team.progress.value,
    reports: team.reports.value, questions: factQuestions(facts, team.leadId, team.questions.value) ?? team.questions.value,
    failed: sources.some(source => source.status === 'error'), held: new Set(),
    ...(busy ? { busy } : {}), ...(team.kind === 'watchdog' && alarms !== null ? { alarms } : {}),
  };
}

export function mountWorld(root: HTMLElement, shell: WorldShell, options: WorldOptions = {}) {
  const now = options.now || (() => Date.now());
  const stage = document.createElement('div');
  stage.className = 'w-stage';
  let frame = 0;
  let office: Office | null = null;
  let inputs = new Map<string, TeamInput>();
  const held = new Map<string, Map<string, { at: number; reads: number }>>();
  const arrived: DeskDocument[] = [];
  const opened = new Set<string>();
  const live = (options.live || (onChange => createWorldLive(onChange, shell.push)))(schedule);
  const labels: PersonLabels = {
    router: t('world.role.router'), watchdog: t('world.role.watchdog'), lead: team => t('world.role.lead', { team }),
  };
  const scene = createOffice({ open: target => open(target) });
  const hud = createHud({ classic: () => shell.classic() });
  const panel = createPanel({
    close: () => closePanel(),
    chat: turn => {
      const id = panel.person();
      if (!id) return;
      panel.setTab('chat');
      shell.dock('chat');
      shell.openChat(id, turn);
    },
    brief: () => shell.dock('brief'),
  });
  const empty = document.createElement('p');
  empty.className = 'w-empty';
  empty.hidden = true;
  empty.textContent = t('world.empty');
  // The office's loading card: what share of its first reads has answered, over the already painted floor.
  const loader = document.createElement('div');
  loader.className = 'w-loader';
  loader.setAttribute('aria-hidden', 'true');
  const loaderTitle = document.createElement('b');
  loaderTitle.textContent = t('world.loading.title');
  const loaderBar = document.createElement('i');
  const loaderCount = document.createElement('span');
  loader.append(loaderTitle, loaderBar, loaderCount);
  stage.append(scene.element, empty, loader);
  root.append(hud.element, stage, panel.head, panel.body);

  function schedule() { if (!frame) frame = requestAnimationFrame(render); }

  /** The panel's person: seated in the office, or any member of the team that owns the opened document. */
  let panelTeam = '';
  function personOf(personId: string): Person | null {
    const seated = office?.people.get(personId);
    if (seated) return seated;
    const input = inputs.get(panelTeam);
    return input ? memberPerson(input, personId, labels) : null;
  }

  function identity(person: Person) {
    const team = office?.teams.find(each => each.key === person.team);
    const state = person.emphasis === 'decision' ? t('world.state.asking') : person.emphasis === 'exception' ? t('world.state.exception')
      : t(person.state === 'working' ? 'world.state.working' : person.state === 'waiting' ? 'world.state.waiting' : 'world.state.idle');
    return { team: person.role === 'worker' ? team?.name || '' : '', color: team?.color || '#d9b25f', state };
  }

  let panelFocus: DeskDocument['ref'] | undefined;
  let briefSignature = '';
  /** Repaints the brief only when what it shows changed, so reading, focus and selection survive live updates. */
  function showBrief(personId: string, force = false): boolean {
    const person = personOf(personId);
    if (!person) return false;
    const brief = projectBrief(person, inputs.get(person.team), now(), panelFocus);
    const who = identity(person);
    const signature = JSON.stringify([brief, who, Math.floor(now() / 60_000)]);
    if (!force && signature === briefSignature) return true;
    briefSignature = signature;
    panel.render(brief, who, now());
    return true;
  }

  function open(target: OpenTarget) {
    if (!office) return;
    let personId = target.id;
    panelFocus = undefined;
    panelTeam = office.people.get(personId)?.team || '';
    if (target.kind === 'document') {
      // A desk document leaves the desk once read; a trophy stays on the shelf.
      const desk = [...office.documents, ...arrived].find(each => each.id === target.id);
      const source = desk || office.trophies.find(each => each.id === target.id);
      if (!source) return;
      personId = source.ref.conversationId;
      panelFocus = source.ref;
      panelTeam = source.team;
      if (desk) opened.add(desk.id);
    }
    panel.setTab('brief');
    if (!showBrief(personId, true)) return;
    if (target.kind === 'document') panel.body.dataset.document = target.id;
    else delete panel.body.dataset.document;
    shell.dock('brief');
    scene.select(personId);
    panel.focus();
    schedule();
  }

  function closePanel() {
    panel.hide();
    shell.dock('closed');
    scene.select(null);
    scene.restoreFocus();
  }

  /** A Worker whose attempt ended stays at its desk until an assignment read that reflects the ending lands
   * (its newest assignment is no longer running), or a second read after the ending, whichever comes first. */
  function trackHeld(previous: TeamInput | undefined, value: TeamInput, team: LiveTeam): Set<string> {
    const holds = held.get(team.key) || new Map<string, { at: number; reads: number }>();
    held.set(team.key, holds);
    const running = busyOf(value);
    for (const id of previous ? busyOf(previous) : []) {
      if (id !== team.leadId && !running.has(id)) holds.set(id, { at: team.assignments.at, reads: 0 });
    }
    const newest = newestAssignments(team.leadId, team.assignments.value);
    for (const [id, hold] of holds) {
      if (team.kind !== 'project') { holds.delete(id); continue; }
      if (team.assignments.at <= hold.at) continue;
      hold.at = team.assignments.at;
      hold.reads += 1;
      const row = newest.get(id);
      if (hold.reads >= 2 || !row || assignmentStatus(row) !== 'running') holds.delete(id);
    }
    return new Set(holds.keys());
  }

  function render() {
    frame = 0;
    const time = now();
    const next = new Map<string, TeamInput>();
    const titles = new Map<string, string>();
    const plans: Array<{ person: string; document: DeskDocument; leaves: boolean }> = [];
    const facts = live.state.facts;
    const busy = busyConversations(facts);
    const alarms = openAlarms(facts);
    for (const team of live.state.teams) {
      const previous = inputs.get(team.key);
      const value = input(team, facts, busy, alarms);
      for (const member of team.team.value?.conversations || []) titles.set(member.conversationId, member.title);
      if (previous) {
        for (const event of reportEvents(previous, value, titles)) {
          if (event.kind === 'worker-report') {
            const document: DeskDocument = {
              id: `result:${event.assignmentId}:${event.resultRevision}`, kind: 'report', author: event.person, team: event.team,
              title: event.summary ? `${event.title} · ${event.summary}` : event.title, at: time,
              ref: { source: 'worker-result', conversationId: event.person, assignmentId: event.assignmentId,
                resultRevision: event.resultRevision, turnId: event.turnId },
            };
            if (!arrived.some(each => each.id === document.id)) arrived.push(document);
            plans.push({ person: event.person, document, leaves: !busyOf(value).has(event.person) });
          } else {
            plans.push({ person: event.person, leaves: false, document: {
              id: `report:${event.person}:${event.turnId}`, kind: event.reportKind === 'delivery' ? 'deliverable' : 'report',
              author: event.person, team: event.team, title: event.title, at: event.at,
              ref: { source: 'lead-report', conversationId: event.person, turnId: event.turnId, reportKind: event.reportKind },
            } });
          }
        }
      }
      value.held = trackHeld(previous, value, team);
      next.set(team.key, value);
    }
    inputs = next;
    office = projectOffice(time, [...next.values()], labels);
    const leadDocuments = new Set(office.documents.map(document => document.id));
    office.documents = [...office.documents, ...arrived.filter(document => !leadDocuments.has(document.id))]
      .filter(document => !opened.has(document.id));
    for (const plan of plans) scene.walk(plan, schedule, schedule);
    scene.update(office);
    const onDesk = scene.documentCounts();
    const push: PushState = !shell.push ? 'offline' : shell.push.connected() ? 'live' : 'connecting';
    // Until every first read answers (a cold office facts baseline takes seconds), the office says so
    // rather than passing an empty or provisional floor off as the truth.
    const loading = !live.state.error && (live.state.loading
      || live.state.teams.some(team => sourcesOf(team).some(source => source.status === 'loading')));
    let working = 0, exceptions = 0;
    for (const person of office.people.values()) {
      if (person.state === 'working') working += 1;
      if (person.emphasis === 'exception') exceptions += 1;
    }
    hud.update({
      push, now: time, working, routine: office.teams.reduce((sum, team) => sum + team.routine, 0),
      reports: onDesk.reports, decisions: office.attention.decisions.length, exceptions, deliverables: onDesk.deliverables, loading,
    });
    if (panel.isOpen() && panel.tab() === 'brief') {
      const body = panel.body;
      const scroll = body.scrollTop;
      showBrief(panel.person());
      body.scrollTop = scroll;
    }
    stage.dataset.ready = live.state.started ? 'true' : 'false';
    stage.setAttribute('aria-busy', String(loading));
    loader.hidden = !loading;
    if (loading) {
      // One step for discovering the teams and the office facts, then every source each team reads.
      let total = 1, done = live.state.loading ? 0 : 1;
      for (const team of live.state.teams) for (const source of sourcesOf(team)) {
        if (source.status === 'idle') continue;
        total += 1;
        if (source.status !== 'loading') done += 1;
      }
      loaderBar.style.setProperty('--done', String(done / total));
      const count = t('world.loading.progress', { done, total });
      if (loaderCount.textContent !== count) loaderCount.textContent = count;
    }
    empty.hidden = !live.state.error;
  }

  // The floor plan follows the window, not the scene column: opening a panel narrows the view (the camera
  // pans to the chosen person) without rearranging the office.
  const onResize = () => { scene.resize(window.innerWidth, scene.element.clientHeight || window.innerHeight); schedule(); };
  window.addEventListener('resize', onResize);
  onResize();

  function onKey(event: KeyboardEvent) {
    if (event.key !== 'Escape' || !panel.isOpen()) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
    closePanel();
    event.preventDefault();
  }
  document.addEventListener('keydown', onKey);
  const minute = window.setInterval(schedule, 60_000);
  void live.start();

  return {
    dispose() {
      live.stop();
      scene.destroy();
      window.removeEventListener('resize', onResize);
      document.removeEventListener('keydown', onKey);
      window.clearInterval(minute);
      if (frame) cancelAnimationFrame(frame);
      shell.dock('closed');
      hud.element.remove(); stage.remove(); panel.head.remove(); panel.body.remove();
    },
    route(_path: string) { schedule(); },
    office: () => office,
  };
}
