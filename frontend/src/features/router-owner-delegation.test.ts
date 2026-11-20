// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RouterOwnerDelegationPorts } from './router-owner-delegation';
import type { RouterOwnerDelegationState } from '../api/router.generated';
import { delegationReceiptProjects } from './fixtures/owner-delegation-receipts';

type Scope = RouterOwnerDelegationState['grant']['scopes'][number];
const revokedGrant = {
  version: 1 as const, owner: 1, state: 'revoked' as const, revision: 0,
  coverage: 'full' as RouterOwnerDelegationState['grant']['coverage'],
  scopes: ['restart', 'conversation_control', 'scheduler', 'lead_authority'] as Scope[],
  grantee: null, grantedAt: null, grantedBy: null, assurance: null, revokedAt: null, revokedBy: null, updatedAt: null,
};
const router = { conversationId: 'coordinator-router', revision: 1, availability: 'available' as const };
const off: RouterOwnerDelegationState = { grant: revokedGrant, active: false, router, actions: [] };
const grantee = { kind: 'global_router' as const, conversationId: 'coordinator-router', bindingRevision: 1, tenant: null };
const on: RouterOwnerDelegationState = {
  grant: { ...revokedGrant, state: 'granted', revision: 1, grantee, grantedAt: 1, assurance: 'authenticated',
    grantedBy: { kind: 'human-ui', credential: 'session' } },
  active: true, router, actions: [],
};
const used: RouterOwnerDelegationState = {
  ...on,
  actions: [{
    id: 'od-1', owner: 1, scope: 'restart', requester: { ...grantee, turnId: 't', attemptId: 'a' },
    reason: 'Load the new release', trigger: { kind: 'owner_message', turnId: 'o', text: '重启一下' },
    target: { kind: 'server' }, grantRevision: 1, grantedBy: null, requestedAt: 1_700_000_000,
    status: 'restarting', code: null, detail: null, updatedAt: 1_700_000_000,
  }],
};
const catalog = {
  'common.loading': '加载中…', 'rich.unknown': '未确认', 'context.loadFailed': '加载失败',
  'routerDelegation.summaryOff': '代我操作：未开启', 'routerDelegation.summaryOn': '代我操作：已开启',
  'routerDelegation.hint': '允许总负责人替你重启 Tofu', 'routerDelegation.switch': '代我操作',
  'routerDelegation.advisory': '仅供参考', 'routerDelegation.noUse': '尚未使用。',
  'routerDelegation.lastUse': '最近一次：{operation}，{status}（{time}）— {reason}',
  'routerDelegation.failed': '未更改：{error}', 'routerDelegation.op.restart': '重启',
  'routerDelegation.status.accepted': '已接受', 'routerDelegation.status.refused': '已拒绝',
  'routerDelegation.op.conversation_control': '会话控制', 'routerDelegation.status.restarting': '正在重启',
  'routerDelegation.op.scheduler': '定时任务', 'routerDelegation.op.lead_authority': '负责人调度 Worker',
  'routerDelegation.coversFull': '涵盖全部：{scopes}。以后新增的也包含。',
  'routerDelegation.coversOnly': '仅涵盖：{scopes}。',
};

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => catalog })));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function setup(read: RouterOwnerDelegationState | RouterOwnerDelegationPorts['read'], set: (granted: boolean) => Promise<RouterOwnerDelegationState>) {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  const i18n = await import('../i18n');
  await i18n.ready?.();
  const { createRouterOwnerDelegation } = await import('./router-owner-delegation');
  const ports: RouterOwnerDelegationPorts = {
    read: vi.fn(typeof read === 'function' ? read : async () => read), set: vi.fn(set),
  };
  const entry = createRouterOwnerDelegation(ports);
  document.body.replaceChildren(entry.settings);
  await vi.waitFor(() => expect(ports.read).toHaveBeenCalled());
  await Promise.resolve();
  const input = entry.settings.querySelector('input') as HTMLInputElement;
  const summary = entry.settings.querySelector('summary') as HTMLElement;
  const lastUse = entry.settings.querySelector('.router-owner-delegation-last') as HTMLElement;
  const error = entry.settings.querySelector('.router-owner-delegation-error') as HTMLElement;
  return { entry, ports, input, summary, lastUse, error };
}

