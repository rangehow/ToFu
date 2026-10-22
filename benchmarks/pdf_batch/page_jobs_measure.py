"""Whole rich PDF core: page jobs vs input-order document jobs, same fixed pool.

Matched canonical budgets, immutable corpus, independent latency/memory processes
and original output/error traces. No production, cold or scaled admission.
"""
import inspect
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch import heterogeneous,measure
from benchmarks.pdf_batch.experiment import OwnedPdfBatch
from benchmarks.pdf_batch.page_jobs import PageJobBatch


def worker(args,*,documents=None):
    source=inspect.getsource(measure.worker)
    boundary="chunksize=1 if args.variant=='reference' else 4"
    assert source.count(boundary)==1;source=source.replace(boundary,'chunksize=1')
    selected=OwnedPdfBatch if args.variant=='reference' else lambda workers:PageJobBatch(workers,owner_user_id=900001)
    namespace=dict(measure.worker.__globals__,OwnedPdfBatch=selected)
    exec(compile(source,'<canonical-pdf-page-job-worker>','exec'),namespace)
    return namespace['worker'](args,documents=documents)


def main():
    source=inspect.getsource(heterogeneous.main)
    boundary="    names = ('benchmarks/pdf_batch/heterogeneous.py',"
    assert source.count(boundary)==1
    source=source.replace(boundary,"    names = ('benchmarks/pdf_batch/page_jobs.py', 'benchmarks/pdf_batch/page_jobs_measure.py', 'lib/pdf_parser/text.py', 'lib/pdf_parser/_common.py', 'lib/pdf_parser/postprocess.py', 'lib/pdf_parser/math.py',\n             'benchmarks/pdf_batch/heterogeneous.py',")
    namespace=dict(heterogeneous.main.__globals__,__file__=__file__,__doc__=__doc__,worker=worker)
    exec(compile(source,'<canonical-pdf-page-job-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
