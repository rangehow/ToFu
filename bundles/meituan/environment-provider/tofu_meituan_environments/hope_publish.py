"""Standalone Worker publication wrapper; no SDK/host imports are required.

Runs the sealed generic runner, snapshots declared result files, and publishes
once to an operation-specific private Hadoop namespace before emitting success.
Unknown/failed publication is never replayed; its exclusive intent is retained.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

RECEIPT_CONTRACT_FILE = 'hope_worker_receipt_contract.json'

def receipt_contract():
    """Machine-readable receipt name/version/byte bound; no I/O at import."""
    return json.loads(Path(__file__).with_name(RECEIPT_CONTRACT_FILE).read_text())


def _call(command, deadline):
    remaining = deadline-time.monotonic()
    if remaining <= 0: raise TimeoutError('publication deadline')
    child = subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        code = child.wait(timeout=remaining)
        if code != 0: raise RuntimeError('publication command failed')
    finally:
        try: os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError: pass  # The completed owned group no longer exists.
        child.wait()


def publish(result, root, configuration, deadline):
    """Configuration is host-sealed; remote paths are never taken from task output."""
    root = Path(root)
    if result['state'] != 'succeeded': return
    if (not {'command','namespace','outputs'} <= set(configuration) or
            set(configuration)-{'command','namespace','outputs','parallel_upload','worker_receipt'}):
        raise ValueError('sealed publication fields required')
    if 'worker_receipt' in configuration:
        expected = configuration['namespace'].rstrip('/')+'/'+receipt_contract()['receipt_name']
        if configuration['worker_receipt'] != {'uri':expected}:
            raise ValueError('owned control receipt URI required')
    if [x['name'] for x in configuration['outputs']] != [x['name'] for x in result['outputs']]:
        raise ValueError('publication declaration mismatch')
    command = configuration['command']; namespace = configuration['namespace']
    upload=configuration.get('parallel_upload');jar=None
    if upload is not None:
        if (not isinstance(upload,dict) or set(upload)!={'jar','sha256','threshold_bytes','lanes'}
                or upload['jar']!='tofu-parallel-upload.jar'
                or type(upload['lanes']) is not int or upload['lanes']<1
                or type(upload['threshold_bytes']) is not int or upload['threshold_bytes']<1):
            raise ValueError('sealed upload configuration required')
        jar=root/upload['jar']
        if jar.is_symlink() or not jar.is_file() or jar.stat().st_size>16777216:
            raise ValueError('upload artifact invalid')
        with jar.open('rb') as stream:raw=stream.read(16777217)
        if len(raw)>16777216 or hashlib.sha256(raw).hexdigest()!=upload['sha256']:
            raise ValueError('upload artifact changed')

    # The intent precedes the first external write, survives uncertainty, and is
    # never removed as permission to replay a partially accepted publication.
    with (root/'tofu-publication-intent.json').open('x') as stream:
        json.dump(configuration,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
    with tempfile.TemporaryDirectory(prefix='tofu-publication-') as temporary:
        snapshots = []
        for index, (declared, receipt) in enumerate(zip(configuration['outputs'],result['outputs'])):
            original = root/receipt['name']; candidate = root
            for part in Path(receipt['name']).parts:
                candidate = candidate/part
                if candidate.is_symlink(): raise ValueError('unsafe publication file')
            digest = hashlib.sha256();size = 0;copy = Path(temporary)/str(index)
            with original.open('rb') as src, copy.open('xb') as dst:
                while True:
                    if time.monotonic() >= deadline: raise TimeoutError('publication deadline')
                    chunk = src.read(min(65536, receipt['bytes']-size+1))
                    if not chunk: break
                    size += len(chunk)
                    if size > receipt['bytes']: raise ValueError('publication file changed')
                    digest.update(chunk);dst.write(chunk)
                dst.flush();os.fsync(dst.fileno())
            if size != receipt['bytes'] or digest.hexdigest() != receipt['sha256']:
                raise ValueError('publication file changed')
            snapshots.append((copy,declared['uri']))
        _call(command+['fs','-mkdir',namespace],deadline)
        _call(command+['fs','-chmod','700',namespace],deadline)
        parents = sorted({uri.rsplit('/',1)[0] for _,uri in snapshots if uri.rsplit('/',1)[0] != namespace},key=lambda x:(x.count('/'),x))
        # -p is restricted to descendants of the newly-created owned namespace.
        for parent in parents: _call(command+['fs','-mkdir','-p',parent],deadline)
        for index,(copy,uri) in enumerate(snapshots):
            receipt=result['outputs'][index]
            if upload is not None and receipt['bytes']>=upload['threshold_bytes']:
                _call(command+['jar',str(jar),'CheckpointFileTransport','upload',str(copy),uri,
                    str(receipt['bytes']),receipt['sha256'],str(upload['lanes'])],deadline)
            else: _call(command+['fs','-put',str(copy),uri],deadline)



def execute(execution, publication, workdir, runner):
    start = time.monotonic();result = runner(execution,workdir)
    if result['state'] == 'succeeded':
        try: publish(result,workdir,publication,start+execution['limits']['runtime_seconds'])
        except Exception:
            result.update(state='failed',error='publication_failed_or_unknown')
    result['elapsed_seconds'] = time.monotonic()-start
    if result['state']=='succeeded' and 'worker_receipt' in publication:
        # Freeze the same bytes for native storage and stdout. The final control
        # commit is bounded by the remaining execution deadline, never replayed.
        try:
            payload=json.dumps(result,sort_keys=True,allow_nan=False).encode()
            if len(payload)>receipt_contract()['maximum_bytes']:
                raise ValueError('control receipt byte limit')
            with tempfile.TemporaryDirectory(prefix='tofu-control-receipt-') as tmp:
                path=Path(tmp)/'receipt.json'
                with path.open('xb') as stream:
                    stream.write(payload);stream.flush();os.fsync(stream.fileno())
                _call(publication['command']+['fs','-put',str(path),publication['worker_receipt']['uri']],
                      start+execution['limits']['runtime_seconds'])
        except Exception:
            result.update(state='failed',error='worker_receipt_publication_failed_or_unknown')
            result['elapsed_seconds']=time.monotonic()-start
    return result


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('execution');parser.add_argument('publication');args=parser.parse_args()
    root=Path.cwd()
    module_spec=importlib.util.spec_from_file_location('tofu_job_runner',root/'tofu-job-runner.py')
    module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
    result=execute(json.loads(Path(args.execution).read_text()),json.loads(Path(args.publication).read_text()),root,module.run_job)
    print('TOFU_JOB_RESULT='+json.dumps(result,sort_keys=True),flush=True)
    raise SystemExit(0 if result['state']=='succeeded' else 1)

if __name__=='__main__':main()
