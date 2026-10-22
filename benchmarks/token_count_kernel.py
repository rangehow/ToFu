"""Alternate independent native count-kernel binaries; no provider/application I/O.

Both binaries run the same token_ingress_measure fixture/oracle and allocator.
wait4 gives per-process CPU and Linux peak RSS; raw paired rows remain auditable.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess


def trial(binary):
    process=subprocess.Popen([str(binary)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output=process.stdout.read();errors=process.stderr.read()
    _,status,usage=os.wait4(process.pid,0)
    process.returncode=os.waitstatus_to_exitcode(status)
    if process.returncode:raise RuntimeError(errors.decode(errors='replace'))
    rows=[json.loads(line) for line in output.splitlines()]
    if {row['encoding'] for row in rows}!={'cl100k_base','o200k_base'}:
        raise ValueError('missing encoding observations')
    return {'rows':rows,'cpu_seconds':usage.ru_utime+usage.ru_stime,'peak_rss_bytes':usage.ru_maxrss*1024}


def measure(baseline,candidate,pairs):
    observations=[]
    for pair in range(pairs):
        order=('baseline','candidate') if pair%2==0 else ('candidate','baseline')
        record={'pair':pair,'order':list(order)}
        for label in order:record[label]=trial(baseline if label=='baseline' else candidate)
        observations.append(record)
    def med(label,key,encoding=None):
        values=[]
        for record in observations:
            result=record[label]
            values.append(result[key] if encoding is None else next(row[key] for row in result['rows'] if row['encoding']==encoding))
        return statistics.median(values)
    summary={}
    for encoding in ('cl100k_base','o200k_base'):
        summary[encoding]={}
        for key in ('cold_wall_ns','cold_cpu_ns','wall_ns','cpu_ns','retained_requested_bytes','peak_requested_bytes'):
            reference=med('baseline',key,encoding);result=med('candidate',key,encoding)
            summary[encoding][key]={'baseline':reference,'candidate':result,'ratio':result/reference}
    reference=med('baseline','peak_rss_bytes');result=med('candidate','peak_rss_bytes')
    summary['process_peak_rss']={'baseline':reference,'candidate':result,'ratio':result/reference}
    return {'scope':'Count-kernel fixture only; not application/provider performance admission',
            'pairs':pairs,'binaries':{label:{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for label,path in [('baseline',baseline),('candidate',candidate)]},
            'summary':summary,'raw':observations}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--pairs',type=int,default=25)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.pairs<1:parser.error('pairs must be positive')
    result=measure(args.baseline,args.candidate,args.pairs)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['summary']))
