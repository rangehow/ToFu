"""Actual PDFs, observed hints, unchanged order/errors and joined parent ownership."""
import multiprocessing
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch.heterogeneous import fixtures
from benchmarks.pdf_batch.experiment import parse_document
from benchmarks.pdf_batch.measured_cost import MeasuredCostPdfBatch
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION


def main():
    multiprocessing.set_executable(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    documents,_=fixtures(); expected=[parse_document(raw) for raw in documents]
    initial=CLASSIC_PDF_ADMISSION.snapshot()['unfinished'];owner=MeasuredCostPdfBatch(4)
    try:
        assert owner.run(documents,chunksize=1)==expected
        assert 0<owner.hint_bytes<=owner.maximum_hint_bytes
        assert owner.run(documents,chunksize=1)==expected
        assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==initial
        # A tiny hint budget evicts derived hints, never documents/results.
        owner.maximum_hint_bytes=1;owner._observe(b'new input',0)
        assert owner.hint_bytes==0 and not owner._hints
        assert owner.run(documents,chunksize=1)==expected
    finally:owner.close()
    assert owner.hint_bytes==0 and not owner._hints
    assert not multiprocessing.active_children() and not owner.unreleased_admission
    print('observed-cost actualPDF cold/repeated/tiny-budget results exact; document errors/order unchanged; hints cleared, admission balanced, workers joined')


if __name__=='__main__':main()
