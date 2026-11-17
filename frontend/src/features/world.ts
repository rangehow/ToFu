import './world/world-office.css';
/** Lazy office-world composition: only this adapter knows the application shell. The world reads live
 * data itself (read-only) and reaches the shell through explicit ports: the application's push owner for
 * wake hints, the conversation dock, the classic conversation loader for chatting with a person (the same
 * ConversationSurface and composer as the classic chat, with the same authority), and the persisted switch
 * to the classic Session UI. */
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { featureRegistry, readLiveRuntimeBinding } from '../feature-registry';
import { goToWorkspace, hasMountedWorkspace, mountWorkspacePage, showWorkspace } from '../application-shell/page-host';
import { rememberLanding } from '../application-shell/landing';
import { mountWorld, type WorldShell } from './world/app';
import type { PushFrame, PushPort } from './world/live';
import type { ChoicePorts } from '../rich-response/choices';
import { cancelPendingReveal, openConversationTurn } from './lead-deliverables/open';

type PushHandler = (frame: PushFrame) => void;

function pushPort(): PushPort | null {
  const subscribe = readLiveRuntimeBinding('pushSubscribe') as ((channel: string, taskId: string, handler: PushHandler) => void) | undefined;
  const unsubscribe = readLiveRuntimeBinding('pushUnsubscribe') as ((channel: string, taskId: string, handler: PushHandler) => void) | undefined;
  const connected = readLiveRuntimeBinding('pushIsConnected') as (() => boolean) | undefined;
  const reconnect = readLiveRuntimeBinding('pushOnReconnect') as ((listener: () => void) => () => void) | undefined;
  if (typeof subscribe !== 'function' || typeof unsubscribe !== 'function') return null;
  return {
    subscribe(channel, handler) {
      subscribe(channel, '*', handler);
      return () => unsubscribe(channel, '*', handler);
    },
    connected: () => typeof connected === 'function' && connected(),
    onReconnect: listener => (typeof reconnect === 'function' ? reconnect(listener) : () => {}),
  };
}

type OpenAtQuestion = (conversationId: string, question: { turnId: string; ordinal?: number; guidanceId: string } | null) => Promise<boolean>;
type AnswerPorts = (conversationId: string, turnId: string) => ChoicePorts;

export function openWorld(): void {
  // Arriving in the office (the classic top bar's link, an address or the landing) makes it the landing view.
  rememberLanding('world');
  if (hasMountedWorkspace('world')) { showWorkspace('world'); return; }
  const legacy = featureRegistry as unknown as Record<string, any>;
  if (legacy.paperMode) legacy.exitPaperMode?.();
  if (legacy.imageGenMode) legacy.exitImageGenMode?.();
  showWorkspace('world');
  const root = document.getElementById('worldPageContainer');
  if (!root) throw new Error('World page container is missing');
  let leaving = false;
  const shell: WorldShell = {
    openChat: openConversationTurn,
    // The same handler as a '?' lead in the sidebar's lead office (runtime ui/turn_nav.js).
    openQuestion(conversationId, question) {
      cancelPendingReveal();
      const openAt = readLiveRuntimeBinding('openConversationAtQuestion') as OpenAtQuestion | undefined;
      if (typeof openAt === 'function') void openAt(conversationId, question);
      else legacy.loadConversation?.(conversationId);
    },
    answerPorts(conversationId, turnId) {
      const ports = readLiveRuntimeBinding('guidanceAnswerPorts') as AnswerPorts | undefined;
      return typeof ports === 'function' ? ports(conversationId, turnId) : undefined;
    },
    // The dock is the body's data-conversation-dock (01-workspace-dock.css places the column from it).
    // Re-announcing the workspace lets the shell resume or suspend the column's transcript transport;
    // while the world is being left, the next workspace announces itself.
    dock: mode => {
      if (document.body.dataset.conversationDock === mode) return;
      document.body.dataset.conversationDock = mode;
      if (!leaving) showWorkspace('world', false);
    },
    classic: () => { rememberLanding('chat'); goToWorkspace('chat'); },
    push: pushPort(),
  };
  const page = mountWorld(root, shell);
  // The dock belongs to the office: leaving it leaves no dock behind in the next workspace.
  mountWorkspacePage(() => {
    leaving = true;
    cancelPendingReveal();
    page.dispose();
    delete document.body.dataset.conversationDock;
  }, page.route);
}
(featureRegistry as unknown as Record<string, unknown>).openWorld = openWorld;
export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('world', name, args, stub);
}
