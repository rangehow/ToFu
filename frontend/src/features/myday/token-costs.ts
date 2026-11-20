/** My Day's current-day estimate: compact summary, details mounted on demand. */
import { _i18nLang, t } from '../../i18n';
import { createDisclosureSummary, createField } from '../../ui';

const LABELS = {
  input: 'myday.costInput',
  output: 'myday.costOutput',
  cache_read: 'myday.costCacheRead',
  cache_write: 'myday.costCacheWrite',
} as const;
type Bucket = { tokens: number; cost_cny: number };
type ModelQuote = { id: string; name: string; cost_cny: number };

export interface DayTokenCosts {
  date: string;
  buckets: Record<keyof typeof LABELS, Bucket>;
  total_cny: number;
  models: ModelQuote[];
  partial: boolean;
  used_fallback_rates: boolean;
}

function cny(value: number): string {
  return new Intl.NumberFormat(_i18nLang, {
    style: 'currency', currency: 'CNY', currencyDisplay: 'narrowSymbol',
    maximumFractionDigits: value > 0 && value < 0.01 ? 6 : 2,
  }).format(value);
}

function note(text: string, kind = ''): HTMLParagraphElement {
  const element = document.createElement('p');
  element.className = `myday-token-note ${kind}`.trim();
  element.textContent = text;
  return element;
}

function breakdown(costs: DayTokenCosts): HTMLTableElement {
  const table = document.createElement('table');
  table.className = 'myday-token-ledger';
  table.setAttribute('aria-label', t('myday.costTitle'));
  const header = table.createTHead().insertRow();
  for (const text of [t('myday.costCategory'), t('myday.costTokens'), t('myday.costRecorded')]) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = text;
    header.append(th);
  }
  const body = table.createTBody();
  const count = new Intl.NumberFormat(_i18nLang);
  for (const [key, label] of Object.entries(LABELS)) {
    const bucket = costs.buckets[key as keyof typeof LABELS];
    const row = body.insertRow();
    const name = document.createElement('th');
    name.scope = 'row';
    name.textContent = t(label);
    row.append(name);
    row.insertCell().textContent = count.format(bucket.tokens);
    row.insertCell().textContent = cny(bucket.cost_cny);
  }
  return table;
}

function createComparison() {
  const field = createField({
    label: t('myday.costCompare'), choices: [],
  });
  const select = field.control as HTMLSelectElement;
  const estimate = document.createElement('output');
  estimate.className = 'myday-token-estimate';
  estimate.htmlFor.add(select.id);
  const box = document.createElement('div');
  box.className = 'myday-token-compare';
  const assumptions = note(t('myday.costEstimateNote'));
  assumptions.id = `${select.id}-assumptions`;
  select.setAttribute('aria-describedby', assumptions.id);
  box.append(field.el, estimate, assumptions);
  let models: ModelQuote[] = [];
  const renderEstimate = () => {
    const model = models.find((row) => row.id === select.value);
    estimate.hidden = !model;
    estimate.textContent = model ? t('myday.costEstimate', { amount: cny(model.cost_cny) }) : '';
  };
  select.addEventListener('change', renderEstimate);
  const update = (next: ModelQuote[]) => {
    // Keep the control (and its focus, selection and native popup) in place.
    if (models !== next && (models.length !== next.length || models.some((row, index) =>
      row.id !== next[index].id || row.name !== next[index].name))) {
      const value = select.value;
      select.replaceChildren(new Option(t('myday.costChoose'), ''), ...next.map(({ id, name }) =>
        new Option(name === id ? name : `${name} · ${id}`, id)));
      select.value = next.some(({ id }) => id === value) ? value : '';
    }
    models = next;
    box.hidden = !next.length;
    renderEstimate();
  };
  return { box, update };
}

export function createDayTokenCostDisclosure(element: () => HTMLElement | null) {
  let generation = 0;
  let date = '';
  let view: ReturnType<typeof mount> | undefined;
  let costs: DayTokenCosts | undefined;

  function mount(host: HTMLElement) {
    const disclosure = document.createElement('details');
    disclosure.className = 'myday-token-costs';
    const summary = createDisclosureSummary(document);
    summary.classList.add('myday-token-summary');
    const labelGroup = document.createElement('span');
    labelGroup.className = 'myday-token-label';
    const label = document.createElement('span');
    label.textContent = t('myday.costTitle');
    const status = document.createElement('span');
    status.className = 'myday-token-status';
    status.setAttribute('role', 'status');
    labelGroup.append(label, status);
    const amount = document.createElement('span');
    amount.className = 'myday-token-total';
    summary.append(labelGroup, amount);
    const body = document.createElement('div');
    body.className = 'myday-token-body';
    body.hidden = true;
    const ledger = document.createElement('div');
    body.append(ledger);
    disclosure.append(summary, body);
    disclosure.addEventListener('toggle', renderDetails);
    host.replaceChildren(disclosure);
    return { disclosure, amount, status, body, ledger, rendered: undefined as DayTokenCosts | undefined,
      comparison: undefined as ReturnType<typeof createComparison> | undefined };
  }

  function renderDetails(): void {
    if (!view?.disclosure.open || !costs || view.rendered === costs) return;
    const hasUsage = Object.values(costs.buckets).some(({ tokens, cost_cny }) => tokens > 0 || cost_cny > 0);
    if (hasUsage) {
      view.ledger.replaceChildren(breakdown(costs), note(
        `${t('myday.costCachedOutput')} · ${t('myday.costUnreported')}`));
      if (costs.used_fallback_rates) view.ledger.append(note(t('myday.costFallback')));
    } else {
      view.ledger.replaceChildren();
      if (!costs.partial) view.ledger.append(note(t('myday.costEmpty')));
    }
    if (costs.partial) view.ledger.append(note(t('myday.costPartial'), 'myday-token-partial'));
    if (costs.models.length && !view.comparison) {
      view.comparison = createComparison();
      view.body.append(view.comparison.box);
    }
    if (view.comparison?.box.contains(document.activeElement) && !costs.models.length) {
      view.disclosure.querySelector('summary')?.focus();
    }
    view.comparison?.update(costs.models);
    view.body.hidden = false;
    view.rendered = costs;
  }

  function hide(): void {
    generation++;
    date = '';
    costs = undefined;
    view?.disclosure.remove();
    view = undefined;
  }

  function paint(value: DayTokenCosts): void {
    if (!view) return;
    costs = value;
    view.amount.textContent = `≈ ${cny(value.total_cny)}`;
    renderDetails();
  }

  function restore(value: DayTokenCosts | undefined): void {
    if (!view || costs || value?.date !== date) return;
    paint(value);
  }

  async function show(day: string, pending: Promise<DayTokenCosts | undefined>, cached?: DayTokenCosts): Promise<void> {
    const host = element();
    if (!host) return;
    if (day !== date) hide();
    date = day;
    const request = ++generation;
    view ??= mount(host);
    restore(cached);
    view.status.textContent = t('myday.costLoading');
    const result = await pending.catch(() => undefined);
    if (request !== generation) return;
    if (result?.date !== day) {
      view.status.textContent = t('myday.costUnavailable');
      return;
    }
    paint(result);
    view.status.textContent = t('myday.costRecordedNote');
  }
  return Object.freeze({ show, hide, restore });
}
