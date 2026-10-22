"""Actual repo-map handler/API with execution-scoped cache lifecycle costs.

Private ports preserve production modules. A recording finalizer verifies one
settlement; durable publication, real tool registry/epochs and agent frequency
remain unverified. All memory workers run the complete measured cohort.
"""
import inspect

import scoped_measure
from tool_port import RepositoryToolPort, handler_template


def tool_operation(port, execution_id):
    settlements = []
    def finalize(task, round_number, entry, metadata, **options):
        settlements.append((metadata, options.get('status', 'done')))
    result = port.invoke({'id': execution_id, 'user_id': scoped_measure.OWNER,
                         'messages': [], 'toolRounds': []}, {}, finalize)
    assert len(settlements) == 1 and settlements[0][1] == 'done' and not result[2]
    return result, settlements[0]


def candidate_worker():
    source = inspect.getsource(scoped_measure.worker)
    changes = {
        '        peak_cache_bytes = 0': '        peak_cache_bytes = 0\n        template = handler_template()',
        '                return execution, scope':
            '                return execution, scope, RepositoryToolPort(str(root), scope, template=template)',
        '            execution, scope = current': '            execution, scope, _ = current',
        '                _, scope = active': '                execution, scope, port = active',
        '            _, scope = active': '            _, scope, _ = active',
        '20+(20 if memory else samples)': '20+samples',
    }
    # Replace the longer indentation boundary first; every seam is asserted.
    for before, after in changes.items():
        assert source.count(before) == 1, before
        source = source.replace(before, after)
    block = '''                repo = scope.scan(OWNER) if scope else outline.scan(str(root))
                if scope: peak_cache_bytes = max(peak_cache_bytes, scope.retained_bytes(OWNER))
                scores = rank.ensure_scores(repo, rank.sumbd(repo, {'lib/repo_map/outline.py'}))
                output = render_map(repo, scores)'''
    assert source.count(block) == 1
    source = source.replace(block, '''                output = tool_operation(port, execution.execution_id)
                if scope: peak_cache_bytes = max(peak_cache_bytes, scope.retained_bytes(OWNER))''')
    namespace = dict(scoped_measure.worker.__globals__, RepositoryToolPort=RepositoryToolPort,
                     handler_template=handler_template, tool_operation=tool_operation)
    exec(compile(source, '<actual-repository-tool-measure>', 'exec'), namespace)
    return namespace['worker']


def main():
    source = inspect.getsource(scoped_measure.main)
    before = "    files = ('scoped_measure.py',"
    assert source.count(before) == 1
    source = source.replace(before, "    files = ('tool_measure.py', 'tool_port.py', 'scoped_measure.py',")
    before = "    production_files = ('lib/repo_map/outline.py',"
    assert source.count(before) == 1
    source = source.replace(before,
        "    production_files = ('lib/repo_map/api.py', 'lib/tasks_pkg/handlers/project.py',\n"
        "                        'lib/repo_map/outline.py',")
    namespace = dict(scoped_measure.main.__globals__, __file__=__file__, __doc__=__doc__, worker=candidate_worker())
    exec(compile(source, '<actual-repository-tool-pairs>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
