#!/usr/bin/env python3
"""Owned PDF batch scheduling experiment, same parser/output policy in both variants.

No production pool/default/store changes. Canonical admission belongs to the
parent until all worker activity is joined. Per-document errors remain explicit;
a crashed process fails the experiment and is never retried ambiguously.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
import threading
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def parse_document(raw):
    from lib.pdf_parser.core import _parse_pdf_without_admission
    try:
        result=_parse_pdf_without_admission(raw,text_mode='rich')
        return {'ok':True,'result':result}
    except Exception as error:
        return {'ok':False,'error_type':type(error).__module__+'.'+type(error).__name__,'message':str(error)}


def fixture_documents():
    import pymupdf
    documents=[]
    for index in range(16):
        document=pymupdf.open()
        page=document.new_page()
        page.insert_text((72,72),f'Batch fixture {index}: immutable extraction evidence.\n'+('Owner and lifecycle remain explicit.\n'*8))
        documents.append(document.tobytes());document.close()
    return documents


class OwnedPdfBatch:
    """Benchmark-only pool with canonical parent admission and joined teardown."""
    def __init__(self,workers,*,initializer=None):
        from lib.pdf_parser.policy import resolve_classic_pdf_budget
        self.budget=resolve_classic_pdf_budget()
        self.workers=min(max(1,workers),self.budget.processes)
        self.pool=ProcessPoolExecutor(max_workers=self.workers,mp_context=multiprocessing.get_context('spawn'),initializer=initializer)
        self.closed=False
        self._close_lock=threading.Lock();self._close_done=threading.Event();self._close_error=None

    def run(self,documents,*,chunksize):
        if self.closed:raise RuntimeError('PDF batch owner closed')
        documents=tuple(documents)
        if any(type(raw) is not bytes for raw in documents):
            raise TypeError('PDF batch inputs must be immutable bytes')
        results=[]
        capacity=self.budget.unfinished_capacity
        for offset in range(0,len(documents),capacity):
            window=documents[offset:offset+capacity]
            balanced_chunk=min(chunksize,max(1,len(window)//self.workers))
            results.extend(self._run_window(window,chunksize=balanced_chunk))
        return results

    def _run_window(self,documents,*,chunksize):
        from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
        if self.closed:raise RuntimeError('PDF batch owner closed')
        leases=[]
        try:
            for _ in documents:leases.append(CLASSIC_PDF_ADMISSION.reserve(self.budget.unfinished_capacity))
            return list(self.pool.map(parse_document,documents,chunksize=chunksize))
        finally:
            # map results are complete before return; on failure join the pool
            # before releasing unfinished payload admission.
            if sys.exc_info()[0] is not None:self.close()
            for lease in leases:lease.release()

    def close(self):
        with self._close_lock:
            first=not self.closed
            if first:self.closed=True
        if first:
            try:self.pool.shutdown(wait=True,cancel_futures=True)
            except BaseException as error:
                self._close_error=error
                raise
            finally:self._close_done.set()
        else:
            self._close_done.wait()
            if self._close_error is not None:raise self._close_error


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true');parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    assert args.check,'measurement adapter follows semantic and lifecycle check'
    documents=fixture_documents();documents.insert(3,b'invalid PDF')
    reference=[parse_document(raw) for raw in documents]
    report={'scope':'actual rich PDF core outputs with benchmark-owned parent admission; no application batching or promotion',
            'fixture_sha256':[hashlib.sha256(raw).hexdigest() for raw in documents],'checks':[]}
    for chunksize in (1,4):
        owner=OwnedPdfBatch(workers=4)
        try:
            started=time.perf_counter_ns();actual=owner.run(documents,chunksize=chunksize)
            assert actual==reference,'batch changes per-document result or error'
            assert actual[3]['ok'] is False and all(row['ok'] for i,row in enumerate(actual) if i!=3)
            report['checks'].append({'chunksize':chunksize,'workers':owner.workers,'unfinished_capacity':owner.budget.unfinished_capacity,'elapsed_ms':(time.perf_counter_ns()-started)/1e6})
        finally:owner.close()
        for rejected in ([],documents):
            try:owner.run(rejected,chunksize=chunksize)
            except RuntimeError:pass
            else:raise AssertionError('closed batch owner admitted work')
        from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
        assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
        assert not multiprocessing.active_children(),'PDF batch owner left live workers'
    if args.output:args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
