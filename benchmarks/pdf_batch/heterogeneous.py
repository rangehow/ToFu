"""Heterogeneous real PDF batches with one frozen input snapshot per comparison.

Vary page count, density and tables, retaining malformed per-document errors.
The unchanged rich parser/admission/pool are shared; no app default changes.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.pdf_batch.experiment import OwnedPdfBatch, parse_document
from benchmarks.pdf_batch.measure import worker


def fixtures():
    import pymupdf
    documents = []; metadata = []
    # Alternate larger/smaller work, including across admission windows.
    for index, pages in enumerate((8, 1, 4, 1, 2, 1, 6, 1)):
        document = pymupdf.open()
        try:
            for number in range(pages):
                page = document.new_page()
                lines = 4 if pages == 1 else 24
                text = f'Heterogeneous fixture {index}, page {number}\n'
                text += ''.join(f'Line {line}: owner, admission and durable output remain explicit.\n'
                                for line in range(lines))
                page.insert_text((40, 40), text, fontsize=9)
                if pages > 1:
                    for row in range(4):
                        for column in range(3):
                            x = 40+column*150; y = 400+row*28
                            page.draw_rect(pymupdf.Rect(x, y, x+150, y+28))
                            page.insert_text((x+4, y+18), f'Cell {row}:{column}', fontsize=9)
            documents.append(document.tobytes())
            metadata.append({'kind': 'text_table' if pages > 1 else 'short_text',
                             'pages': pages, 'fixture_index': index})
        finally:
            document.close()
    documents.insert(2, b'invalid heterogeneous PDF')
    metadata.insert(2, {'kind': 'malformed', 'pages': None})
    return documents, metadata


def check(documents):
    reference = [parse_document(raw) for raw in documents]
    assert not reference[2]['ok']
    assert all(result['ok'] for index, result in enumerate(reference) if index != 2)
    from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
    initial = CLASSIC_PDF_ADMISSION.snapshot()['unfinished']
    reports = []
    for chunk in (1, 4):
        owner = OwnedPdfBatch(workers=4)
        try:
            actual = owner.run(documents, chunksize=chunk)
            if actual != reference:
                differences = []
                def compare(left, right, path):
                    if type(left) is not type(right):
                        differences.append(path+':type')
                    elif isinstance(left, dict):
                        for key in left.keys() | right.keys():
                            if key not in left or key not in right:
                                differences.append(path+'/'+str(key)+':missing')
                            else:
                                compare(left[key], right[key], path+'/'+str(key))
                    elif isinstance(left, (list, tuple)):
                        if len(left) != len(right): differences.append(path+':length')
                        for index, (a, b) in enumerate(zip(left, right)):
                            compare(a, b, path+'/'+str(index))
                    elif left != right:
                        differences.append(path+':value')
                compare(reference, actual, 'documents')
                errors = [(index, result.get('error_type'), result.get('message', '')[:250])
                          for index, result in enumerate(actual) if not result['ok']]
                raise AssertionError('batch result mismatch: '+', '.join(differences[:30])
                                     +'; worker errors='+repr(errors[:3]))
            reports.append({'chunksize': chunk, 'workers': owner.workers,
                            'unfinished_capacity': owner.budget.unfinished_capacity,
                            'result_order_and_error_parity': True})
        finally:
            owner.close()
        assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished'] == initial
        assert not multiprocessing.active_children()
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--measure', action='store_true')
    parser.add_argument('--worker', choices=('reference', 'candidate'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    # The experiment may run through a loader wrapper on this host. Spawn
    # children must use that same explicit runtime, rather than the raw
    # sys.executable whose native PDF extensions cannot load host libraries.
    multiprocessing.set_executable(os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable))
    if args.worker:
        manifest = json.loads((args.source/'manifest.json').read_text())
        documents = []
        for row in manifest:
            raw = (args.source/row['file']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == row['sha256']
            documents.append(raw)
        options = SimpleNamespace(case='batch', variant=args.worker,
                                  memory=args.memory, samples=args.samples)
        print(json.dumps(worker(options, documents=documents)))
        return
    assert args.output and (args.check or args.measure)
    assert args.samples > 0 and args.pairs > 0
    documents, metadata = fixtures()
    for raw, row in zip(documents, metadata):
        row.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    names = ('benchmarks/pdf_batch/heterogeneous.py', 'benchmarks/pdf_batch/measure.py',
             'benchmarks/pdf_batch/experiment.py', 'lib/pdf_parser/core.py',
             'lib/pdf_parser/policy.py', 'lib/pdf_parser/admission.py')
    digests = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    report = {'format': 'tofu.heterogeneous-pdf/v1', 'scope': __doc__,
              'whole_system_verdict': 'incomplete', 'fixtures': metadata,
              'runtime': {'python': sys.version, 'platform': platform.platform(),
                          'affinity': sorted(os.sched_getaffinity(0))},
              'sources': digests, 'samples': args.samples, 'pairs': [],
              'checks': check(documents) if args.check else [],
              'limitations': ['no web/PDF mixed application scheduler or persistence',
                              'no application cancellation/crash recovery admission',
                              'cold diagnostic only; memory tree sampled every 5ms',
                              'CPU measured in a separate repeated interval']}
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    if not args.measure:
        print('heterogeneous rich PDF order/error parity; leases balanced and workers joined')
        return
    with tempfile.TemporaryDirectory(prefix='tofu-heterogeneous-pdf-') as folder:
        source = Path(folder); manifest = []
        for index, raw in enumerate(documents):
            filename = f'{index:03}.pdf'; (source/filename).write_bytes(raw)
            manifest.append({'file': filename, 'sha256': metadata[index]['sha256']})
        (source/'manifest.json').write_text(json.dumps(manifest))
        for pair in range(args.pairs):
            values = {}
            for variant in (('reference', 'candidate') if pair%2 == 0 else ('candidate', 'reference')):
                for name, digest in digests.items():
                    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
                command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__,
                           '--worker', variant, '--source', str(source), '--samples', str(args.samples)]
                done = subprocess.run(command, capture_output=True, text=True, check=True, timeout=3600)
                result = json.loads(done.stdout.splitlines()[-1])
                done = subprocess.run(command+['--memory'], capture_output=True, text=True, check=True, timeout=3600)
                memory = json.loads(done.stdout.splitlines()[-1])
                assert result['pid'] != memory['pid']
                assert result['output_sha256'] == memory['output_sha256']
                result['memory_pid'] = memory['pid']
                result['sampled_peak_tree_rss_bytes'] = memory['sampled_peak_tree_rss_bytes']
                result['rss_samples'] = memory['rss_samples']; values[variant] = result
            assert values['reference']['output_sha256'] == values['candidate']['output_sha256']
            report['pairs'].append(values)
            args.output.write_text(json.dumps(report, indent=2)+'\n')
            print('heterogeneous', pair, {metric: round(values['candidate'][metric]/values['reference'][metric], 3)
                  for metric in ('p50_ms', 'p99_ms', 'tree_cpu_ms_per_batch', 'sampled_peak_tree_rss_bytes')}, flush=True)


if __name__ == '__main__':
    main()
