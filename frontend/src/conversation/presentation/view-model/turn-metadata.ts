/**
 * Pure Turn metadata and action-capability projection. Reads canonical route, cost,
 * finish and source facts.
 * Entry: metadataFrom, actionsFor.
 * Dependencies: conversation-sync.generated, turn-finish, types. */
import {
  type TurnProjection,
  type TurnRecord,
  type TurnStatus,
  type TurnTranslation,
} from '../../../api/conversation-sync.generated';
import {
  type TurnFinishPresentation,
} from '../turn-finish';
import {
  type ConversationBlockViewModel,
  type TurnMetadataViewModel,
  type TranslationDisplayMode,
  type TranslationActivity,
  type ConversationTurnActionViewModel,
} from './types';

/** Display text of a failure value: a durable `{reason, error, at}` verdict,
 *  a live task's error string or an error envelope's message. */
function failureText(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const record = value as Readonly<Record<string, unknown>>;
  for (const key of ['error', 'message', 'reason']) {
    const text = record[key];
    if (typeof text === 'string' && text.trim()) return text.trim();
  }
  return '';
}

export function metadataFrom(
  projection: TurnProjection,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
  preferredTranslationMode: TranslationDisplayMode,
  activity?: TranslationActivity,
  costSignature?: string,
): TurnMetadataViewModel {
  const hasFallback = Boolean(
    projection.fallbackModel || projection.fallbackFrom
    || projection.fallbackReason || projection.fallbackKind,
  );
  const fallbackInTimeline = (projection.activityTimeline?.entries ?? [])
    .some((entry) => entry.status === 'switched') || Boolean(
      projection.routeSnapshot?.transitions.some(
        (transition) => transition.kind !== 'initial',
      ),
    );
  const translationAvailable = blocks.some((block) => (
    (block.kind === 'text' || block.kind === 'thinking')
      && Boolean(block.translatedMarkdown)
  ) || (
    block.kind === 'proposed-plan' && Boolean(block.translatedMarkdown)
  ));
  const translation = projection.translation as TurnTranslation | undefined;
  const completed = translation?.status === 'completed'
    || translation?.status === 'skipped';
  const pending = activity?.status === 'pending'
    || (translation?.status === 'pending' && !translationAvailable);
  /* A live failed task reports the latest attempt; otherwise the durable
   * verdict (`_translateFailed`, projected as `sendFailure`) does. */
  const activityFailed = activity?.status === 'failed';
  const failed = !translationAvailable && !completed && !pending && (
    activityFailed || Boolean(translation?.sendFailure)
      || translation?.status === 'failed');
  const failureDetail = !failed ? '' : activityFailed
    ? failureText(activity?.error) || failureText(activity?.message)
    : failureText(translation?.sendFailure) || failureText(translation?.error);
  return {
    ...(projection.model ? { model: projection.model } : {}),
    ...(projection.preset ? { preset: projection.preset } : {}),
    ...((projection.providerId || projection.provider_id)
      ? { providerId: projection.providerId || projection.provider_id } : {}),
    ...(projection.thinkingDepth != null
      ? { thinkingDepth: projection.thinkingDepth } : {}),
    ...(projection.usage ? { usage: projection.usage } : {}),
    ...(costSignature ? { costSignature } : {}),
    ...(projection.lastRoundUsage
      ? { lastRoundUsage: projection.lastRoundUsage } : {}),
    ...(projection.routeSnapshot
      ? { routeSnapshot: projection.routeSnapshot } : {}),
    ...(projection.modifiedFiles != null
      ? { modifiedFiles: projection.modifiedFiles } : {}),
    ...(projection.modifiedFileList
      ? { modifiedFileList: projection.modifiedFileList } : {}),
    ...(projection.todoState ? { todoState: projection.todoState } : {}),
    ...(projection.waitingOn ? { waitingOn: projection.waitingOn } : {}),
    ...(projection.orchestration
      ? { orchestration: projection.orchestration } : {}),
    ...(projection.origin ? { origin: projection.origin } : {}),
    ...(hasFallback ? { fallback: {
      ...(projection.fallbackModel ? { model: projection.fallbackModel } : {}),
      ...(projection.fallbackFrom ? { from: projection.fallbackFrom } : {}),
      ...(projection.fallbackReason ? { reason: projection.fallbackReason } : {}),
      ...(projection.fallbackKind ? { kind: projection.fallbackKind } : {}),
    } } : {}),
    fallbackInTimeline,
    translation: {
      completed,
      available: translationAvailable,
      displayMode: translationAvailable ? preferredTranslationMode : 'original',
      pending,
      failed,
      ...(failureDetail ? { failureDetail } : {}),
      ...(translation?.skippedReason
        ? { skippedReason: translation.skippedReason } : {}),
      ...(translation?.model ? { model: translation.model } : {}),
      ...((activity?.error ?? translation?.error) == null
        ? {} : { error: activity?.error ?? translation?.error }),
    },
  };
}

export function actionsFor(
  actor: TurnRecord['actor'],
  laneId: string,
  commandPending: string | null,
  finish: TurnFinishPresentation | null,
  translation: TurnMetadataViewModel['translation'],
  status: TurnStatus,
  taskId: string | undefined,
  requestInspectorEnabled: boolean,
  backgroundCommandInjected: boolean,
): ConversationTurnActionViewModel[] {
  const disabled = Boolean(commandPending);
  if (laneId !== 'main') {
    return [{ action: 'copy', disabled: false }];
  }
  const review: ConversationTurnActionViewModel[] = actor === 'assistant' && !backgroundCommandInjected
    ? [{ action: 'review', disabled: false }] : [];
  if (status === 'pending' || status === 'running') return review;
  const actions: ConversationTurnActionViewModel[] = [
    { action: 'copy', disabled: false },
    ...review,
    ...(requestInspectorEnabled && taskId
      ? [{ action: 'inspect' as const, operation: taskId, disabled: false }]
      : []),
    /* A background-command completion turn is machine-authored evidence:
     * editing would rewrite the injected payload and regenerating would
     * re-run a notice, so only copy/inspect/delete stay meaningful. */
    ...(backgroundCommandInjected ? [] : [{ action: 'edit' as const, disabled }]),
  ];
  if (actor === 'human') {
    if (!backgroundCommandInjected) {
      actions.push({ action: 'regenerate', disabled });
    }
  } else {
    const resume = finish?.resumeOptions?.[0];
    if (resume?.operation) {
      actions.push({ action: 'resume', operation: resume.operation, disabled });
    }
    actions.push(
      {
        action: 'translate',
        ...(translation.available ? {
          operation: translation.displayMode === 'translated'
            ? 'show-original' : 'show-translated',
        } : {}),
        disabled,
      },
      { action: 'export', disabled: false },
      { action: 'promote-decision', disabled },
      { action: 'branch', disabled },
    );
  }
  actions.push({ action: 'delete', disabled });
  return actions;
}
