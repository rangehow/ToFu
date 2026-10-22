"""Real page-job equality including malformed docs, bounds and progress order."""
from benchmarks.pdf_batch.heterogeneous import fixtures
from benchmarks.pdf_batch.experiment import parse_document
from benchmarks.pdf_batch.page_jobs import PageJobBatch
from lib.pdf_parser.core import _parse_pdf_without_admission
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION


def check():
    documents,_=fixtures();reference=[parse_document(raw) for raw in documents]
    owner=PageJobBatch(2,owner_user_id=900001)
    try:
        assert owner.run(documents,chunksize=1)==reference
        for options in ({'max_pages':2},{'max_text_chars':512},{}):
            expected_progress=[];actual_progress=[]
            expected=_parse_pdf_without_admission(documents[0],**options,progress_callback=lambda *args:expected_progress.append(args))
            actual=owner.parse(documents[0],**options,progress_callback=lambda *args:actual_progress.append(args))
            assert actual==expected and actual_progress==expected_progress
    finally:owner.close()
    assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
    print('page jobs: real rich outputs/error carriers, bounds and exact ordered progress; joined close; admission0')


if __name__=='__main__':check()
