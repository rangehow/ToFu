"""Owner/source-fenced production Codex credentials for issued native routes.

Entry NativeCodexCredentialResolver: revalidate selected v2 grant, resolve through
existing outbound/refresh authority, then revalidate grant/source before returning.
No vault, cache, executor, route fallback or persistence implementation is added.
"""
from urllib.parse import urlsplit
import json
import hashlib

from lib.identity import PrincipalContext
from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
from lib.model_routing.native_route import NativeCredentialRequest, NativeRouteError
from lib.model_routing.repository import OwnerBoundary


def _unavailable(reason):
    return SubscriptionCredentialUnavailableError(reason=reason,credential_source='tofu_oauth',transient=reason=='credential_source_unavailable')


def resolve_scoped_native_headers(resolver, account_fingerprints, request):
    """Verify the exact headers about to cross the joined native callback."""
    from lib.oauth.codex import account_fingerprint, codex_access_token_account_id
    headers = resolver(request)
    if not isinstance(headers, dict):
        raise _unavailable('credential_source_binding')
    headers = dict(headers)
    if not all(isinstance(name, str) for name in headers):
        raise _unavailable('credential_source_binding')
    bearer = [value for name, value in headers.items() if name.lower() == 'authorization']
    account = [value for name, value in headers.items() if name.lower() == 'chatgpt-account-id']
    if (len(bearer) != 1 or not isinstance(bearer[0], str)
            or not bearer[0].startswith('Bearer ')):
        raise _unavailable('credential_source_binding')
    bearer_account = codex_access_token_account_id(bearer[0][7:])
    if account != [bearer_account] or account_fingerprint(bearer_account) not in account_fingerprints:
        raise _unavailable('account_changed')
    return headers


class NativeCodexCredentialResolver:
    __slots__=('repository','principal','boundary','_source_binding','_account_binding')

    def __init__(self, *, repository, principal: PrincipalContext, account_binding=None):
        self.repository,self.principal=repository,principal
        self._source_binding=None
        self.boundary=OwnerBoundary.create(principal.require_owner(context='native OAuth resolver'),principal.tenant_id)
        if account_binding is not None and (account_binding.boundary != self.boundary or account_binding.provider != 'codex'):
            raise _unavailable('credential_source_binding')
        self._account_binding = account_binding

    def _authorize(self, request):
        from lib.model_routing.domain import NativeModelSelection, ProviderOfferingRef
        from lib.model_routing.native_route import compile_native_route, route_binding_sha256
        if (not isinstance(request,NativeCredentialRequest)
                or request.principal.require_owner(context='native OAuth attempt')!=self.boundary.owner_user_id
                or (request.principal.tenant_id or '')!=self.boundary.tenant_id or request.subscription!='codex'):
            raise NativeRouteError('native_route_owner')
        if request.cancelled.is_set():
            import asyncio
            raise asyncio.CancelledError
        route=compile_native_route(self.repository,self.boundary,NativeModelSelection(None,
            ProviderOfferingRef(request.provider_id,request.offering_id)),credential_resolver=self)
        try:
            metadata,private=route.consume(request.principal)
            try:
                if (route_binding_sha256(metadata)!=request.route_binding_sha256
                        or private['secret_reference']!=request.secret_reference
                        or private['headers']!=request.extra_headers):
                    raise NativeRouteError('native_route_credential_revoked')
                endpoint=private['endpoint']
                # The existing source only authorizes the first-party destination.
                # Proxy egress stays in transport policy, never an arbitrary URL.
                url=urlsplit(endpoint)
                if (endpoint!=request.endpoint or url.scheme!='https' or url.hostname!='chatgpt.com'
                        or url.port not in (None,443) or url.path!='/backend-api/codex/responses'
                        or url.query or url.fragment or url.username or url.password):
                    raise NativeRouteError('native_route_credential_destination')
                return tuple(private.get('codex_account_fingerprints', ()))
            finally:
                private.get('headers',{}).clear()
                private.clear()
        finally:route.close()

    def _source(self):
        from lib.oauth.token_store import load_stored_token
        stored=load_stored_token('codex')
        if not isinstance(stored,dict):raise _unavailable('credential_source_unavailable')
        reference=stored.get('codex_shared_reference')
        if isinstance(reference,dict):
            owner=reference.get('owner_user_id')
            from lib.oauth.codex_shared_login import local_owner
            if local_owner(owner) != owner:
                raise _unavailable('credential_source_binding')
            account=reference.get('account_id')
            tenant=reference.get('tenant_id','')
            try:
                identity=('shared',hashlib.sha256(json.dumps(reference,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest())
            except (TypeError,ValueError):
                raise _unavailable('credential_source_binding') from None
        else:
            owner=stored.get('owner_user_id')
            tenant=stored.get('tenant_id','')
            account=stored.get('account_id')
            identity=('owned',owner,tenant,account)
            if owner is None:
                # Reuse the declared personal legacy source boundary. Core code
                # never infers that a provider-global store belongs to any owner.
                from lib.oauth.codex_shared_login import local_owner
                owner=local_owner(self.boundary.owner_user_id)
        if (type(owner) is not int or owner!=self.boundary.owner_user_id
                or tenant!=self.boundary.tenant_id or not isinstance(account,str) or not account):
            raise _unavailable('credential_source_binding')
        return account,identity

    def __call__(self, request):
        account_fingerprints = self._authorize(request)
        if self._account_binding is not None:
            return self._resolve_account(request, account_fingerprints)
        account,identity=self._source()
        binding=(account,identity)
        if self._source_binding is None:
            self._source_binding=binding
        elif self._source_binding!=binding:
            raise _unavailable('credential_source_changed')
        from lib.oauth.outbound import resolve_oauth_request
        body={'model':request.wire_model,'_task_id':request.task_id}
        from lib.llm.llm_errors import SubscriptionLoginRequiredError
        try:
            token,headers,_=resolve_oauth_request('codex',body,request.extra_headers,
                user_id=str(self.boundary.owner_user_id),expected_account_id=account,
                cache_affinity_key=request.cache_affinity_key,
                **({'codex_account_fingerprints': account_fingerprints} if account_fingerprints else {}))
        except (SubscriptionLoginRequiredError,SubscriptionCredentialUnavailableError):
            self._authorize(request)
            if self._source()!=(account,identity):
                raise _unavailable('credential_source_changed') from None
            raise
        self._authorize(request)
        after_account,after_identity=self._source()
        if after_account!=account or after_identity!=identity:raise _unavailable('credential_source_changed')
        if not isinstance(token,str) or not token:raise _unavailable('credential_source_unavailable')
        headers=dict(headers)
        headers['Authorization']='Bearer '+token
        return headers

    def reject_http_401(self):
        if self._account_binding is not None:
            from lib.oauth.account_dispatch import reject_bound_account
            reject_bound_account((self._account_binding, self.repository))

    def _resolve_account(self, request, account_fingerprints):
        from lib.oauth.outbound import resolve_oauth_request

        body = {'model': request.wire_model, '_task_id': request.task_id}
        token, headers, _ = resolve_oauth_request(
            'codex', body, request.extra_headers, user_id=str(self.boundary.owner_user_id),
            cache_affinity_key=request.cache_affinity_key, codex_account_fingerprints=account_fingerprints,
            account_binding=self._account_binding, account_repository=self.repository)
        self._authorize(request)
        from lib.oauth.account_dispatch import revalidate_codex_account
        revalidate_codex_account(self._account_binding, self.repository)
        return {**headers, 'Authorization': 'Bearer ' + token}
