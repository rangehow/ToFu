"""Isolated frozen-reference controls, NOT a Python or Rust producer receipt.

Run without application pytest fixtures:
PYTHONPATH=$PWD .venv/bin/python3 -m unittest tests.golden_documents.test_pptx_metadata_projection
"""

import copy
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import golden_documents as gd
from tests.golden_documents import check_rust_document_candidate as full
from tests.golden_documents import pptx_metadata_projection as gate


class MetadataProjectionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.case = next(c for c in gd.load_json(gd.MANIFEST)['cases']
                         if c['id'] == full.CASE_ID)
        self.references = {}
        for key in gate.OP_KEYS:
            expected = Path(gd.CORPUS) / 'expected' / full.CASE_ID / key / 'result.json'
            self.references[key] = json.loads(expected.read_text())

    def output(self, value, name='control'):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        (directory / 'result.json').write_text(json.dumps(value))
        return directory

    def rejected(self, key, value):
        try:
            differences = gate.check_metadata(key, self.output(value))
        except (ValueError, TypeError, KeyError):
            return
        self.assertTrue(differences)

    def test_pinned_fields_and_original_slide_selection(self):
        for key, pages in zip(gate.OP_KEYS, [
                ['pages/01.page', 'pages/02.page', 'pages/03.page'], ['pages/02.page']]):
            with self.subTest(key=key):
                value = gate.project(self.references[key])
                self.assertEqual(value['report']['pages'], len(pages))
                manifest = value['deck']['deck.pptd']
                self.assertEqual(manifest['pages'], pages)
                self.assertEqual(manifest['size'], [959, 540])
                self.assertEqual(manifest['title'], 'pptx_import_fidelity')
                self.assertEqual(manifest['version'], 'v2')
                self.assertEqual(value['report']['theme'], manifest['theme'])
                self.assertEqual(gate.check_metadata(key, self.output(value)), [])

    def test_missing_and_unknown_fields_at_every_selected_level(self):
        for key in gate.OP_KEYS:
            value = gate.project(self.references[key])
            pending = [((), value)]
            while pending:
                path, node = pending.pop()
                if not isinstance(node, dict):
                    continue
                for field in [*node, 'unexpected']:
                    changed = copy.deepcopy(value)
                    target = changed
                    for part in path:
                        target = target[part]
                    if field == 'unexpected':
                        target[field] = None
                    else:
                        del target[field]
                    with self.subTest(key=key, path=path, field=field):
                        self.rejected(key, changed)
                pending.extend(((*path, field), child) for field, child in node.items())

    def test_bad_counts_order_dimensions_types_and_partial_outputs(self):
        for key in gate.OP_KEYS:
            for change in ('count', 'bool', 'float-count', 'size', 'float-size', 'order',
                           'renumber', 'duplicate', 'theme', 'wrong-theme', 'font-type',
                           'empty', 'null', 'full', 'partial', 'skipped'):
                value = gate.project(self.references[key])
                manifest = value['deck']['deck.pptd']
                if change == 'count': value['report']['pages'] += 1
                elif change == 'bool': value['report']['pages'] = True
                elif change == 'float-count': value['report']['pages'] = float(value['report']['pages'])
                elif change == 'size': manifest['size'][0] = 960
                elif change == 'float-size': manifest['size'][0] = 959.0
                elif change == 'order': manifest['pages'] = list(reversed(manifest['pages'])) + ['pages/03.page']
                elif change == 'renumber': manifest['pages'][0] = 'pages/00.page'
                elif change == 'duplicate': manifest['pages'][-1] = manifest['pages'][0] if len(manifest['pages']) > 1 else 'pages/01.page'
                elif change == 'theme': value['report']['theme']['colors']['bg'] = '#000000'
                elif change == 'wrong-theme':
                    for theme in (manifest['theme'], value['report']['theme']): theme['colors']['bg'] = '#000000'
                elif change == 'font-type': manifest['theme']['textStyles']['body']['lineHeight'] = 1
                elif change == 'empty': value = {}
                elif change == 'null': value = None
                elif change == 'full': value = self.references[key]
                elif change == 'partial': del manifest['theme']['textStyles']['title']['fontFamily']
                elif change == 'skipped': value['report']['skipped'] = []
                with self.subTest(key=key, change=change):
                    self.rejected(key, value)

    def test_same_length_wrong_order_and_slide_two_renumbering(self):
        for key in gate.OP_KEYS:
            value = gate.project(self.references[key])
            pages = value['deck']['deck.pptd']['pages']
            pages[:] = pages[::-1] if len(pages) > 1 else ['pages/01.page']
            self.rejected(key, value)

    def test_finite_exact_types_before_shared_tolerance(self):
        for key in gate.OP_KEYS:
            value = gate.project(self.references[key])
            for number in (math.nan, math.inf, -math.inf, True, 1):
                changed = copy.deepcopy(value)
                changed['deck']['deck.pptd']['theme']['textStyles']['body']['lineHeight'] = number
                self.rejected(key, changed)
            for theme in (value['report']['theme'], value['deck']['deck.pptd']['theme']):
                theme['textStyles']['body']['lineHeight'] = math.nextafter(1.5, math.inf)
            self.assertEqual(gate.check_metadata(key, self.output(value)), [])
            for delta, matches in ((0.9e-9, True), (1.1e-9, False)):
                for theme in (value['report']['theme'], value['deck']['deck.pptd']['theme']):
                    theme['textStyles']['body']['lineHeight'] = 1.5 + delta
                differences = gate.check_metadata(key, self.output(value))
                self.assertEqual(not differences, matches)

    def test_optional_python_directory_is_independently_checked_not_trusted(self):
        for key in gate.OP_KEYS:
            frozen = self.references[key]
            candidate = self.output(gate.project(frozen))
            python = self.output(frozen, 'not-a-producer-receipt')
            self.assertEqual(gate.check_metadata(key, candidate, python_dir=python), [])
            divergent = gate.project(copy.deepcopy(frozen))
            divergent['deck']['deck.pptd']['size'][0] = 960
            candidate = self.output(divergent)
            self.assertTrue(gate.check_metadata(key, candidate, python_dir=python))
            changed = copy.deepcopy(frozen)
            changed['deck']['deck.pptd']['size'][0] = 960
            candidate = self.output(gate.project(changed))
            python = self.output(changed, 'not-a-producer-receipt')
            self.assertTrue(gate.check_metadata(key, candidate, python_dir=python))

    def test_excluded_features_and_warnings_are_not_compared_or_fabricated(self):
        for key in gate.OP_KEYS:
            value = copy.deepcopy(self.references[key])
            value['report']['skipped'] = [{'slide': 2, 'shapes': ['unsupported']}]
            value['deck']['pages/02.page'] = {'notes': 'excluded', 'elements': []}
            projected = gate.project(value)
            self.assertNotIn('skipped', projected['report'])
            self.assertEqual(gate.check_metadata(key, self.output(projected)), [])

    def test_full_checker_still_refuses_honest_subset(self):
        subset = self.output(gate.project(self.references[full.OP_KEY]))
        self.assertTrue(full.check_candidate(subset))

    def test_missing_malformed_duplicate_and_escaping_outputs(self):
        key = gate.OP_KEYS[0]
        with self.assertRaises(OSError): gate.check_metadata(key, self.root / 'absent')
        output = self.output({})
        for text in ('{', '{"report":{},"report":{}}', '1e309'):
            (output / 'result.json').write_text(text)
            with self.assertRaises(ValueError): gate.check_metadata(key, output)
        (output / 'result.json').unlink()
        (output / 'result.json').symlink_to(self.root / 'elsewhere.json')
        with self.assertRaisesRegex(ValueError, 'locally contained'):
            gate.check_metadata(key, output)
        with self.assertRaisesRegex(ValueError, 'separate from corpus'):
            gate.check_metadata(key, Path(gd.CORPUS))
        with self.assertRaises(ValueError): gate.check_metadata('doc.extract', output)

    def test_real_input_and_each_expected_digest_fail_before_candidate_read(self):
        source = Path(gd.CORPUS)
        selected = {**self.case, 'operations': [o for o in self.case['operations']
                                               if o['key'] in gate.OP_KEYS]}
        for index, target in enumerate(('input', *gate.OP_KEYS)):
            corpus = self.root / f'integrity-{index}'
            input_path = corpus / self.case['input']
            input_path.parent.mkdir(parents=True)
            shutil.copyfile(source / self.case['input'], input_path)
            for key in gate.OP_KEYS:
                relative = Path('expected') / full.CASE_ID / key
                shutil.copytree(source / relative, corpus / relative)
            with patch.object(gd, 'CORPUS', str(corpus)):
                self.assertIsNone(gd.availability(selected, private_root=''))
                damaged = (input_path if target == 'input' else
                           corpus / 'expected' / full.CASE_ID / target / 'result.json')
                with damaged.open('ab') as handle:
                    handle.write(b' ')
                with self.assertRaisesRegex(ValueError, 'sha256'):
                    gate.check_metadata(gate.OP_KEYS[0], self.root / 'absent')

    def test_both_reference_digests_are_checked_before_candidate_read(self):
        for operation in gate.OP_KEYS:
            def availability(case, private_root):
                self.assertEqual([o['key'] for o in case['operations']], list(gate.OP_KEYS))
                self.assertEqual(private_root, '')
                return f'expected {operation} sha256 differs'
            with patch.object(gd, 'availability', side_effect=availability):
                with self.assertRaisesRegex(ValueError, 'sha256'):
                    gate.check_metadata(gate.OP_KEYS[0], self.root / 'absent')


if __name__ == '__main__':
    unittest.main()
