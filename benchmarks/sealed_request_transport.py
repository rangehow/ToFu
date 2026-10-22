#!/usr/bin/env python3
"""Compare actual requests HTTP dispatch of admitted JSON versus retained bytes.

Offline synthetic upstream, no provider calls or storage. The listener owns a
separate process so client CPU/RSS exclude upstream parsing. This measures the
transport segment, not an incremental context compiler or model latency.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from lib.llm.request import seal_request, transport_body, transport_body_arguments


def fixture():
    body={'model':'synthetic','stream':True,'messages':[
        {'role':'user' if index%2==0 else 'assistant',
         'content':f'{index} '+('bounded context 中文 '+str(index)+' ')*128}
        for index in range(128)]}
    return seal_request(body,model='synthetic',input_tokens=1,semantic_id='fixture')


def serve(connection, accepted_hashes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        # Avoid a fixture-only delayed-ACK/Nagle pause between response headers
        # and its tiny body. This setting is identical in both variants.
        disable_nagle_algorithm=True
        def log_message(self,*args):
            pass
        def do_POST(self):
            size=int(self.headers.get('Content-Length','0'))
            if size<1 or size>2*1024*1024:
                self.send_error(413);return
            payload=self.rfile.read(size)
            valid=(hashlib.sha256(payload).hexdigest() in accepted_hashes
                   and self.headers.get('Content-Type')=='application/json')
            self.send_response(200 if valid else 400)
            self.send_header('Content-Length','2')
            self.end_headers();self.wfile.write(b'{}')
            self.wfile.flush()
    server=HTTPServer(('127.0.0.1',0),Handler)
    connection.send(server.server_address[1]);connection.close()
    try:
        server.serve_forever()
    finally:
        server.server_close()


def worker(variant,samples,memory):
    sealed=fixture()
    baseline=requests.Request('POST','http://fixture.invalid',json=transport_body(sealed)).prepare().body
    if isinstance(baseline,str):baseline=baseline.encode()
    assert json.loads(baseline)==json.loads(sealed.payload)
    parent,child=multiprocessing.Pipe(duplex=False)
    server=multiprocessing.Process(target=serve,args=(child,
        {hashlib.sha256(baseline).hexdigest(),hashlib.sha256(sealed.payload).hexdigest()}))
    server.start();child.close()
    session=requests.Session();session.trust_env=False
    raw=[]
    try:
        if not parent.poll(10):raise RuntimeError('owned upstream did not become ready')
        url=f'http://127.0.0.1:{parent.recv()}/fixture'
        def operation():
            if variant=='decoded':
                response=session.post(url,json=transport_body(sealed),timeout=10)
            else:
                response=session.post(url,**transport_body_arguments(sealed,transport='requests'),
                    headers={'Content-Type':'application/json'},timeout=10)
            response.raise_for_status()
            assert response.content==b'{}'
            response.close()
        for _ in range(20):operation()
        for _ in range(samples):
            if memory:
                operation();continue
            started=time.perf_counter_ns();cpu=time.process_time_ns()
            operation()
            raw.append({'wall_ms':(time.perf_counter_ns()-started)/1e6,
                        'cpu_ms':(time.process_time_ns()-cpu)/1e6})
        if memory:
            return {'peak_client_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}
        wall=sorted(row['wall_ms'] for row in raw)
        return {'p50_ms':statistics.median(wall),'p95_ms':wall[int((samples-1)*.95)],
                'p99_ms':wall[int((samples-1)*.99)],
                'cpu_ms_per_operation':statistics.mean(row['cpu_ms'] for row in raw),
                'wire_bytes':len(baseline) if variant=='decoded' else len(sealed.payload),
                'client_pid':os.getpid(),'upstream_pid':server.pid,'raw':raw}
    finally:
        session.close();parent.close()
        server.terminate();server.join(5)
        if server.is_alive():server.kill();server.join(5)
        if server.is_alive():raise RuntimeError('owned upstream did not settle')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--worker',choices=['decoded','sealed'])
    parser.add_argument('--memory',action='store_true')
    parser.add_argument('--samples',type=int,default=1000)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.samples<100:parser.error('at least 100 samples required')
    if args.worker:
        print(json.dumps(worker(args.worker,args.samples,args.memory)));return
    if args.output is None:parser.error('--output outside checkout is required')
    report={'scope':'real HTTP client dispatch to owned synthetic upstream; excludes context/admission/model/storage',
            'whole_system_verdict':'incomplete','python':sys.version,'requests':requests.__version__,
            'platform':platform.platform(),'affinity':sorted(os.sched_getaffinity(0)),
            'samples_per_variant':args.samples,'memory_separate_processes':True,'pairs':[],
            'source_sha256':{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in (Path(__file__),ROOT/'lib/llm/request.py')}}
    for pair in range(3):
        result={}
        for variant in (('decoded','sealed') if pair%2==0 else ('sealed','decoded')):
            for name,digest in report['source_sha256'].items():
                if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
                    raise RuntimeError('measured source changed: '+name)
            command=[args.python,__file__,'--worker',variant,'--samples',str(args.samples)]
            measured=subprocess.run(command,check=True,capture_output=True,text=True,timeout=120)
            result[variant]=json.loads(measured.stdout)
            measured=subprocess.run(command+['--memory'],check=True,capture_output=True,text=True,timeout=120)
            result[variant].update(json.loads(measured.stdout))
        report['pairs'].append(result)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        print(pair,{key:result['sealed'][key]/result['decoded'][key] for key in
                    ('p50_ms','p99_ms','cpu_ms_per_operation','peak_client_rss_bytes')},flush=True)


if __name__=='__main__':
    main()
