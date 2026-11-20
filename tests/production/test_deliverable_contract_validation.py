"""Inert public JSON contract validation; no preflight, recipes or CLI execution.

Also runnable with unittest in a disposable TOFU_DATA_DIR without pytest boot.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lib.production.deliverable_launch import ContractError, load_contract


class DeliverableContractValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def load_json(self, document):
        return load_contract(json.loads(json.dumps(document)), base_dir=self.base)

    def definition(self, kind):
        return {
            'schema_version': 1, 'kind': kind, 'output_dir': 'out',
            'inputs': {'material': 'material.md'} if kind == 'slides' else {'doc_dir': 'paper'},
            'parameters': ({'topic': 'Case-sensitive topic', 'lang': 'zh', 'max_pages': 3}
                           if kind == 'slides' else {'draft': 'main.tex'}),
            'model': {'name': 'inert-model'}, 'budget': {'deadline_s': 12},
            'owner_user_id': 7, 'required_tools': [{'executable': 'inert-tool'}],
        }

    def test_valid_slides_and_document_are_unchanged(self):
        for kind, role, relative in [('slides', 'material', 'material.md'),
                                     ('document', 'doc_dir', 'paper')]:
            with self.subTest(kind=kind):
                document = self.definition(kind)
                expected = dict(document, inputs={role: str(self.base / relative)},
                                output_dir=str(self.base / 'out'))
                self.assertEqual(self.load_json(document), expected)
                self.assertEqual(document, self.definition(kind))
                self.assertEqual(list(self.base.iterdir()), [])

    def test_kind_requires_an_exact_supported_string(self):
        for kind in [[], ['slides'], {}, {'name': 'slides'}, None, True, 1, 1.0,
                     '', 'video', 'Slides', 'DOCUMENT', ' slides', 'document ']:
            for valid_kind in ['slides', 'document']:
                with self.subTest(kind=kind, definition=valid_kind):
                    document = dict(self.definition(valid_kind), kind=kind)
                    with self.assertRaises(ContractError) as caught:
                        self.load_json(document)
                    self.assertEqual(str(caught.exception),
                                     "kind must be one of ['document', 'slides']")

    def test_missing_kind_has_the_existing_error(self):
        document = self.definition('slides')
        del document['kind']
        with self.assertRaises(ContractError) as caught:
            self.load_json(document)
        self.assertEqual(str(caught.exception), "kind must be one of ['document', 'slides']")

    def test_schema_version_requires_exact_int_one(self):
        for version in [True, False, 1.0, 0.0, '1', None, [], {}, 0, 2, -1]:
            for kind in ['slides', 'document']:
                with self.subTest(version=version, kind=kind):
                    with self.assertRaises(ContractError) as caught:
                        self.load_json(dict(self.definition(kind), schema_version=version))
                    self.assertEqual(str(caught.exception), 'schema_version must be 1')
        for kind in ['slides', 'document']:
            with self.subTest(valid_kind=kind):
                contract = self.load_json(self.definition(kind))
                self.assertIs(type(contract['schema_version']), int)
                self.assertEqual(contract['schema_version'], 1)

    def test_missing_schema_version_has_the_existing_error(self):
        document = self.definition('document')
        del document['schema_version']
        with self.assertRaises(ContractError) as caught:
            self.load_json(document)
        self.assertEqual(str(caught.exception), 'schema_version must be 1')


if __name__ == '__main__':
    unittest.main()
