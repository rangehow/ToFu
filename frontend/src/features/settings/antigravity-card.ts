/** Antigravity's disposable Subscription Login card. The OAuth tab owns its
 * status, sign-in and prompts; none can publish after the tab retires. */
import {ApiError} from '../../api/errors';
import type {AntigravityState, createOauthEndpoints} from '../../api/endpoint-registry/oauth';
import {featureRegistry} from '../../feature-registry';
import {t} from '../../i18n';
import {createLifecycleScope, type LifecycleScope} from '../../lifecycle';
import {createButton, createField, setFieldError} from '../../ui';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';

const LOGIN_COMMAND = 'agy acp --login';
const stateLabels = {
  unsupported: 'settings.oauthAntigravityUnsupported',
  missing_cli: 'settings.oauthAntigravityMissingCli',
  login_required: 'settings.oauthNotLoggedIn',
  ready: 'settings.oauthAntigravityReady',
  unavailable: 'settings.oauthAntigravityUnavailable',
} as const;
let active: LifecycleScope | null = null;
let prepareDetail: (() => void) | null = null;

export function prepareAntigravityDetail(): void { prepareDetail?.(); }

function createCard() {
  const card = document.createElement('section');
  card.className = 'oauth-provider-card';
  card.setAttribute('aria-label', 'Antigravity');
  const header = document.createElement('div');
  header.className = 'oauth-provider-header';
  const name = document.createElement('h3');
  name.className = 'oauth-provider-name';
  name.tabIndex = -1;
  name.textContent = 'Antigravity';
  const status = document.createElement('span');
  status.setAttribute('role', 'status');
  const actions = document.createElement('div');
  actions.className = 'oauth-provider-actions';
  const refresh = createButton({label: t('settings.oauthAntigravityRefresh'), size: 'sm', wrap: true});
  const login = createButton({label: t('settings.oauthAntigravityLogin'), variant: 'primary', size: 'sm', wrap: true});
  const cancel = createButton({label: t('common.cancel'), size: 'sm', wrap: true});
  const install = createButton({label: t('settings.oauthAntigravityInstallRetry'), size: 'sm', wrap: true});
  install.hidden = true;
  actions.append(login, cancel, install, refresh);
  header.append(name, status, actions);

  const installNotice = document.createElement('p');
  installNotice.className = 'oauth-provider-desc';
  installNotice.setAttribute('aria-live', 'polite');
  const setup = document.createElement('p');
  setup.className = 'oauth-provider-desc';
  const browserHint = document.createElement('p');
  browserHint.className = 'oauth-provider-desc';
  const form = document.createElement('form');
  const fields = document.createElement('div');
  fields.className = 'ui-stack ui-stack--bounded';
  const prompt = document.createElement('textarea');
  prompt.className = 'ui-input';
  prompt.rows = 3;
  const field = createField({label: t('settings.oauthAntigravityPrompt'), control: prompt});
  const disclosure = document.createElement('p');
  disclosure.className = 'ui-field__hint ui-text-block';
  const sendRow = document.createElement('div');
  sendRow.className = 'ui-row';
  const send = createButton({label: t('settings.oauthAntigravitySend'), variant: 'primary',
    size: 'sm', type: 'submit', wrap: true});
  sendRow.append(send);
  const output = document.createElement('pre');
  output.className = 'ui-text-block';
  output.setAttribute('aria-live', 'polite');
  fields.append(field.el, disclosure, sendRow, output);
  form.append(fields);
  card.append(header, installNotice, browserHint, setup, form);
  return {card, status, installNotice, setup, browserHint, login, cancel, install, refresh,
    form, prompt, field, disclosure, send, output};
}

