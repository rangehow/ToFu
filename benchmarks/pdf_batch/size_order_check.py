"""Frozen heterogeneous PDF order/errors and real size-order pool ownership."""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch.heterogeneous import fixtures
from benchmarks.pdf_batch.experiment import parse_document
from benchmarks.pdf_batch.size_order import SizeOrderedPdfBatch
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    multiprocessing.set_executable(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    documents,metadata=fixtures();expected=[parse_document(raw) for raw in documents]
    initial=CLASSIC_PDF_ADMISSION.snapshot()['unfinished']
    owner=SizeOrderedPdfBatch(workers=4)
    try:
        actual=owner.run(documents,chunksize=1)
        assert actual==expected
        assert actual[2]['ok'] is False and all(row['ok'] for i,row in enumerate(actual) if i!=2)
        assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==initial
    finally:owner.close()
    assert not multiprocessing.active_children() and not owner.unreleased_admission
    report={'scope':__doc__,'whole_system_verdict':'incomplete','checks':{'exact_order_and_error_parity':True,
        'admission_balanced':True,'workers_joined':True},'fixture_sha256':[hashlib.sha256(raw).hexdigest() for raw in documents],
        'fixture_metadata':metadata,'workers':owner.workers,'unfinished_capacity':owner.budget.unfinished_capacity}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print('nine real heterogeneous PDFs including malformed input: exact order/errors; parent admission balanced and pool joined')


if __name__=='__main__':main()
