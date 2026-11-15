"""Path-scoped rules stay complete and isolated without blocking execution."""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# A rule file the symlink specs create in their project, not a repository file.
_AUDIT_SYNTHETIC_REPO_PATHS = {'docs/RULES.md'}


def _project(tmp_path):
    project = tmp_path / 'project'
    target = project / 'src' / 'sub' / 'new.py'
    target.parent.mkdir(parents=True)
    (tmp_path / 'AGENTS.md').write_text('foreign server rules')
    (project / 'AGENTS.md').write_text('root rules')
    (project / 'src' / 'AGENTS.md').write_text('src rules')
    (project / 'src' / 'sub' / 'AGENTS.md').write_text('sub rules')
    sibling = project / 'other'
    sibling.mkdir()
    (sibling / 'AGENTS.md').write_text('sibling rules')
    return project, target


def test_nearest_guidance_chain_excludes_siblings_and_server_ancestors(tmp_path):
    from lib.project_mod.instructions import read_scoped_guidance, render_guidance
    project, target = _project(tmp_path)
    rows = read_scoped_guidance(str(project), str(target))
    assert [row['content'] for row in rows] == ['root rules', 'src rules', 'sub rules']
    rendered = render_guidance(rows)
    assert 'foreign server rules' not in rendered and 'sibling rules' not in rendered
    assert str(project / 'src' / 'sub' / 'AGENTS.md') in rendered
    assert [row['content'] for row in read_scoped_guidance(str(project), str(project))] == ['root rules']


def test_target_beneath_a_regular_file_has_no_nested_guidance(tmp_path):
    # A linked worktree's `.git` is a file; shell targets such as
    # `.git/info/exclude` pass through it. No rule can exist beneath a file.
    from lib.project_mod.instructions import read_scoped_guidance
    project, _ = _project(tmp_path)
    (project / '.git').write_text('gitdir: /elsewhere\n')
    rows = read_scoped_guidance(str(project), str(project / '.git' / 'info' / 'exclude'))
    assert [row['content'] for row in rows] == ['root rules']


def test_root_and_extra_root_guidance_are_labeled_not_inferred(tmp_path):
    from lib.project_mod.config import build_root_policy, execution_root_scope
    from lib.project_mod.indexer import get_context_for_prompt
    project, _ = _project(tmp_path)
    reference = tmp_path / 'reference'
    reference.mkdir()
    (reference / 'AGENTS.md').write_text('reference rules')
    with execution_root_scope('owned', build_root_policy(str(project), [str(reference)], [str(reference)])):
        rendered = get_context_for_prompt(str(project), conv_id='owned')
    assert 'reference rules' in rendered and 'root rules' in rendered
    assert f'Applies only within: {reference}' in rendered
    assert 'src rules' not in rendered


def test_outside_targets_fail_closed_and_escaping_rules_are_never_read(tmp_path):
    from lib.project_mod.instructions import read_scoped_guidance
    project, target = _project(tmp_path)
    with pytest.raises(PermissionError):
        read_scoped_guidance(str(project), str(tmp_path / 'foreign.py'))
    guide = project / 'src' / 'sub' / 'AGENTS.md'
    guide.unlink()
    guide.symlink_to(tmp_path / 'AGENTS.md')
    rows = read_scoped_guidance(str(project), str(target))
    assert [row.get('skipped') for row in rows] == [None, None, 'outside_root']
    assert 'foreign server rules' not in str(rows)


def test_partial_read_failure_is_not_absence(tmp_path, monkeypatch):
    from lib.project_mod.instructions import read_scoped_guidance
    project, target = _project(tmp_path)
    import os
    original = os.open
    def fail(path, flags, *args, **kwargs):
        directory = kwargs.get('dir_fd')
        if (path == 'AGENTS.md' and directory is not None and
                Path(f'/proc/self/fd/{directory}').resolve() == project / 'src'):
            raise PermissionError(13, 'Permission denied')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', fail)
    rows = read_scoped_guidance(str(project), str(target))
    assert [(Path(row['path']).parent, row.get('skipped')) for row in rows] == [
        (project, None), (project / 'src', 'unreadable'), (project / 'src' / 'sub', None)]


