"""Exercise producer races and publication failure through the actual runtime prototype."""
from concurrent.futures import ThreadPoolExecutor
from runtime import create, normalized

def check():
    for variant in ('reference','native'):
        runtime,task=create(variant,256)
        identity=task['id']
        runtime.mark_running(identity)
        def produce(producer):
            sequences=[]
            for offset in range(500):
                sequences.append(runtime.append_event(identity,
                    {'type':'delta','producer':producer,'offset':offset},deliver_push=False))
            return sequences
        with ThreadPoolExecutor(max_workers=4) as pool:
            sequences=list(pool.map(produce,range(4)))
        assert sorted(value for row in sequences for value in row)==list(range(2000))
        assert all(row==sorted(row) for row in sequences)
        payload=runtime.poll(identity,0)
        assert payload['next_cursor']==2000
        assert [event['seq'] for event in payload['events']]==list(range(1744,2000))
        assert payload['cursor']['reset'] is True
        def fail(seq):
            raise OSError('injected durable sink failure')
        event={'type':'delta','attemptId':'fixture-attempt','delta':'withheld'}
        before=task['_eventNextSeq']
        runtime.append_event(identity,event,before_push=fail,deliver_push=False)
        assert task['_eventNextSeq']==before+1
        assert task['_pushWithheldCount']==1
        assert runtime.poll(identity,before)['events'][-1]['delta']=='withheld'
        runtime.append_event(identity,{'type':'delta','delta':'recovered'},
                             before_push=lambda seq:None,deliver_push=False)
        assert '_pushWithheldCount' not in task
        runtime.append_event(identity,{'type':'delta','delta':'x'*(2*1024*1024)},deliver_push=False)
        assert len(task['events'])==0 and task['_eventRetainedBytes']==0
        assert runtime.poll(identity,0)['events']==[]
        runtime.append_event(identity,{'type':'delta','delta':'after-reset'},deliver_push=False)
        assert runtime.poll(identity,0)['events'][0]['delta']=='after-reset'
        assert runtime.abort(identity)
        assert task['abort_event'].is_set()
        runtime.finish(identity,result='fixture-result')
        assert runtime.poll(identity,task['_eventBaseSeq'])['done'] is True
    print('four concurrent producers, withheld persistence/recovery, oversize reset and abort/finish passed')

if __name__=='__main__':
    check()
