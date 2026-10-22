"""Real size-order workers: concurrent shutdown and SIGKILL preserve ownership."""
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
from types import FunctionType, SimpleNamespace

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch import faults
from benchmarks.pdf_batch.size_order import SizeOrderedPdfBatch


def main():
    multiprocessing.set_executable(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    def owner(workers):
        return SizeOrderedPdfBatch(workers,parser=faults.blocked_document)
    namespace=dict(faults.concurrent_disposal.__globals__,experiment=SimpleNamespace(OwnedPdfBatch=owner))
    for function in (faults.concurrent_disposal,faults.worker_crash):
        with tempfile.TemporaryDirectory(prefix='tofu-size-order-fault-') as folder:
            FunctionType(function.__code__,namespace)(Path(folder))
    print('actual size-order pool: concurrent close joins; SIGKILL fails without retry; parent admission balanced')


if __name__=='__main__':main()
