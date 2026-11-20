/** Subscription account inventory presentation. Only the provider adapter knows wire fields. */
import {createButton, createField} from '../../ui';

export interface AccountUsageBucket {
  requests: number;
  tokens: number;
  costUsd: number;
  pricedRequests: number;
  firstUsedAtMs: number | null;
  lastUsedAtMs: number | null;
}
export interface AccountUsageSummary {
  source: 'local_record';
  provider: string;
  status?: 'unavailable';
  accounts?: Record<string, AccountUsageBucket>;
  unknown?: AccountUsageBucket;
  window?: {fromMs: number; toMs: number} | null;
  truncated?: boolean;
}
export interface SubscriptionAccount {
  id: string;
  alias: string;
  status: 'ready' | 'expired' | 'pending' | 'error' | 'unknown';
  usage?: AccountUsageBucket;
}
export interface SubscriptionAccounts {
  accounts: readonly SubscriptionAccount[];
  selectedId: string | null;
  usage?: AccountUsageSummary | null;
}
export type AccountCopyKey =
  'cancel' | 'actionFailed' | 'add' | 'alias' | 'default'
  | 'expired' | 'loadFailed' | 'loading' | 'makeDefault' | 'noneSelected'
  | 'number' | 'quotaUnavailable' | 'ready' | 'refresh' | 'relogin'
  | 'remove' | 'rename' | 'unknown' | 'working' | 'heading'
  | 'error' | 'waitingAuth' | 'save' | 'empty'
  | 'usageNote' | 'usageWindow' | 'usageUnavailable' | 'usage'
  | 'usageWithCost' | 'lastUsed' | 'unknownUsage';

export interface AccountTextParams {
  n?: number;
  name?: string;
  requests?: number;
  tokens?: number;
  cost?: string;
  from?: string;
  to?: string;
  time?: string;
}

export interface SubscriptionAccountActions {
  text(key: AccountCopyKey, params?: AccountTextParams): string;
  isCurrent(): boolean;
  refresh(): void | Promise<void>;
  add?(): Promise<void | false>;
  relogin?(id: string, alias: string): Promise<void | false>;
  rename?(id: string, alias: string): Promise<void | false>;
  remove?(id: string): Promise<void | false>;
  select?(id: string): Promise<void | false>;
  /** The host must present the alias as plain text and await human approval. */
  confirmRemove(alias: string): Promise<boolean>;
}

function label(alias: string): string {
  // Keep names single-directionally isolated by the caller's <bdi>; strip
  // explicit bidi controls and other non-printing control characters.
  return alias.replace(/[\p{Cc}\p{Bidi_Control}]/gu, '').trim();
}

const statuses = {
  ready: 'ready', expired: 'expired',
  pending: 'waitingAuth', error: 'error',
  unknown: 'unknown',
} as const;
const tones = {ready: 'authenticated', expired: 'error', pending: 'pending', error: 'error', unknown: ''};

function count(value: number): string {
  return new Intl.NumberFormat().format(Math.max(0, Math.trunc(value)));
}

function money(value: number): string {
  return value.toFixed(4);
}

function moment(ms: number | null | undefined): string {
  return typeof ms === 'number' && Number.isFinite(ms) ? new Date(ms).toLocaleString() : '';
}

