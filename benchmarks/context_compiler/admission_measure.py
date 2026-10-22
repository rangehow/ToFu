"""Protocol-copy comparison including actual final local token/budget admission.

Raw histories and canonical final recount are shared by both arms. Reuses paired
independent latency/memory accounting; no producer/compaction/media/transport or
whole-context admission is inferred.
"""
import inspect
import types
import importlib.metadata
import conversion_copy_measure
import provider_measure
import native_copy_measure
from admission_project import make_project,check


def worker(variant,profile,samples,memory,density='heavy'):
    projection=make_project(variant)
    namespace=dict(provider_measure.worker.__globals__)
    namespace['fixture']=lambda selected:native_copy_measure.fixture(selected,density)
    namespace['project']=lambda rows,protocol,feature:projection(rows,'anthropic' if protocol=='anthropic' else feature)
    function=types.FunctionType(provider_measure.worker.__code__,namespace,argdefs=provider_measure.worker.__defaults__)
    result=function('raw','first',profile,samples,memory)
    result['tokenizer_package_version']=importlib.metadata.version('tiktoken')
    return result


def main():
    source=inspect.getsource(conversion_copy_measure.main)
    boundary="'benchmarks/context_compiler/conversion_copy_measure.py',"
    assert source.count(boundary)==1
    source=source.replace(boundary,"'benchmarks/context_compiler/admission_measure.py', 'benchmarks/context_compiler/admission_project.py', 'lib/llm/body/_admission.py', 'lib/token_counter/api.py', 'lib/token_counter/tiktoken_counter.py', 'lib/token_counter/evidence.py', 'lib/token_counter/resolver.py', 'lib/token_counter/config.py', 'lib/llm/body/_clamp.py', "+boundary)
    namespace=dict(conversion_copy_measure.main.__globals__,__file__=__file__,__doc__=__doc__,worker=worker,conversion_check=check)
    # The borrowed main binds its check from a module name; expose a private
    # check object rather than replacing any module attribute.
    namespace['conversion_copy']=types.SimpleNamespace(check=check)
    exec(compile(source,'<context-final-token-admission-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
