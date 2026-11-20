/** Subscription account Settings controller. The generated client owns every wire path. */
import {ApiError, request} from '../../api/transport';
import {createOAuthAccountClient, type OAuthAccountCommand, type OAuthAccountFlow} from '../../api/oauth-accounts.generated';
import {t} from '../../i18n';
import {createLifecycleScope} from '../../lifecycle';
import {currentSettingsReadSession} from './panel-read-session';
import {createSubscriptionAccountView, type AccountCopyKey, type AccountTextParams} from './oauth-account-view';
import {runAccountFlow} from './oauth-account-flow';

function copy(key: AccountCopyKey, params?: AccountTextParams): string {
  if (key === 'number') return t('settings.oauthAccountNumber', {n: params?.n ?? 0});
  if (key === 'alias') return t('settings.oauthAccountAlias', {name: params?.name ?? ''});
  if (key === 'usage') return t('settings.oauthAccountUsage', {requests: params?.requests ?? 0, tokens: params?.tokens ?? 0});
  if (key === 'usageWithCost') return t('settings.oauthAccountUsageCost', {requests: params?.requests ?? 0, tokens: params?.tokens ?? 0, cost: params?.cost ?? '0'});
  if (key === 'lastUsed') return t('settings.oauthAccountLastUsed', {time: params?.time ?? ''});
  if (key === 'unknownUsage') return t('settings.oauthAccountUnknownUsage', {requests: params?.requests ?? 0, tokens: params?.tokens ?? 0});
  if (key === 'usageWindow') return t('settings.oauthAccountsUsageWindow', {from: params?.from ?? '', to: params?.to ?? ''});
  const keys = {
    heading: 'settings.oauthAccountsHeading', add: 'settings.oauthAccountsAdd', empty: 'settings.oauthAccountsEmpty',
    default: 'settings.oauthAccountDefault', makeDefault: 'settings.oauthAccountMakeDefault',
    noneSelected: 'settings.oauthAccountNoneSelected', quotaUnavailable: 'settings.oauthAccountQuotaUnknown',
    cancel: 'settings.cancel', remove: 'common.remove', rename: 'paper.folderRename', save: 'settings.save',
    expired: 'err.k.oauth_reauth.chip', relogin: 'settings.oauthAccountsRelogin', refresh: 'ri.refresh',
    ready: 'settings.oauthAccountAuthorized', unknown: 'ri.statusUnknown', working: 'settings.channels.working',
    error: 'settings.oauthError', waitingAuth: 'settings.oauthWaitingAuth', loading: 'common.loading',
    actionFailed: 'settings.oauthAccountCommandFailed', loadFailed: 'settings.authSourcesLoadFail',
    usageNote: 'settings.oauthAccountsUsageNote', usageWindow: 'settings.oauthAccountsUsageWindow',
    usageUnavailable: 'settings.oauthAccountsUsageUnavailable',
  } as const;
  return t(keys[key]);
}

type RowWrite = Extract<OAuthAccountCommand, {action: 'rename' | 'default' | 'remove'}>;
type WriteFields = Omit<Extract<RowWrite, {action: 'rename'}>, 'provider' | 'expected_revision'>
  | Omit<Extract<RowWrite, {action: 'default' | 'remove'}>, 'provider' | 'expected_revision'>;
let retire: (() => void) | undefined;

