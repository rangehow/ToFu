/** Add-device wizard: choose the kind → connect it with a one-shot code, link or one-line command → confirm the
 * device that redeemed it → name it and choose what this Home allows.
 *
 * The code is minted by the Home (POST /api/v4/desktop/pair-code) and is single-use with a short expiry; a
 * node that redeems it waits as `pending` until the owner confirms here (owner decision 5), so a leaked code
 * cannot silently add a machine. The wizard notices the redeeming node through the live store (the registry
 * publishes `devices.changed`), never by polling. The preset only sets the Home ceiling; the device keeps its
 * own switch. Entry point: openAddDeviceWizard. Dependencies: the devices store, ui kit, i18n.
 */
import { t } from '../../i18n';
import { createButton, createChoice, createDialog, createField, createSpinner, toast } from '../../ui';
import { clockTime, el } from './dom';
import { defaultKindLabel, routeLabel } from './labels';
import type { DevicesSnapshot, DevicesStore } from './live';
import { PRESETS, type DeviceRow, type PresetName } from './model';
import type { PairCode } from './client';

type Kind = 'computer' | 'server' | 'browser' | 'phone' | 'home';
type Step = 1 | 2 | 3 | 4;

export interface WizardPorts {
  /** Page base the pairing link is built on (origin + proxy prefix). */
  pageBase(): string;
  openBrowserExtension?: () => void;
  now?(): number;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function copyButton(value: string): HTMLButtonElement {
  const button = createButton({ label: t('devicesPage.wizard.copy'), variant: 'secondary', size: 'sm',
    onClick: async () => {
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = t('devicesPage.wizard.copied');
      } catch (error) {
        toast(t('devicesPage.error.copy', { message: errorText(error) }), { variant: 'error' });
      }
    } });
  return button;
}

function codeBlock(value: string, className = ''): HTMLElement {
  const wrap = el('div', `devices-wizard__code ui-row ${className}`);
  const code = el('code', '', value);
  wrap.append(code, copyButton(value));
  return wrap;
}

function headlessCommand(code: PairCode): string {
  return code.command || `python -m lib.desktop_agent --join '${code.link}'`;
}

