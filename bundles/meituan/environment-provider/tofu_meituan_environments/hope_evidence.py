"""Owned Hope execution receipts: repository binding, Worker exit and resources.

HopeEvidencePort is a trusted host callback for known submitted references.
Unknown submission reconciliation remains a separately supplied read-only port.
No login, network, storage selection or background work occurs at import.
"""
import hashlib
import json
import math
import re
import time
from tofu_environments.contracts import EnvironmentError,seal
from tofu_environments.jobs import validate_reference
from .hope_attempts import PREEMPTION_DIAGNOSTIC, select_worker_attempt
from .hope_publish import RECEIPT_CONTRACT_FILE, receipt_contract

def decode_hope_response(raw):
    if isinstance(raw,dict):return raw
    if not isinstance(raw,str):raise ValueError('Hope response must be an object or JSON text')
    text=raw.lstrip().removeprefix('MCP Error: ').lstrip()
    value,end=json.JSONDecoder().raw_decode(text)
    remainder=text[end:].strip()
    if remainder:
        repeated,finish=json.JSONDecoder().raw_decode(remainder)
        if repeated!=value or remainder[finish:].strip():raise ValueError('Conflicting or trailing Hope response')
    if not isinstance(value,dict):raise ValueError('Hope response must be an object')
    return value