def test_in_root_symlinked_rule_files_are_read(tmp_path):
    from lib.project_mod.instructions import read_scoped_guidance, render_guidance
    project, target = _project(tmp_path)
    (project / 'CLAUDE.md').symlink_to('AGENTS.md')
    shared = project / 'docs' / 'SRC_RULES.md'
    shared.parent.mkdir()
    shared.write_text('shared src rules')
    (project / 'src' / 'AGENTS.md').unlink()
    (project / 'src' / 'AGENTS.md').symlink_to('../docs/SRC_RULES.md')
    rows = read_scoped_guidance(str(project), str(target))
    assert [(row['path'], row['scope'], row['content']) for row in rows] == [
        (str(project / 'CLAUDE.md'), str(project), 'root rules'),
        (str(project / 'AGENTS.md'), str(project), 'root rules'),
        (str(project / 'src' / 'AGENTS.md'), str(project / 'src'), 'shared src rules'),
        (str(project / 'src' / 'sub' / 'AGENTS.md'), str(project / 'src' / 'sub'), 'sub rules')]
    assert 'skipped' not in render_guidance(rows)


def _unusable(kind, project, tmp_path, monkeypatch):
    """Make the root CLAUDE.md a rule file of KIND that cannot be read."""
    import os
    rule = project / 'CLAUDE.md'
    if kind == 'outside_root':
        rule.symlink_to(tmp_path / 'AGENTS.md')
    elif kind == 'dangling_symlink':
        rule.symlink_to('missing-shared-rules.md')
    elif kind == 'symlink_loop':
        rule.symlink_to('LOOP.md')
        (project / 'LOOP.md').symlink_to('CLAUDE.md')
    else:
        rule.write_text('unreadable rules')
        original = os.open
        def deny(path, flags, *args, **kwargs):
            if path == 'CLAUDE.md' and kwargs.get('dir_fd') is not None:
                raise PermissionError(13, 'Permission denied')
            return original(path, flags, *args, **kwargs)
        monkeypatch.setattr(os, 'open', deny)
    return rule


@pytest.mark.parametrize('kind', ['outside_root', 'dangling_symlink', 'symlink_loop', 'unreadable'])
def test_unusable_rule_file_is_a_typed_visible_notice_and_a_warning(tmp_path, monkeypatch, caplog, kind):
    import logging
    from lib.project_mod.config import build_root_policy
    from lib.project_mod.instructions import read_root_guidance, render_guidance
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, target = _project(tmp_path)
    rule = _unusable(kind, project, tmp_path, monkeypatch)
    notice = f'project rule file {rule} skipped: {kind} ('
    task = {'id': f'unusable-rule-{kind}', '_userId': 7, 'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    messages = [{'role': 'user', 'content': 'implement'}]
    caplog.set_level(logging.WARNING)

    refresh_project_guidance(task, messages, model='')

    visible = str(messages)
    assert notice in visible and 'root rules' in visible
    assert 'foreign server rules' not in visible and 'unreadable rules' not in visible
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert sum(notice in message for message in warnings) == 1
    # Later accesses keep the notice current without failing or warning again.
    assert check_project_guidance(task, 'write_file', {'path': str(target)}) == ''
    refresh_project_guidance(task, messages, model='')
    assert str(messages).count(notice) == 1
    assert sum(notice in record.getMessage() for record in caplog.records) == 1
    caplog.clear()
    standalone = render_guidance(read_root_guidance(str(project)))
    assert notice in standalone and 'root rules' in standalone
    assert [record.levelno for record in caplog.records if notice in record.getMessage()] == [
        logging.WARNING]


@pytest.mark.parametrize('swapped', ['rule', 'directory'])
def test_a_symlink_swapped_during_the_read_cannot_read_outside_the_root(tmp_path, monkeypatch, swapped):
    import os
    from lib.project_mod.instructions import read_scoped_guidance
    project, target = _project(tmp_path)
    shared = project / 'docs' / 'RULES.md'
    shared.parent.mkdir()
    shared.write_text('in-root shared rules')
    (project / 'CLAUDE.md').symlink_to('docs/RULES.md')
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'RULES.md').write_text('foreign server rules')
    original = os.open
    def swap(path, flags, *args, **kwargs):
        name = 'RULES.md' if swapped == 'rule' else 'docs'
        if path == name and kwargs.get('dir_fd') is not None and not os.path.islink(project / 'docs'):
            # Validation resolved CLAUDE.md inside the root; replace the
            # resolved file (or its directory) with a link out of it.
            if swapped == 'rule':
                shared.unlink()
                shared.symlink_to(foreign / 'RULES.md')
            else:
                shared.unlink()
                shared.parent.rmdir()
                shared.parent.symlink_to(foreign, target_is_directory=True)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swap)

    rows = read_scoped_guidance(str(project), str(target))

    claude = next(row for row in rows if row['path'] == str(project / 'CLAUDE.md'))
    assert claude['skipped'] == 'changed_during_read' and claude['content'] == ''
    assert 'foreign server rules' not in str(rows)
    assert [row['content'] for row in rows if row is not claude] == ['root rules', 'src rules', 'sub rules']