export function openAddDeviceWizard(store: DevicesStore, ports: WizardPorts, confirmNodeId?: string,
  onClosed?: () => void): () => void {
  const now = ports.now || (() => Date.now());
  const body = el('div', 'devices-wizard');
  const dialog = createDialog({ title: t('devicesPage.wizard.title'), body });
  dialog.el.classList.add('devices-wizard-shell');
  let step: Step = confirmNodeId ? 3 : 1;
  let kind: Kind = 'server';
  let code: PairCode | null = null;
  let minting = false;
  let mintError = '';
  let expired = false;
  let expiryTimer: ReturnType<typeof setTimeout> | null = null;
  let knownKeys = new Set<string>();
  let nodeId = confirmNodeId || '';
  let confirmedWithoutReview = false;
  let busy = false;
  let preset: PresetName = 'read';
  let makeDefault = false;
  let name = '';

  const rowOf = (snapshot: DevicesSnapshot): DeviceRow | null =>
    snapshot.fleet?.rows.find(row => row.nodeId === nodeId) || null;

  const mint = async () => {
    minting = true; mintError = ''; expired = false; code = null;
    knownKeys = new Set((store.snapshot().fleet?.rows || []).map(row => row.key));
    render();
    try {
      code = await store.client.pairCode(kind === 'computer' ? 'computer' : 'server', ports.pageBase());
      if (expiryTimer) clearTimeout(expiryTimer);
      expiryTimer = setTimeout(() => { expired = true; render(); }, Math.max(0, code.expires_at * 1000 - now()));
    } catch (error) {
      mintError = errorText(error);
    } finally {
      minting = false;
      render();
    }
  };

  const stepper = () => {
    const list = el('ol', 'devices-wizard__steps');
    const labels = [t('devicesPage.wizard.step1'), t('devicesPage.wizard.step2'), t('devicesPage.wizard.step3'),
      t('devicesPage.wizard.step4')];
    labels.forEach((label, index) => {
      const item = el('li', 'devices-wizard__step');
      const number = el('span', 'devices-wizard__step-number', String(index + 1));
      number.setAttribute('aria-hidden', 'true');
      item.append(number, el('span', '', label));
      if (index + 1 === step) item.setAttribute('aria-current', 'step');
      if (index + 1 < step) item.classList.add('devices-wizard__step--done');
      list.append(item);
    });
    return list;
  };

  const footer = (...buttons: HTMLElement[]) => {
    const bar = el('div', 'devices-wizard__footer ui-row ui-row--wrap');
    bar.append(...buttons);
    return bar;
  };

  const chooseKind = () => {
    const panel = el('div', 'devices-wizard__panel');
    panel.append(el('h3', '', t('devicesPage.wizard.step1')));
    const group = el('div', 'devices-wizard__kinds');
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', t('devicesPage.wizard.step1'));
    const options: Array<[Kind, string, string]> = [
      ['computer', t('devicesPage.wizard.kindComputer'), t('devicesPage.wizard.kindComputerBody')],
      ['server', t('devicesPage.wizard.kindServer'), t('devicesPage.wizard.kindServerBody')],
      ['browser', t('devicesPage.wizard.kindBrowser'), t('devicesPage.wizard.kindBrowserBody')],
      ['phone', t('devicesPage.wizard.kindPhone'), t('devicesPage.wizard.kindPhoneBody')],
      ['home', t('devicesPage.wizard.kindHome'), t('devicesPage.wizard.kindHomeBody')],
    ];
    for (const [value, label, description] of options) {
      const option = el('div', 'devices-wizard__kind');
      const choice = createChoice({ type: 'radio', name: 'devicesWizardKind', value, label, checked: kind === value,
        onChange: checked => { if (checked) { kind = value; render(); } } });
      option.append(choice.el, el('p', 'devices-muted', description));
      if (kind === value) option.classList.add('devices-wizard__kind--selected');
      group.append(option);
    }
    panel.append(group, footer(
      createButton({ label: t('devicesPage.cancel'), variant: 'ghost', onClick: () => dialog.close() }),
      createButton({ label: t('devicesPage.wizard.next'), variant: 'primary', onClick: () => {
        step = 2;
        if (kind === 'computer' || kind === 'server') void mint(); else render();
      } }),
    ));
    return panel;
  };

  const connect = () => {
    const panel = el('div', 'devices-wizard__panel');
    panel.append(el('h3', '', kind === 'server' ? t('devicesPage.wizard.step2Server') : t('devicesPage.wizard.step2')));
    const back = createButton({ label: t('devicesPage.wizard.back'), variant: 'ghost', onClick: () => { step = 1; render(); } });
    if (kind === 'browser') {
      panel.append(el('p', '', t('devicesPage.wizard.browserBody')));
      const actions = [back];
      if (ports.openBrowserExtension) {
        const open = ports.openBrowserExtension;
        actions.push(createButton({ label: t('devicesPage.wizard.browserOpen'), variant: 'primary',
          onClick: () => { dialog.close(); open(); } }));
      }
      panel.append(footer(...actions));
      return panel;
    }
    if (kind === 'phone') {
      panel.append(el('p', '', t('devicesPage.wizard.phoneBody')), codeBlock(ports.pageBase() || window.location.origin),
        footer(back, createButton({ label: t('devicesPage.wizard.done'), variant: 'primary', onClick: () => dialog.close() })));
      return panel;
    }
    if (kind === 'home') {
      panel.append(el('p', '', t('devicesPage.wizard.homeBody')),
        footer(back, createButton({ label: t('devicesPage.wizard.done'), variant: 'primary', onClick: () => dialog.close() })));
      return panel;
    }
    if (minting || (!code && !mintError)) {
      const loading = el('div', 'ui-row');
      loading.append(createSpinner({ size: 'sm', label: t('devicesPage.wizard.minting') }), el('span', '', t('devicesPage.wizard.minting')));
      panel.append(loading, footer(back));
      return panel;
    }
    if (mintError || !code) {
      panel.append(el('p', 'devices-error', t('devicesPage.wizard.pairFailed', { message: mintError })),
        footer(back, createButton({ label: t('devicesPage.retry'), variant: 'primary', onClick: () => void mint() })));
      return panel;
    }
    const expiry = el('p', expired ? 'devices-error' : 'devices-muted', expired
      ? t('devicesPage.wizard.expired') : t('devicesPage.wizard.expires', { time: clockTime(code.expires_at * 1000) }));
    panel.append(expiry);
    if (!expired) {
      if (kind === 'server') {
        panel.append(codeBlock(headlessCommand(code), 'devices-wizard__command'));
        panel.append(el('p', 'devices-muted', t('devicesPage.wizard.orCode')));
      } else {
        panel.append(el('p', '', t('devicesPage.wizard.computerBody')), codeBlock(code.link));
        panel.append(el('p', 'devices-muted', t('devicesPage.wizard.orCode')));
      }
      const digits = el('div', 'devices-wizard__digits', `${code.code.slice(0, 3)} ${code.code.slice(3)}`);
      digits.setAttribute('aria-label', t('devicesPage.wizard.codeLabel', { code: code.code.split('').join(' ') }));
      panel.append(digits, el('p', 'devices-muted', t('devicesPage.wizard.reach')));
      const next = el('ol', 'devices-wizard__next');
      next.setAttribute('aria-label', t('devicesPage.wizard.nextLabel'));
      next.append(el('li', '', t('devicesPage.wizard.nextConfirm')), el('li', '', t('devicesPage.wizard.nextAllow')));
      panel.append(next);
      const waiting = el('div', 'ui-row devices-wizard__waiting');
      waiting.setAttribute('role', 'status');
      waiting.append(createSpinner({ size: 'sm', label: t('devicesPage.wizard.waiting') }), el('span', '', t('devicesPage.wizard.waiting')));
      panel.append(waiting);
    }
    const actions = [back];
    if (expired) actions.push(createButton({ label: t('devicesPage.wizard.newCode'), variant: 'primary', onClick: () => void mint() }));
    panel.append(footer(...actions));
    return panel;
  };

  const review = (snapshot: DevicesSnapshot) => {
    const panel = el('div', 'devices-wizard__panel');
    panel.append(el('h3', '', t('devicesPage.wizard.step3')));
    const row = rowOf(snapshot);
    if (!row) {
      panel.append(el('p', 'devices-muted', snapshot.fleet ? t('devicesPage.wizard.gone') : t('devicesPage.loading')),
        footer(createButton({ label: t('devicesPage.cancel'), variant: 'ghost', onClick: () => dialog.close() })));
      return panel;
    }
    const card = el('div', 'ui-card devices-wizard__candidate');
    card.append(el('p', 'devices-wizard__candidate-title', t('devicesPage.wizard.wantsToJoin', { name: row.name })));
    const facts = [row.system, row.version ? `v${row.version}` : '',
      row.route ? t('devicesPage.wizard.reachedVia', { route: routeLabel(row.route) }) : '']
      .filter(Boolean);
    if (facts.length) card.append(el('p', 'devices-muted', facts.join(' · ')));
    if (row.fingerprint) {
      const fingerprint = el('p', '');
      fingerprint.append(t('devicesPage.wizard.fingerprint'), ' ', el('code', 'devices-wizard__fingerprint', row.fingerprint));
      card.append(fingerprint, el('p', 'devices-muted', t('devicesPage.wizard.fingerprintHint')));
    }
    panel.append(card, el('p', 'devices-muted', t('devicesPage.wizard.confirmHint')));
    const confirm = createButton({ label: t('devicesPage.wizard.confirm'), variant: 'primary', disabled: busy,
      onClick: async () => {
        busy = true; render();
        try {
          await store.client.confirm(row.nodeId || '', row.name);
          name = row.name;
          step = 4;
        } catch (error) {
          toast(t('devicesPage.error.save', { message: errorText(error) }), { variant: 'error' });
        } finally {
          busy = false;
          await store.refresh();
        }
      } });
    const reject = createButton({ label: t('devicesPage.wizard.reject'), variant: 'secondary', disabled: busy,
      onClick: async () => {
        busy = true; render();
        try {
          await store.client.reject(row.nodeId || '');
          toast(t('devicesPage.wizard.rejected'), { variant: 'info' });
          dialog.close();
        } catch (error) {
          toast(t('devicesPage.error.save', { message: errorText(error) }), { variant: 'error' });
        } finally {
          busy = false;
          await store.refresh();
        }
      } });
    panel.append(footer(reject, confirm));
    return panel;
  };

  const allow = (snapshot: DevicesSnapshot) => {
    const panel = el('div', 'devices-wizard__panel');
    panel.append(el('h3', '', t('devicesPage.wizard.step4')));
    const row = rowOf(snapshot);
    if (confirmedWithoutReview && row) {
      panel.append(el('p', '', t('devicesPage.wizard.joined', { name: row.name })));
    }
    const field = createField({ label: t('devicesPage.wizard.name'), value: name || row?.name || '' });
    const input = field.control as HTMLInputElement;
    input.addEventListener('input', () => { name = input.value; });
    panel.append(field.el);
    const permissionsServed = snapshot.sources?.permissions.state === 'ok';
    if (permissionsServed) {
      const group = el('fieldset', 'devices-wizard__presets');
      group.append(el('legend', '', t('devicesPage.wizard.preset')));
      const presets: Array<[PresetName, string]> = [
        ['read', t('devicesPage.wizard.presetRead')],
        ['developer', t('devicesPage.wizard.presetDeveloper')],
        ['full', t('devicesPage.wizard.presetFull')],
      ];
      for (const [value, label] of presets) {
        group.append(createChoice({ type: 'radio', name: 'devicesWizardPreset', value, label, checked: preset === value,
          onChange: checked => { if (checked) preset = value; } }).el);
      }
      group.append(el('p', 'devices-muted', t('devicesPage.wizard.presetHint')));
      panel.append(group);
    } else {
      panel.append(el('p', 'devices-muted', t('devicesPage.absent.permissions')));
    }
    const defaultKind = row?.kind === 'server' ? 'server' : 'computer';
    if (snapshot.sources?.defaults.state === 'ok') {
      panel.append(createChoice({ type: 'checkbox', checked: makeDefault,
        label: t('devicesPage.wizard.makeDefault', { kind: defaultKindLabel(defaultKind) }),
        onChange: checked => { makeDefault = checked; } }).el);
    }
    const done = createButton({ label: t('devicesPage.wizard.done'), variant: 'primary', disabled: busy || !row,
      onClick: async () => {
        if (!row?.nodeId) return;
        const id = row.nodeId;
        busy = true; render();
        try {
          const trimmed = name.trim();
          if (trimmed && trimmed !== row.name) await store.client.rename(id, trimmed);
          if (permissionsServed) {
            const { read: _read, ...tiers } = PRESETS[preset];
            await store.client.setCeiling(id, tiers);
          }
          if (makeDefault) await store.client.setDefault(defaultKind, id);
          toast(t('devicesPage.wizard.added', { name: trimmed || row.name }), { variant: 'success' });
          dialog.close();
        } catch (error) {
          toast(t('devicesPage.error.save', { message: errorText(error) }), { variant: 'error' });
        } finally {
          busy = false;
          await store.refresh();
        }
      } });
    panel.append(footer(done));
    return panel;
  };

  const observe = (snapshot: DevicesSnapshot) => {
    if (step !== 2 || !code || !snapshot.fleet) return;
    const joined = snapshot.fleet.rows.find(row => !knownKeys.has(row.key) && row.nodeId
      && (row.kind === 'computer' || row.kind === 'server'));
    if (!joined?.nodeId) return;
    nodeId = joined.nodeId;
    if (joined.state === 'pending') {
      step = 3;
    } else {
      // A Home that admits nodes without a confirmation step: the node has already joined.
      confirmedWithoutReview = true;
      name = joined.name;
      step = 4;
    }
  };

  function render(snapshot: DevicesSnapshot = store.snapshot()) {
    observe(snapshot);
    const focused = document.activeElement instanceof HTMLInputElement && body.contains(document.activeElement)
      ? document.activeElement.name || document.activeElement.id : '';
    const panel = step === 1 ? chooseKind() : step === 2 ? connect() : step === 3 ? review(snapshot) : allow(snapshot);
    body.replaceChildren(stepper(), panel);
    if (focused) body.querySelector<HTMLInputElement>(`input[name="${CSS.escape(focused)}"]:checked, #${CSS.escape(focused)}`)?.focus();
  }

  const unsubscribe = store.subscribe(snapshot => render(snapshot));
  const onLanguage = () => render();
  window.addEventListener('tofu:language-change', onLanguage);
  void dialog.open().then(() => {
    unsubscribe();
    if (expiryTimer) clearTimeout(expiryTimer);
    window.removeEventListener('tofu:language-change', onLanguage);
    onClosed?.();
  });
  return () => dialog.close();
}