export function setCodexAccountDetailActive(active: boolean): void {
  if (!active) { retire?.(); return; }
  if (retire) return;
  const session = currentSettingsReadSession();
  const root = document.getElementById('oauthCodexAccounts');
  if (!session?.isCurrent() || !root) return;
  const scope = createLifecycleScope(window);
  const close = () => scope.destroy();
  session.scope.signal.addEventListener('abort', close, {once: true});
  retire = close;
  scope.add(() => {
    retire = undefined;
    session.scope.signal.removeEventListener('abort', close);
    root.replaceChildren();
  });
  const isCurrent = () => session.isCurrent() && !scope.signal.aborted && root.isConnected;
  const client = createOAuthAccountClient({
    get: path => request(path, {signal: scope.signal}),
    post: (path, json) => request(path, {method: 'POST', json}),
  });
  const list = document.createElement('div'), flowRoot = document.createElement('div');
  const notice = document.createElement('p'), help = document.createElement('p');
  notice.className = 'oauth-account-notice';
  help.className = 'oauth-account-help';
  notice.setAttribute('role', 'status');
  help.textContent = t('settings.oauthAccountsHelp');
  root.setAttribute('aria-label', t('settings.oauthAccountsHeading'));
  root.append(list, notice, help, flowRoot);
  let revision: number | null = null;
  let generation = 0;

  async function refresh() {
    if (!isCurrent()) return;
    const read = ++generation;
    try {
      const inventory = await client.inventory();
      if (!isCurrent() || generation !== read) return;
      revision = inventory.revision;
      view.present({selectedId: inventory.default_account_id, usage: inventory.usage ?? null,
        accounts: inventory.accounts.map(row => ({
          id: row.account_id, alias: row.alias, status: row.status === 'authorized' ? 'ready'
            : row.status === 'needs_reauth' ? 'expired' : 'unknown',
          usage: inventory.usage?.accounts?.[row.account_id],
        }))});
    } catch {
      if (!isCurrent() || generation !== read) return;
      revision = null;
      view.present(null, true);
    }
  }

  async function command<T>(operation: () => Promise<T>, context: 'account' | 'sign-in' = 'account'): Promise<T | false> {
    if (!isCurrent() || revision === null) return false;
    notice.textContent = '';
    try { return await operation(); }
    catch (error) {
      if (error instanceof ApiError && (error.status === 409 || error.status === 404) && isCurrent()) {
        notice.textContent = t(error.status === 409 ? 'settings.oauthAccountsConflict'
          : context === 'sign-in' ? 'settings.oauthAccountSignInUnavailable' : 'settings.oauthAccountCommandFailed');
        await refresh();
        return false;
      }
      throw error;
    }
  }

  async function write(fields: WriteFields): Promise<void | false> {
    const expected_revision = revision!;
    const result = await command(() => client.command({provider: 'codex', expected_revision, ...fields}));
    if (result === false) return false;
    if (!('revision' in result)) throw new Error('Missing account acknowledgement');
  }

  async function login(id?: string, alias?: string): Promise<void | false> {
    const expected_revision = revision!;
    const result = await command(() => runAccountFlow(flowRoot, id ? `${copy('relogin')}: ${alias}` : copy('add'), {
      scope,
      async start(method) {
        if (!isCurrent()) throw new Error('Retired OAuth settings');
        const base = {provider: 'codex' as const, expected_revision, method};
        const result = await client.command(id ? {...base, action: 'reauth', account_id: id} : {...base, action: 'add'});
        if (!('flow_id' in result) || (id && result.account_id !== id)) throw new Error('Wrong authorization target');
        return result;
      },
      async complete(flow_id, callback): Promise<OAuthAccountFlow> {
        if (!isCurrent()) throw new Error('Retired OAuth settings');
        const base = {provider: 'codex' as const, flow_id};
        try {
          const result = await client.command(callback ? {...base, action: 'complete', ...callback} : {...base, action: 'poll'});
          if (!('flow_id' in result)) throw new Error('Missing flow acknowledgement');
          return result;
        } catch (error) {
          // A lost response can follow a committed exchange. Read the exact flow,
          // never repeat the write and never infer success from singleton status.
          if (!(error instanceof ApiError) || (error.status !== 0 && error.status < 500) || !isCurrent()) throw error;
          const observed = await client.flow(flow_id);
          if (observed.status !== 'authorized') throw error;
          return observed;
        }
      },
    }), 'sign-in');
    if (result === false) return false;
  }

  const view = createSubscriptionAccountView(list, {
    text: copy, isCurrent, refresh,
    add: () => login(), relogin: login,
    rename: (account_id, alias) => write({action: 'rename', account_id, alias}),
    remove: account_id => write({action: 'remove', account_id}),
    select: account_id => write({action: 'default', account_id}),
    confirmRemove: async alias => isCurrent() && window.confirm(t('settings.oauthAccountRemoveConfirm', {name: alias})),
  });
  void refresh();
}
