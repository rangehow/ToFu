"""Actual native Turn mutations before producer/body/token/protocol projection.

Entry: MutableContextCorpus. Reuse owned native authority; bind canonical Turn
facade and compaction-store method to its client in private namespaces. Summary
text is a deterministic fixture; no model summary or provider call is simulated.
"""
import base64
import copy
import io
from pathlib import Path
import sys
import types

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).parent))
from native_producer import NativeContextCorpus, make_project
from native_copy import fixture as native_fixture
from check_provider import OWNER
from lib import turn_lifecycle
from lib.tasks_pkg import persistence_store
from PIL import Image


def multimodal_row():
    buffer=io.BytesIO();Image.new('RGB',(64,64),(20,40,60)).save(buffer,format='PNG')
    uri='data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()
    return {'role':'user','content':[{'type':'text','text':'inspect this owned image'},
                                   {'type':'image_url','image_url':{'url':uri}}]}


class MutableContextCorpus(NativeContextCorpus):
    def __init__(self,root,engine,profile='openai',*,multimodal=False):
        super().__init__(root,engine)
        self.profile=profile;self.append_count=0;self.compaction_count=0
        try:
            rows=native_fixture(profile,turns=16)
            if multimodal: rows[-1]=multimodal_row()
            self.populate(rows)
            namespace=dict(vars(turn_lifecycle))
            for name, original in vars(turn_lifecycle).items():
                if isinstance(original,types.FunctionType) and original.__globals__ is vars(turn_lifecycle):
                    clone=types.FunctionType(original.__code__,namespace,name,original.__defaults__,original.__closure__)
                    clone.__kwdefaults__=original.__kwdefaults__;namespace[name]=clone
            namespace['_turn_client']=lambda **_:self.client
            self.turns=types.SimpleNamespace(**namespace)
            method=persistence_store.DefaultConversationStore.compact_turn_transcript
            space=dict(vars(persistence_store),_storage_client=lambda **_:self.client)
            bound=types.FunctionType(method.__code__,space,method.__name__,method.__defaults__,method.__closure__)
            bound.__kwdefaults__=method.__kwdefaults__
            self.compact_transcript=types.MethodType(bound,persistence_store.DefaultConversationStore())
        except BaseException:
            self.close();raise

    def append_row(self,row):
        value=copy.deepcopy(row);role=value.pop('role');actor='human' if role=='user' else role
        for wire,stored in (('_responses_items','_responsesItems'),('_anthropic_content_blocks','_anthropicContentBlocks')):
            if wire in value:value[stored]=value.pop(wire)
        command='mutation-append-'+str(self.append_count)
        self.append_count+=1
        return self.turns.append_settled_turn(self.conversation_id,command_id=command,actor=actor,
            projection=value,user_id=OWNER,created_at=1701000000000+self.append_count*1000)

    def append_pair(self,index):
        self.append_row({'role':'assistant','content':f'added answer {index}'})
        self.append_row({'role':'user','content':f'added question {index}'})

    def edit(self,index,*,expected_revision=None):
        turn=self.turns.get_turn(self.conversation_id,'context-turn-1',user_id=OWNER)
        projection=copy.deepcopy(turn['projection']);projection['content']=f'edited answer {index}'
        revision=turn['projectionRevision'] if expected_revision is None else expected_revision
        return self.turns.update_turn_projection(self.conversation_id,'context-turn-1',projection=projection,
            expected_projection_revision=revision,user_id=OWNER)

    def compact(self,index,*,stale=False):
        self.append_pair(index);self.append_pair(index+100000)
        current=self.messages();assert len(current)>6
        summary={'role':'assistant','content':f'folded context fixture {index}', '_isCompactionSummary':True,
                 'compaction':{'blockId':'compaction','sourceTurns':5}}
        desired=[current[0],summary,*current[6:]]
        revision=self.turns.get_conversation_revision(self.conversation_id,user_id=OWNER)
        command=f'mutation-compaction-{self.compaction_count}'
        self.compaction_count+=1
        applied=self.compact_transcript(self.conversation_id,current,desired,revision-(1 if stale else 0),
            command_id=command+'-'+('stale' if stale else 'accepted'),user_id=OWNER)
        if stale: assert applied==0 and self.messages()==current
        else:
            assert applied==1
            after=self.messages();assert len(after)==len(current)-4
            assert after[0]['_turnId']==current[0]['_turnId']
            assert [r['_turnId'] for r in after[2:]]==[r['_turnId'] for r in current[6:]]
        return applied
