import type { RouterOwnerDelegationState } from '../../api/router.generated';

type Action = RouterOwnerDelegationState['actions'][number];

export const delegationReceiptProjects = ['synthetic-paper', 'synthetic-slides'].map((project, index) => {
  const owner = 101 + index;
  const router = { conversationId: `router-${project}`, revision: 1, availability: 'available' as const };
  const grantee = { kind: 'global_router' as const, conversationId: router.conversationId, bindingRevision: 1, tenant: null };
  const grantedBy = { kind: 'human-ui' as const, ownerUserId: owner, credential: 'session' };
  const grant: RouterOwnerDelegationState['grant'] = {
    version: 1, owner, state: 'granted', revision: 7, coverage: 'full',
    scopes: ['restart', 'conversation_control', 'owner_settings', 'project_lifecycle', 'project_repair', 'scheduler', 'lead_authority'], grantee, grantedAt: 1700000000, grantedBy,
    assurance: 'authenticated', revokedAt: null, revokedBy: null, updatedAt: 1700000000,
  };
  const action: Action = {
    id: `od-${project}`, owner, scope: 'restart',
    requester: { kind: 'project_lead', conversationId: `lead-${project}`, bindingRevision: 2, tenant: null,
      turnId: `turn-${project}`, attemptId: `attempt-${project}` },
    reason: `Load the release for ${project}`, trigger: { kind: 'owner_message', turnId: `owner-${project}`, text: `Restart for ${project}` },
    target: { kind: 'server' },
    grantRevision: 7, grantedBy, requestedAt: 1700000001, updatedAt: 1700000001,
    status: 'accepted', code: null, detail: null,
  };
  const standing: RouterOwnerDelegationState = { grant, active: true, router, actions: [action] };
  const revoked: RouterOwnerDelegationState = {
    grant: { ...grant, state: 'revoked', revision: 8, revokedAt: 1700000002, revokedBy: grantedBy, updatedAt: 1700000002 },
    active: false, router,
    actions: [{ ...action, status: 'refused', updatedAt: 1700000003, code: 'owner_delegation_not_granted',
      detail: 'The owner has not granted the main coordinator their delegation. Ask the owner to turn on '
        + '"Act for me" in the main coordinator\'s header in Tofu, or to do this themselves (a restart: '
        + 'Settings → Update). No approval request was created.' }],
  };
  return { project, standing, revoked };
});
