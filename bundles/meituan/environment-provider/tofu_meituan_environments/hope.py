"""Hope batch adapter; trusted host supplies scoped MCP and source/result ports.

No ambient CLI account is selected. The preparer must seal packaged sources,
verify the declared command/resources, and enforce remote runtime/result limits.
The evidence port proves ownership/operation markers and terminal exit codes.
"""
from tofu_environments.contracts import EnvironmentError, canonical, seal
from tofu_environments.jobs import validate_reference
import json
from .hope_evidence import decode_hope_response

class HopeJobProvider:
    provider_id = 'hope'
    def __init__(self, actor, call, prepare, evidence, outputs, *, catalog_profile=None):
        self.catalog_project_root = actor.project_root
        self.catalog_profile = dict(catalog_profile) if catalog_profile else None
        self.scope = actor.scope
        self.call, self.prepare, self.evidence, self.outputs = call, prepare, evidence, outputs

    def _owner(self, actor):
        if actor.scope != self.scope:
            raise EnvironmentError('credential_owner_mismatch', 'Hope binding belongs to another owner')

    def _call(self, actor, tool, arguments, *, mutation=False, pending_reference=False):
        self._owner(actor)
        try:
            result = self.call(actor, 'mcp__hope__'+tool, arguments, retry_on_disconnect=not mutation)
            result = decode_hope_response(result)
            if not isinstance(result, dict): raise ValueError('non-object result')
        except Exception as exc:
            raise EnvironmentError('hope_transport_unknown', 'Hope response unavailable', uncertain=mutation) from exc
        if not result.get('ok') and not (pending_reference and result.get('state')=='SUBMITTING' and isinstance(result.get('hope_run_id'),str) and result['hope_run_id']):
            # A failed submission may already have minted a run or lost acceptance.
            rejected=(result.get('state')=='UNSUBMITTED' and (result.get('verify') or {}).get('verdict')=='submission_failed' and not result.get('app_id'))
            raise EnvironmentError('hope_call_failed', 'Hope rejected or could not verify the operation', uncertain=mutation and not rejected)
        return result

    def compute_catalog(self, actor):
        self._owner(actor)
        if actor.project_root != self.catalog_project_root:
            raise EnvironmentError('hope_scope_mismatch', 'Catalog belongs to another project')
        from .hope_catalog import read_catalog
        return read_catalog(self, actor, self.catalog_profile)

    def _prepare_job(self, actor, spec, operation_id):
        self._owner(actor)
        options = spec['provider_options']
        allowed = {'usergroup','job_type','queue','priority','resource_code','conf_name'}
        if set(options)-allowed or not all(isinstance(options.get(k),str) and options[k] for k in ['usergroup','job_type','queue','priority']):
            raise EnvironmentError('hope_options_required', 'Explicit usergroup, job type, queue and priority required')
        if options['priority'] not in {'P0','P1'} or (options['priority']=='P0' and not options.get('resource_code')):
            raise EnvironmentError('hope_priority_invalid', 'Hope priority requires a compatible resource code')
        prepared = self.prepare(actor, spec, operation_id=operation_id)
        if not isinstance(prepared, dict) or set(prepared) != {'workdir','include','source_sha256','configuration_sha256','limits_enforced','binding_revision','prepared_sha256'}:
            raise EnvironmentError('hope_preparation_invalid', 'Sealed source/configuration preparation required')
        if prepared['source_sha256'] != spec['source']['sha256'] or prepared['limits_enforced'] is not True:
            raise EnvironmentError('hope_preparation_invalid', 'Source identity and remote bounds must be verified')
        if not isinstance(prepared['configuration_sha256'],str) or len(prepared['configuration_sha256'])!=64:
            raise EnvironmentError('hope_preparation_invalid', 'Configuration digest required')
        if not isinstance(prepared['workdir'],str) or not prepared['workdir']:
            raise EnvironmentError('hope_preparation_invalid', 'Prepared workdir required')
        if not isinstance(prepared['include'],list) or not prepared['include'] or len(prepared['include'])>200 or not all(isinstance(x,str) and x for x in prepared['include']):
            raise EnvironmentError('hope_preparation_invalid', 'Explicit bounded packaging include list required')
        if not isinstance(prepared['prepared_sha256'],str) or len(prepared['prepared_sha256'])!=64:
            raise EnvironmentError('hope_preparation_invalid', 'Full prepared content digest required')
        return json.loads(canonical(prepared))

    @staticmethod
    def _admission(prepared):
        # Local staging is reconstructible transport, not approved job intent.
        return {key:value for key,value in prepared.items() if key != 'workdir'}

    def validate_job(self, actor, spec, *, operation_id):
        return self._admission(self._prepare_job(actor,spec,operation_id))

    def submit_job(self, actor, spec, operation_id, admission):
        # Recheck source/configuration at the last boundary before packaging.
        prepared = self._prepare_job(actor,spec,operation_id)
        if self._admission(prepared)!=admission:
            raise EnvironmentError('source_changed', 'Prepared job changed before submission')
        marker = 'tofu:'+seal([actor.scope,actor.project_root,operation_id])
        arguments = {**spec['provider_options'], 'workdir':prepared['workdir'],
                     'include':admission['include'], 'jobname':operation_id, 'annotation':marker,
                     'retry_on_quota':False, 'return_details':True}
        result = self._call(actor,'submit_job',arguments,mutation=True,pending_reference=True)
        run_id = result.get('hope_run_id')
        if not isinstance(run_id,str) or not run_id:
            raise EnvironmentError('hope_reference_missing','Submission lacks Hope run identity',uncertain=True)
        return validate_reference({'kind':'hope_run_id','id':run_id})

    def job_status(self, actor, reference, operation_id):
        self._owner(actor);validate_reference(reference)
        if reference['kind']!='hope_run_id':
            raise EnvironmentError('hope_reference_kind','Hope run ID required')
        result=self._call(actor,'get_job',{'hope_run_id':reference['id'],'detail':'full'})
        # Platform state alone cannot prove experiment exit or exact operation.
        return self.evidence(actor,operation_id,reference,result)

    def reconcile_job(self, actor, record):
        self._owner(actor)
        # Evidence implementation performs bounded read-only marker lookup.
        return self.evidence(actor,record['operation_id'],None,record)

    def cancel_job(self, actor, reference, operation_id):
        # Ownership is mandatory; a broken execution contract must remain stoppable.
        checker=getattr(self.evidence,'verify_cancel_reference',None)
        if checker is None:
            self.job_status(actor, reference, operation_id)
        else:
            result=self._call(actor,'get_job',{'hope_run_id':reference['id'],'detail':'full'})
            checker(actor,operation_id,reference,result)
        arguments = {'hope_run_id':reference['id'], 'reason':'Tofu owned job cancellation', 'stop_sub_jobs':False}
        preview = self._call(actor, 'stop_job', {**arguments, 'dry_run':True})
        token = preview.get('confirmation_token')
        if not isinstance(token,str) or not token:
            raise EnvironmentError('hope_stop_confirmation_missing','Hope stop preview lacks confirmation')
        self._call(actor, 'stop_job', {**arguments, 'dry_run':False, 'confirmation_token':token}, mutation=True)
        # Acknowledgement alone is not a terminal cancellation receipt.

    def read_job_output(self, actor, reference, operation_id, name):
        self._owner(actor)
        if validate_reference(reference)['kind']!='hope_run_id':
            raise EnvironmentError('hope_reference_kind','Hope run ID required')
        return self.outputs(actor,reference,operation_id,name)


def hope_provider_factory(actor, context):
    names=['hope_mcp_call','hope_prepare_job','hope_job_evidence','hope_job_outputs']
    if not any(name in context for name in names):
        from .hope_composition import compose_hope_provider
        from .hope_host import load_host_binding
        composed=dict(context)
        composed.setdefault('hope_binding_loader',load_host_binding)
        return compose_hope_provider(actor,composed)
    if not all(callable(context.get(name)) for name in names):
        raise EnvironmentError('hope_unconfigured','Configure owner-bound Hope MCP, source, evidence and output ports')
    return HopeJobProvider(actor,*(context[name] for name in names))
