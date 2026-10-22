"""Bounded size-priority scheduling around unchanged rich PDF parsing.

Byte length is a cost hint, not a page/complexity guarantee. Parent admission,
per-document errors and caller order remain unchanged; no app defaults change.
"""
import sys
from benchmarks.pdf_batch.experiment import OwnedPdfBatch, parse_document


class SizeOrderedPdfBatch(OwnedPdfBatch):
    def __init__(self,workers,*,parser=parse_document):
        super().__init__(workers)
        self.parser=parser
        self.unreleased_admission=[]

    def _run_window(self,documents,*,chunksize):
        from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
        if self.closed:raise RuntimeError('PDF batch owner closed')
        if chunksize < 1:raise ValueError('chunksize must be >= 1.')
        if not isinstance(chunksize,int):raise TypeError('integer chunksize required')
        leases=[];completed=False
        try:
            for _ in documents:
                leases.append(CLASSIC_PDF_ADMISSION.reserve(self.budget.unfinished_capacity))
            order=sorted(range(len(documents)),key=lambda index:(-len(documents[index]),index))
            futures={index:self.pool.submit(self.parser,documents[index]) for index in order}
            result=[futures[index].result() for index in range(len(documents))]
            completed=True
            return result
        finally:
            if not completed:
                primary=sys.exc_info()[1]
                try:self.close()
                except BaseException as cleanup:
                    # Unknown quiescence cannot return payload admission. Retain
                    # leases on the owner and surface the teardown failure.
                    self.unreleased_admission.extend(leases)
                    if primary is not None:
                        primary.add_note('PDF owner shutdown also failed: '+type(cleanup).__name__)
                        raise primary
                    raise
            for lease in leases:lease.release()
