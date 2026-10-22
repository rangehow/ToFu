"""Actual final local token admission around the existing protocol-copy comparison.

Canonical build_body, final adapted-body recount and budget clamp stay identical
in both arms. Wire identifiers are synthetic; no provider/model calls or default
changes. This excludes durable producers, media acquisition and compaction.
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from lib.llm.body import _build
from lib.llm.body._admission import enforce_body_input_limit
from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY
import conversion_copy
import native_copy
from conversion_copy_measure import receipt


def make_project(variant):
    projection=native_copy.project if variant=='reference' else conversion_copy.candidate_project()
    projection=conversion_copy.clone(projection,asdict=receipt)
    def project(rows,profile):
        counts=[]
        def admitted_builder(model,messages,**options):
            body=_build.build_body(model,messages,**options)
            count=enforce_body_input_limit(body,model=model,context_window=options['context_window'])
            assert body[ADMITTED_INPUT_TOKENS_KEY]==count and count>32
            counts.append(count)
            return body
        output=projection(rows,profile,admitted_builder)
        assert len(counts)==1
        return dict(output,admitted_input_tokens=counts[0])
    return project


def check():
    import copy
    from lib.llm.llm_errors import PromptTooLongError
    from lib.token_counter import count_tokens
    from native_copy_measure import fixture
    baseline=make_project('reference');candidate=make_project('specialized')
    checks=[]
    for profile in ('openai','compatible','anthropic'):
        for density in ('heavy','sparse','none'):
            rows=fixture(profile,density);untouched=copy.deepcopy(rows)
            expected=baseline(rows,profile);actual=candidate(rows,profile)
            assert actual==expected and rows==untouched
            if actual['payload'] is not None:
                actual['payload'].clear();assert candidate(rows,profile)==expected
            checks.append({'profile':profile,'density':density,'exact_count_payload_and_receipt_parity':True})
    body={'messages':[{'role':'user','content':'small fixture'}],ADMITTED_INPUT_TOKENS_KEY:1}
    initial=enforce_body_input_limit(body,model='gpt-4o',context_window=4096,reuse_admission=True)
    body['messages'][0]['content']='expanded token admission fixture 中文 '*10000
    try:enforce_body_input_limit(body,model='gpt-4o',context_window=4096,reuse_admission=True)
    except PromptTooLongError:pass
    else:raise AssertionError('stale supplied admission skipped final recount')
    assert initial>1
    facts=count_tokens([{'role':'user','content':'local counting proof'}],model='gpt-4o',mode='tiktoken')
    assert facts['method']=='tiktoken','actual local tokenizer required; fallback is not the measured backend'
    print('nine final-token/protocol parity cases; detached payload; stale supplied count rejected after mutation; actual local tiktoken')
    return checks


if __name__=='__main__':check()
