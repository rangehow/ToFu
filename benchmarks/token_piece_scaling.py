"""Alternate frozen native long-piece fixtures with the same exact oracle."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess


def measure(baseline,candidate,pairs):
    raw=[]
    for pair in range(pairs):
        record={'pair':pair,'order':['baseline','candidate'] if pair%2==0 else ['candidate','baseline']}
        for name in record['order']:
            result=subprocess.run([str(baseline if name=='baseline' else candidate)],capture_output=True,check=True)
            record[name]=[json.loads(line) for line in result.stdout.splitlines()]
        raw.append(record)
    summary=[]
    for row in raw[0]['baseline']:
        key=(row['encoding'],row['shape'],row['bytes']);values={}
        for arm in ('baseline','candidate'):
            samples=[]
            for record in raw:
                value=next(value for value in record[arm] if (value['encoding'],value['shape'],value['bytes'])==key)
                if value['tokens']!=row['tokens']:raise ValueError('token oracle differs')
                samples.append(value['wall_ns'])
            values[arm]=statistics.median(samples)
        summary.append(dict(encoding=key[0],shape=key[1],bytes=key[2],**values,ratio=values['candidate']/values['baseline']))
    return {'scope':'Synthetic long-piece kernel; exact oracle outside timed region','pairs':pairs,
            'binaries':{name:{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for name,path in [('baseline',baseline),('candidate',candidate)]},
            'summary':summary,'raw':raw}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--pairs',type=int,default=5)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.pairs<1:parser.error('pairs must be positive')
    result=measure(args.baseline,args.candidate,args.pairs)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['summary']))
