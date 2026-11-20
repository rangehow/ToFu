import '../styles/application/27-world-office.css';
/** Lazy office-world composition: only this adapter knows the application shell. The world reads live
 * data itself (read-only) and reaches the shell through explicit ports: the application's push owner for
 * wake hints, the brief's dock, leaving for a person's whole conversation in the classic view (at a Turn or
 * an open question), artifact download addresses, and the persisted switch to the classic Session UI. */
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { featureRegistry, readLiveRuntimeBinding } from '../feature-registry';
import { goToWorkspace, hasMountedWorkspace, mountWorkspacePage, showWorkspace } from '../application-shell/page-host';
import { rememberLanding } from '../application-shell/landing';
import { mountWorld, type WorldShell } from './world/app';
import type { PushFrame, PushPort } from './world/live';
import type { ChoicePorts } from '../rich-response/choices';
import { cancelPendingReveal, openConversationTurn } from './lead-deliverables/open';
import { apiTransport } from '../api/transport';
import { createEndpointPorts } from '../api/endpoint-registry/ports';
import { createProjectEndpoints } from '../api/endpoint-registry/project';
import { createArtifactsEndpoints } from '../api/endpoint-registry/artifacts';

const artifactUrls = createArtifactsEndpoints(createEndpointPorts(apiTransport, () => document.documentElement.lang || 'zh'));

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
  // The office shows progress and files; the whole conversation is the classic view's (owner request
  // 2026-11-18). Leaving for it keeps the office as the landing view; the conversation opens once the
  // office is gone, so tearing the office down cannot cancel the pending reveal of its Turn.
  let handoff: (() => void) | null = null;
  const leaveFor = (open: () => void) => {
    handoff = open;
    goToWorkspace('chat');
  };
  const shell: WorldShell = {
    openConversation(conversationId, turn) {
      leaveFor(() => openConversationTurn(conversationId, turn));
    },
    // The same handler as a '?' lead in the sidebar's lead office (runtime ui/turn_nav.js).
    openQuestion(conversationId, question) {
      leaveFor(() => {
        cancelPendingReveal();
        const openAt = readLiveRuntimeBinding('openConversationAtQuestion') as OpenAtQuestion | undefined;
        if (typeof openAt === 'function') void openAt(conversationId, question);
        else legacy.loadConversation?.(conversationId);
      });
    },
    answerPorts(conversationId, turnId) {
      const ports = readLiveRuntimeBinding('guidanceAnswerPorts') as AnswerPorts | undefined;
      return typeof ports === 'function' ? ports(conversationId, turnId) : undefined;
    },
    downloadUrl: artifactId => artifactUrls.downloadUrl(artifactId),
    async confirmDefault(coordinatorId, guidanceId) {
      const project = createProjectEndpoints(createEndpointPorts(apiTransport, () => document.documentElement.lang || 'zh'));
      await project.confirmOwnerQuestionDefault({ coordinatorId, guidanceId });
    },
    // The dock is the body's data-conversation-dock (01-workspace-dock.css places the column from it).
    dock: mode => {
      if (!leaving) document.body.dataset.conversationDock = mode;
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
    const open = handoff;
    handoff = null;
    open?.();
  }, page.route);
}
(featureRegistry as unknown as Record<string, unknown>).openWorld = openWorld;
export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('world', name, args, stub);
}
