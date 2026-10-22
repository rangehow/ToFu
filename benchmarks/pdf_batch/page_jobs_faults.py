"""Actual page workers: parent/worker close join, SIGKILL and failed quiescence."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch.page_jobs import PageJobBatch,page_chunk
from benchmarks.pdf_batch.faults import wait_until
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION


def blocked_chunk(raw,indices,header,total_pages,owner):
    import pymupdf
    document=pymupdf.open(stream=raw,filetype='pdf')
    try:directory=Path(document.metadata['subject'])
    finally:document.close()
    (directory/('started-'+str(os.getpid()))).write_text('owned page worker')
    while not (directory/'release').exists():time.sleep(.005)
    return page_chunk(raw,indices,header,total_pages,owner)


def payload(directory):
    import pymupdf
    document=pymupdf.open()
    try:
        for index in range(8):document.new_page().insert_text((72,72),'Page '+str(index))
        document.set_metadata({'subject':str(directory)})
        return document.tobytes()
    finally:document.close()


def run(directory,crash=False,failed_shutdown=False):
    owner=PageJobBatch(2,owner_user_id=900001,parser=blocked_chunk)
    original_shutdown=owner.pool.shutdown
    with ThreadPoolExecutor(max_workers=3) as threads:
        work=threads.submit(owner.parse,payload(directory),text_mode='rich')
        try:
            wait_until(lambda:bool(list(directory.glob('started-*'))))
            if crash:
                if failed_shutdown:
                    def rejected_shutdown(**kwargs):raise OSError('injected unverified page shutdown')
                    owner.pool.shutdown=rejected_shutdown
                pid=int(next(directory.glob('started-*')).name.split('-')[1])
                import psutil
                assert pid in {child.pid for child in psutil.Process().children(recursive=True)}
                os.kill(pid,signal.SIGKILL)
                try:work.result(timeout=20)
                except BrokenProcessPool as error:
                    if failed_shutdown:
                        assert owner.unreleased_admission and CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==1
                        assert any('quiescence' in note for note in error.__notes__)
                else:raise AssertionError('lost page worker became successful raw fallback')
            else:
                first=threads.submit(owner.close)
                wait_until(lambda:not owner._accepting)
                second=threads.submit(owner.close);time.sleep(.05)
                assert not first.done() and not second.done()
                assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==1
                try:owner.parse(payload(directory))
                except RuntimeError:pass
                else:raise AssertionError('closing page owner admitted another document')
        finally:
            (directory/'release').write_text('release fixture')
            if not work.done():work.result(timeout=20)
            owner.pool.shutdown=original_shutdown
            if failed_shutdown:
                original_shutdown(wait=True,cancel_futures=True)
                # Only this fault fixture discharges explicitly retained debt
                # after observing successful shutdown; no automatic guess.
                for lease in owner.unreleased_admission:lease.release()
                owner.unreleased_admission.clear()
            else:owner.close()
    assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
    assert not multiprocessing.active_children()


def parent_disposal(directory):
    from threading import Event
    owner=PageJobBatch(2,owner_user_id=900001)
    entered=Event();release=Event()
    def progress(stage,done,total):
        if stage=='images' and not entered.is_set():
            entered.set()
            assert release.wait(timeout=20),'parent image fixture release expired'
    with ThreadPoolExecutor(max_workers=3) as threads:
        work=threads.submit(owner.parse,payload(directory),progress_callback=progress)
        try:
            assert entered.wait(timeout=20),'parent image phase not reached'
            first=threads.submit(owner.close);wait_until(lambda:not owner._accepting)
            second=threads.submit(owner.close);time.sleep(.05)
            assert not first.done() and not second.done(),'close skipped running parent parse'
            assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==1
        finally:
            release.set();work.result(timeout=20);owner.close()
    assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0 and not multiprocessing.active_children()


def main():
    multiprocessing.set_executable(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    for options in ({},{'crash':True},{'crash':True,'failed_shutdown':True}):
        with tempfile.TemporaryDirectory(prefix='tofu-page-job-fault-') as directory:run(Path(directory),**options)
    with tempfile.TemporaryDirectory(prefix='tofu-page-parent-close-') as directory:parent_disposal(Path(directory))
    print('page jobs: parent/worker concurrent close waits, new admission denied, SIGKILL explicit failure, failed shutdown retains lease/primary; verified cleanup0')


if __name__=='__main__':main()