def verify_worker_receipt(stdout,spec):
    lines=[x[len('TOFU_JOB_RESULT='):] for x in stdout.splitlines() if x.startswith('TOFU_JOB_RESULT=')]
    if len(lines)!=1:raise EnvironmentError('hope_receipt_missing','Exactly one complete Worker receipt required')
    try:
        result=json.loads(lines[0],parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite JSON')))
        required={'state','exit_code','error','elapsed_seconds','outputs','stdout','stderr'}
        if not isinstance(result,dict) or set(result)!=required:raise ValueError('receipt fields')
        if type(result['exit_code']) is not int or result['state'] not in {'succeeded','failed'}:raise ValueError('exit/state')
        elapsed=result['elapsed_seconds']
        if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<0:raise ValueError('elapsed')
        if not isinstance(result['stdout'],str) or not isinstance(result['stderr'],str):raise ValueError('streams')
        if result['state']=='succeeded' and (result['exit_code']!=0 or result['error'] is not None or elapsed>spec['limits']['runtime_seconds']):raise ValueError('success contradiction')
        if not isinstance(result['outputs'],list):raise ValueError('outputs')
        total=0;names=[]
        for item in result['outputs']:
            if not isinstance(item,dict) or set(item)!={'name','bytes','sha256'}:raise ValueError('output fields')
            if type(item['bytes']) is not int or item['bytes']<0 or not isinstance(item['name'],str):raise ValueError('output type')
            if not isinstance(item['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',item['sha256']):raise ValueError('output hash')
            total+=item['bytes'];names.append(item['name'])
        if len(names)!=len(set(names)) or any(n not in spec['outputs'] for n in names):raise ValueError('undeclared output')
        if total>spec['limits']['result_bytes']:raise ValueError('result byte limit')
        if result['state']=='succeeded' and names!=spec['outputs']:raise ValueError('missing/ordered output')
    except (ValueError,KeyError,TypeError) as exc:
        raise EnvironmentError('hope_receipt_invalid','Worker receipt does not match the sealed execution spec') from exc
    return {'state':result['state'],'exit_code':result['exit_code'],'outputs':result['outputs'],
            'elapsed_seconds':elapsed,'sha256':hashlib.sha256(lines[0].encode()).hexdigest()}

class HopeEvidencePort:
    def __init__(self,actor,repository,call,*,expected_creator,reconcile=None,worker_receipt_reader=None,receipt_timeout_seconds=60):
        if not isinstance(expected_creator,str) or not expected_creator:
            raise EnvironmentError('hope_identity_required','Trusted external creator binding required')
        self.scope,self.project_root=actor.scope,actor.project_root
        self.repository,self.call=repository,call
        self.expected_creator,self.reconcile=expected_creator,reconcile
        if worker_receipt_reader is not None and not callable(worker_receipt_reader):
            raise EnvironmentError('hope_unconfigured','Trusted worker receipt reader required')
        if type(receipt_timeout_seconds) is not int or not 1<=receipt_timeout_seconds<=300:
            raise EnvironmentError('hope_receipt_limits','Bounded receipt deadline required')
        self.worker_receipt_reader=worker_receipt_reader
        self.receipt_timeout_seconds=receipt_timeout_seconds

    def receipt_text(self,actor,operation_id,reference,log,*,deadline=None):
        """One receipt authority, two transports; disagreement never selects a winner."""
        record=self._owned_record(actor,operation_id)
        if record['reference']!=reference:
            raise EnvironmentError('hope_reference_mismatch','Receipt reference differs from owned operation')
        stdout=log.get('log_text','')
        lines=[x for x in stdout.splitlines() if x.startswith('TOFU_JOB_RESULT=')]
        durable=RECEIPT_CONTRACT_FILE in record['plan'].get('admission',{}).get('include',[])
        if not durable:
            if log.get('truncated'):
                raise EnvironmentError('hope_receipt_truncated','Complete Worker receipt required')
            return stdout
        if len(lines)>1:
            raise EnvironmentError('hope_receipt_missing','Ambiguous Worker receipts cannot be reconciled')
        if self.worker_receipt_reader is None:
            raise EnvironmentError('hope_unconfigured','Durable receipt transport required')
        limit=receipt_contract()['maximum_bytes']
        end=time.monotonic()+self.receipt_timeout_seconds if deadline is None else deadline
        raw=self.worker_receipt_reader(actor,reference,operation_id,deadline=end)
        if not isinstance(raw,bytes) or not 0<len(raw)<=limit:
            raise EnvironmentError('hope_receipt_invalid','Bounded complete durable receipt required')
        try:text='TOFU_JOB_RESULT='+raw.decode('utf-8')
        except UnicodeError as exc:raise EnvironmentError('hope_receipt_invalid','UTF8 receipt required') from exc
        native=verify_worker_receipt(text,record['plan']['spec'])
        if lines:
            streamed=verify_worker_receipt(stdout,record['plan']['spec'])
            if streamed!=native:
                raise EnvironmentError('hope_receipt_conflict','Durable and streamed receipts disagree')
        return text

    def _call(self,actor,name,args):
        try:result=decode_hope_response(self.call(actor,'mcp__hope__'+name,args,retry_on_disconnect=True))
        except Exception as exc:raise EnvironmentError('hope_evidence_unavailable','Read-only Hope evidence unavailable') from exc
        if not result.get('ok'):raise EnvironmentError('hope_evidence_unavailable','Read-only Hope evidence rejected')
        return result

    def _owned_record(self,actor,operation_id):
        if actor.scope!=self.scope or actor.project_root!=self.project_root:
            raise EnvironmentError('hope_scope_mismatch','Evidence port belongs to another owner/project')
        row=self.repository.get(actor,operation_id)
        if not row:raise EnvironmentError('hope_binding_missing','Owned durable operation binding required')
        record=row['value'];plan=record['plan']
        if record['operation_id']!=operation_id or plan['scope']!=actor.scope or plan['project_root']!=actor.project_root or seal(plan)!=record['plan_hash']:
            raise EnvironmentError('hope_scope_mismatch','Operation seal/scope mismatch')
        return record

    def _owned_platform_job(self,record,reference,result,*,allow_pending):
        validate_reference(reference)
        if reference!=record['reference'] or reference['kind']!='hope_run_id':
            raise EnvironmentError('hope_reference_mismatch','Reference must equal the persisted owned submission')
        result=decode_hope_response(result);job=result.get('job',{})
        if not result.get('ok') or job.get('hope_run_id')!=reference['id']:
            raise EnvironmentError('hope_identity_mismatch','Platform creator/reference differs from trusted binding')
        state=str(job.get('state','')).upper();app_id=job.get('app_id')
        if not isinstance(app_id,str) or not app_id:
            if state in {'SUBMITTING','INITIALIZED','ASSIGNED','PENDING'} and job.get('creator') in (None,self.expected_creator) and allow_pending:return None
            raise EnvironmentError('hope_application_missing','Application reference required for execution evidence')
        if job.get('creator')!=self.expected_creator:
            raise EnvironmentError('hope_identity_mismatch','Platform creator differs from trusted binding')
        apps=(result.get('raw') or {}).get('status',{}).get('apps_info',[])
        if not isinstance(apps,list) or len(apps)!=1 or apps[0].get('run_id')!=reference['id'] or apps[0].get('app_id')!=app_id or apps[0].get('creator')!=self.expected_creator or str(apps[0].get('state','')).upper()!=state:
            raise EnvironmentError('hope_identity_mismatch','Raw platform application identity/state does not match')
        return job

    def verify_cancel_reference(self,actor,operation_id,reference,result):
        """Prove exact owned identity; stopping does not admit execution outputs."""
        record=self._owned_record(actor,operation_id)
        self._owned_platform_job(record,reference,result,allow_pending=False)

    def __call__(self,actor,operation_id,reference,result):
        record=self._owned_record(actor,operation_id);plan=record['plan']
        if reference is None:
            return self.reconcile(actor,record) if self.reconcile else None
        job=self._owned_platform_job(record,reference,result,allow_pending=True)
        if job is None:return {'state':'queued','exit_code':None}
        state=str(job.get('state','')).upper();app_id=job['app_id']
        if state in {'KILLED','STOPPED','CANCELLED'}:return {'state':'cancelled','exit_code':None}
        details=self._call(actor,'get_job_attempts',{'job_id':app_id})
        if details.get('job_id')!=app_id:
            raise EnvironmentError('hope_reference_mismatch','Worker lookup belongs to a different application')
        attempts=details.get('attempts',{});blocks=attempts.get('Worker',[]) if isinstance(attempts,dict) else [x for x in attempts if x.get('role')=='Worker']
        workers=[a for block in blocks for a in block.get('attemptInfo',[])]
        if not workers:
            if state in {'SUBMITTING','INITIALIZED','ASSIGNED','PENDING'}:return {'state':'queued','exit_code':None}
            raise EnvironmentError('hope_worker_missing','Worker attempt evidence required')
        worker=select_worker_attempt(workers,plan['spec']);worker_state=str(worker.get('status','')).upper()
        recovery=plan['spec'].get('recovery')
        if (recovery and state=='RUNNING' and worker_state=='FAILED'
                and worker.get('diagnostics')==PREEMPTION_DIAGNOSTIC
                and len(workers)<recovery['max_attempts']):
            return {'state':'queued','exit_code':None}
        # A terminal owned Worker cannot make further progress even when the
        # platform application is still Running. Keep its failed exit distinct
        # from successful computation whose publication failed.
        if state not in {'SUCCEEDED','SUCCESS','FAILED','FINISHED'} and worker_state in {'FAILED','KILLED','STOPPED','CANCELLED'}:
            return {'state':'failed','exit_code':None}
        if state not in {'SUCCEEDED','SUCCESS','FAILED','KILLED','STOPPED','CANCELLED','FINISHED'}:
            return {'state':'running' if worker_state=='RUNNING' else 'queued','exit_code':None}
        locator={'cluster':worker['cluster'],'namespace':worker['namespace'],'pod_name':worker['podName'],
                 'node_name':worker.get('nodeName') or worker['hostName'],'method':'tail','size':4096,'max_chars':100000}
        log=self._call(actor,'get_log_file',{**locator,'file_name':'stdout'})
        if state=='FAILED' and 'TOFU_JOB_RESULT=' not in log.get('log_text',''):
            return {'state':'failed','exit_code':None}
        stdout=self.receipt_text(actor,operation_id,reference,log)
        receipt=verify_worker_receipt(stdout,plan['spec'])
        if state=='FAILED' or receipt['state']=='failed':return {'state':'failed','exit_code':receipt['exit_code']}
        if state not in {'SUCCEEDED','SUCCESS'} or worker_state not in {'SUCCEEDED','SUCCEED','SUCCESS'}:
            raise EnvironmentError('hope_terminal_unverified','Platform and Worker must both prove success')
        for allocated_worker in workers:
            allocation_locator={**locator,'cluster':allocated_worker['cluster'],'namespace':allocated_worker['namespace'],
                'pod_name':allocated_worker['podName'],'node_name':allocated_worker.get('nodeName') or allocated_worker['hostName']}
            resource=self._call(actor,'get_log_file',{**allocation_locator,'file_name':'sys_afo.log','regex':'AFO_RESOURCE_CONFIG'})
            configs=[]
            try:
                for line in resource.get('log_text','').splitlines():
                    if 'AFO_RESOURCE_CONFIG,value:' in line:configs.append(json.loads(line.split('AFO_RESOURCE_CONFIG,value:',1)[1])['worker'])
                requested=plan['spec']['resources'];expected={'cpu':requested['cpu'],'mem':math.ceil(requested['memory_gib']*1024),'gpu':requested['gpu_count'],'num':1}
                if not configs or resource.get('truncated') or any(any(type(c.get(k)) not in (int,float) or not math.isfinite(c[k]) or c[k]!=v for k,v in expected.items()) for c in configs):raise ValueError('allocation mismatch')
            except (ValueError,KeyError,TypeError) as exc:
                raise EnvironmentError('hope_resource_unverified','Actual Worker allocation differs from sealed resources') from exc
        return {'state':'succeeded','exit_code':0}