function mountCard(panel: HTMLElement, session: SettingsReadSession): LifecycleScope {
  const scope = createLifecycleScope();
  const {card, status, installNotice, setup, browserHint, login, cancel, install, refresh,
    form, prompt, field, disclosure, send, output} = createCard();
  const current = () => !scope.signal.aborted && session.isCurrent() && card.isConnected;
  const api = () => (featureRegistry.Api as {oauth: ReturnType<typeof createOauthEndpoints>}).oauth;
  let state: AntigravityState = 'unavailable';
  let pending: 'status' | 'install' | 'login' | 'prompt' | null = null;
  let browserLoginAvailable = false;
  let loginRequest: AbortController | null = null;
  let invalidPrompt = false;
  // The card installs a missing vendor CLI once per mount, without asking:
  // a refused or failed install stays visible and retryable.
  let autoInstallTried = false;
  let installFailed = false;
  let installMessage: (() => string) | null = null;

  function setInstallNotice(build: (() => string) | null) {
    installMessage = build;
    installNotice.textContent = build ? build() : '';
    installNotice.hidden = !build;
  }

  function updateSend() { send.disabled = pending !== null || prompt.value.trim() === ''; }
  function render() {
    const badge = pending ? ' pending' : state === 'ready' ? ' authenticated' : '';
    status.className = `oauth-status-badge${badge}`;
    status.textContent = t(pending === 'status' ? 'settings.loadingStatus'
      : pending === 'install' ? 'settings.oauthAntigravityInstalling'
      : pending === 'login' ? 'settings.oauthWaitingAuth'
      : pending === 'prompt' ? 'settings.oauthAntigravitySending' : stateLabels[state]);
    const canLogin = state === 'login_required' && browserLoginAvailable;
    login.hidden = !canLogin || pending === 'login';
    login.disabled = pending !== null;
    cancel.hidden = pending !== 'login';
    browserHint.hidden = !canLogin;
    const command = state === 'missing_cli'
      ? 'curl -fsSL https://antigravity.google/cli/install.sh | bash' : LOGIN_COMMAND;
    setup.textContent = state === 'missing_cli' ? t('settings.oauthAntigravitySetup', {command}) : command;
    // The manual command is the fallback, not the first instruction.
    setup.hidden = !((state === 'missing_cli' && installFailed) || state === 'login_required');
    install.hidden = !(state === 'missing_cli' && installFailed);
    install.disabled = pending !== null;
    form.hidden = state !== 'ready';
    refresh.disabled = pending !== null;
    prompt.readOnly = pending !== null;
    updateSend();
  }
  function updateFieldError(invalid: boolean) {
    invalidPrompt = invalid;
    setFieldError(field, invalid ? t('settings.oauthAntigravityInvalidPrompt') : null);
  }
  function translate() {
    browserHint.textContent = t('settings.oauthAntigravityBrowserHint');
    login.textContent = t('settings.oauthAntigravityLogin');
    cancel.textContent = t('common.cancel');
    disclosure.textContent = t('settings.oauthAntigravityDisclosure');
    refresh.textContent = t('settings.oauthAntigravityRefresh');
    install.textContent = t('settings.oauthAntigravityInstallRetry');
    if (installMessage) installNotice.textContent = installMessage();
    send.textContent = t('settings.oauthAntigravitySend');
    field.el.querySelector('label')!.textContent = t('settings.oauthAntigravityPrompt');
    updateFieldError(invalidPrompt);
    render();
  }
  async function installCli(): Promise<void> {
    if (!current() || pending) return;
    pending = 'install';
    installFailed = false;
    setInstallNotice(null);
    render();
    let usable = false;
    try {
      // No view signal: a host install is an accepted change that must settle
      // even if this opening retires. The server bounds it.
      const result = await api().antigravityInstall({});
      if (!current()) return;
      usable = result.status === 'installed' || result.status === 'already';
      installFailed = !usable;
      if (usable) {
        const version = result.version || '';
        const path = result.executable || '';
        setInstallNotice(result.status === 'installed'
          ? () => t('settings.oauthAntigravityInstalled', {version, path})
          : () => t('settings.oauthAntigravityAlreadyInstalled', {path}));
      } else {
        setInstallNotice(result.status === 'unsupported'
          ? () => t('settings.oauthAntigravityInstallUnsupported')
          : () => t('settings.oauthAntigravityInstallFailed'));
      }
    } catch {
      if (!current()) return;
      installFailed = true;
      setInstallNotice(() => t('settings.oauthAntigravityInstallFailed'));
    } finally {
      pending = null;
      if (current()) render();
    }
    if (usable && current()) void run('status');
  }

  async function run(action: 'status' | 'login' | 'prompt', promptText = '') {
    if (!current() || pending) return;
    const sending = action === 'prompt';
    const request = action === 'login' ? new AbortController() : null;
    const abort = () => request?.abort();
    if (request) scope.signal.addEventListener('abort', abort, {once: true});
    loginRequest = request;
    pending = action;
    updateFieldError(false);
    if (sending) output.textContent = '';
    render();
    try {
      const options = {signal: request?.signal ?? scope.signal};
      const result = sending ? await api().antigravityPrompt(promptText, options)
        : request ? await api().antigravityLogin(options) : await api().antigravityStatus(options);
      if (!current() || request?.signal.aborted) return;
      state = Object.hasOwn(stateLabels, result.state) ? result.state : 'unavailable';
      browserLoginAvailable = 'browser_login_available' in result && result.browser_login_available === true;
      if (sending && state === 'ready') {
        if ('text' in result && typeof result.text === 'string') {
          output.textContent = result.text;
          prompt.value = '';
        } else state = 'unavailable';
      }
    } catch (error) {
      if (!current() || request?.signal.aborted) return;
      if (sending && error instanceof ApiError && error.status === 400) updateFieldError(true);
      else state = 'unavailable';
    } finally {
      if (request) scope.signal.removeEventListener('abort', abort);
      loginRequest = null;
      pending = null;
      if (current()) render();
    }
    if (action === 'status') prepareDetail?.();
  }

  prepareDetail = () => {
    if (current() && !panel.hidden && !pending && state === 'missing_cli' && !autoInstallTried) {
      autoInstallTried = true;
      void installCli();
    }
  };
  scope.add(() => { prepareDetail = null; card.remove(); });
  scope.listen(session.scope.signal, 'abort', () => scope.destroy(), {once: true});
  scope.listen(window, 'tofu:language-change', translate);
  scope.listen(refresh, 'click', () => { void run('status'); });
  scope.listen(install, 'click', () => { void installCli(); });
  scope.listen(login, 'click', () => { void run('login'); });
  scope.listen(cancel, 'click', () => loginRequest?.abort());
  scope.listen(prompt, 'input', () => { updateFieldError(false); updateSend(); });
  scope.listen(form, 'submit', event => {
    event.preventDefault();
    if (state === 'ready' && prompt.value.trim()) void run('prompt', prompt.value);
  });
  setInstallNotice(null);
  translate();
  panel.append(card);
  void run('status');
  return scope;
}

export function syncAntigravitySettingsTab(tab: string): void {
  if (tab !== 'oauth') { active?.destroy(); return; }
  if (active) return;
  const session = currentSettingsReadSession();
  const panel = document.getElementById('oauthDetailAntigravity');
  if (!session?.isCurrent() || !panel) return;
  active = mountCard(panel, session);
  active.add(() => { active = null; });
}
