"""Immutable pre-cursor extraction oracle from a retained Git ancestor.

Only the reference function is compiled; its original names resolve through the
current outline namespace. This keeps scanner/read/rank owners shared while
preventing production promotion from silently replacing the extraction oracle.
"""
import ast
import hashlib
from pathlib import Path
import subprocess

from index import outline

REVISION = 'e9fee0085'
PATH = 'lib/repo_map/outline.py'


def load():
    source = subprocess.check_output(
        ['/usr/bin/git', 'show', f'{REVISION}:{PATH}'],
        cwd=Path(__file__).resolve().parents[2],
    ).decode()
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == '_extract_treesitter_tags')
    namespace = dict(vars(outline))
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, f'{REVISION}:{PATH}', 'exec'), namespace)
    return namespace['_extract_treesitter_tags'], hashlib.sha256(ast.get_source_segment(source, function).encode()).hexdigest()


extract_reference, REFERENCE_SHA256 = load()
