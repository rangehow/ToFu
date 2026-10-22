"""Uncached reference/cursor extraction plus actual transient task publication.

One private fixed ancestor scanner versus current cursor, through real public
handler/API/finalizer. Independent cold/warm/edit latency/full-memory cohorts.
No execution cache, durable/gateway or whole-repository admission is inferred.
"""
import inspect
from types import FunctionType
import publication_measure
from publication_port import TransientChatRuntime
from task_measure import task_worker as canonical_worker
from extraction_epoch import loaded_identity
from published_extraction import ReferenceExtractionPort,CursorExtractionPort,REFERENCE_SHA256,REVISION


def task_worker():
    original=canonical_worker()
    def worker(source,variant,case,plan,samples,memory=False):
        selected=ReferenceExtractionPort if variant=='full' else CursorExtractionPort
        bound=FunctionType(original.__code__,dict(original.__globals__,TaskRuntime=TransientChatRuntime,
            TaskRepositoryToolPort=selected,handler_template=lambda:None),argdefs=original.__defaults__)
        bound.__kwdefaults__=original.__kwdefaults__
        # Both arms take the uncached lifecycle branch; the driver variant
        # selects only the extraction implementation.
        result=bound(source,'full',case,plan,samples,memory)
        result['reference_revision']=REVISION;result['reference_function_sha256']=REFERENCE_SHA256
        result['loaded_extractor_packages']=loaded_identity()['packages']
        return result
    return worker


def main():
    source=inspect.getsource(publication_measure.main)
    boundary="'publication_measure.py', 'publication_port.py',"
    assert source.count(boundary)==1
    source=source.replace(boundary,"'published_extraction_measure.py', 'published_extraction.py', 'reference_tags.py', 'publication_measure.py', 'publication_port.py',")
    namespace=dict(publication_measure.main.__globals__,__file__=__file__,__doc__=__doc__,task_worker=task_worker)
    exec(compile(source,'<public-extraction-implementation-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
