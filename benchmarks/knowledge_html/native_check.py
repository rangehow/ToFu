"""Exact Rust/Python HTML differential with a compiled source witness.

Uses synthetic bounded corpora and the actual retained Python owner. It grants
no performance or ingestion/default verdict. A mismatch always fails the run;
the seed and first counterexamples are retained without changing the oracle.
"""
import argparse
import hashlib
import html.parser
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def malformed(seed, count):
    rng = random.Random(seed)
    alphabet = ' ab=\'"/<>\t\r\n&;012x#.!-[]\x00中文\x1c'
    for _ in range(count):
        raw = ''.join(rng.choices(alphabet, k=rng.randrange(1,90)))
        yield rng.choice(['<a','<h1','</a','<![','<!--','<?','&','&#',''])+raw+rng.choice(['>tail',';','</a>end','<p>x</p>',''])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=271828)
    parser.add_argument('--malformed', type=int, default=60000)
    args = parser.parse_args()
    if not 0 <= args.malformed <= 1000000:
        raise ValueError('diagnostic corpus must be within 0..1000000 cases')
    from scripts.gen_native_html_projection import cases
    from lib.knowledge.html_text import extract_html_text
    identity = json.loads(subprocess.check_output([str(args.binary),'--identity'],text=True,timeout=10))
    for name, digest in identity.items():
        assert name in ('html_projection.rs','html_tokenizer.rs','generated_html_projection.rs')
        assert hashlib.sha256((ROOT/'packages/tofu-runtime/src'/name).read_bytes()).hexdigest() == digest, name
    assert len(identity) == 3
    pin = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    source_pin = hashlib.sha256((ROOT/'lib/knowledge/html_text.py').read_bytes()).hexdigest()
    sources = list(cases())+list(malformed(args.seed,args.malformed))
    wire = ''.join(json.dumps(source)+'\n' for source in sources)
    result = subprocess.run([str(args.binary)],input=wire,capture_output=True,text=True,timeout=180)
    assert result.returncode == 0, result.stderr[-4000:]
    outputs = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(outputs) == len(sources)
    failures = 0; examples = []; trace = hashlib.sha256()
    for index, (source, actual) in enumerate(zip(sources,outputs)):
        try: expected = dict(text=extract_html_text(source))
        except ValueError: expected = dict(error='NumericReference')
        if actual != expected:
            failures += 1
            if len(examples) < 20:
                examples.append(dict(index=index,source=source,expected=expected,actual=actual))
        trace.update(json.dumps(expected,sort_keys=True).encode()); trace.update(b'\0')
    assert hashlib.sha256(args.binary.read_bytes()).hexdigest() == pin
    assert hashlib.sha256((ROOT/'lib/knowledge/html_text.py').read_bytes()).hexdigest() == source_pin
    report = dict(scope=__doc__,python=sys.version,seed=args.seed,count=len(sources),
        binary_sha256=pin,compiled_sources=identity,python_owner_sha256=source_pin,
        tokenizer_sha256=hashlib.sha256(Path(html.parser.__file__).read_bytes()).hexdigest(),
        corpus_sha256=hashlib.sha256(wire.encode()).hexdigest(),expected_trace_sha256=trace.hexdigest(),
        mismatches=failures,examples=examples,performance_admitted=False,default_selection_admitted=False)
    args.output.write_text(json.dumps(report,ensure_ascii=True,indent=2)+'\n')
    print(json.dumps(dict(count=len(sources),mismatches=failures)))
    if failures: raise SystemExit(1)


if __name__ == '__main__':
    main()