def test_rule_change_is_fingerprinted_and_oversize_never_clipped(tmp_path):
    from lib.project_mod.instructions import read_root_guidance, MAX_GUIDANCE_BYTES
    project, _ = _project(tmp_path)
    before = read_root_guidance(str(project))[0]['hash']
    (project / 'AGENTS.md').write_text('updated root rules')
    assert read_root_guidance(str(project))[0]['hash'] != before
    (project / 'AGENTS.md').write_text('x' * (MAX_GUIDANCE_BYTES + 1))
    with pytest.raises(ValueError): read_root_guidance(str(project))


def test_mutation_proceeds_while_guidance_refreshes_on_change_and_removal(tmp_path):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, target = _project(tmp_path)
    task = {'id': 'scoped-rule-task', 'convId': 'scope-chat', '_userId': 7,
            'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    args = {'path': str(target), 'content': 'new code'}
    assert check_project_guidance(task, 'write_file', args) == ''
    assert not target.exists()
    messages = [{'role': 'user', 'content': 'implement'}]
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' in str(messages)
    assert check_project_guidance(task, 'write_file', args) == ''
    guide = project / 'src' / 'sub' / 'AGENTS.md'
    guide.write_text('updated subtree rules')
    assert check_project_guidance(task, 'write_file', args) == ''
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'write_file', args) == ''
    guide.unlink()
    assert check_project_guidance(task, 'write_file', args) == ''
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'write_file', args) == ''


def test_open_tool_batch_defers_guidance_without_blocking_mutation(tmp_path):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, target = _project(tmp_path)
    task = {'id': 'scope-open-task', 'convId': 'scope-chat', '_userId': 7,
            'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    args = {'path': str(target)}
    assert check_project_guidance(task, 'write_file', args) == ''
    messages = [{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'unfinished', 'type': 'function', 'function': {'name': 'read_files', 'arguments': '{}'}}]}]
    refresh_project_guidance(task, messages, model='')
    assert len(messages) == 1
    assert check_project_guidance(task, 'write_file', args) == ''


@pytest.mark.parametrize('command', ['printf changed > src/sub/new.py', 'cd src/sub && printf changed > new.py'])
def test_shell_target_acquires_nested_guidance_without_blocking(tmp_path, command):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, _ = _project(tmp_path)
    task = {'id': 'shell-guidance', 'convId': 'shell-chat', '_userId': 7,
            'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    messages = [{'role': 'user', 'content': 'work'}]
    assert check_project_guidance(task, 'run_command', {'command': 'git status'}) == ''
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' not in str(messages)
    args = {'command': command}
    assert check_project_guidance(task, 'run_command', args) == ''
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' in str(messages)
    assert check_project_guidance(task, 'run_command', args) == ''


def test_two_sibling_guides_retain_subtree_applicability(tmp_path):
    from lib.project_mod.instructions import read_scoped_guidance, render_guidance
    project, target = _project(tmp_path)
    rows = read_scoped_guidance(str(project), str(target))
    other = read_scoped_guidance(str(project), str(project / 'other' / 'new.py'))
    combined = list({row['path']: row for row in [*rows, *other]}.values())
    rendered = render_guidance(combined)
    assert f'Applies only within: {project / "src" / "sub"}' in rendered
    assert f'Applies only within: {project / "other"}' in rendered
    assert next(row for row in rows if row['content'] == 'sub rules')['scope'] != str(project)


def test_rule_swap_between_validation_and_open_cannot_import_foreign_bytes(tmp_path, monkeypatch):
    import os
    from lib.project_mod.instructions import read_scoped_guidance
    project, target = _project(tmp_path)
    guide = project / 'src' / 'sub' / 'AGENTS.md'
    original = os.open
    def swap(path, flags, *args, **kwargs):
        directory = kwargs.get('dir_fd')
        if (path == 'AGENTS.md' and directory is not None and
                Path(f'/proc/self/fd/{directory}').resolve() == guide.parent):
            guide.unlink()
            guide.symlink_to(tmp_path / 'AGENTS.md')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swap)
    rows = read_scoped_guidance(str(project), str(target))
    assert rows[-1]['path'] == str(guide) and rows[-1]['skipped'] == 'changed_during_read'
    assert 'foreign server rules' not in str(rows)


@pytest.mark.parametrize('command', [
    'cd -P src/sub && printf changed > new.py',
    '(cd other); printf changed > src/sub/new.py',
    'printf "%s" "$HOME" > src/sub/new.py',
])
def test_shell_structure_or_expansion_cannot_hide_nested_write(tmp_path, command):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    project, _ = _project(tmp_path)
    task = {'id': 'literal-target-task', '_userId': 7, 'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    messages = [{'role': 'user', 'content': 'implement'}]
    check_project_guidance(task, 'run_command', {'command': 'git status'})
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'run_command', {'command': command}) == ''
    refresh_project_guidance(task, messages, model='')
    assert 'sub rules' in str(messages)
    assert check_project_guidance(task, 'run_command', {'command': command}) == ''


