"""Native durability, cold replay, duplicate receipts and actual failure recovery."""
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from durable_runtime import DurableEventRuntime


def check(engine):
    for variant in ('reference', 'candidate'):
        with tempfile.TemporaryDirectory(prefix='tofu-event-native-check-') as directory:
            owned = DurableEventRuntime(Path(directory), engine, variant, count=1024)
            try:
                owned.seed(1050)
                assert [r['event_id'] for r in owned.replay(0,1050)] == list(range(1050))
                assert owned.memory_page(0)['cursor']['reset'] is True
                with ThreadPoolExecutor(max_workers=4) as pool:
                    sequences = list(pool.map(lambda producer: owned.append(2000+producer,producer=producer),range(4)))
                assert sorted(sequences)==list(range(1050,1054))
                assert sorted(r['payload']['producer'] for r in owned.replay(1050,4))==list(range(4))
                before=owned.task['_eventNextSeq'];owned.client.fail_writes=True
                owned.append(9999,authoritative=True)
                assert owned.task['_pushWithheldCount']==1
                assert owned.replay(before,1)==[]
                assert owned.client.failed_commands==['event.append_batch','event.append']
                owned.client.fail_writes=False
                owned.append(10000,authoritative=True)
                assert '_pushWithheldCount' not in owned.task
                recovered=owned.replay(before,2)
                assert [r['event_id'] for r in recovered]==[before+1]
                event=owned.task['events'][-1]
                receipt=owned.log.append_persistent_event(owned.identity,event['seq'],event)
                assert receipt.get('inserted') is False
                assert len(owned.replay(before,2))==1
                # Close/join transport, then cold reopen the same owned authority.
                assert owned.batcher.close(timeout=30);owned.batcher=None
                owned.authority.close();owned.authority.open()
                rows=owned.authority.client.query('event.list',{'task_id':owned.identity,'after_sequence':before-1,'limit':8})
                assert [r['sequence'] for r in rows]==[before+1]
            finally:
                owned.close()
    print('PASS: real native commits/replay/reopen; four producers; pre-dispatch batch+fallback failure; withheld frame, recovery, deduplication and owned teardown')


if __name__=='__main__': check(Path(os.environ['TOFU_EVENT_NATIVE_ENGINE']))
