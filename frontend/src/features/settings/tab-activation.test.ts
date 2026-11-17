// @vitest-environment jsdom
import {afterEach, describe, expect, it, vi} from 'vitest';

const registered = vi.hoisted(() => ({populate: vi.fn(), destroy: vi.fn(), evaluated: 0}));

vi.mock('./database', async () => {
  const {featureRegistry} = await import('../../feature-registry');
  registered.evaluated += 1;
  featureRegistry._populateDatabaseTab = registered.populate;
  featureRegistry._destroyDatabaseTab = registered.destroy;
  return {};
});

import {featureRegistry} from '../../feature-registry';
import {beginSettingsReadSession, retireSettingsReads} from './panel-read-session';
import {activateIndependentSettingsTab} from './tab-activation';

afterEach(() => retireSettingsReads());

describe('demand-loaded Settings owners', () => {
  it('loads the Database owner on its first visit and then populates it', async () => {
    delete featureRegistry._populateDatabaseTab;
    beginSettingsReadSession();
    activateIndependentSettingsTab('database');
    expect(registered.populate).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(registered.populate).toHaveBeenCalledTimes(1));
    expect(registered.evaluated).toBe(1);
    retireSettingsReads();
    expect(registered.destroy).toHaveBeenCalledTimes(1);
  });

  it('does not populate a loaded owner for a session that has ended', async () => {
    delete featureRegistry._populateDatabaseTab;
    registered.populate.mockClear();
    beginSettingsReadSession();
    activateIndependentSettingsTab('database');
    retireSettingsReads();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(registered.populate).not.toHaveBeenCalled();
  });
});
