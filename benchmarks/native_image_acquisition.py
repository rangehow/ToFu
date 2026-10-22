"""Compare concurrent pinned archive acquisition with shared immutable images.

Entry CLI --archive --output. Both arms use identical concurrent client counts;
timing includes archive self-check children, host CPU excludes child CPU. Report
unique memfd payload bytes, not RSS or whole-task performance. All FDs close.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lib.agent_core.native_artifact import seal_archive
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import Budget, _joined
from scripts.build_native_runtime import verify


async def measure(archive,digest,version,budget,count,shared):
    owner=None;descriptors=[]
    try:
        if shared:
            owner=NativeArchiveImage(archive=archive,sha256=digest,application_version=version,budget=budget)
        wall=time.perf_counter_ns();cpu=time.process_time_ns()
        jobs=[owner.duplicate(budget.binary_bytes) if shared else asyncio.to_thread(
            seal_archive,archive,digest,version,budget.binary_bytes,budget.shutdown_seconds,budget.frame_bytes)
            for _ in range(count)]
        results=await _joined(asyncio.gather(*jobs,return_exceptions=True))
        descriptors=[value for value in results if not isinstance(value,BaseException)]
        for value in results:
            if isinstance(value,BaseException):raise value
        elapsed=time.perf_counter_ns()-wall;used=time.process_time_ns()-cpu
        objects={(os.fstat(fd.fileno()).st_dev,os.fstat(fd.fileno()).st_ino):os.fstat(fd.fileno()).st_size
                 for fd in descriptors}
        return {'wall_ns':elapsed,'host_cpu_ns':used,'unique_kernel_images':len(objects),
                'unique_image_bytes':sum(objects.values())}
    finally:
        for descriptor in descriptors:descriptor.close()
        if owner is not None:await owner.close()


async def compare(args):
    archive=args.archive.resolve();manifest=verify(archive,run=True)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    budget=Budget(1,manifest['files']['bin/tofu-native']['bytes'],65536,65536,30)
    pairs=[]
    for index in range(args.pairs):
        row={}
        for shared in ([False,True] if index%2==0 else [True,False]):
            row['shared' if shared else 'independent']=await measure(archive,digest,
                manifest['build']['application_version'],budget,args.clients,shared)
        pairs.append(row)
    return {'format':'tofu.native-image-acquisition/v1','scope':'concurrent archive acquisition only',
            'clients':args.clients,'archive_sha256':digest,'pairs':pairs,
            'sources':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                ['lib/agent_core/native_image.py','lib/agent_core/native_artifact.py',
                 'lib/agent_core/native_process.py','benchmarks/native_image_acquisition.py']},
            'limits':['host CPU excludes self-check child CPU','unique payload bytes are not RSS',
                      'not task throughput or production lifecycle admission']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--clients',type=int,default=4)
    args=parser.parse_args()
    if args.pairs<=0 or args.clients<=0:parser.error('pairs and clients must be positive')
    args.output.write_text(json.dumps(asyncio.run(compare(args)),indent=2)+'\n')