describe('Act for me (the owner delegation switch)', () => {
  it('never presents a pending or rejected initial read as confirmed off or empty history', async () => {
    let rejectRead!: (error: Error) => void;
    const view = await setup(() => new Promise((_resolve, reject) => { rejectRead = reject; }), async () => on);
    expect(view.summary.textContent).toContain('加载中…');
    expect(view.summary.textContent).toContain('代我操作');
    expect(view.lastUse.hidden).toBe(true);
    expect(view.lastUse.textContent).toBe('');
    expect(view.input.disabled).toBe(true);
    rejectRead(new Error('passive read unavailable'));
    await vi.waitFor(() => expect(view.error.hidden).toBe(false));
    expect(view.summary.textContent).toContain('未确认');
    expect(view.summary.dataset.i18n).not.toBe('routerDelegation.summaryOff');
    expect(view.error.textContent).toContain('加载失败');
    expect(view.error.textContent).toContain('passive read unavailable');
    expect(view.lastUse.hidden).toBe(true);
    view.input.checked = true;
    view.input.dispatchEvent(new Event('change'));
    expect(view.ports.set).not.toHaveBeenCalled();
    view.entry.destroy();
  });

  it.each(delegationReceiptProjects)('withdraws stale $project evidence after a failed refresh and recovers with confirmed off/empty', async ({ standing }) => {
    const view = await setup(standing, async () => on);
    expect(view.input.checked).toBe(true);
    expect(view.lastUse.textContent).toContain(standing.actions[0].reason);
    vi.mocked(view.ports.read).mockRejectedValue(new Error('refresh unavailable'));
    view.entry.settings.open = true;
    view.entry.settings.dispatchEvent(new Event('toggle'));
    await vi.waitFor(() => expect(view.error.hidden).toBe(false));
    expect(view.summary.textContent).toContain('未确认');
    expect(view.lastUse.hidden).toBe(true);
    expect(view.lastUse.textContent).toBe('');
    expect(view.entry.settings.textContent).not.toContain(standing.actions[0].reason);
    expect(view.entry.settings.querySelector<HTMLElement>('.router-owner-delegation-scopes')!.hidden).toBe(true);
    expect(view.input.disabled).toBe(true);
    window.dispatchEvent(new Event('tofu:language-change'));
    expect(view.summary.textContent).toContain('未确认');
    vi.mocked(view.ports.read).mockResolvedValue(off);
    view.entry.settings.dispatchEvent(new Event('toggle'));
    await vi.waitFor(() => expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOff'));
    expect(view.error.hidden).toBe(true);
    expect(view.input.disabled).toBe(false);
    expect(view.lastUse.hidden).toBe(false);
    expect(view.lastUse.dataset.i18n).toBe('routerDelegation.noUse');
    expect(view.ports.set).not.toHaveBeenCalled();
    view.entry.destroy();
  });

  it.each(delegationReceiptProjects)('renders $project standing and revoked receipts without inferring pending approval', async ({ standing, revoked }) => {
    const view = await setup(standing, async () => { throw new Error('No authority writes in this fixture'); });
    const detail = view.entry.settings.querySelector('.router-owner-delegation-detail') as HTMLElement;
    expect(view.input.checked).toBe(true);
    expect(view.lastUse.textContent).toContain('已接受');
    expect(detail.hidden).toBe(true);
    vi.mocked(view.ports.read).mockResolvedValue(revoked);
    view.entry.settings.open = true;
    view.entry.settings.dispatchEvent(new Event('toggle'));
    await vi.waitFor(() => expect(view.input.checked).toBe(false));
    expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOff');
    expect(view.lastUse.textContent).toContain('已拒绝');
    expect(detail.hidden).toBe(false);
    expect(detail.textContent).toBe(revoked.actions[0].detail);
    expect(detail.textContent).toContain('No approval request was created.');
    expect(view.ports.set).not.toHaveBeenCalled();
    view.entry.destroy();
  });

  it('shows code-only evidence as text and clears old refusal detail on a fresh empty read', async () => {
    const state = structuredClone(delegationReceiptProjects[0].revoked);
    state.actions[0].detail = null;
    state.actions[0].code = '<refused>';
    const view = await setup(state, async () => off);
    const detail = view.entry.settings.querySelector('.router-owner-delegation-detail') as HTMLElement;
    expect(detail.textContent).toBe('<refused>');
    expect(detail.children).toHaveLength(0);
    vi.mocked(view.ports.read).mockResolvedValue(off);
    view.entry.settings.open = true;
    view.entry.settings.dispatchEvent(new Event('toggle'));
    await vi.waitFor(() => expect(detail.hidden).toBe(true));
    expect(detail.textContent).toBe('');
    expect(view.ports.set).not.toHaveBeenCalled();
    view.entry.destroy();
  });
  it('shows the server state and grants only through the owner request', async () => {
    const view = await setup(off, async () => on);
    expect(view.input.checked).toBe(false);
    expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOff');
    expect(view.lastUse.dataset.i18n).toBe('routerDelegation.noUse');
    view.input.checked = true;
    view.input.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOn'));
    expect(view.ports.set).toHaveBeenCalledWith(true);
    expect(view.input.checked).toBe(true);
    view.entry.destroy();
  });

  it('keeps the server state and says why when the server refuses', async () => {
    const view = await setup(off, async () => { throw new Error('human_approval_required'); });
    view.input.checked = true;
    view.input.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(view.error.hidden).toBe(false));
    expect(view.error.textContent).toContain('human_approval_required');
    expect(view.input.checked).toBe(false);
    expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOff');
    view.entry.destroy();
  });

  it('revokes at once and shows the last audited use', async () => {
    const view = await setup(used, async () => off);
    await vi.waitFor(() => expect(view.input.checked).toBe(true));
    expect(view.lastUse.dataset.i18n).toBeUndefined();
    expect(view.lastUse.textContent).toContain('Load the new release');
    view.input.checked = false;
    view.input.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(view.input.checked).toBe(false));
    expect(view.ports.set).toHaveBeenCalledWith(false);
    expect(view.lastUse.dataset.i18n).toBe('routerDelegation.noUse');
    view.entry.destroy();
  });

  it('shows which scopes the grant covers: every scope when full, only its own when enumerated', async () => {
    const view = await setup(on, async () => off);
    const scopes = view.entry.settings.querySelector('.router-owner-delegation-scopes') as HTMLElement;
    await vi.waitFor(() => expect(scopes.textContent).toBe('涵盖全部：重启, 会话控制, 定时任务, 负责人调度 Worker。以后新增的也包含。'));
    expect(scopes.hidden).toBe(false);
    view.entry.destroy();

    const narrow = { ...on, grant: { ...on.grant, coverage: 'enumerated' as const, scopes: ['restart'] as Scope[] } };
    const narrowView = await setup(narrow, async () => off);
    const narrowScopes = narrowView.entry.settings.querySelector('.router-owner-delegation-scopes') as HTMLElement;
    await vi.waitFor(() => expect(narrowScopes.textContent).toBe('仅涵盖：重启。'));
    narrowView.entry.destroy();
  });

  it('marks an open-mode grant as advisory and rereads only when opened', async () => {
    const view = await setup({ ...on, grant: { ...on.grant, assurance: 'advisory' } }, async () => on);
    const advisory = view.entry.settings.querySelector('.router-owner-delegation-advisory') as HTMLElement;
    await vi.waitFor(() => expect(advisory.hidden).toBe(false));
    expect(view.ports.read).toHaveBeenCalledTimes(1);
    view.entry.settings.open = true;
    view.entry.settings.dispatchEvent(new Event('toggle'));
    await vi.waitFor(() => expect(view.ports.read).toHaveBeenCalledTimes(2));
    view.entry.destroy();
  });
});
