"""Page-job single-document hypothesis on one frozen 32-page rich PDF.

Same full-core defaults and budget as the mixed batch. Parent generates bytes
once, shared by independent latency/RSS workers. New workload never waives mixed
batch regressions or proves scaled/cold/durable application admission.
"""
import inspect
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch import page_jobs_measure,heterogeneous


def fixtures():
    import pymupdf
    document=pymupdf.open()
    try:
        for index in range(32):
            page=document.new_page()
            page.insert_text((40,40),f'Single document, original page {index}\n'+''.join(
                f'Line {line}: explicit owner, budgets, progress and preserved results.\n' for line in range(24)),fontsize=9)
            for row in range(4):
                for column in range(3):
                    x=40+column*150;y=400+row*28
                    page.draw_rect(pymupdf.Rect(x,y,x+150,y+28))
                    page.insert_text((x+4,y+18),f'Cell {row}:{column}',fontsize=9)
        return [document.tobytes()],[{'kind':'single_text_table','pages':32,'fixture_index':0}]
    finally:document.close()


def main():
    source=inspect.getsource(heterogeneous.main)
    boundary="    names = ('benchmarks/pdf_batch/heterogeneous.py',"
    assert source.count(boundary)==1
    source=source.replace(boundary,"    names = ('benchmarks/pdf_batch/single_document_measure.py', 'benchmarks/pdf_batch/page_jobs_measure.py', 'benchmarks/pdf_batch/page_jobs.py', 'lib/pdf_parser/text.py', 'lib/pdf_parser/_common.py', 'lib/pdf_parser/postprocess.py', 'lib/pdf_parser/math.py',\n             'benchmarks/pdf_batch/heterogeneous.py',")
    namespace=dict(heterogeneous.main.__globals__,__file__=__file__,__doc__=__doc__,worker=page_jobs_measure.worker,fixtures=fixtures)
    exec(compile(source,'<single-document-page-job-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
