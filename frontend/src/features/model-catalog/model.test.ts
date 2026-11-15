import { describe, expect, it } from 'vitest';

import {
  buildVendorGroups,
  countModelKinds,
  filterGroupsByKind,
} from './model';
import type { ModelCatalogDocument, OfficialModel } from './types';

const model = (modelId: string, capabilities: string[], facts: Partial<OfficialModel> = {}): OfficialModel => ({
  creator_id: 'google', model_id: modelId, display_name: modelId, capabilities,
  context_window: 32768, quality_rank: 1, ...facts,
});

function catalog(...models: OfficialModel[]): ModelCatalogDocument {
  return {
    contract_version: 'tofu.model-routing/v2',
    creators: [{ creator_id: 'google', name: 'Google' }],
    models,
  };
}

const official = { currency: 'USD' as const, rates: [{ unit: 'per_image' as const, price: 0.039 }] };
const registered = { currency: 'CNY' as const, rates: [{ unit: 'per_image' as const, price: 0.2 }] };

describe('catalog model kinds', () => {
  it('classifies every row and carries registered-else-official media facts', () => {
    const groups = buildVendorGroups(catalog(
      model('gemini-chat', ['text', 'vision', 'video']),
      model('nano-banana', ['image_gen'], {
        official_media_pricing: official,
        official_media_profile: { aspect_ratios: ['1:1'] },
      }),
      model('nano-banana-owned', ['image_gen'], {
        media_pricing: registered, official_media_pricing: official,
      }),
      model('omni-video', ['video_gen']),
      model('lyria', ['audio_gen']),
    ));
    const rows = new Map(groups.flatMap((group) => group.models).map((row) => [row.modelId, row]));
    expect(rows.get('gemini-chat')?.kind).toBe('chat');
    expect(rows.get('nano-banana')?.kind).toBe('image_generation');
    expect(rows.get('nano-banana')?.mediaPricing).toEqual(official);
    expect(rows.get('nano-banana')?.mediaProfile).toEqual({ aspect_ratios: ['1:1'] });
    expect(rows.get('nano-banana-owned')?.mediaPricing).toEqual(registered);
    expect(rows.get('omni-video')?.kind).toBe('video_generation');
    expect(rows.get('lyria')?.kind).toBe('audio_generation');
    expect([...countModelKinds(groups)].sort()).toEqual([
      ['audio_generation', 1], ['chat', 1], ['image_generation', 2], ['video_generation', 1],
    ]);
  });

  it('uses the injected server taxonomy and filters vendors by kind', () => {
    const groups = buildVendorGroups(
      catalog(model('a', ['text']), model('b', ['hologram_gen'])),
      {},
      (capabilities) => (capabilities.includes('hologram_gen') ? 'hologram_generation' : 'chat'),
    );
    const holograms = filterGroupsByKind(groups, 'hologram_generation');
    expect(holograms.flatMap((group) => group.models.map((row) => row.modelId))).toEqual(['b']);
    expect(filterGroupsByKind(groups, null)).toBe(groups);
    expect(filterGroupsByKind(groups, 'video_generation')).toEqual([]);
  });

  it('attaches measured speed by exact identity and keeps it independent of scores', () => {
    const speed = {
      output_tokens_per_second: 142.2, time_to_first_token_seconds: 1.3,
      time_to_first_answer_token_seconds: 49.4, fast: true,
      aa_name: 'Gemini Flash', aa_slug: 'gemini-flash',
    };
    const groups = buildVendorGroups(
      catalog(model('gemini-flash', ['text']), model('google/gemini-flash', ['text']),
        model('gemini-pro', ['text'])),
      {},
      undefined,
      { 'google::google/gemini-flash': speed },
    );
    const rows = new Map(groups.flatMap((group) => group.models).map((row) => [row.modelId, row]));
    // The canonical bare registration takes the speed its namespaced twin matched.
    expect(rows.get('gemini-flash')?.speed).toEqual(speed);
    expect(rows.get('gemini-flash')?.aa).toBeNull();
    expect(rows.get('gemini-pro')?.speed).toBeNull();
  });

  it('re-derives the kind after semantic twins merge their capabilities', () => {
    const groups = buildVendorGroups(catalog(
      model('veo-x', ['text']),
      model('google/veo-x', ['video_gen']),
    ));
    const [row] = groups.flatMap((group) => group.models);
    expect(row.modelId).toBe('veo-x');
    expect(row.kind).toBe('video_generation');
  });
});
