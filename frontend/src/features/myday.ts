/** Lazy My Day composition and feature entry. */
import '../runtime/myday-presenters.generated.js';
import '../styles/application/16-my-day-goals.css';
import '../styles/application/16-my-day-token-costs.css';
import { createDayTokenCostDisclosure } from './myday/token-costs';
import { featureRegistry } from '../feature-registry';
import { t } from '../i18n';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { getRuntimeService } from '../runtime/app-runtime.js';
import {
  createMyDayGoalBoard,
  type MyDayBoardElement,
  type MyDayGoalApi,
  type MyDayGoalBoard,
} from './myday/goal-board';

interface MyDayPanelPresentation {
  renderCalendar(): void;
  showToday(): void;
}

type MyDayBindings = Window & {
  Api?: { daily?: MyDayGoalApi };
  MyDayPanelPresentation?: MyDayPanelPresentation;
  MyDayGoalBoard?: MyDayGoalBoard;
  MyDayTokenCosts?: ReturnType<typeof createDayTokenCostDisclosure>;
};

function bindings(): MyDayBindings {
  return featureRegistry as unknown as MyDayBindings;
}

function dailyApi(): MyDayGoalApi {
  const api = bindings().Api?.daily;
  if (!api) throw new Error('My Day daily API is not ready');
  return api;
}

function presentation(): MyDayPanelPresentation {
  const owner = bindings().MyDayPanelPresentation;
  if (!owner) throw new Error('My Day panel presentation is not ready');
  return owner;
}

/* The empty state shows the one shipped mascot; its URL and cache-bust token
   live in core/brand_logo.js, so this entry never spells out a logo path. */
function brandLogoAttrs(size: number): string {
  const helper = getRuntimeService('brandLogoImgAttrs');
  if (typeof helper !== 'function') throw new Error('brand mascot helper is not ready');
  return (helper as (size: number) => string)(size);
}

async function prepareMyDayRuntime(): Promise<void> {
  const background = await import('./background');
  await background.prepareMyDayBackground();
}

const api: MyDayGoalApi = {
  goals: () => dailyApi().goals(),
  goalCreate: (text) => dailyApi().goalCreate(text),
  goalUpdate: (goalId, body) => dailyApi().goalUpdate(goalId, body),
  goalItemAdd: (goalId, text) => dailyApi().goalItemAdd(goalId, text),
  goalItemUpdate: (goalId, itemId, body) => dailyApi().goalItemUpdate(goalId, itemId, body),
  goalItemRemove: (goalId, itemId) => dailyApi().goalItemRemove(goalId, itemId),
  goalSuggestions: (goalId, decision) => dailyApi().goalSuggestions(goalId, decision),
  goalDecompose: (goalId) => dailyApi().goalDecompose(goalId),
  goalLink: (goalId, body) => dailyApi().goalLink(goalId, body),
  goalUnlink: (goalId) => dailyApi().goalUnlink(goalId),
  goalRequests: () => dailyApi().goalRequests(),
  report: (date) => dailyApi().report(date),
};

bindings().MyDayGoalBoard = createMyDayGoalBoard({
  api,
  translate: t as unknown as (key: string, values?: Record<string, unknown>) => string,
  element: (id: MyDayBoardElement) => document.getElementById(id),
  renderCalendar: () => presentation().renderCalendar(),
  showToday: () => presentation().showToday(),
  brandLogoAttrs,
  warn: (message, detail) => console.warn(message, detail),
});

bindings().MyDayTokenCosts = createDayTokenCostDisclosure(
  () => document.getElementById('mydayTokenCosts'),
);

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  await prepareMyDayRuntime();
  return invokeFeatureEntry('myday', name, args, stub);
}

export async function prepare(): Promise<void> {
  await prepareMyDayRuntime();
}
