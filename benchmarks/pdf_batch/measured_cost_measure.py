"""Observed-cost scheduling on frozen rich PDFs; no app/default admission."""
import inspect
from pathlib import Path
import sys
from types import FunctionType

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch import size_order_measure
from benchmarks.pdf_batch.measured_cost import MeasuredCostPdfBatch


def worker(args,*,documents=None):
    original=size_order_measure.worker
    bound=FunctionType(original.__code__,dict(original.__globals__,SizeOrderedPdfBatch=MeasuredCostPdfBatch),
                       argdefs=original.__defaults__)
    bound.__kwdefaults__=original.__kwdefaults__
    return bound(args,documents=documents)


def main():
    source=inspect.getsource(size_order_measure.main)
    before="'benchmarks/pdf_batch/size_order.py', 'benchmarks/pdf_batch/size_order_measure.py',"
    after="'benchmarks/pdf_batch/measured_cost.py', 'benchmarks/pdf_batch/measured_cost_measure.py', 'benchmarks/pdf_batch/size_order.py', 'benchmarks/pdf_batch/size_order_measure.py',"
    assert source.count(before)==1,'PDF source inventory changed'
    source=source.replace(before,after)
    namespace=dict(size_order_measure.main.__globals__,__file__=__file__,__doc__=__doc__,worker=worker)
    exec(compile(source,'<observed-cost-pdf-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
