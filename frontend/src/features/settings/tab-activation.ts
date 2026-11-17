/** Independent Settings panels load from explicit visits, not config hydration.
 * Entry: activateIndependentSettingsTab. The opening owns first-visit state and
 * cleanup; each feature retains its own drafts, commands and refresh controls.
 */
import {featureRegistry} from '../../feature-registry';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';

interface IndependentPanel {
  populate: string;
  destroy?: string;
  refreshOnVisit?: boolean;
  /** Demand-loads the owner that registers `populate`, keeping it out of the Settings chunk. */
  load?: () => Promise<unknown>;
}

const panels: Readonly<Record<string, IndependentPanel>> = {
  mcp: {populate: '_populateMcpTab'},
  skills: {populate: '_populateSkillsTab', destroy: '_destroySkills'},
  tools: {populate: 'populateToolsInventory', destroy: '_destroyToolsInventory'},
  knowledge: {populate: '_populateKnowledgeTab', destroy: '_destroyKnowledgeTab', refreshOnVisit: true},
  preferences: {populate: '_populatePreferencesTab', destroy: '_destroyPreferences'},
  database: {populate: '_populateDatabaseTab', destroy: '_destroyDatabaseTab', refreshOnVisit: true,
    load: () => import('./database')},
  dx: {populate: '_populateDxTab', destroy: '_destroyDxTab', refreshOnVisit: true,
    load: () => import('./dx-channel')},
  github: {populate: '_populateGitHubTab', destroy: '_destroyGitHubTab', refreshOnVisit: true,
    load: () => import('./github-channel')},
  wecom: {populate: '_populateWecomTab', destroy: '_destroyWecomTab', refreshOnVisit: true,
    load: () => import('./bot-channel')},
  qq: {populate: '_populateQqTab', destroy: '_destroyQqTab', refreshOnVisit: true,
    load: () => import('./bot-channel')},
  wechat: {populate: '_populateWechatTab', destroy: '_destroyWechatTab', refreshOnVisit: true,
    load: () => import('./bot-channel')},
  messaging: {populate: '_populateMessagingTab', refreshOnVisit: true},
};
const visits = new WeakMap<SettingsReadSession, Set<string>>();

export function activateIndependentSettingsTab(tab: string): void {
  const session = currentSettingsReadSession(), panel = panels[tab];
  if (!session?.isCurrent() || !panel) return;
  const populate = featureRegistry[panel.populate];
  if (typeof populate !== 'function') {
    if (panel.load) void panel.load().then(() => {
      if (session.isCurrent() && typeof featureRegistry[panel.populate] === 'function') {
        activateIndependentSettingsTab(tab);
      }
    });
    return;
  }
  let visited = visits.get(session);
  if (!visited) { visited = new Set(); visits.set(session, visited); }
  const first = !visited.has(tab);
  if (first) {
    visited.add(tab);
    if (panel.destroy) session.scope.add(() => {
      const destroy = featureRegistry[panel.destroy!];
      if (typeof destroy === 'function') destroy();
    });
  }
  if (first || panel.refreshOnVisit) populate();
}
