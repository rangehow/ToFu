"""Real owned workers: concurrent disposal and process death must reach quiescence."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch import experiment
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION


def blocked_document(raw):
    directory=Path(raw.decode())
    (directory/('started-'+str(os.getpid()))).write_text('owned worker')
    while not (directory/'release').exists():time.sleep(.005)
    return {'ok':True,'result':'released'}


def wait_until(predicate,timeout=20):
    deadline=time.monotonic()+timeout
    while not predicate():
        assert time.monotonic()<deadline,'owned fault fixture deadline expired'
        time.sleep(.005)


def concurrent_disposal(directory):
    owner=experiment.OwnedPdfBatch(workers=2)
    with ThreadPoolExecutor(max_workers=3) as threads:
        work=threads.submit(owner.run,[str(directory).encode()],chunksize=1)
        try:
            wait_until(lambda:bool(list(directory.glob('started-*'))))
            first=threads.submit(owner.close);wait_until(lambda:owner.closed)
            second=threads.submit(owner.close);time.sleep(.05)
            assert not first.done(),'disposal completed before running worker exited'
            assert not second.done(),'second disposal caller bypassed worker quiescence'
            assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==1
        finally:
            (directory/'release').write_text('release owned fixture')
            work.result(timeout=20);owner.close()
    assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
    assert not multiprocessing.active_children()


def worker_crash(directory):
    owner=experiment.OwnedPdfBatch(workers=2)
    with ThreadPoolExecutor(max_workers=1) as threads:
        work=threads.submit(owner.run,[str(directory).encode()],chunksize=1)
        try:
            wait_until(lambda:bool(list(directory.glob('started-*'))))
            files=list(directory.glob('started-*'));assert len(files)==1
            pid=int(files[0].name.split('-')[1])
            import psutil
            assert pid in {child.pid for child in psutil.Process().children(recursive=True)}
            os.kill(pid,signal.SIGKILL)
            try:work.result(timeout=20)
            except BrokenProcessPool:pass
            else:raise AssertionError('worker crash reported successful parse')
            assert len(list(directory.glob('started-*')))==1,'ambiguous crash retried'
        finally:
            (directory/'release').write_text('release owned fixture');owner.close()
    assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
    assert not multiprocessing.active_children()


def main():
    experiment.parse_document=blocked_document
    with tempfile.TemporaryDirectory(prefix='tofu-pdf-dispose-') as temporary:
        concurrent_disposal(Path(temporary))
    with tempfile.TemporaryDirectory(prefix='tofu-pdf-crash-') as temporary:
        worker_crash(Path(temporary))
    print(json.dumps({'concurrent_disposal':'quiescent','worker_crash':'failed without retry; admission balanced and workers joined'}))

if __name__=='__main__':main()
