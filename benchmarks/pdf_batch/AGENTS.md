# PDF batch experiment

This directory compares scheduling around the unchanged rich PDF parser.
`experiment.py` owns isolated spawn workers and canonical parent admission;
errors are per-document carriers, and teardown joins workers before release.
Do not interpret faster batching as a production pool/default change. Measure
cold/warm, full worker RSS/CPU, cancellation and failure before admission.

`heterogeneous.py` owns varied real PDF fixtures and their frozen-byte cohort.
It checks output order/errors and reuses `measure.py` for worker accounting.
Every process reads the same parent snapshot; fixture identity is not regenerated
independently in measurement workers.

`size_order.py` submits largest byte payloads first inside each canonical
admission window and restores caller order. It keeps parser/process budgets.
Verify `size_order_check.py` and real-process `size_order_faults.py`, then use
`size_order_measure.py` for frozen heterogeneous paired comparisons. Byte size
is only a cost hint; failed shutdown retains leases rather than guessing joins.

`measured_cost.py` adds execution-owned bounded SHA256→worker-duration hints
to window scheduling, never parsed-result reuse. Its byte budget derives from
the canonical text envelope. Verify real-doc/tiny-budget semantics and actual
close/SIGKILL scripts before `measured_cost_measure.py` paired comparisons.

`page_jobs.py` owns experimental bounded page chunks using canonical serial
page policy and complete heading metadata. `page_jobs_check.py` verifies real
results/bounds/progress; `page_jobs_faults.py` verifies parent/worker close,
SIGKILL and retained lease/primary-error behavior after unverified shutdown.
`page_jobs_measure.py` compares matched fixed-pool core work. Larger/scaled,
startup/default truncation pressure and application integration remain gaps.

`single_document_measure.py` owns a distinct frozen 32-page workload, never a
replacement for mixed-batch controls. `page_jobs_reentrant_check.py` checks
observer self-close and permanent admission refusal before measuring. Report
single-document speed/RSS tradeoffs with the existing admission guard unchanged.
