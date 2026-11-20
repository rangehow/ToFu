"""Scoped Codex browser/device completion; legacy flows never consume these intents."""
import secrets
import time

from lib.model_routing.domain import ModelRoutingError
from lib.oauth.token_store import OAuthExchangeError
from ._state import _active_flows, _flows_lock, _FLOW_TIMEOUT


def _key(boundary):
    return ('subscription-account', boundary)


def _unavailable():
    return ModelRoutingError('OAuth account flow unavailable', kind='subscription_account_flow_unavailable')


def _current(boundary, flow_id):
    flow = _active_flows.get(_key(boundary))
    if not flow or flow['flow_id'] != flow_id:
        raise _unavailable()
    if flow['expires_at'] <= time.monotonic():
        raise _unavailable()
    return flow


def _project(flow):
    return {key: flow[key] for key in ('flow_id', 'provider', 'account_id', 'status',
                                       'auth_url', 'user_code', 'verification_url', 'interval') if key in flow}


def start(service, boundary, provider, expected_revision, *, method, account_id=None):
    from lib.oauth.codex import codex_build_auth_url
    from ._device import _CodexDevice

    if provider != 'codex' or method not in {'browser', 'device'}:
        raise _unavailable()
    intent = service.begin(boundary, provider, expected_revision, account_id=account_id)
    flow = {'flow_id': secrets.token_hex(16), 'provider': provider, 'account_id': intent.account_id,
            'intent': intent, 'method': method, 'status': 'starting', 'expires_at': time.monotonic() + _FLOW_TIMEOUT}
    with _flows_lock:
        for key, previous in tuple(_active_flows.items()):
            if isinstance(key, tuple) and key[0] == 'subscription-account' and previous['expires_at'] <= time.monotonic():
                del _active_flows[key]
        _active_flows[_key(boundary)] = flow
    try:
        if method == 'browser':
            auth = codex_build_auth_url()
            fields = {'auth_url': auth['auth_url'], 'state': auth['state'], 'pkce': auth['pkce']}
            timeout = _FLOW_TIMEOUT
        else:
            fields = _CodexDevice.request(user_id=str(boundary.owner_user_id), device_id='')
            timeout = min(int(fields['expires_in']), _CodexDevice.timeout())
    except OAuthExchangeError:
        with _flows_lock:
            if _active_flows.get(_key(boundary)) is flow:
                del _active_flows[_key(boundary)]
        raise _unavailable() from None
    with _flows_lock:
        _current(boundary, flow['flow_id'])
        flow.update(fields, status='pending', expires_at=time.monotonic() + timeout)
        return _project(flow)


def status(boundary, flow_id):
    with _flows_lock:
        return _project(_current(boundary, flow_id))


def _finish(service, boundary, flow_id, token):
    from lib.oauth.codex import codex_access_token_account_id

    subject = codex_access_token_account_id((token or {}).get('access_token', ''))
    with _flows_lock:
        flow = _current(boundary, flow_id)
        if flow['status'] != 'exchanging':
            raise _unavailable()
        try:
            if not subject or token.get('account_id') != subject:
                raise _unavailable()
            service.complete(flow['intent'], verified_subject=subject, token=token, expires_at=token['expire'])
        except Exception:
            flow['status'] = 'error'
            raise
        for private_field in ('intent', 'state', 'pkce', 'device_auth_id'):
            flow.pop(private_field, None)
        flow['status'] = 'authorized'
        return _project(flow)


def complete(service, boundary, flow_id, *, code=None, state=None):
    """Accept exact browser state, or explicitly poll the bound device grant."""
    from lib.oauth.codex import CODEX_OAUTH_CONFIG, codex_exchange_code
    from ._device import _CodexDevice

    with _flows_lock:
        flow = _current(boundary, flow_id)
        if flow['status'] != 'pending':
            raise _unavailable()
        method = flow['method']
        if method == 'browser':
            if not isinstance(code, str) or not code or not isinstance(state, str) or not secrets.compare_digest(state.encode(), flow['state'].encode()):
                raise _unavailable()
            verifier = flow['pkce']['code_verifier']
            redirect = ''
        elif code is not None or state is not None or time.monotonic() < flow.get('next_poll_at', 0):
            raise _unavailable()
        else:
            flow['next_poll_at'] = time.monotonic() + flow['interval']
        flow['status'] = 'exchanging'
    owner = str(boundary.owner_user_id)
    try:
        if method == 'device':
            result = _CodexDevice.poll(flow['device_auth_id'], flow['user_code'], user_id=owner, device_id='')
            if result is None:
                with _flows_lock:
                    pending = _current(boundary, flow_id)
                    pending['status'] = 'pending'
                    return _project(pending)
            code, verifier = result['authorization_code'], result['code_verifier']
            redirect = CODEX_OAUTH_CONFIG['device_redirect_uri']
        token = codex_exchange_code(code, verifier, user_id=owner, redirect_uri=redirect, persist=False)
    except OAuthExchangeError:
        with _flows_lock:
            _current(boundary, flow_id)['status'] = 'error'
        raise _unavailable() from None
    return _finish(service, boundary, flow_id, token)
