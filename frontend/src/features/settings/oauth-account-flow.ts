/** Exact Codex account flow UI. Browser input and device codes live only in this mount. */
import type {OAuthAccountFlow} from '../../api/oauth-accounts.generated';
import {t} from '../../i18n';
import {createButton, createField} from '../../ui';
import type {LifecycleScope} from '../../lifecycle';

type Method = 'browser' | 'device';
interface FlowPorts {
  scope: LifecycleScope;
  start(method: Method): Promise<OAuthAccountFlow>;
  complete(flowId: string, callback?: {code: string; state: string}): Promise<OAuthAccountFlow>;
}

function providerLink(value?: string): URL {
  const url = new URL(value ?? '');
  if (url.origin !== 'https://auth.openai.com' || url.username || url.password) throw new Error('OAuth link');
  return url;
}

function readCallback(value: string, expected: URL) {
  const url = new URL(value.trim());
  const code = url.searchParams.getAll('code'), state = url.searchParams.getAll('state');
  if (!['http://localhost:1455', 'http://127.0.0.1:1455'].includes(url.origin)
      || url.pathname !== '/auth/callback' || url.username || url.password
      || code.length !== 1 || !code[0] || state.length !== 1 || !state[0]
      || state[0] !== expected.searchParams.get('state')) throw new Error('OAuth callback');
  return {code: code[0], state: state[0]};
}

export function runAccountFlow(root: HTMLElement, title: string, ports: FlowPorts): Promise<void | false> {
  const {scope} = ports;
  const doc = root.ownerDocument;
  return new Promise((resolve, reject) => {
    const form = doc.createElement('form');
    form.className = 'oauth-account-flow';
    const heading = doc.createElement('strong');
    heading.textContent = title;
    heading.tabIndex = -1;
    const status = doc.createElement('p');
    status.setAttribute('role', 'status');
    const content = doc.createElement('div');
    content.className = 'oauth-account-actions';
    const launch = doc.activeElement;
    let active = true;
    let timer = 0;
    let unregister = () => {};
    function finish(outcome: 'cancel' | 'done' | 'error', restore = true) {
      if (!active) return;
      active = false;
      scope.cancelTimeout(timer);
      unregister();
      form.remove();
      if (restore && launch instanceof HTMLElement && launch.isConnected) launch.focus();
      if (outcome === 'error') reject(new Error('Account authorization unsuccessful'));
      else resolve(outcome === 'cancel' ? false : undefined);
    }
    function action(label: string, click: () => void) {
      return createButton({label, size: 'sm', wrap: true, onClick: click}, doc);
    }
    function showLink(text: string, url: URL) {
      const link = doc.createElement('a');
      link.href = url.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = text;
      content.append(link);
    }
    async function start(method: Method) {
      if (!active) return;
      // Keep keyboard events inside the form while the focused method button disappears.
      heading.focus();
      content.replaceChildren();
      status.textContent = t('settings.oauthPreparing');
      try {
        const flow = await ports.start(method);
        if (!active) return;
        if (flow.status !== 'pending' || !flow.flow_id) throw new Error('OAuth flow');
        status.textContent = t('settings.oauthWaitingAuth');
        if (method === 'browser') browser(flow);
        else device(flow);
      } catch (error) {
        if (!active) return;
        active = false;
        unregister();
        form.remove();
        if (launch instanceof HTMLElement && launch.isConnected) launch.focus();
        reject(error);
      }
    }
    async function settle(flow: OAuthAccountFlow, callback?: {code: string; state: string}) {
      // An accepted completion may settle even if the user closes this view.
      dismiss.textContent = t('common.close');
      try {
        const next = await ports.complete(flow.flow_id, callback);
        if (!active) return;
        if (next.flow_id !== flow.flow_id || next.account_id !== flow.account_id) return finish('error');
        if (next.status === 'authorized') finish('done');
        else if (!callback && next.status === 'pending') schedule(next);
        else finish('error');
      } catch (error) {
        if (!active) return;
        active = false;
        unregister();
        form.remove();
        if (launch instanceof HTMLElement && launch.isConnected) launch.focus();
        reject(error);
      }
    }
    function browser(flow: OAuthAccountFlow) {
      const url = providerLink(flow.auth_url);
      showLink(t('settings.oauthDeviceOpenPage'), url);
      const field = createField({label: t('settings.oauthCodexPh')}, doc);
      const input = field.control as HTMLInputElement;
      input.autocomplete = 'off';
      input.spellcheck = false;
      const submit = createButton({label: t('settings.oauthSubmit'), type: 'submit', size: 'sm'}, doc);
      const hint = doc.createElement('p');
      hint.className = 'oauth-manual-hint';
      hint.textContent = t('settings.oauthCodexCbHint');
      content.append(hint, field.el, submit);
      form.addEventListener('submit', event => {
        event.preventDefault();
        if (!active || submit.disabled) return;
        let callback;
        try { callback = readCallback(input.value, url); }
        catch { status.textContent = t('settings.oauthAccountCallbackInvalid'); input.focus(); return; }
        input.value = '';
        heading.focus();
        content.replaceChildren();
        status.textContent = t('settings.oauthGettingToken');
        void settle(flow, callback);
      });
    }
    function schedule(flow: OAuthAccountFlow) {
      const interval = flow.interval;
      if (typeof interval !== 'number' || !Number.isFinite(interval) || interval <= 0) return finish('error');
      timer = scope.timeout(() => { if (active) void settle(flow); }, interval * 1000);
    }
    function device(flow: OAuthAccountFlow) {
      if (!flow.user_code) throw new Error('OAuth device code');
      const code = doc.createElement('code');
      code.textContent = flow.user_code;
      showLink(t('settings.oauthDeviceOpenPage'), providerLink(flow.verification_url));
      content.append(code);
      schedule(flow);
    }
    form.addEventListener('submit', event => event.preventDefault());
    form.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      finish('cancel');
    });
    content.append(action(t('settings.oauthLoginChatGPT'), () => void start('browser')),
      action(t('settings.oauthDeviceLogin'), () => void start('device')));
    const dismiss = action(t('settings.cancel'), () => finish('cancel'));
    form.append(heading, status, content, dismiss);
    root.append(form);
    const onClose = () => finish('cancel', false);
    scope.signal.addEventListener('abort', onClose, {once: true});
    unregister = () => scope.signal.removeEventListener('abort', onClose);
    heading.focus();
  });
}
