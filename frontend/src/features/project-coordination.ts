/** Demand-only coordinator workspace. Normal conversations remain the canvas;
 * this dialog owns read demand and view state, never execution or transcript state.
 */
import type { CoordinationCanvasState } from './project-coordination-canvas';
import { createButton, createDialog, createAlert, createTabs } from '../ui';
import '../styles/application/33-project-coordination.css';
import { coordinationText as text, coordinationTaskDigest, createCoordinationBoard, projectCoordinationWorkGroups } from './project-coordination-board';
export { projectCoordinationWorkGroups } from './project-coordination-board';
import { t } from '../i18n';
import { projectCoordinationGraph } from './project-coordination-graph';
import { createCoordinationWorkList, type CoordinationConversation } from './project-coordination-work-list';
import { captureCoordinationView, createCoordinationReference } from './project-coordination-reference';
import { createCoordinationManagement } from './project-coordination-management';
import type { CoordinatorUsageState, ProjectDispatchPage } from '../api/project-dispatch.generated';
import { createAssignmentHistoryNavigation } from './project-assignment-history';
import type { CoordinatorReportState, CoordinatorReportReadRequest, CoordinationBinding, CoordinatorProgressState, ProjectMessagePage, CoordinatorConversationPage } from '../api/project-coordination.generated';
import { createProjectMessageReceipts } from './project-message-receipts';
import { createProjectDispatch, type ProjectDispatchPorts } from './project-dispatch';
import { createProjectScopeChanges, type ProjectScopeChangePorts } from './project-scope-changes';
import { createProjectResponsibilities, type ProjectResponsibilityPorts } from './project-responsibilities';
import { createProjectSharedFiles, type ProjectSharedFilesPorts } from './project-shared-files';
import { createCoordinatorBudget, type CoordinatorBudgetPorts } from './coordinator-budget';
import type { RouterWorkerModelPorts } from './router-worker-model';

export interface CoordinationWork {
  id: string; conversationId?: string; title?: string;
  status: 'active' | 'completed' | 'failed' | 'cancelled';
  resultSummary?: string; changedPaths?: string[];
}
export interface ProjectCoordinationPorts {
  path: string; currentConversationId: string; currentPath(): string;
  currentConversation?(): string;
  dispatch?: ProjectDispatchPorts;
  scopeChanges?: ProjectScopeChangePorts;
  responsibilities?: ProjectResponsibilityPorts;
  sharedFiles?: ProjectSharedFilesPorts;
  conversations?(): CoordinationConversation[];
  workerConversations?(path: string, cursor?: string, signal?: AbortSignal): Promise<CoordinatorConversationPage>;
  subscribe?(refresh: () => void): () => void;
  binding(path: string, signal?: AbortSignal): Promise<CoordinationBinding>;
  reports?: { read(path: string, signal?: AbortSignal): Promise<CoordinatorReportState>;
    markRead(request: CoordinatorReportReadRequest): Promise<CoordinatorReportState> };
  budget?: CoordinatorBudgetPorts;
  workerModel?: RouterWorkerModelPorts;
  progress?: { read(path: string, signal?: AbortSignal): Promise<CoordinatorProgressState> };
  messages?(path: string, conversationId: string, beforeId: string): Promise<ProjectMessagePage>;
  board(path: string, signal?: AbortSignal): Promise<{ active: CoordinationWork[]; recentOutcomes: CoordinationWork[] } | null>;
  bind(path: string, conversationId: string, expectedRevision: number): Promise<CoordinationBinding>;
  create?(path: string, expectedRevision: number): Promise<CoordinationBinding>;
  navigate(conversationId: string): void;
  inspect?(conversationId: string): void;
  diagnose?(conversationId: string): void;
  openEvidence(): void;
}

