"""Differential histories cover edits, compaction, policy, pending calls and media."""
import json
import random
import compiler as implementation
from compiler import History,Compiler,reference

def check():
    rng=random.Random(20261007)
    history=History(900001);compiler=Compiler(900001,4*1024*1024)
    examples=[{'role':'user','content':'start'},
        {'role':'assistant','tool_calls':[{'id':'call','type':'function','function':{'name':'read','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'call','content':'completed'},
        {'role':'assistant','content':'answer','reasoning_content':'reasoning'},
        {'role':'user','content':[{'type':'text','text':'image'},{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]},
        {'role':'assistant','content':''},None,
        {'role':'user','content':'second','_contextComposer':{'source':'fixture'}}]
    source={'role':'user','content':'immutable'};frozen=history.append(source);source['content']='mutated'
    assert json.loads(frozen.records[0].payload)['content']=='immutable'
    for index in range(2000):
        action=rng.randrange(10)
        if action<7 or not history.records:history=history.append(rng.choice(examples))
        elif action<9:history=history.replace(rng.randrange(len(history.records)),rng.choice(examples))
        else:history=history.compact(len(history.records)//2,{'role':'user','content':'summary'})
        provider='sankuai-fixture' if index%17==0 else ''
        actual=compiler.compile(history,provider)
        expected=reference(history,provider)
        assert actual==expected,(index,actual,expected)
    try:compiler.compile(History(900002))
    except PermissionError:pass
    else:raise AssertionError('foreign owner authorized')
    compiler.close();assert compiler.retained_bytes==0
    repeated=History(900001)
    for _ in range(100):repeated=repeated.append({'role':'system','content':'same policy'})
    budgeted=Compiler(900001,256)
    assert budgeted.compile(repeated)==reference(repeated)
    assert budgeted.retained_bytes==sum(size for _,size in budgeted.groups.values())<=256
    history=History(900001).append({'role':'user','content':'tool task'})
    history=history.append({'role':'assistant','tool_calls':[{'id':'seed','type':'function',
        'function':{'name':'read','arguments':'{}'}}]})
    history=history.append({'role':'tool','tool_call_id':'seed','content':'seed result'})
    original=implementation.apply_wire_sanitize
    expected=reference(history)
    calls=[]
    def observed(messages,**kwargs):
        calls.append(len(messages))
        return original(messages,**kwargs)
    implementation.apply_wire_sanitize=observed
    try:
        fresh=Compiler(900001,65536)
        assert fresh.compile(history)==expected
        assert calls==[3],calls
    finally:
        implementation.apply_wire_sanitize=original
    for index in range(200):
        call={'id':f'valid-{index}','type':'function','function':{'name':'read','arguments':'{}'}}
        history=history.append({'role':'assistant','tool_calls':[call]})
        assert compiler.compile(history)==reference(history)
        history=history.append({'role':'tool','tool_call_id':call['id'],'content':'result'})
        assert compiler.compile(history)==reference(history)
        if index>0:assert compiler.reused_messages>0
    history=history.replace(3,{'role':'assistant','tool_calls':[{'id':'changed','type':'function','function':{'name':'read','arguments':'{}'}}]})
    assert compiler.compile(history)==reference(history)
    compiler=Compiler(900001,1)
    assert compiler.compile(history)==reference(history) and compiler.retained_bytes==0
    print('2000 randomized revisions, 400 incremental batches, early edit, owner denial and bounded teardown passed')

if __name__=='__main__':check()
