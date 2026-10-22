"""Execution-owned observed PDF cost hints; no parsed-result reuse or app default.

Worker clocks measure only the unchanged parser call. SHA256 hints retain no raw
documents; an LRU byte budget derives from the launch-derived text envelope.
Sorting stays inside canonical unfinished windows and restores caller order.
"""
from collections import OrderedDict
from functools import partial
import hashlib
import inspect
import sys
from threading import RLock
import time
from types import MethodType

from benchmarks.pdf_batch.experiment import parse_document
from benchmarks.pdf_batch.size_order import SizeOrderedPdfBatch


def timed_document(raw, *, parser):
    started = time.perf_counter_ns()
    result = parser(raw)
    return result, time.perf_counter_ns()-started


class MeasuredCostPdfBatch(SizeOrderedPdfBatch):
    def __init__(self, workers, *, parser=parse_document):
        super().__init__(workers, parser=partial(timed_document, parser=parser))
        self._hints = OrderedDict(); self._hint_lock = RLock()
        self.hint_bytes = 0
        self.maximum_hint_bytes = max(1, self.budget.max_text_chars//8)
        try:
            self._bind_window()
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                primary.add_note('PDF constructor shutdown also failed: '+type(cleanup).__name__)
            raise

    def _bind_window(self):
        source = inspect.getsource(SizeOrderedPdfBatch._run_window)
        before = 'order=sorted(range(len(documents)),key=lambda index:(-len(documents[index]),index))'
        assert source.count(before)==1, 'size-order scheduling seam changed'
        source = source.replace(before, 'order=self._priority_order(documents)')
        before = '            result=[futures[index].result() for index in range(len(documents))]'
        assert source.count(before)==1, 'size-order result seam changed'
        source = source.replace(before, '''            result=[]
            for index in range(len(documents)):
                value, elapsed_ns=futures[index].result()
                self._observe(documents[index], elapsed_ns)
                result.append(value)''')
        import textwrap
        namespace = dict(SizeOrderedPdfBatch._run_window.__globals__)
        exec(compile(textwrap.dedent(source), '<measured-cost-pdf-window>', 'exec'), namespace)
        self._run_window = MethodType(namespace['_run_window'], self)

    def _priority_order(self, documents):
        with self._hint_lock:
            hints = [self._hints.get(hashlib.sha256(raw).digest()) for raw in documents]
        # Known observed costs rank first; unknown inputs use the earlier byte
        # hint. Do not compare nanoseconds with bytes as if they shared units.
        return sorted(range(len(documents)), key=lambda i:
                      (hints[i] is None, -(hints[i][0] if hints[i] else len(documents[i])), i))

    def _observe(self, raw, elapsed_ns):
        if type(elapsed_ns) is not int or elapsed_ns < 0:
            raise ValueError('worker cost must be observed nonnegative nanoseconds')
        key = hashlib.sha256(raw).digest()
        charge = 192+sys.getsizeof(key)+sys.getsizeof(elapsed_ns)
        with self._hint_lock:
            if self.closed:
                return
            while self._hints and self.hint_bytes > self.maximum_hint_bytes:
                _, evicted = self._hints.popitem(last=False); self.hint_bytes -= evicted[1]
            previous = self._hints.pop(key, None)
            if previous:
                self.hint_bytes -= previous[1]
            if charge > self.maximum_hint_bytes:
                return
            while self._hints and self.hint_bytes+charge > self.maximum_hint_bytes:
                _, evicted = self._hints.popitem(last=False); self.hint_bytes -= evicted[1]
            self._hints[key] = (elapsed_ns, charge); self.hint_bytes += charge

    def close(self):
        # Failed worker quiescence retains canonical admission, just like the
        # inherited owner; reconstructible hints can still be cleared safely.
        try:
            super().close()
        finally:
            with self._hint_lock:
                self._hints.clear(); self.hint_bytes = 0
