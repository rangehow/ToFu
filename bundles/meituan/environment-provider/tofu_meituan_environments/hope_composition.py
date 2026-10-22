"""Compose the Hope ports from one trusted actor/project binding.

Entry: compose_hope_provider(actor, context). The host supplies the selected
repository, cleanup registrar, capacity and binding loader. No ambient account,
network or daemon is selected at import. Binding secrets never enter plan JSON.
"""
import time
from tofu_environments.contracts import EnvironmentError
from .hope import HopeJobProvider
from .hope_mcp import HopeMCPPort
from .hope_source import HopeSourcePreparer
from .hope_evidence import HopeEvidencePort, decode_hope_response
from .hope_outputs import HopeOutputPort
from .hope_attempts import select_worker_attempt
from .hope_artifacts import HopeArtifactReader


def compose_hope_provider(actor, context):
    loader=context.get('hope_binding_loader');cleanup=context.get('register_cleanup')
    repository=context.get('repository')
    if not callable(loader) or not callable(cleanup) or repository is None:
        raise EnvironmentError('hope_unconfigured','Trusted owned binding, repository and call lifecycle required')
    binding=loader(actor)
    fields={'scope','project_root','profile','artifact_profile','expected_creator','environment','mcp_configuration','mcp_timeout_seconds','output_timeout_seconds'}
    if not isinstance(binding,dict) or set(binding)!=fields:
        raise EnvironmentError('hope_binding_invalid','Complete trusted Hope binding required')
    if binding['scope']!=actor.scope or binding['project_root']!=actor.project_root:
        raise EnvironmentError('hope_scope_mismatch','Hope profile belongs to another actor/project')
    factory=context.get('hope_bridge_factory')
    if factory is None:
        def factory(a):
            from lib.mcp.client import MCPBridge
            return MCPBridge(owner_user_id=a.owner_id)
    if not callable(factory):
        raise EnvironmentError('hope_unconfigured','Exclusive actor-owned bridge factory required')
    transport=HopeMCPPort(actor,factory,lambda a:binding['mcp_configuration'],timeout_seconds=binding['mcp_timeout_seconds'])
    cleanup(transport.close)
    preparer=HopeSourcePreparer(actor,binding['profile'],byte_limit=context['context_byte_limit'],
                               file_limit=context['context_file_limit'],total_byte_limit=context['context_byte_limit'],
                               artifact_profile=binding['artifact_profile'])
    cleanup(preparer.close)
    reader=HopeArtifactReader(actor,repository,root_uri=binding['artifact_profile']['root_uri'],
                              command=binding['artifact_profile']['command'],environment=binding['environment'])
    proof=HopeEvidencePort(actor,repository,transport,expected_creator=binding['expected_creator'],
                          worker_receipt_reader=reader.worker_receipt,receipt_timeout_seconds=min(300,binding['output_timeout_seconds']))
    def timed_call(a,tool,arguments,deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise EnvironmentError('hope_output_timeout','Output evidence deadline exceeded')
        # Keep transport calls inside the one output deadline; port serializes calls.
        with transport.lock:
            old=transport.timeout_seconds
            transport.timeout_seconds=max(1,min(old,int(remaining)))
            try:return decode_hope_response(transport(a,tool,arguments,retry_on_disconnect=True))
            finally:transport.timeout_seconds=old
    def deadline_proof(a,op,reference,state,deadline):
        scoped=HopeEvidencePort(actor,repository,lambda a,t,args,**kw:timed_call(a,t,args,deadline),expected_creator=binding['expected_creator'],
                               worker_receipt_reader=lambda a,r,op,**kw:reader.worker_receipt(a,r,op,deadline=deadline))
        return scoped(a,op,reference,state)
    def prove(a,reference,op,*,deadline):
        state=timed_call(a,'mcp__hope__get_job',{'hope_run_id':reference['id'],'detail':'full'},deadline)
        # Evidence port validates creator, operation, Worker and actual resources.
        result=deadline_proof(a,op,reference,state,deadline)
        if time.monotonic()>=deadline:raise EnvironmentError('hope_output_timeout','Output evidence deadline exceeded')
        return result
    def receipt(a,reference,op,*,deadline):
        state=timed_call(a,'mcp__hope__get_job',{'hope_run_id':reference['id'],'detail':'full'},deadline)
        if deadline_proof(a,op,reference,state,deadline)!={'state':'succeeded','exit_code':0}:
            raise EnvironmentError('hope_terminal_unverified','Owned successful Worker required')
        details=timed_call(a,'mcp__hope__get_job_attempts',{'job_id':state['job']['app_id']},deadline)
        blocks=details['attempts'];blocks=blocks.get('Worker',[]) if isinstance(blocks,dict) else [x for x in blocks if x.get('role')=='Worker']
        workers=[w for block in blocks for w in block.get('attemptInfo',[])]
        if details.get('job_id')!=state['job']['app_id']:
            raise EnvironmentError('hope_reference_mismatch','Receipt Worker lookup belongs to another application')
        record=proof._owned_record(a,op)
        if record['reference']!=reference:
            raise EnvironmentError('hope_reference_mismatch','Receipt reference changed after success proof')
        w=select_worker_attempt(workers,record['plan']['spec'])
        if str(w.get('status','')).upper() not in {'SUCCEEDED','SUCCEED','SUCCESS'}:
            raise EnvironmentError('hope_terminal_unverified','Successful Worker changed before receipt read')
        log=timed_call(a,'mcp__hope__get_log_file',{'cluster':w['cluster'],'namespace':w['namespace'],
                       'pod_name':w['podName'],'node_name':w.get('nodeName') or w['hostName'],
                       'method':'tail','size':4096,'max_chars':100000,'file_name':'stdout'},deadline)
        if not log.get('ok'):
            raise EnvironmentError('hope_receipt_truncated','Complete Worker receipt required')
        return proof.receipt_text(a,op,reference,log,deadline=deadline)
    outputs=HopeOutputPort(actor,repository,prove,receipt,reader,timeout_seconds=binding['output_timeout_seconds'])
    provider=HopeJobProvider(actor,transport,preparer,proof,outputs,catalog_profile=binding['profile'])
    return provider
