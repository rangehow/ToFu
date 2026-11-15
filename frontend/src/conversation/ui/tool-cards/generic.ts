/**
 * Generic structured view for any JSON-object tool result or argument set:
 * labeled fields, multi-line text collapsed, nested values indented, raw
 * JSON only inside the technical disclosure. Labels are the data's own keys
 * made readable; every authored word comes from the translator.
 * Entry points: genericFieldsHtml, readableKey.
 */
import type { Translator } from '../../../i18n';

import { collapsibleHtml, escape, fieldHtml } from './markup';
import { isRecord, type UnknownRecord } from './values';

/** `next_step` / `nextStep` → `Next step`: the key itself, not authored copy. */
export function readableKey(key: string): string {
  const words = key.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_-]+/g, ' ').trim().toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : key;
}

function scalarHtml(value: unknown, t: Translator): string {
  if (typeof value === 'boolean') return escape(t(value ? 'toolCard.generic.yes' : 'toolCard.generic.no'));
  if (typeof value === 'number') return escape(String(value));
  if (typeof value === 'string') return escape(value);
  return '';
}

function valueHtml(key: string, value: unknown, t: Translator, markdown: (source: string) => string): string {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'string' && value.includes('\n')) {
    return collapsibleHtml(t('toolCard.generic.showText', { lines: value.split('\n').length }), markdown(value));
  }
  if (Array.isArray(value)) {
    if (!value.length) return escape(t('toolCard.generic.empty'));
    const items = value.map((item) => isRecord(item)
      ? `<li>${genericFieldsHtml(item, t, markdown)}</li>`
      : `<li>${valueHtml(key, item, t, markdown)}</li>`).join('');
    return `<ul class="ptool-coord-list">${items}</ul>`;
  }
  if (isRecord(value)) {
    const inner = genericFieldsHtml(value, t, markdown);
    return inner ? `<div class="ptool-coord-nested">${inner}</div>` : '';
  }
  return scalarHtml(value, t);
}

export function genericFieldsHtml(
  value: UnknownRecord, t: Translator, markdown: (source: string) => string,
): string {
  return Object.entries(value)
    .map(([key, item]) => fieldHtml(readableKey(key), valueHtml(key, item, t, markdown)))
    .join('');
}