let activeDrawer: ReturnType<typeof createDialog> | null = null;
export async function openProjectCoordination(ports: ProjectCoordinationPorts): Promise<void> {
  activeDrawer?.close();
  const root = document.createElement('div'); root.className = 'ui-stack coordination-workspace-body';
  const drawer = createDialog({ title: t('projectCoordination.title'), body: root, appearance: 'project',
    actions: [{ id: 'close', label: t('projectCoordination.close') }] });
  drawer.el.classList.add('coordination-workspace'); activeDrawer = drawer; document.body.append(drawer.el);
  let alive = true, generation = 0, activeTab = 'overview', loading = false, refreshPending = false, bindingPending = false;
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let unsubscribe: (() => void) | undefined;
  let binding: CoordinationBinding | undefined;
  let board: Awaited<ReturnType<ProjectCoordinationPorts['board']>> = null;
  let assignmentPage: ProjectDispatchPage | null = null;
  let workerPage: CoordinatorConversationPage | null = null;
  let rosterUnavailable = false, rosterLoaded = false, budgetLoaded = false, workLoaded = false;
  let rosterLoading = false, budgetLoading = false;
  let historyCursor: string | undefined, rosterCursor: string | undefined;
  let lastHistoryCursor: string | undefined;
  const graphState: CoordinationCanvasState = {}, teamState: CoordinationCanvasState = {};
  let updateView: ((budget: CoordinatorUsageState | null) => void) | undefined;
  let currentBudget: CoordinatorUsageState | null = null;
  let management: ReturnType<typeof createCoordinationManagement> | undefined;
  const feedback = document.createElement('div'); feedback.className = 'coordination-refresh-feedback'; feedback.setAttribute('role', 'status');
  let loadSecondary: () => Promise<void> = async () => {};
  const closed = drawer.open().then(() => {
    alive = false; generation++; unsubscribe?.(); clearTimeout(refreshTimer); management?.destroy(); drawer.el.remove();
    if (activeDrawer === drawer) activeDrawer = null;
  });
  root.append(text(t('projectCoordination.loading'), 'p'));
  const valid = () => alive && ports.currentPath().replace(/[/\\]+$/, '') === ports.path.replace(/[/\\]+$/, '')
    && (!ports.currentConversation || ports.currentConversation() === ports.currentConversationId);
  const navigate = (id: string) => { if (valid()) { drawer.close(); ports.navigate(id); } else drawer.close(); };
  const inspect = ports.inspect ? (id: string) => { if (valid()) { drawer.close(); ports.inspect!(id); } } : undefined;
  const diagnose = ports.diagnose ? (id: string) => { if (valid()) { drawer.close(); ports.diagnose!(id); } } : undefined;
  const fail = (reason: unknown) => {
    if (!valid()) return;
    if (!feedback.isConnected) root.replaceChildren(feedback);
    feedback.replaceChildren(createAlert({ variant: 'warning', title: t('projectCoordination.error'),
      body: reason instanceof Error ? reason.message : String(reason) }).el,
    createButton({ label: t('projectCoordination.retry'), onClick: () => { void load(); } }));
  };
  const conversations = (): CoordinationConversation[] => workerPage ? workerPage.conversations.map(item => ({
    id: item.conversationId, title: item.title, turnCount: item.turnCount,
  })) : ports.conversations?.() || ports.dispatch?.recipients?.() || [];
  function scheduleRefresh() {
    if (!alive || loading || bindingPending || !refreshPending || activeTab !== 'overview' || refreshTimer !== undefined) return;
    refreshTimer = setTimeout(() => { refreshTimer = undefined; void load(true); }, 300);
  }
  function mount() {
    root.replaceChildren();
    const heading = document.createElement('div'); heading.className = 'coordination-workspace-heading';
    const location = text(ports.path, 'small'); location.className = 'coordination-workspace-path';
    heading.append(location, createButton({ label: t('projectCoordination.refresh'), size: 'sm', variant: 'ghost', onClick: () => { void load(); } }));
    const entry = document.createElement('section'); entry.className = 'coordination-entry';
    const overview = document.createElement('div'); overview.className = 'ui-stack coordination-overview';
    const overviewContent = document.createElement('div'); overviewContent.className = 'ui-stack'; overviewContent.tabIndex = -1;
    const team = document.createElement('div'); team.className = 'ui-stack';
    const controls = document.createElement('div'); controls.className = 'ui-stack';
    const resources = document.createElement('div'); resources.className = 'ui-stack';
    const messages = document.createElement('div'); messages.className = 'ui-stack';
    const controlForms = document.createElement('div'); controlForms.className = 'ui-stack';
    const controlFeedback = document.createElement('div'); controlFeedback.setAttribute('role', 'status');
    let controlsLoaded = false, resourcesLoaded = false, messagesLoaded = false, controlsRevision: number | undefined;
    const tabs = createTabs({ activeId: activeTab, tabs: [
      { id: 'overview', label: t('projectCoordination.overview'), panel: overview },
      { id: 'team', label: t('projectCoordination.team'), panel: team },
      { id: 'controls', label: t('projectCoordination.controls'), panel: controls },
    ], onChange: id => {
      activeTab = id;
      void loadSecondary();
      if (refreshPending && !loading && id === 'overview') void load();
      scheduleRefresh();
    } });
    root.append(heading, feedback, entry, tabs.el);
    const scope = () => {
      const snapshot = binding!;
      return { path: ports.path, conversationId: ports.currentConversationId, binding: snapshot,
        valid: () => valid() && binding?.revision === snapshot.revision, readBinding: ports.binding };
    };
    const budgetView = ports.budget ? createCoordinatorBudget(ports.budget, scope()) : undefined;
    management = createCoordinationManagement(ports, { binding: () => binding!, valid, refresh: () => load(), inspect, diagnose });
    const authorization = document.createElement('details'); authorization.className = 'coordination-authorization';
    authorization.append(text(t('projectCoordination.authorization'), 'summary'), controlForms);
    const usage = document.createElement('details'); usage.className = 'coordination-usage';
    usage.append(text(t('coordinatorBudget.title'), 'summary'));
    if (budgetView) usage.append(budgetView.summary);
    const connections = document.createElement('details'); connections.className = 'coordination-connections';
    connections.append(text(t('projectCoordination.connections'), 'summary'), resources, messages);
    controls.append(management.el, controlFeedback, authorization, usage, connections);
    authorization.addEventListener('toggle', () => {
      if (!authorization.open || controlsLoaded) return;
      controlsLoaded = true; controlsRevision = binding!.revision;
      const current = scope();
      if (ports.dispatch) controlForms.append(createProjectDispatch(ports.dispatch, { ...current, navigate }));
      if (ports.dispatch && ports.scopeChanges) controlForms.append(createProjectScopeChanges(ports.scopeChanges, ports.dispatch, current));
      if (ports.dispatch && ports.responsibilities) controlForms.append(createProjectResponsibilities(ports.responsibilities, ports.dispatch, current));
    });
    resources.append(createButton({ label: t('projectCoordination.evidence'), onClick: () => {
      if (!valid()) { drawer.close(); return; } drawer.close(); ports.openEvidence();
    } }));
    connections.addEventListener('toggle', () => {
      if (!connections.open) return;
      if (!resourcesLoaded) {
        resourcesLoaded = true;
        if (ports.sharedFiles) resources.prepend(createProjectSharedFiles(ports.sharedFiles, scope(), ports.dispatch));
      }
      if (!messagesLoaded) {
        messagesLoaded = true; messages.append(text(t('projectCoordination.communicationHint'), 'p'));
        if (ports.messages && ports.currentConversationId) messages.append(createProjectMessageReceipts(
          beforeId => ports.messages!(ports.path, ports.currentConversationId, beforeId), valid));
      }
    });
    let entrySignature = '', overviewSignature = '', teamSignature = '';
    const readSecondary = async (kind: 'roster' | 'budget') => {
      if (kind === 'roster') {
        if (!ports.workerConversations || rosterLoading) return;
        rosterLoading = true;
        try {
          const page = await ports.workerConversations(ports.path, rosterCursor);
          if (!valid()) return;
          workerPage = page; rosterLoaded = true; rosterUnavailable = false;
        } catch { if (valid()) rosterUnavailable = true; }
        finally { rosterLoading = false; }
      } else {
        if (!ports.budget || budgetLoading || budgetLoaded) return;
        budgetLoading = true;
        try { currentBudget = await ports.budget.read(ports.path); } catch { currentBudget = null; }
        finally { budgetLoading = false; budgetLoaded = true; }
      }
      if (valid()) updateView?.(currentBudget);
    };
    loadSecondary = async () => {
      if (activeTab === 'team') await readSecondary('roster');
      if (activeTab === 'controls') await readSecondary('budget');
    };
    updateView = budget => {
      const restore = captureCoordinationView(root);
      if (entrySignature !== JSON.stringify(binding)) {
        entrySignature = JSON.stringify(binding);
        entry.replaceChildren(text(t('projectCoordination.coordinator'), 'h2'));
        if (binding!.availability === 'available') {
          entry.append(text(t('projectCoordination.coordinatorHint'), 'p'));
          const actions = document.createElement('div'); actions.className = 'coordination-entry-actions';
          actions.append(createButton({ label: t('projectCoordination.openCoordinator'), variant: 'primary', onClick: () => navigate(binding!.conversationId) }),
            createCoordinationReference({ conversationId: binding!.conversationId }, 'coordinator', inspect)); entry.append(actions);
        } else entry.append(text(t(binding!.availability === 'unavailable' ? 'projectCoordination.unavailable' : 'projectCoordination.unbound')));
        if (ports.create && binding!.availability === 'unbound') {
          const create = createButton({ label: t('projectCoordination.create'), onClick: async () => {
            if (!valid() || bindingPending) return;
            bindingPending = true; create.disabled = true;
            try { await ports.create!(ports.path, binding!.revision); }
            catch (reason) { entrySignature = ''; fail(reason); return; }
            finally { bindingPending = false; }
            await load();
          } }); entry.append(create);
        }
      }
      if (controlsRevision !== undefined && controlsRevision !== binding!.revision) {
        controlForms.inert = true;
        controlFeedback.replaceChildren(text(t('projectCoordination.replacement.draftsKept')));
      }
      const assignments = assignmentPage?.assignments ?? null;
      const groups = projectCoordinationWorkGroups(board?.active || [], board?.recentOutcomes || []);
      const options = { assignments, coordinatorId: binding!.conversationId, activeConversationId: ports.currentConversationId };
      const graph = projectCoordinationGraph(groups, options);
      const filter = graphState.workFilter || 'all';
      const nextOverview = JSON.stringify([workLoaded, board !== null, graph, historyCursor, filter, assignmentPage?.olderCursor, assignmentPage?.newerCursor]);
      if (!workLoaded) {
        overviewContent.replaceChildren(text(t('projectCoordination.loadingTasks')));
        if (!overview.contains(overviewContent)) overview.append(overviewContent);
        budgetView?.update(budget); restore(); return;
      }
      if (nextOverview !== overviewSignature) {
        overviewSignature = nextOverview; overviewContent.replaceChildren();
        const filters = document.createElement('div'); filters.className = 'coordination-work-filters'; filters.setAttribute('role', 'group');
        filters.setAttribute('aria-label', t('projectCoordination.filterLabel'));
        for (const [id, count] of [['all', graph.total], ['active', graph.counts.running + graph.counts.queued + graph.counts.waiting],
          ['attention', graph.counts.failed + graph.counts.blocked + graph.counts.unknown], ['completed', graph.counts.completed]] as const) {
          const button = createButton({ label: t(`projectCoordination.filter.${id}`, { n: count }), size: 'sm', variant: filter === id ? 'secondary' : 'ghost', onClick: () => {
            graphState.workFilter = id; graphState.workPage = 0;
            if (id === 'completed' && graph.counts.completed === 0 && assignmentPage?.olderCursor && !historyCursor) {
              historyCursor = assignmentPage.olderCursor; void load();
            } else updateView?.(budget);
          } });
          button.dataset.workFilter = id; button.setAttribute('aria-pressed', String(filter === id)); filters.append(button);
        }
        overviewContent.append(filters);
        if (filter !== 'all') overviewContent.append(text(t('projectCoordination.filterCoverage'), 'small'));
        if (!board) overviewContent.append(createAlert({ variant: 'warning', title: t('projectCoordination.boardUnavailable'),
          body: t('projectCoordination.boardUnavailableBody') }).el);
        overviewContent.append(createCoordinationBoard(groups, navigate, options,
          { viewState: graphState, tasksOnly: true, filter, inspect, heading: false }));
        if (filter === 'completed' && assignmentPage?.olderCursor) overviewContent.append(createButton({ label: t('projectCoordination.completedMore'), size: 'sm', onClick: () => {
          if (loading) return; historyCursor = assignmentPage!.olderCursor!; graphState.workPage = 0; void load();
        } }));
        if (ports.dispatch) overviewContent.append(createAssignmentHistoryNavigation(assignmentPage, historyCursor, cursor => {
          if (!valid() || loading) return;
          historyCursor = cursor; graphState.workPage = 0; graphState.showHistory = Boolean(cursor); void load();
        }));
        if (!board || assignments === null) overviewContent.append(createButton({ label: t('projectCoordination.retry'), size: 'sm', onClick: () => { void load(); } }));
      }
      if (!overview.contains(overviewContent)) overview.append(overviewContent);
      const roster = conversations();
      const nextTeam = JSON.stringify([graph, roster, rosterCursor, workerPage?.nextCursor, rosterUnavailable]);
      if (nextTeam !== teamSignature) {
        teamSignature = nextTeam;
        team.replaceChildren(text(t('projectCoordination.teamHint')),
          createCoordinationWorkList(graph, navigate, { state: teamState, conversations: roster, team: true, inspect, digest: coordinationTaskDigest }));
        if (workerPage?.nextCursor) team.append(createButton({ label: t('projectCoordination.roster.more'), size: 'sm', onClick: () => {
          if (rosterLoading) return; rosterCursor = workerPage!.nextCursor!; rosterLoaded = false; teamState.workPage = 0; void loadSecondary();
        } }));
        if (rosterCursor) team.append(createButton({ label: t('projectCoordination.roster.first'), size: 'sm', variant: 'ghost', onClick: () => {
          if (rosterLoading) return; rosterCursor = undefined; rosterLoaded = true; teamState.workPage = 0; void loadSecondary();
        } }));
        if (rosterUnavailable) team.append(text(t('projectCoordination.roster.unavailable'), 'p'), createButton({ label: t('projectCoordination.retry'), size: 'sm', onClick: () => { rosterLoaded = false; void loadSecondary(); } }));
      }
      budgetView?.update(budget); restore();
    };
  }
  async function load(background = false): Promise<boolean> {
    if (background && activeTab !== 'overview') return false;
    if (!valid()) { drawer.close(); return false; }
    if (loading || bindingPending) { refreshPending = true; return false; }
    loading = true; refreshPending = false; clearTimeout(refreshTimer); refreshTimer = undefined;
    const own = ++generation;
    drawer.el.setAttribute('aria-busy', 'true');
    try {
      const workReads = Promise.allSettled([
        ports.board(ports.path),
        ports.dispatch && (activeTab === 'overview' || lastHistoryCursor === undefined)
          ? ports.dispatch.list(ports.path, undefined, (background ? lastHistoryCursor : historyCursor)
            ? { cursor: (background ? lastHistoryCursor : historyCursor)! } : undefined)
          : Promise.resolve(assignmentPage || { assignments: [], olderCursor: null, newerCursor: null }),
      ]);
      binding = await ports.binding(ports.path).catch(async error => {
        // Preserve the read-generation barrier: a later refresh cannot overtake
        // task evidence already in flight when binding fails early.
        await workReads;
        throw error;
      });
      if (own !== generation) return false;
      if (!valid()) { drawer.close(); return false; }
      if (background && activeTab !== 'overview') { refreshPending = true; return false; }
      if (!updateView) mount();
      updateView!(currentBudget);
      void loadSecondary();
      const [boardRead, assignmentsRead] = await workReads;
      if (own !== generation) return false;
      if (!valid()) { drawer.close(); return false; }
      if (background && activeTab !== 'overview') { refreshPending = true; return false; }
      board = boardRead.status === 'fulfilled' ? boardRead.value : null;
      assignmentPage = assignmentsRead.status === 'fulfilled' ? assignmentsRead.value : null;
      lastHistoryCursor = historyCursor;
      workLoaded = true;
      updateView!(currentBudget);
      feedback.replaceChildren();
      return true;
    } catch (reason) { if (own === generation && valid()) fail(reason); return false; }
    finally { loading = false; drawer.el.removeAttribute('aria-busy'); scheduleRefresh(); }
  }
  unsubscribe = ports.subscribe?.(() => { refreshPending = true; scheduleRefresh(); });
  await load(); await closed;
}
