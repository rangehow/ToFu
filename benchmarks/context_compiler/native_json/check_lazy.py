"""Lazy factory race, failures, canonical controls and input ownership checks."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from check import load
from decoder import make_decoder
import lazy as lazy_module
from lazy import LazyArgumentDecoder


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--library',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    attempts=[]
    def factory():attempts.append('load');return make_decoder(load(args.library))
    decoder=LazyArgumentDecoder(factory)
    for raw in ['{"n":1}',json.dumps({'text':'界'*2000}),json.dumps({'text':'界'*2000},ensure_ascii=False)]:
        assert decoder(raw)==json.loads(raw)
    assert not attempts and decoder.activations==0
    barrier=threading.Barrier(8)
    def call(index):
        barrier.wait();value={'code':'x'*2000,'index':index}
        for _ in range(200):assert decoder(json.dumps(value))==value
        return index
    with ThreadPoolExecutor(max_workers=8) as pool:assert sorted(pool.map(call,range(8)))==list(range(8))
    assert attempts==['load'] and decoder.activations==1
    failed=[]
    def retry_factory():
        failed.append('try')
        if len(failed)==1:raise OSError('fixture native load failure')
        return make_decoder(load(args.library))
    retry=LazyArgumentDecoder(retry_factory);raw=json.dumps({'code':'x'*2000})
    try:retry(raw)
    except OSError:pass
    else:raise AssertionError('native loading failure was hidden')
    assert retry.activations==0 and retry._decoder is None
    assert retry(raw)==json.loads(raw) and retry.activations==1
    bad=LazyArgumentDecoder(lambda:None)
    try:bad(raw)
    except TypeError:pass
    else:raise AssertionError('invalid factory result accepted')
    assert bad._decoder is None and bad.activations==0
    reentrant = None
    def reentrant_factory():
        assert reentrant(raw) == json.loads(raw)
        return make_decoder(load(args.library))
    reentrant = LazyArgumentDecoder(reentrant_factory)
    assert reentrant(raw) == json.loads(raw) and reentrant.activations == 1
    assert reentrant._initializing is False
    # A factory may intentionally impose a stricter eligibility policy. Do not
    # strip its guarded callable merely because it is an owned decoder type.
    module = load(args.library)
    original = module.float_tokens_finite
    token_calls = []
    def observed(value):
        token_calls.append(value)
        return original(value)
    module.float_tokens_finite = observed
    stricter = LazyArgumentDecoder(
        lambda: make_decoder(module, minimum_characters=4096),
        minimum_characters=512)
    assert stricter(raw) == json.loads(raw) and token_calls == []
    matched = LazyArgumentDecoder(
        lambda: make_decoder(module, minimum_characters=512),
        minimum_characters=512)
    assert matched(raw) == json.loads(raw) and len(token_calls) == 1
    # Creating state can fail before publication. The instance must remain
    # retryable; this resource failure must never be hidden by canonical parsing.
    original_state = lazy_module._InitializationState
    allocation_retry = LazyArgumentDecoder(lambda: json.loads)
    def fail_state_allocation():
        raise MemoryError('fixture initialization state allocation')
    lazy_module._InitializationState = fail_state_allocation
    try:
        try:
            allocation_retry(raw)
        except MemoryError:
            pass
        else:
            raise AssertionError('state allocation failure was hidden')
        assert allocation_retry._state is None and allocation_retry.activations == 0
    finally:
        lazy_module._InitializationState = original_state
    assert allocation_retry(raw) == json.loads(raw) and allocation_retry.activations == 1
    # The publication lock must not serialize native factory loading for
    # independent services. Both factories must reach the barrier together.
    independent_barrier = threading.Barrier(2)
    def independent_factory():
        independent_barrier.wait(timeout=5)
        return json.loads
    independent = [LazyArgumentDecoder(independent_factory) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda service: service(raw), independent)) == [json.loads(raw)] * 2
    assert all(service.activations == 1 for service in independent)
    report={'format':'tofu.native-json-lazy-check/v1','threads':8,'operations':1600,'activations':1,'canonical_controls_native_activations':0,'factory_failure_retry_invalid_result_reentry_and_policy_matching':'pass','state_allocation_failure_retry_and_independent_factory_concurrency':'pass'}
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
