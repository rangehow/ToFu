"""Size-priority PDF scheduler vs input-order single-document scheduling.

Same frozen heterogeneous inputs, parser, canonical process/admission budget and
paired process-tree accounting. Size is a hint; no product/default admission.
"""
import inspect
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))

from benchmarks.pdf_batch import heterogeneous,measure
from benchmarks.pdf_batch.experiment import OwnedPdfBatch
from benchmarks.pdf_batch.size_order import SizeOrderedPdfBatch


def worker(args,*,documents=None):
    source=inspect.getsource(measure.worker)
    boundary="chunksize=1 if args.variant=='reference' else 4"
    assert source.count(boundary)==1
    source=source.replace(boundary,'chunksize=1')
    selected=OwnedPdfBatch if args.variant=='reference' else SizeOrderedPdfBatch
    namespace=dict(measure.worker.__globals__,OwnedPdfBatch=selected)
    exec(compile(source,'<size-order-pdf-worker>','exec'),namespace)
    return namespace['worker'](args,documents=documents)


def main():
    source=inspect.getsource(heterogeneous.main)
    boundary="    names = ('benchmarks/pdf_batch/heterogeneous.py',"
    assert source.count(boundary)==1
    source=source.replace(boundary,
        "    names = ('benchmarks/pdf_batch/size_order.py', 'benchmarks/pdf_batch/size_order_measure.py',\n"
        "             'benchmarks/pdf_batch/heterogeneous.py',")
    namespace=dict(heterogeneous.main.__globals__,__file__=__file__,__doc__=__doc__,worker=worker)
    exec(compile(source,'<size-order-pdf-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
