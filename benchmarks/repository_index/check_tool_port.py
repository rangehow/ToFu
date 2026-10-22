"""Actual repo-map tool text/metadata/error settlement through a scoped index port."""
from pathlib import Path
import tempfile

from owned_scope import OwnedIndexScope
from tool_port import RepositoryToolPort

OWNER = 900001


def invoke(port, arguments=None, *, owner=OWNER, root=None):
    settlements = []
    def finalize(task, round_number, entry, metadata, **options):
        settlements.append((metadata, options.get('status', 'done')))
    result = port.invoke({'id': 'repository-port-task', 'user_id': owner,
                         'messages': [], 'toolRounds': []}, arguments or {}, finalize,
                         project_root=root)
    assert len(settlements) == 1
    return result, settlements[0]


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-repository-tool-port-') as folder:
        root = Path(folder)/'authorized'; root.mkdir()
        foreign = Path(folder)/'foreign'; foreign.mkdir()
        (foreign/'private.py').write_text('def foreign_secret():\n    return 1\n')
        (root/'selected').mkdir()
        source = root/'selected/module.py'
        source.write_text('def selected_symbol(value):\n    return value\n')
        (root/'outside.py').write_text('def outside_symbol():\n    return 2\n')
        epoch = [0]
        scope = OwnedIndexScope(OWNER, str(root), 1024*1024, epoch_source=lambda: epoch[0])
        baseline = RepositoryToolPort(str(root))
        candidate = RepositoryToolPort(str(root), scope)
        comparisons = 0
        try:
            before = {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*.py')}
            for arguments in ({}, {'path':'selected'}, {'focus':'module'}, {'max_tokens':1}, {'max_tokens':512}):
                expected = invoke(baseline, arguments)
                for _ in range(2):
                    actual = invoke(candidate, arguments)
                    assert actual == expected and not actual[0][2]
                    comparisons += 1
            assert {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*.py')} == before
            source.write_text('def changed_symbol(value):\n    return value+1\n')
            assert invoke(candidate) == invoke(baseline)
            assert 'changed_symbol' in invoke(candidate)[0][1]
            epoch[0] += 1
            assert invoke(candidate) == invoke(baseline)
            for port in (baseline, candidate):
                result, (_, status) = invoke(port, root=str(foreign))
                assert result[2] and status == 'error' and 'another project root' in result[1]
                assert 'foreign_secret' not in result[1]
            result, (_, status) = invoke(candidate, owner=OWNER+1)
            assert result[2] and status == 'error' and 'foreign index scope owner' in result[1]
            scope.close(OWNER)
            result, (_, status) = invoke(candidate)
            assert result[2] and status == 'error' and 'closed' in result[1]
            assert scope.retained_bytes(OWNER) == 0
        finally:
            scope.close(OWNER)
    print(f'{comparisons} exact actual-handler text/metadata settlements; source edits/epochs, read-only roots, owner denial and terminal close')


if __name__ == '__main__':
    check()
