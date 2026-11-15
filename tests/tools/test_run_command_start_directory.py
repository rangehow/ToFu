"""run_command has one start directory; a leading `cd` is the only way to move.

Spec for "Command start directory" in docs/contracts/TOOL_EXECUTION_POLICY.md:
no directory argument in either schema, a directory argument is refused at
ingest (never dropped), and the judges of where a command runs follow the
command's own literal `cd`.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from lib.project_mod.git_command_scope import leading_cd_directory
from lib.tool_input_repair import ingest_tool_call
from lib.tools.code_exec import CODE_EXEC_TOOL
from lib.tools.contracts import compile_execution_contract_documents
from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

pytestmark = pytest.mark.unit


def test_neither_schema_offers_a_directory_argument():
    for tool in (PROJECT_TOOL_RUN_COMMAND, CODE_EXEC_TOOL):
        properties = tool['function']['parameters']['properties']
        assert not {'working_dir', 'workdir', 'cwd'} & set(properties)
    assert 'starting in the project root' in PROJECT_TOOL_RUN_COMMAND['function']['description']
    standalone = CODE_EXEC_TOOL['function']['description']
    assert "starting in the server's working directory" in standalone
    assert 'project root' not in standalone


@pytest.mark.parametrize('key', ['working_dir', 'workdir', 'cwd'])
def test_ingest_refuses_a_directory_argument_with_the_cd_that_expresses_it(key):
    contracts = compile_execution_contract_documents([PROJECT_TOOL_RUN_COMMAND])
    call = ingest_tool_call(
        {'id': 'call-dir', 'function': {'name': 'run_command', 'arguments': json.dumps(
            {'command': 'rm -rf build', key: 'frontend'})}},
        known_tools={'run_command'}, contract_documents_by_name=contracts,
        emit_audit=False)
    assert call.parse_error is not None and 'NOT executed' in call.parse_error
    assert f'`{key}`' in call.parse_error and '`cd <dir> && `' in call.parse_error
    assert call.contract_error['code'] == 'unknown_arguments'
    assert call.contract_error['path'] == f'$.{key}'


def test_ingest_admits_a_command_that_moves_with_cd():
    contracts = compile_execution_contract_documents([PROJECT_TOOL_RUN_COMMAND])
    call = ingest_tool_call(
        {'id': 'call-cd', 'function': {'name': 'run_command', 'arguments': json.dumps(
            {'command': 'cd frontend && npm test'})}},
        known_tools={'run_command'}, contract_documents_by_name=contracts,
        emit_audit=False)
    assert call.parse_error is None and call.contract_error is None
    assert call.fn_args == {'command': 'cd frontend && npm test'}


def test_leading_cd_directory_follows_only_the_leading_chain(tmp_path):
    base = os.path.realpath(tmp_path)

    def where(command):
        return os.path.relpath(leading_cd_directory(command, base), base)

    assert where('cd a && cd b; python x') == os.path.join('a', 'b')
    assert where('cd a\npython x') == 'a'
    assert where('cd a && cd ../ro && rm x') == 'ro'  # lexical, like `cd -L`
    assert where('cd /abs && make') == os.path.relpath('/abs', base)
    for unmoved in ('python x', 'cd a | python x', 'cd a || python x',
                    '(cd a) && python x', 'cd "$D" && python x', 'echo cd a',
                    "cat > s.sh <<'EOF'\ncd a && make\nEOF", 'make && cd a && rm x'):
        assert where(unmoved) == '.', unmoved


def test_cd_alone_neither_writes_nor_hides_where_relative_writes_land(tmp_path):
    from lib.project_mod.command_analysis import _extract_write_targets, _is_destructive_command

    assert not _is_destructive_command('cd docs && ls -la')
    assert not _is_destructive_command('pushd docs && grep -rn x . && popd')
    assert _is_destructive_command('cd docs && rm -f x')
    assert _is_destructive_command('cd "$(touch y)" && ls')
    assert _is_destructive_command('cd docs > log')
    # The cd moves relative write targets, so undo tracking stays opaque.
    assert _extract_write_targets("cd docs && sed -i 's/a/b/' f", str(tmp_path)) is None


def test_read_only_guard_follows_a_cd_into_a_read_only_root(tmp_path):
    from lib.project_mod.config import clear_conv_state, set_conv_roots
    from lib.project_mod.tools import execute_tool

    primary, reference = tmp_path / 'primary', tmp_path / 'reference'
    primary.mkdir()
    reference.mkdir()
    (reference / 'keep.txt').write_text('reference')
    (primary / 'ref-link').symlink_to(reference, target_is_directory=True)
    conv_id = 'conv-start-directory-ro'
    set_conv_roots(conv_id, str(primary), extras=[str(reference)],
                   readonly_paths=[str(reference)])

    def run(command):
        return execute_tool('run_command', {'command': command}, str(primary), conv_id=conv_id)

    try:
        refused = run(f'cd {reference} && rm -f keep.txt')
        refused_via_link = run('cd ref-link && rm -f keep.txt')
        listed = run(f'cd {reference} && ls')
        script = run(f"cat > s.sh <<'EOF'\ncd {reference} && rm -f keep.txt\nEOF")
    finally:
        clear_conv_state(conv_id)
    assert 'READ-ONLY workspace root' in refused
    assert 'READ-ONLY workspace root' in refused_via_link
    assert (reference / 'keep.txt').read_text() == 'reference'
    assert 'keep.txt' in listed
    # A heredoc body is file content: writing it never enters the root.
    assert 'READ-ONLY' not in script
    assert (primary / 's.sh').read_text() == f'cd {reference} && rm -f keep.txt\n'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX shell shim')
def test_rm_shim_judges_a_relative_operand_by_the_shell_directory(tmp_path):
    from lib.project_mod.run_command import _maybe_wrap_rm_with_trash

    workspace, outside = tmp_path / 'workspace', tmp_path / 'outside'
    (workspace / '.git').mkdir(parents=True)
    outside.mkdir()
    (workspace / 'inside.txt').write_text('inside')
    (outside / 'scratch.txt').write_text('scratch')
    command = _maybe_wrap_rm_with_trash(
        f'cd {outside} && rm -f scratch.txt; cd {workspace} && rm -f inside.txt',
        str(workspace))
    subprocess.run(['sh', '-c', command], cwd=workspace, check=True)

    trash = workspace / '.tofu_trash'
    trashed = {path.name for path in trash.rglob('*') if path.is_file()}
    assert not (outside / 'scratch.txt').exists()
    assert 'scratch.txt' not in trashed  # deleted in place, never copied into the bin
    assert 'inside.txt' in trashed and not (workspace / 'inside.txt').exists()


def test_python_environment_follows_a_leading_cd_inside_the_root(tmp_path):
    from lib.project_mod.run_command import _get_cmd_env

    root = tmp_path / 'root'
    nested = root / 'service'
    venv_bin = nested / '.venv' / 'bin'
    venv_bin.mkdir(parents=True)
    (nested / '.venv' / 'pyvenv.cfg').write_text('home = /usr/bin\n')
    python = venv_bin / 'python'
    python.write_text('#!/bin/sh\n')
    python.chmod(0o755)

    entered = _get_cmd_env(str(root), project_root=str(root), venv_start=str(nested))
    outside = _get_cmd_env(str(root), project_root=str(root), venv_start=str(tmp_path))
    assert entered['VIRTUAL_ENV'] == str(nested / '.venv')
    assert 'VIRTUAL_ENV' not in outside
