"""Generated-metadata-only handoff consistency specification; NOT a release gate."""
from __future__ import annotations

from copy import deepcopy
import unittest


FROZEN_CANDIDATE = 'a28b2b7bf4cc28b981720fbf36e535ea7034fc5c'
KNOWN_MANIFEST_ONLY = {
    'candidate': FROZEN_CANDIDATE,
    'generation': 313671,
    'cut': 5278467,
    'manifest_sha256': '9d92db4c6084d9110367a4746decb12584e59bb27c35ae2436cc0615d05e9a9a',
}
BINDING_TYPES = {
    'candidate': str, 'tenant': str, 'owner': str, 'authority_uuid': str,
    'generation': int, 'cut': int, 'manifest_sha256': str,
    'source_storage': str, 'custody_storage': str, 'target': str, 'restore_run': str,
    'attachment_inventory_sha256': str, 'ack_sequence': int, 'ack_frontier_sha256': str,
    'engine_build_sha256': str,
}
REQUIRED = {
    'graph': {'coverage': 'full-reachable-graph', 'retention': 'independent-immutable'},
    'attachments': {'coverage': 'exact-inventory', 'custody': 'verified'},
    'restore': {'selector': 'exact', 'mode': 'full', 'outcome': 'completed'},
    'verify': {'verdict': 'ok', 'missing_blocks': 0, 'coverage': 'full'},
    'ack': {'coverage': 'restored-acknowledged-frontier', 'outcome': 'equal'},
}


def check_handoff(binding, evidence_refs, authenticated_observations):
    """Check joins of externally authenticated observations, not their truth.

    The caller must authenticate and independently retain observations OUTSIDE
    this fixture. Passing arbitrary JSON as that mapping proves nothing.
    No parser, count, status string or digest authenticates its own provenance.
    """
    if not isinstance(binding, dict) or set(binding) != set(BINDING_TYPES):
        return 'refuse:binding-fields'
    if any(type(binding[key]) is not kind for key, kind in BINDING_TYPES.items()):
        return 'refuse:binding-types'
    if any(not value for value in binding.values()):
        return 'refuse:empty-binding'
    if binding['candidate'] != FROZEN_CANDIDATE:
        return 'refuse:frozen-scope'
    if all(binding[key] == value for key, value in KNOWN_MANIFEST_ONLY.items()):
        return 'blocked:8605-manifest-only'
    if (binding['generation'] < 1 or binding['cut'] < 1
            or not 0 < binding['ack_sequence'] <= binding['cut']):
        return 'refuse:ack-outside-cut'
    if binding['source_storage'] == binding['custody_storage']:
        return 'refuse:same-storage'
    if binding['target'] in (binding['source_storage'], binding['custody_storage']):
        return 'refuse:target-storage-alias'
    if not isinstance(evidence_refs, dict) or set(evidence_refs) != set(REQUIRED):
        return 'refuse:required-evidence'
    refs = list(evidence_refs.values())
    if any(type(ref) is not str or not ref for ref in refs) or len(set(refs)) != len(refs):
        return 'refuse:evidence-identities'
    for stage, required in REQUIRED.items():
        observation = authenticated_observations.get(evidence_refs[stage])
        if not isinstance(observation, dict) or set(observation) != {'binding', 'result'}:
            return f'refuse:{stage}-authentication'
        observed_binding = observation['binding']
        if (not isinstance(observed_binding, dict)
                or any(type(observed_binding.get(k)) is not t for k, t in BINDING_TYPES.items())
                or observed_binding != binding):
            return f'refuse:{stage}-identity'
        result = observation['result']
        if (not isinstance(result, dict) or result != required
                or any(type(result.get(k)) is not type(v) for k, v in required.items())):
            return f'refuse:{stage}-coverage'
    return 'fixture-consistent:not-release-proof'


def generated_case():
    binding = {
        'candidate': FROZEN_CANDIDATE, 'tenant': 'generated-tenant',
        'owner': 'generated-owner', 'authority_uuid': '00000000-0000-4000-8000-000000000001',
        'generation': 17, 'cut': 23, 'manifest_sha256': '1' * 64,
        'source_storage': 'generated-source', 'custody_storage': 'generated-independent-copy',
        'target': 'generated-target', 'restore_run': 'generated-run',
        'attachment_inventory_sha256': '2' * 64, 'ack_sequence': 22,
        'ack_frontier_sha256': '3' * 64, 'engine_build_sha256': '4' * 64,
    }
    refs = {stage: f'generated-observation-{stage}' for stage in REQUIRED}
    observations = {refs[stage]: {'binding': deepcopy(binding), 'result': deepcopy(result)}
                    for stage, result in REQUIRED.items()}
    return binding, refs, observations


