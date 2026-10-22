"""Progress-observer self-close cannot deadlock or reopen page admission."""
import multiprocessing
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch.heterogeneous import fixtures
from benchmarks.pdf_batch.page_jobs import PageJobBatch
from lib.pdf_parser.core import _parse_pdf_without_admission
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION


def main():
    multiprocessing.set_executable(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    for owner_id in (False,0,-1,'900001',900001.0):
        try:PageJobBatch(2,owner_user_id=owner_id)
        except ValueError:pass
        else:raise AssertionError('invalid page owner admitted')
    documents,_=fixtures();raw=documents[0];expected=_parse_pdf_without_admission(raw)
    owner=PageJobBatch(2,owner_user_id=900001);refused=[]
    def progress(stage,done,total):
        if refused:return
        try:owner.close()
        except RuntimeError as error:
            assert 'own parse' in str(error);refused.append(True)
        else:raise AssertionError('self-close completed while parent parse was active')
    try:
        actual=owner.parse(raw,progress_callback=progress)
        assert actual==expected and refused==[True]
        for documents in ([],[raw]):
            try:owner.run(documents,chunksize=1)
            except RuntimeError:pass
            else:raise AssertionError('self-close refusal reopened admission')
    finally:owner.close()
    assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
    assert not multiprocessing.active_children()
    print('page owner validation; self-close denied without deadlock; accepted parse completes; admission stays closed; joined cleanup0')


if __name__=='__main__':main()
