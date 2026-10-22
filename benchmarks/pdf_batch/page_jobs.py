"""Experimental page jobs over the canonical classic helper and fixed pool owner.

One document owns its parent admission until every scheduled page job joins.
Workers reopen immutable bytes per bounded chunk, reuse the parent's complete
heading object, and close documents. No global page cache or product default.
"""
from concurrent.futures import wait
import math
import pickle
from types import FunctionType
from threading import Condition,get_ident

from benchmarks.pdf_batch.experiment import OwnedPdfBatch


def initialize_page_worker():
    from contextlib import redirect_stdout
    from io import StringIO
    # Prime all classic imports before pickle decodes the heading object.
    # An upstream promotion banner is not the supervisor's JSON output.
    with redirect_stdout(StringIO()):
        from lib.pdf_parser.text import _load_pymupdf_rag
        _load_pymupdf_rag()


def page_chunk(raw,indices,header,total_pages,owner_user_id):
    if type(owner_user_id) is not int or owner_user_id<=0:raise ValueError('explicit positive page-job owner required')
    import pymupdf
    from lib.pdf_parser._common import PYMUPDF_LOCK
    from lib.pdf_parser.text import _extract_classic_page
    with PYMUPDF_LOCK:
        document=pymupdf.open(stream=raw,filetype='pdf')
        try:
            rows=[]
            for index in indices:
                try:
                    value,failures=_extract_classic_page(document,index,header,total_pages)
                    rows.append((index,value,failures,None))
                except Exception as error:
                    # Serial normalization errors trigger whole-document raw
                    # fallback. Keep them distinct from lost worker transport.
                    rows.append((index,None,0,(type(error).__name__,str(error))))
            return rows
        finally:document.close()



class PageDocument:
    def __init__(self,pool,budget,workers,raw,owner_user_id,options,parser):
        self.pool,self.budget,self.raw,self.owner_user_id=pool,budget,raw,owner_user_id
        self.parser=parser;self.options=options;self.workers=workers;self.futures=[];self.pages={};self.started=False;self.serial=False;self.failure=None

    def page(self,document,index,header,total_pages):
        from lib.pdf_parser import text
        if not self.started:
            self.started=True
            limit=text.bounded_pdf_pages(self.options.get('max_pages',0),self.budget)
            count=min(total_pages,limit)
            # Explicit small text ceilings and short documents retain the
            # unchanged serial path. No eager worker or extra page is needed.
            if self.options.get('max_text_chars',0) or count<self.workers*2:
                self.serial=True
            else:
                try:pickle.dumps(header)
                except (TypeError,pickle.PicklingError):self.serial=True
            if not self.serial:
                chunks=min(self.budget.unfinished_capacity,max(1,self.workers*3),count)
                size=math.ceil(count/chunks)
                for begin in range(0,count,size):
                    indices=tuple(range(begin,min(count,begin+size)))
                    try:future=self.pool.submit(self.parser,self.raw,indices,header,total_pages,self.owner_user_id)
                    except Exception as error:self.failure=error;raise
                    self.futures.append(future)
                    for number in indices:self.pages[number]=future
        if self.serial:return text._extract_classic_page(document,index,header,total_pages)
        future=self.pages[index]
        try:rows=future.result()
        except Exception as error:self.failure=error;raise
        for number,value,failures,error in rows:
            if number==index:
                if error is not None:raise RuntimeError('page normalization failed: '+error[0]+': '+error[1])
                return value,failures
        self.failure=RuntimeError('page job omitted requested page')
        raise self.failure

    def join(self):
        for future in self.futures:future.cancel()
        if self.futures:wait(self.futures)
        # Completed values/errors are transport state, never durable output.
        self.pages.clear();self.futures.clear()


class PageJobBatch(OwnedPdfBatch):
    def __init__(self,workers,*,owner_user_id,parser=page_chunk):
        if type(owner_user_id) is not int or owner_user_id<=0:raise ValueError('explicit positive batch owner required')
        super().__init__(workers,initializer=initialize_page_worker);self.owner_user_id=owner_user_id;self.parser=parser
        self._condition=Condition();self._accepting=True;self._active=0;self._threads={}
        self.unreleased_admission=[]

    def parse(self,raw,**options):
        from lib.pdf_parser import core,text
        from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
        if type(raw) is not bytes:raise TypeError('immutable PDF bytes required')
        thread=get_ident()
        with self._condition:
            if not self._accepting or self.closed:raise RuntimeError('PDF page-job owner closed')
            self._active+=1;self._threads[thread]=self._threads.get(thread,0)+1
        lease=None;document=None;primary=None
        try:
            lease=CLASSIC_PDF_ADMISSION.reserve(self.budget.unfinished_capacity)
            document=PageDocument(self.pool,self.budget,self.workers,raw,self.owner_user_id,options,self.parser)
            extract=FunctionType(text._extract_pdf_text_with_meta_without_admission.__code__,
                dict(text._extract_pdf_text_with_meta_without_admission.__globals__,_extract_classic_page=document.page),
                argdefs=text._extract_pdf_text_with_meta_without_admission.__defaults__)
            extract.__kwdefaults__=text._extract_pdf_text_with_meta_without_admission.__kwdefaults__
            parse=FunctionType(core._parse_pdf_without_admission.__code__,
                dict(core._parse_pdf_without_admission.__globals__,_extract_pdf_text_with_meta_without_admission=extract),
                argdefs=core._parse_pdf_without_admission.__defaults__)
            parse.__kwdefaults__=core._parse_pdf_without_admission.__kwdefaults__
            result=parse(raw,**options)
            if document.failure is not None:raise document.failure
            return result
        except BaseException as error:
            primary=error;raise
        finally:
            try:
                if document is not None:
                    document.join()
                    if document.failure is not None:
                        with self._condition:self._accepting=False
                        # Worker loss must join the process pool before payload
                        # admission returns, even if text fallback swallowed it.
                        super().close()
                if lease is not None:lease.release()
            except BaseException as cleanup:
                if lease is not None:self.unreleased_admission.append(lease)
                if primary is None:raise
                primary.add_note('page job quiescence also failed: '+type(cleanup).__name__)
            finally:
                with self._condition:
                    self._active-=1;self._threads[thread]-=1
                    if not self._threads[thread]:del self._threads[thread]
                    self._condition.notify_all()

    def close(self):
        with self._condition:
            self._accepting=False
            if self._threads.get(get_ident()):raise RuntimeError('page job caller cannot synchronously close its own parse')
            while self._active:self._condition.wait()
        super().close()
        # Failed quiescence never guessed release; a later verified close may
        # discharge retained leases if the base pool owner can recover.
        for lease in self.unreleased_admission:lease.release()
        self.unreleased_admission.clear()

    def run(self,documents,*,chunksize):
        with self._condition:
            if not self._accepting or self.closed:raise RuntimeError('PDF page-job owner closed')
        documents=tuple(documents)
        if any(type(raw) is not bytes for raw in documents):raise TypeError('PDF page inputs must be immutable bytes')
        output=[]
        for raw in documents:
            try:output.append({'ok':True,'result':self.parse(raw,text_mode='rich')})
            except Exception as error:
                if not self._accepting or self.closed:raise
                output.append({'ok':False,'error_type':type(error).__module__+'.'+type(error).__name__,'message':str(error)})
        return output