def test_guidance_stops_before_opening_files_beyond_remaining_budget(tmp_path, monkeypatch):
    import os
    from lib.project_mod.instructions import read_scoped_guidance
    directories = [tmp_path]
    for index in range(5):
        directories.append(directories[-1] / f'sub{index}')
        directories[-1].mkdir()
    for directory in directories:
        (directory / 'AGENTS.md').write_text('x' * 300000)
    opened = []
    real_open = os.open
    def count(path, flags, *args, **kwargs):
        if path == 'AGENTS.md' and kwargs.get('dir_fd') is not None:
            opened.append(path)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', count)
    with pytest.raises(ValueError):
        read_scoped_guidance(str(tmp_path), str(directories[-1] / 'file.py'))
    assert len(opened) == 3  # The fourth file exceeds remaining bytes before open.



def test_one_acquisition_opens_each_rule_once_for_many_targets(tmp_path, monkeypatch):
    import os
    from lib.project_mod.config import build_root_policy, clear_conv_state
    from lib.tasks_pkg.project_guidance import check_project_guidance
    project, target = _project(tmp_path)
    opened = []
    real_open = os.open
    def count(path, flags, *args, **kwargs):
        directory = kwargs.get('dir_fd')
        if path == 'AGENTS.md' and directory is not None:
            opened.append(Path(f'/proc/self/fd/{directory}').resolve() / path)
        return real_open(path, flags, *args, **kwargs)
    task = {'id': 'many-target-task', '_userId': 7, 'config': {'projectPath': str(project)},
            '_workspaceRootPolicy': build_root_policy(str(project))}
    reads = [{'path': str(target.parent / name)} for name in ('a.py', 'b.py', 'c.py')]
    monkeypatch.setattr(os, 'open', count)
    try:
        assert check_project_guidance(task, 'read_files', {'reads': reads}) == ''
    finally:
        clear_conv_state(task['id'])
    guides = [(project / relative / 'AGENTS.md').resolve() for relative in ('.', 'src', 'src/sub')]
    assert sorted(opened) == sorted(guides)
    assert set(task['_projectGuidance']['rules']) == {str(guide) for guide in guides}

def test_verified_deleted_rule_is_pruned_before_candidate_bound_check(tmp_path):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    root = tmp_path / 'project'
    sub = root / 'sub'
    sub.mkdir(parents=True)
    guide = root / 'AGENTS.md'
    guide.write_text('r' * 600000)
    task = {'id': 'removed-rule-task', '_userId': 7, 'config': {'projectPath': str(root)},
            '_workspaceRootPolicy': build_root_policy(str(root))}
    messages = [{'role': 'user', 'content': 'implement'}]
    assert check_project_guidance(task, 'write_file', {'path': str(root / 'file.py')}) == ''
    refresh_project_guidance(task, messages, model='')
    guide.unlink()
    (sub / 'AGENTS.md').write_text('s' * 600000)
    args = {'path': str(sub / 'file.py')}
    assert check_project_guidance(task, 'write_file', args) == ''
    assert str(guide) not in task['_projectGuidance']['rules']
    refresh_project_guidance(task, messages, model='')
    assert check_project_guidance(task, 'write_file', args) == ''


@pytest.mark.parametrize('option', ['', '-P '])
def test_symlink_cd_parent_hop_preserves_shell_logical_and_physical_scope(tmp_path, option):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.project_guidance import check_project_guidance, refresh_project_guidance
    root = tmp_path / 'project'
    destination = root / 'physical' / 'deep'
    destination.mkdir(parents=True)
    (root / 'link').symlink_to(destination, target_is_directory=True)
    for parent, text in ((root, 'root guide'), (root / 'logical', 'logical target guide'),
                         (root / 'physical' / 'logical', 'physical target guide')):
        parent.mkdir(exist_ok=True)
        (parent / 'AGENTS.md').write_text(text)
    task = {'id': 'symlink-cd-task', '_userId': 7, 'config': {'projectPath': str(root)},
            '_workspaceRootPolicy': build_root_policy(str(root))}
    messages = [{'role': 'user', 'content': 'implement'}]
    args = {'command': f'cd {option}link && cd .. && printf changed > logical/new.py'}
    assert check_project_guidance(task, 'run_command', args) == ''
    refresh_project_guidance(task, messages, model='')
    expected = 'physical target guide' if option else 'logical target guide'
    assert expected in str(messages)
    assert check_project_guidance(task, 'run_command', args) == ''
