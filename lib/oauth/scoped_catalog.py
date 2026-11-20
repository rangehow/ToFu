"""Explicit initial Codex supply admission from an account's observed catalog."""
from lib.model_routing.domain import ModelRoutingError
from lib.oauth.accounts import SubscriptionAccounts


def restrict_to_observed_account(candidate, imported, fingerprint):
    """Translate initial imported model grants to exact account/wire grants."""
    ids = {row['credential_id'] for row in imported['credentials']}
    wires = {}
    # Deployment points to the offering, not directly to ModelRef.
    offerings = {row['offering_id']: row.get('model') for row in imported['offerings']}
    for row in imported['deployments']:
        model = offerings.get(row['offering_id'])
        if model:
            wires[tuple(sorted(model.items()))] = row['wire_model_id']
    for credential in candidate['credentials']:
        if credential['credential_id'] not in ids:
            continue
        authorization = credential['authorization']
        grants = [{**model, 'wire_model_id': wires[tuple(sorted(model.items()))]}
                  for model in authorization['models'] if tuple(sorted(model.items())) in wires]
        if not grants:
            raise ModelRoutingError('No maintained Codex supply was observed', kind='subscription_account_supply_unavailable')
        authorization['models'] = []
        authorization['codex_account_grants'] = {fingerprint: grants}


def admit_initial_codex_supply(repository, boundary, account_id, expected_revision):
    """A separate explicit owner command; never modifies established supply."""
    from lib.oauth.codex_catalog import _fetch_catalog, _provider_models
    from lib.oauth.outbound import _managed_oauth_entry, _upsert_oauth_provider

    accounts = SubscriptionAccounts(repository)
    current, rows = accounts._read(boundary, 'codex', expected_revision)
    if any(row['provider_id'] == 'oauth_codex' for row in current.document['providers']):
        raise ModelRoutingError('Codex supply is already configured', kind='subscription_account_supply_exists')
    row = accounts._find(rows, 'codex', account_id)
    binding = accounts.bind(boundary, 'codex', account_id)
    if binding.secret_reference != row['secret_reference']:
        raise ModelRoutingError('Subscription accounts changed', kind='model_routing_revision_conflict')
    subject = accounts.revalidate(binding)
    try:
        fetched = _fetch_catalog({}, user_id=str(boundary.owner_user_id), boundary=boundary,
                                 observed_revision=current.revision, account_binding=binding, account_repository=repository)
    except (RuntimeError, ValueError):
        raise ModelRoutingError('Codex account catalog unavailable', kind='subscription_account_supply_unavailable') from None
    evidence = fetched.evidence
    if (evidence is None or evidence.account_fingerprint != subject
            or evidence.boundary != boundary or evidence.observed_revision != current.revision):
        raise ModelRoutingError('No exact Codex catalog evidence was observed', kind='subscription_account_supply_unavailable')
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import load_provider_templates
    maintained = {row['model_id'] for row in official_model_directory(
        [row for row in load_provider_templates() if row['key'] == 'openai' and row.get('category') == 'official'])
        if row['creator_id'] == 'openai'}
    models = _provider_models([row for row in fetched.rows if row['slug'] in maintained & evidence.visible_wires])
    if not models:
        raise ModelRoutingError('No maintained Codex supply was observed', kind='subscription_account_supply_unavailable')
    accounts.revalidate(binding)
    _upsert_oauth_provider(_managed_oauth_entry('codex', '', observed_models=models),
                           owner_user_id=boundary.owner_user_id, tenant_id=boundary.tenant_id,
                           repository=repository, current=current, scoped_fingerprint=subject)
    return repository.get(boundary).revision
