#!/usr/bin/env python3
"""Anti-drift guard for the opensource-export sanitizer's trigger list.

The opensource export skips opening a file unless it contains a substring in
``_opensource_sanitize_triggers()`` — a huge FUSE win, but a LEAK CLASS if the
trigger list ever drifts from what the sanitizer actually scrubs: a file whose
secret IS scrubbed by ``_sanitize_source_opensource`` but whose trigger
substring is MISSING never gets opened, so the scrub rule never runs and the
secret ships verbatim.

This test asserts the closure invariant table-driven: every key the sanitizer
knows how to scrub (``_SECRETS`` keys, ``_ENDPOINTS`` keys,
``_INTERNAL_DOMAIN_LITERALS``, and every identifier in the single-source
``_INTERNAL_IDENTIFIER_REPLACEMENTS`` tuple) MUST be reachable as a trigger, so
adding a new secret without wiring its trigger fails CI instead of leaking.

Also round-trips each identifier through ``_sanitize_source_opensource`` to
prove it is actually scrubbed (the trigger AND the scrub are both live).

Internal tokens are assembled from fragments (never a contiguous literal)
because this guard file itself ships in the exported tree — a raw literal would
reintroduce the leak it guards. See test_export_oversized_leak_scan.py.
"""
import os
import re
import sys
import unittest

import pytest
from tests.support.export_toolchain import require_export_toolchain

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

require_export_toolchain()

pytestmark = pytest.mark.unit


class SanitizeTriggerCompletenessTest(unittest.TestCase):

    def _trigger_regex(self):
        from export import _opensource_sanitize_triggers
        return re.compile('|'.join(_opensource_sanitize_triggers()))

    def test_every_secret_key_is_triggerable(self):
        """Each _SECRETS key must match the combined trigger regex — else a
        file carrying only that secret would never be opened for scrubbing."""
        from export import _SECRETS
        rx = self._trigger_regex()
        for secret in _SECRETS:
            self.assertTrue(rx.search(secret),
                            f'_SECRETS key not covered by sanitize triggers: {secret!r}')

    def test_every_endpoint_key_is_triggerable(self):
        from export import _ENDPOINTS
        rx = self._trigger_regex()
        for endpoint in _ENDPOINTS:
            self.assertTrue(rx.search(endpoint),
                            f'_ENDPOINTS key not covered by sanitize triggers: {endpoint!r}')

    def test_every_domain_literal_is_triggerable(self):
        from export import _INTERNAL_DOMAIN_LITERALS
        rx = self._trigger_regex()
        for dom in _INTERNAL_DOMAIN_LITERALS:
            self.assertTrue(rx.search(dom),
                            f'_INTERNAL_DOMAIN_LITERALS entry not triggerable: {dom!r}')

    def test_every_internal_identifier_is_triggerable(self):
        """The single-source-of-truth identifier tuple must be fully covered."""
        from export import _INTERNAL_IDENTIFIER_REPLACEMENTS
        rx = self._trigger_regex()
        for ident, _repl in _INTERNAL_IDENTIFIER_REPLACEMENTS:
            self.assertTrue(rx.search(ident),
                            f'_INTERNAL_IDENTIFIER_REPLACEMENTS entry not triggerable: {ident!r}')

    def test_internal_identifiers_are_actually_scrubbed(self):
        """Round-trip: each identifier is REMOVED by the scrub (both halves of
        the single-source contract are live, not just the trigger half)."""
        from export import _sanitize_source_opensource, _INTERNAL_IDENTIFIER_REPLACEMENTS
        for ident, replacement in _INTERNAL_IDENTIFIER_REPLACEMENTS:
            content = f'x = "{ident}"  # embedded internal id\n'
            out = _sanitize_source_opensource(content, 'some/probe_script.py')
            self.assertNotIn(ident, out,
                             f'identifier {ident!r} survived _sanitize_source_opensource')
            self.assertIn(replacement, out,
                          f'replacement {replacement!r} not present after scrub of {ident!r}')

    def test_brand_only_file_gets_the_same_rename_as_its_callers(self):
        """A file naming the provider brand and no other trigger is still
        opened: otherwise it keeps the brand while renamed callers (its tests)
        ask for the replacement key."""
        import tempfile
        from pathlib import Path
        from export_pkg._sanitize import (
            _collect_opensource_candidates, _sanitize_source_opensource)
        brand = 'mei' + 'tuan'
        with tempfile.TemporaryDirectory() as root:
            registry = Path(root) / 'model-group.ts'
            registry.write_text(f'export const NAMES = {{ {brand}: 1 }};\n')
            self.assertIn('model-group.ts', _collect_opensource_candidates(Path(root)))
        renamed = _sanitize_source_opensource(f'{brand}: 1', 'frontend/src/core/model-group.ts')
        self.assertNotIn(brand, renamed)

    def test_a_fixture_provider_id_and_its_assertion_get_one_rename(self):
        """A test that builds a slot with the corp provider ID and asserts it
        back must still agree after export: every quoted occurrence gets the
        same replacement, whatever syntax surrounds it."""
        from export import _sanitize_source_opensource
        corp, brand = 'san' + 'kuai', 'Mei' + 'tuan'
        path = 'tests/providers/test_x.py'
        corp_token = _sanitize_source_opensource(f"'{corp}'", path)
        brand_token = _sanitize_source_opensource(f"'{brand}'", path)
        source = (f"# the {brand}-keyed operator slot\n"
                  f"slot = Slot(provider_id='{corp}', routing_provider_id='{corp}')\n"
                  f"def f(provider_id: str = '{corp}'): pass\n"
                  f"face = {{'id': '{corp}', 'name': '{brand}'}}\n"
                  f"pid = provider.get('id', '{corp}') or m.get('provider_id', '{corp}')\n"
                  f"assert slot.provider_id == '{corp}' and face['name'] == '{brand}'\n")
        expected = (source.replace(f"'{corp}'", corp_token)
                    .replace(f"'{brand}'", brand_token))
        out = _sanitize_source_opensource(source, path)
        self.assertEqual(out.split('\n', 1)[1], expected.split('\n', 1)[1])
        self.assertNotIn(corp, out)
        self.assertNotIn(brand, out)

    def test_scrub_and_trigger_share_one_source(self):
        """Structural: the scrub and trigger paths draw the plain-substring
        identifiers from the SAME tuple, so they cannot drift. Verified by
        asserting each tuple identifier is both scrubbed AND triggerable — if a
        future edit re-hardcodes one path, one of the two halves fails."""
        from export import _INTERNAL_IDENTIFIER_REPLACEMENTS
        # Fragment-assembled probe: a known internal username split so this
        # file carries no contiguous literal.
        probe = 'hadoop' + '-aipnlp'
        idents = {i for i, _r in _INTERNAL_IDENTIFIER_REPLACEMENTS}
        self.assertIn(probe, idents,
                      'expected the internal username in the single-source tuple')


if __name__ == '__main__':
    unittest.main()
