"""Native append/edit/compaction conflict, multimodal and protocol correctness."""
import argparse
import copy
import json
from pathlib import Path
import tempfile
from mutations import MutableContextCorpus, make_project
from lib.turn_lifecycle import LifecycleConflict


def check(engine):
    original=None
    for profile in ('openai','compatible','anthropic'):
        with tempfile.TemporaryDirectory(prefix='tofu-context-mutations-check-') as directory:
            corpus=MutableContextCorpus(Path(directory),engine,profile)
            try:
                reference=make_project('reference',corpus);candidate=make_project('specialized',corpus)
                before=corpus.messages();original=copy.deepcopy(before)
                corpus.append_pair(0);assert len(corpus.messages())==len(before)+2
                assert candidate(profile)==reference(profile) and before==original
                corpus.edit(1);committed=corpus.messages()
                assert committed[1]['content']=='edited answer 1'
                assert candidate(profile)==reference(profile)
                try:corpus.edit(2,expected_revision=0)
                except LifecycleConflict as error:assert error.code=='stale_projection'
                else:raise AssertionError('stale Turn edit accepted')
                assert corpus.messages()==committed
                corpus.compact(0,stale=True);corpus.compact(1)
                expected=reference(profile);assert candidate(profile)==expected
                if profile!='compatible':assert 'folded context fixture 1' in json.dumps(expected)
                committed=corpus.messages();corpus.close();corpus.open()
                assert corpus.messages()==committed and candidate(profile)==reference(profile)
            finally:corpus.close()
    with tempfile.TemporaryDirectory(prefix='tofu-context-image-check-') as directory:
        corpus=MutableContextCorpus(Path(directory),engine,multimodal=True)
        try:
            before=corpus.messages();assert before[-1]['content'][1]['type']=='image_url'
            reference=make_project('reference',corpus);candidate=make_project('specialized',corpus)
            expected=reference('openai');assert expected==candidate('openai')
            assert 'data:image/png;base64,' in json.dumps(expected)
            assert corpus.messages()==before
        finally:corpus.close()
    print('PASS: native append/edit CAS, stale-edit denial, atomic compaction and stale-revision preservation; exact protocol/token receipts, persisted history reopen, real inline PNG and immutable source')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--engine',type=Path,required=True)
    check(parser.parse_args().engine)