export function createSubscriptionAccountView(root: HTMLElement, actions: SubscriptionAccountActions) {
  const textFor = actions.text;
  const doc = root.ownerDocument;
  const title = doc.createElement('h4');
  const list = doc.createElement('ul');
  const toolbar = doc.createElement('div');
  const header = doc.createElement('div');
  const notice = doc.createElement('p');
  const empty = doc.createElement('p');
  const usageNote = doc.createElement('p');
  const usageWindow = doc.createElement('p');
  title.textContent = textFor('heading');
  title.tabIndex = -1;
  list.className = 'oauth-account-list';
  list.setAttribute('role', 'list');
  toolbar.className = 'oauth-account-actions';
  notice.className = 'oauth-account-notice';
  notice.setAttribute('role', 'status');
  root.classList.add('oauth-account-inventory');
  header.className = 'oauth-account-toolbar';
  header.append(title, toolbar);
  empty.className = 'oauth-account-empty';
  empty.textContent = textFor('empty');
  empty.hidden = true;
  usageNote.className = 'oauth-account-help oauth-account-usage-note';
  usageNote.hidden = true;
  usageWindow.className = 'oauth-account-notice oauth-account-usage-window';
  usageWindow.hidden = true;
  root.replaceChildren(header, notice, empty, list, usageNote, usageWindow);
  let snapshot: SubscriptionAccounts | null = null;
  let pending = false;
  let failure = false;
  let refreshFailed = false;
  let editing: string | null = null;
  let buttons = new Map<string, HTMLButtonElement>();
  const isCurrent = () => actions.isCurrent() && root.contains(title);

  function updateBusy() {
    for (const button of buttons.values()) button.setAttribute('aria-disabled', String(pending));
    notice.textContent = pending ? textFor('working')
      : failure ? textFor('actionFailed')
      : refreshFailed ? textFor('loadFailed')
      : !snapshot ? textFor('loading')
      : snapshot.accounts.length && snapshot.selectedId === null ? textFor('noneSelected') : '';
  }

  async function run(command?: () => Promise<void | false>) {
    if (pending || !isCurrent() || (command && refreshFailed)) return;
    pending = true;
    failure = false;
    updateBusy();
    let awaitingAcknowledgement = !!command;
    try {
      const result = await command?.();
      awaitingAcknowledgement = false;
      if (result !== false && isCurrent()) await actions.refresh();
    } catch {
      if (isCurrent()) {
        failure = awaitingAcknowledgement; // Never project raw server diagnostics.
        present(snapshot, true);
      }
    } finally {
      pending = false;
      if (isCurrent()) updateBusy();
    }
  }

  function button(key: string, text: string, onClick: () => void, aria = text, danger = false) {
    const variant = danger ? 'danger' : key === 'add' ? 'primary' : key === 'refresh' ? 'ghost' : 'secondary';
    const control = createButton({label: text, size: 'sm', wrap: true, variant, onClick: () => {
        if (isCurrent() && !pending && buttons.get(key) === control) onClick();
      }}, doc);
    control.setAttribute('aria-label', aria);
    buttons.set(key, control);
    return control;
  }

  function present(snapshotValue: SubscriptionAccounts | null, error = false) {
    if (!isCurrent()) return;
    if (snapshot === snapshotValue && refreshFailed === error && buttons.size) return;
    const previousFocus = [...buttons].find(([, control]) => control === doc.activeElement)?.[0];
    const hadFocus = root.contains(doc.activeElement);
    snapshot = snapshotValue;
    refreshFailed = error;
    empty.hidden = error || snapshot?.accounts.length !== 0;
    presentUsage(snapshotValue?.usage ?? null, error);
    if (!error) failure = false;
    editing = null;
    buttons = new Map();
    toolbar.replaceChildren();
    list.replaceChildren();
    if (error) toolbar.append(button('retry', textFor('refresh'), () => void run()));
    else if (snapshot) {
      if (actions.add) toolbar.append(button('add', textFor('add'), () => void run(actions.add!)));
      toolbar.append(button('refresh', textFor('refresh'), () => void run()));
    }
    const aliases = snapshot?.accounts.map(account => label(account.alias)) ?? [];
    const counts = new Map<string, number>();
    for (const alias of aliases) counts.set(alias, (counts.get(alias) ?? 0) + 1);
    snapshot?.accounts.forEach((account, index) => {
      const alias = aliases[index];
      const ordinal = textFor('number', {n: index + 1});
      const name = alias ? (counts.get(alias) === 1 ? alias : `${alias} · ${ordinal}`) : ordinal;
      list.append(accountRow(account, name, alias, snapshotValue?.selectedId === account.id));
    });
    const unknown = snapshotValue?.usage?.unknown;
    if (snapshotValue?.usage?.status !== 'unavailable' && unknown && unknown.requests > 0) {
      const row = doc.createElement('li');
      row.className = 'oauth-account-row oauth-account-usage-unknown';
      const label = doc.createElement('span');
      label.textContent = textFor('unknownUsage', {requests: unknown.requests, tokens: unknown.tokens});
      row.append(label);
      list.append(row);
    }
    updateBusy();
    if (hadFocus) (buttons.get(previousFocus ?? '') ?? title).focus({preventScroll: true});
  }

  function presentUsage(usage: AccountUsageSummary | null, error: boolean) {
    if (!usage || error) {
      usageNote.hidden = true;
      usageWindow.hidden = true;
      return;
    }
    usageNote.textContent = usage.status === 'unavailable'
      ? textFor('usageUnavailable') : textFor('usageNote');
    usageNote.hidden = false;
    const window = usage.window;
    usageWindow.hidden = !window;
    usageWindow.textContent = window
      ? textFor('usageWindow', {from: moment(window.fromMs), to: moment(window.toMs)})
      : '';
  }

  function usageLine(usage: AccountUsageBucket): HTMLElement {
    const line = doc.createElement('p');
    line.className = 'oauth-account-notice oauth-account-usage';
    line.textContent = usage.pricedRequests > 0
      ? textFor('usageWithCost', {requests: usage.requests, tokens: usage.tokens,
                                  cost: money(usage.costUsd)})
      : textFor('usage', {requests: usage.requests, tokens: usage.tokens});
    return line;
  }

  function accountRow(account: SubscriptionAccount, name: string, aliasValue: string, selected: boolean) {
    const id = account.id;
    const row = doc.createElement('li');
    row.className = 'oauth-account-row';
    const heading = doc.createElement('div');
    heading.className = 'oauth-account-heading';
    const alias = doc.createElement('bdi');
    alias.textContent = name;
    const status = doc.createElement('span');
    status.className = `oauth-status-badge ${tones[account.status] || ''}`;
    status.textContent = textFor(statuses[account.status] ?? statuses.unknown);
    heading.append(alias, status);
    if (selected) {
      const badge = doc.createElement('span');
      badge.className = 'oauth-status-badge';
      badge.textContent = textFor('default');
      heading.append(badge);
    }
    const quota = doc.createElement('p');
    quota.className = 'oauth-account-notice';
    quota.textContent = textFor('quotaUnavailable');
    const commands = doc.createElement('div');
    commands.className = 'oauth-account-actions';
    row.append(heading, quota);
    if (account.usage) {
      row.append(usageLine(account.usage));
      const lastUsed = moment(account.usage.lastUsedAtMs);
      if (lastUsed) {
        const line = doc.createElement('p');
        line.className = 'oauth-account-notice oauth-account-last-used';
        line.textContent = textFor('lastUsed', {time: lastUsed});
        row.append(line);
      }
    }
    row.append(commands);
    if (!refreshFailed) appendActions(commands, {id, name, aliasValue, selected, row});
    return row;
  }

  interface Target {id: string; name: string; aliasValue: string; selected: boolean; row: HTMLElement}
  function appendActions(commands: HTMLElement, target: Target) {
    const {id, name, selected} = target;
    const rowAction = (kind: string, text: string, click: () => void, danger = false) => {
      const control = button(`${kind}:${id}`, text, click, `${text}: ${name}`, danger);
      commands.append(control);
      return control;
    };
    if (actions.relogin) rowAction('relogin', textFor('relogin'), () => void run(() => actions.relogin!(id, name)));
    if (actions.rename) rowAction('rename', textFor('rename'), () => openRename(target));
    if (actions.select && !selected) rowAction('default', textFor('makeDefault'), () => void run(() => actions.select!(id)));
    if (actions.remove) rowAction('remove', textFor('remove'), () => void run(async () => {
      if (!await actions.confirmRemove(name) || !isCurrent() || !list.contains(target.row)) return false;
      return actions.remove!(id);
    }), true);
  }

  function openRename({id, name, aliasValue, row}: Target) {
    if (editing !== null) return;
    editing = id;
    const field = createField({label: textFor('alias', {name}), value: aliasValue, required: true}, doc);
    const input = field.control as HTMLInputElement;
    input.autocomplete = 'off';
    input.maxLength = 128;
    const form = doc.createElement('form');
    form.className = 'oauth-account-edit';
    const close = () => {
      form.remove();
      editing = null;
      buttons.get(`rename:${id}`)?.focus();
    };
    form.append(field.el, createButton({label: textFor('save'), type: 'submit', size: 'sm'}, doc),
      createButton({label: textFor('cancel'), size: 'sm', onClick: close}, doc));
    form.addEventListener('submit', event => {
      event.preventDefault();
      const alias = label(input.value);
      if (!alias || editing !== id || pending || !row.contains(form)) return;
      close();
      void run(() => actions.rename!(id, alias));
    });
    form.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.stopPropagation(); close(); }
    });
    row.append(form);
    input.focus();
    input.select();
  }

  updateBusy();
  return {present};
}
