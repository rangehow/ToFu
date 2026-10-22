"""Experimental immutable input revisions and incremental sanitized wire JSON.

No context acquisition, admission or persistence authority. Only exact builtin
JSON input is accepted. Canonical sanitization remains the oracle and fallback.
"""
from dataclasses import dataclass
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from lib.tasks_pkg.wire_messages import apply_wire_sanitize, _gateway_sanitize_enabled
from lib.llm_sanitize._toolcalls import _TOOL_NAME_VALID_RE


def encode(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()


def validate_json(value):
    kind=type(value)
    if kind in (str,int,float,bool,type(None)):return
    if kind is list:
        for item in value:validate_json(item)
        return
    if kind is dict and all(type(key) is str for key in value):
        for item in value.values():validate_json(item)
        return
    raise TypeError('committed history requires exact builtin JSON values')


@dataclass(frozen=True)
class Record:
    payload: bytes
    role: str
    calls: tuple[str,...]
    result: str
    safe: bool

    @classmethod
    def capture(cls,message):
        validate_json(message)
        role=message.get('role','') if type(message) is dict else ''
        calls=[];safe=role in ('system','user','assistant','tool')
        raw=message.get('tool_calls') if type(message) is dict else None
        if raw:
            safe= safe and role=='assistant' and type(raw) is list
            for call in raw if type(raw) is list else []:
                fn=call.get('function',{}) if type(call) is dict else {}
                identifier=call.get('id') if type(call) is dict else None
                valid=(type(identifier) is str and bool(identifier)
                    and call.get('type')=='function' and type(fn) is dict
                    and type(fn.get('name')) is str and bool(_TOOL_NAME_VALID_RE.match(fn['name']))
                    and type(fn.get('arguments')) is str)
                safe=safe and valid
                if valid:calls.append(identifier)
        elif role=='assistant':
            content=message.get('content')
            safe=safe and (type(content) is str and bool(content.strip())
                or type(content) is list and any(type(block) is dict and
                    (block.get('type')=='image_url' or type(block.get('text')) is str and block['text'].strip())
                    for block in content))
        result=message.get('tool_call_id','') if role=='tool' else ''
        if role=='tool':safe=safe and type(result) is str and bool(result)
        return cls(encode(message),role,tuple(calls),result,bool(safe))


@dataclass(frozen=True)
class History:
    owner_id: int
    records: tuple[Record,...] = ()
    def materialize_provider_input(self,owner_id):
        """Detached raw source IR for the actual body/protocol owners.

        Sanitized debug/wire snapshots cannot replace this input: private
        native carriers and preparation diagnostics must reach those owners.
        """
        if (type(owner_id) is not int or type(self.owner_id) is not int
                or self.owner_id<1 or owner_id!=self.owner_id):
            raise PermissionError('foreign committed history')
        return [json.loads(record.payload) for record in self.records]
    def append(self,message):
        return History(self.owner_id,self.records+(Record.capture(message),))
    def replace(self,index,message):
        rows=list(self.records);rows[index]=Record.capture(message)
        return History(self.owner_id,tuple(rows))
    def compact(self,first_retained,summary):
        return History(self.owner_id,(Record.capture(summary),)+self.records[first_retained:])


def safe_cut(records):
    claimed=set();pending=set();group_start=0;previous=None
    for index,record in enumerate(records):
        if not record.safe:return None
        if record.role=='tool':
            if record.result not in pending:return None
            pending.remove(record.result)
        else:
            if pending:return None
            if previous and previous.role==record.role and record.role in ('user','assistant') and not previous.calls and not record.calls:
                return None
            if record.calls:
                group_start=index
                if len(set(record.calls))!=len(record.calls) or claimed.intersection(record.calls):return None
                claimed.update(record.calls);pending=set(record.calls)
        previous=record
    return group_start if pending else len(records)


class Compiler:
    def __init__(self,owner_id,maximum_cache_bytes):
        if owner_id<1 or maximum_cache_bytes<1:raise ValueError('explicit owner and positive cache bound required')
        self.owner_id=owner_id;self.maximum_cache_bytes=maximum_cache_bytes
        self.close()
    def close(self):
        from collections import OrderedDict
        self.groups=OrderedDict();self.retained_bytes=0;self.reused_messages=0
    def retain(self,key,group,chunks):
        size=sum(len(record.payload) for record in group)+sum(map(len,chunks))
        if size>self.maximum_cache_bytes:return
        previous=self.groups.pop(key,None)
        if previous is not None:self.retained_bytes-=previous[1]
        while self.groups and self.retained_bytes+size>self.maximum_cache_bytes:
            _,(_,retired_size)=self.groups.popitem(last=False)
            self.retained_bytes-=retired_size
        self.groups[key]=(chunks,size);self.retained_bytes+=size
    def compile(self,history,provider_id=''):
        if history.owner_id!=self.owner_id:raise PermissionError('foreign committed history')
        cut=safe_cut(history.records)
        self.reused_messages=0
        if cut is None:
            self.close()
            return reference(history,provider_id)
        policy=_gateway_sanitize_enabled(provider_id)
        # First use performs the canonical pipeline once. Splitting its output
        # is safe only when validated closed inputs preserve one message each.
        if not self.groups:
            messages=[json.loads(record.payload) for record in history.records]
            chunks=tuple(encode(message) for message in apply_wire_sanitize(
                messages,provider_id=provider_id,user_id=self.owner_id))
            if len(chunks)==len(history.records):
                position=0
                while position<cut:
                    end=position+1
                    if history.records[position].calls:
                        while end<cut and history.records[end].role=='tool':end+=1
                    group=history.records[position:end]
                    self.retain((policy,group),group,chunks[position:end])
                    position=end
            return b'['+b','.join(chunks)+b']'
        output=[];position=0
        while position<len(history.records):
            end=position+1
            if history.records[position].calls:
                while end<len(history.records) and history.records[end].role=='tool':end+=1
            group=history.records[position:end]
            key=(policy,group)
            cached=self.groups.get(key)
            if cached is not None:
                chunks,size=cached;self.groups.move_to_end(key)
                self.reused_messages+=len(group)
            else:
                messages=[json.loads(record.payload) for record in group]
                chunks=tuple(encode(message) for message in apply_wire_sanitize(
                    messages,provider_id=provider_id,user_id=self.owner_id))
                size=0
                if end<=cut and len(chunks)==len(group):
                    self.retain(key,group,chunks)
            output.extend(chunks);position=end
        return b'['+b','.join(output)+b']'


def reference(history,provider_id=''):
    messages=[json.loads(record.payload) for record in history.records]
    return encode(apply_wire_sanitize(messages,provider_id=provider_id,user_id=history.owner_id))
