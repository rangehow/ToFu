"""Authenticated versioned direct subscription account adapter."""
import re

from quart import request

from lib.agent_core.storage_errors import StorageError
from lib.api_response import api_bad_request, api_conflict, api_not_found, api_ok
from lib.log import get_logger
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.repository import ModelRoutingRepository, OwnerBoundary
from lib.oauth.accounts import SubscriptionAccounts
from lib.oauth.generated_accounts_http import COMMAND_FIELDS, COMMAND_REQUEST
from lib.oauth.manager import account_flow
from lib.openapi import api_meta
from lib.request_parser import BadRequest, parse_body, require_str
from .auth import request_principal, require_scope


logger = get_logger(__name__)


def _identity():
    principal = request_principal()
    return OwnerBoundary.create(principal.require_owner(context='subscription accounts'), principal.tenant_id)


def _service():
    return SubscriptionAccounts(ModelRoutingRepository())


def _identifier(data, field, pattern):
    value = require_str(data, field, max_len=35, strip=False)
    if not re.fullmatch(pattern, value):
        raise BadRequest('Invalid subscription identifier', field=field)
    return value


def _error(error):
    if error.kind in {'model_routing_revision_conflict', 'subscription_account_exists', 'subscription_account_supply_exists'}:
        return api_conflict(str(error))
    if error.kind == 'subscription_account_supply_unavailable':
        return api_bad_request(str(error))
    if error.kind in {'subscription_account_unavailable', 'subscription_account_flow_unavailable'}:
        return api_not_found('Subscription account or flow unavailable')
    raise error


def register_accounts(blueprint):
    @blueprint.route('/api/v1/oauth/accounts', methods=['GET'])
    @require_scope('admin')
    @api_meta(summary='List direct Codex subscription accounts', tags=['capabilities'], scope='admin')
    def oauth_accounts_inventory():
        if set(request.args) != {'provider'} or request.args.getlist('provider') != ['codex']:
            return api_bad_request('Expected provider=codex', field='provider')
        boundary = _identity()
        inventory = _service().inventory(boundary, 'codex')
        account_ids = [row['account_id'] for row in inventory['accounts']]
        try:
            from lib.oauth.account_usage import load_account_usage

            inventory['usage'] = load_account_usage(
                boundary.owner_user_id, provider='codex', account_ids=account_ids)
        except (StorageError, RuntimeError) as error:  # local analytics must not hide account management
            logger.warning('[OAuth] local account usage unavailable: %s', error)
            inventory['usage'] = {'source': 'local_record', 'provider': 'codex',
                                  'status': 'unavailable'}
        return api_ok(inventory)

    @blueprint.route('/api/v1/oauth/accounts/commands', methods=['POST'])
    @require_scope('admin')
    @api_meta(summary='Manage a direct Codex account or authorization flow', tags=['capabilities'], scope='admin',
              request_body=COMMAND_REQUEST)
    def oauth_accounts_command():
        body = parse_body(strict=True)
        action = require_str(body, 'action', max_len=16, strip=False)
        fields = COMMAND_FIELDS.get(action)
        if fields is None or body.keys() != {'action', 'provider', *fields} or body['provider'] != 'codex' or request.args:
            return api_bad_request('Invalid subscription account command')
        boundary, service = _identity(), _service()
        try:
            if action in {'complete', 'poll'}:
                flow_id = _identifier(body, 'flow_id', r'[a-f0-9]{32}')
                completion = {'code': require_str(body, 'code', max_len=16384, strip=False),
                              'state': require_str(body, 'state', max_len=4096, strip=False)} if action == 'complete' else {}
                return api_ok(account_flow.complete(service, boundary, flow_id, **completion))
            revision = body['expected_revision']
            if type(revision) is not int or revision < 0:
                return api_bad_request('Expected a nonnegative JSON integer', field='expected_revision')
            account = _identifier(body, 'account_id', r'oa_[a-f0-9]{32}') if 'account_id' in fields else None
            if action in {'add', 'reauth'}:
                method = require_str(body, 'method', max_len=16)
                if method not in {'browser', 'device'}:
                    return api_bad_request('Invalid account login method', field='method')
                return api_ok(account_flow.start(service, boundary, 'codex', revision, method=method, account_id=account))
            if action == 'admit_supply':
                from lib.oauth.scoped_catalog import admit_initial_codex_supply
                return api_ok(revision=admit_initial_codex_supply(service.repository, boundary, account, revision))
            alias = require_str(body, 'alias', max_len=128, allow_empty=True) if action == 'rename' else None
            if alias and not alias.isprintable():
                return api_bad_request('Invalid account alias', field='alias')
            return api_ok(revision=service.update(boundary, 'codex', account, revision, action=action, alias=alias))
        except ModelRoutingError as error:
            return _error(error)

    @blueprint.route('/api/v1/oauth/accounts/flows/<flow_id>', methods=['GET'])
    @require_scope('admin')
    @api_meta(summary='Read an exact direct Codex authorization flow', tags=['capabilities'], scope='admin')
    def oauth_accounts_flow(flow_id):
        if request.args:
            return api_bad_request('Unexpected OAuth flow parameters')
        flow_id = _identifier({'flow_id': flow_id}, 'flow_id', r'[a-f0-9]{32}')
        try:
            return api_ok(account_flow.status(_identity(), flow_id))
        except ModelRoutingError as error:
            return _error(error)
