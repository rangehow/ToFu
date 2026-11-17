/** Localized labels of the devices vocabulary: one literal i18n key per value, so the catalog gate sees each. */
import { t } from '../../i18n';
import { ageParts, type DefaultKind, type DeviceKind, type DeviceState, type Tier } from './model';

export function kindGroupLabel(kind: DeviceKind): string {
  switch (kind) {
    case 'self': return t('devicesPage.kind.self');
    case 'computer': return t('devicesPage.kind.computer');
    case 'server': return t('devicesPage.kind.server');
    case 'browser': return t('devicesPage.kind.browser');
  }
}

export function kindShortLabel(kind: DeviceKind): string {
  switch (kind) {
    case 'self': return t('devicesPage.kindShort.self');
    case 'computer': return t('devicesPage.kindShort.computer');
    case 'server': return t('devicesPage.kindShort.server');
    case 'browser': return t('devicesPage.kindShort.browser');
  }
}

export function defaultKindLabel(kind: DefaultKind): string {
  switch (kind) {
    case 'computer': return t('devicesPage.defaultKind.computer');
    case 'server': return t('devicesPage.defaultKind.server');
    case 'browser': return t('devicesPage.defaultKind.browser');
  }
}

export function stateLabel(state: DeviceState): string {
  switch (state) {
    case 'online': return t('devicesPage.state.online');
    case 'offline': return t('devicesPage.state.offline');
    case 'never': return t('devicesPage.state.never');
    case 'pending': return t('devicesPage.state.pending');
    case 'conflict': return t('devicesPage.state.conflict');
  }
}

export function routeLabel(route: string): string {
  switch (route) {
    case 'local': return t('devicesPage.route.local');
    case 'direct': return t('devicesPage.route.direct');
    case 'internal': return t('devicesPage.route.internal');
    case 'browser_relay': return t('devicesPage.route.browserRelay');
    case 'ssh': return t('devicesPage.route.ssh');
    case 'extension': return t('devicesPage.route.extension');
    case 'home_relay': return t('devicesPage.route.homeRelay');
    default: return route || t('devicesPage.route.unknown');
  }
}

export function tierLabel(tier: Tier): string {
  switch (tier) {
    case 'read': return t('devicesPage.tier.read');
    case 'write': return t('devicesPage.tier.write');
    case 'exec': return t('devicesPage.tier.exec');
    case 'gui': return t('devicesPage.tier.gui');
    case 'egress': return t('devicesPage.tier.egress');
    case 'codex_login': return t('devicesPage.tier.codexLogin');
  }
}

export function tierLongLabel(tier: Tier): string {
  switch (tier) {
    case 'read': return t('devicesPage.tierLong.read');
    case 'write': return t('devicesPage.tierLong.write');
    case 'exec': return t('devicesPage.tierLong.exec');
    case 'gui': return t('devicesPage.tierLong.gui');
    case 'egress': return t('devicesPage.tierLong.egress');
    case 'codex_login': return t('devicesPage.tierLong.codexLogin');
  }
}

export function ageLabel(thenMs: number, nowMs: number): string {
  const { unit, value } = ageParts(thenMs, nowMs);
  const n = String(value);
  switch (unit) {
    case 's': return t('devicesPage.age.seconds', { n });
    case 'm': return t('devicesPage.age.minutes', { n });
    case 'h': return t('devicesPage.age.hours', { n });
    case 'd': return t('devicesPage.age.days', { n });
  }
}
