#!/usr/bin/env python3
"""Offline Python/native Anthropic tool-input translation kernel comparison.

Build the current crate example with --release, then pass --native-probe. Both
owners parse the same event shape and return a verified full replay snapshot.
This excludes SSE framing, HTTP, billing, model latency and process startup.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
from lib.llm.anthropic_outbound._sse import AnthropicSSETranslator

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--native-probe',type=Path,required=True);parser.add_argument('--fragments',type=int,default=10000);args=parser.parse_args()
 if not 1<=args.fragments<=100000:parser.error('fragments must be in 1..100000')
 probe=args.native_probe.resolve(strict=True)
 native=json.loads(subprocess.check_output([str(probe),str(args.fragments)],text=True))
 n=args.fragments
 assert native['fragments']==n and native['events']==n+4 and native['input_bytes']==n
 def wire(event):return json.dumps(event,ensure_ascii=False,separators=(',',':'))
 first=wire({'type':'content_block_start','index':0,'content_block':{'type':'server_tool_use','id':'host','name':'search','input':{}}})
 def delta(text):return wire({'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':text}})
 begin,append,end=delta('{"text":"'),delta('x'),delta('"}')
 stop=wire({'type':'message_stop'})
 samples=[]
 for _ in range(7):
  started=time.perf_counter();owner=AnthropicSSETranslator()
  for raw in (first,begin):assert owner.translate(raw)==[]
  for _ in range(n):assert owner.translate(append)==[]
  assert owner.translate(end)==[];assert owner.translate(stop)==['[DONE]']
  snapshot=owner.anthropic_content_blocks
  assert snapshot[0]['input']['text']=='x'*n
  samples.append(time.perf_counter()-started)
 result={'scope':'translation kernel only; no application speedup claim','fragments':n,'events':n+4,
  'python_samples_seconds':samples,'native_samples_seconds':native['samples_seconds'],
  'python_median_seconds':statistics.median(samples),'native_median_seconds':statistics.median(native['samples_seconds']),
  'native_charge_bytes':native['charged_bytes'],'native_probe_sha256':hashlib.sha256(probe.read_bytes()).hexdigest()}
 result['median_ratio']=result['python_median_seconds']/result['native_median_seconds']
 root=Path(__file__).resolve().parents[1]
 result['source_sha256']={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in (
  'lib/llm/anthropic_outbound/_sse.py','packages/tofu-runtime/src/anthropic.rs','packages/tofu-runtime/examples/anthropic_stream_cost.rs')}
 print(json.dumps(result,indent=2))
if __name__=='__main__':main()