class HandoffAcceptanceFixture(unittest.TestCase):
    def test_full_generated_custody_restore_and_ack_are_consistent(self):
        self.assertEqual(check_handoff(*generated_case()), 'fixture-consistent:not-release-proof')

    def test_known_cut_remains_blocked_even_with_invented_positive_claims(self):
        binding, refs, observations = generated_case()
        binding.update(KNOWN_MANIFEST_ONLY)
        for observation in observations.values():
            observation['binding'] = deepcopy(binding)
        self.assertEqual(check_handoff(binding, refs, observations), 'blocked:8605-manifest-only')
        self.assertEqual(check_handoff(binding, {}, {}), 'blocked:8605-manifest-only')

    def test_manifest_catalog_and_smoke_are_not_full_evidence(self):
        binding, _, _ = generated_case()
        for weak in ({'manifest': 'valid'}, {'catalog_count': 1000000}, {'smoke': 'ok'}):
            with self.subTest(weak=weak):
                self.assertEqual(check_handoff(binding, weak, {}), 'refuse:required-evidence')
        binding, refs, observations = generated_case()
        observations[refs['restore']]['result']['mode'] = 'smoke'
        self.assertEqual(check_handoff(binding, refs, observations), 'refuse:restore-coverage')

    def test_every_cross_identity_mismatch_refuses_at_every_stage(self):
        for stage in REQUIRED:
            for field, kind in BINDING_TYPES.items():
                with self.subTest(stage=stage, field=field):
                    binding, refs, observations = generated_case()
                    other = observations[refs[stage]]['binding']
                    other[field] = other[field] + 1 if kind is int else other[field] + '-foreign'
                    self.assertEqual(check_handoff(binding, refs, observations),
                                     f'refuse:{stage}-identity')

    def test_target_cannot_alias_source_or_custody_storage(self):
        for storage in ('source_storage', 'custody_storage'):
            with self.subTest(storage=storage):
                binding, refs, observations = generated_case()
                binding['target'] = binding[storage]
                for observation in observations.values():
                    observation['binding'] = deepcopy(binding)
                self.assertEqual(check_handoff(binding, refs, observations),
                                 'refuse:target-storage-alias')

    def test_restored_ack_below_required_frontier_refuses_with_sufficient_cut(self):
        binding, refs, observations = generated_case()
        self.assertEqual((binding['cut'], binding['ack_sequence']), (23, 22))
        observations[refs['ack']]['binding']['ack_sequence'] = 21
        self.assertEqual(check_handoff(binding, refs, observations), 'refuse:ack-identity')

    def test_each_stage_requires_its_own_authenticated_observation(self):
        for stage in REQUIRED:
            with self.subTest(stage=stage):
                binding, refs, observations = generated_case()
                del observations[refs[stage]]
                self.assertEqual(check_handoff(binding, refs, observations),
                                 f'refuse:{stage}-authentication')
                binding, refs, observations = generated_case()
                del refs[stage]
                self.assertEqual(check_handoff(binding, refs, observations),
                                 'refuse:required-evidence')
        binding, refs, observations = generated_case()
        refs['ack'] = refs['verify']
        self.assertEqual(check_handoff(binding, refs, observations), 'refuse:evidence-identities')

    def test_partial_or_malformed_coverage_refuses(self):
        for stage, requirements in REQUIRED.items():
            for field, value in requirements.items():
                with self.subTest(stage=stage, field=field):
                    binding, refs, observations = generated_case()
                    observations[refs[stage]]['result'][field] = 1 if type(value) is int else 'unknown'
                    self.assertEqual(check_handoff(binding, refs, observations),
                                     f'refuse:{stage}-coverage')
        binding, refs, observations = generated_case()
        observations[refs['verify']]['result']['missing_blocks'] = False
        self.assertEqual(check_handoff(binding, refs, observations), 'refuse:verify-coverage')

    def test_missing_extra_and_wrong_type_identity_fields_refuse(self):
        for field in BINDING_TYPES:
            for mutation in ('missing', 'extra', 'type'):
                with self.subTest(field=field, mutation=mutation):
                    binding, refs, observations = generated_case()
                    observed = observations[refs['ack']]['binding']
                    if mutation == 'missing':
                        del observed[field]
                    elif mutation == 'extra':
                        observed['unbound_identity'] = 'foreign'
                    else:
                        observed[field] = None
                    self.assertEqual(check_handoff(binding, refs, observations),
                                     'refuse:ack-identity')

    def test_expected_scope_storage_and_ack_boundaries_refuse(self):
        for field, value, reason in (
            ('candidate', 'foreign-candidate', 'frozen-scope'),
            ('custody_storage', 'generated-source', 'same-storage'),
            ('ack_sequence', 24, 'ack-outside-cut'),
            ('ack_sequence', 0, 'empty-binding'),
            ('ack_sequence', True, 'binding-types'),
            ('owner', '', 'empty-binding'),
        ):
            with self.subTest(field=field, value=value):
                binding, refs, observations = generated_case()
                binding[field] = value
                self.assertEqual(check_handoff(binding, refs, observations), f'refuse:{reason}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
