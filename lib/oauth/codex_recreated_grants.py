"""Prevent recreated Codex credentials from bypassing retained owner policy."""


def suppress_recreated_codex_grants(previous: dict, candidate: dict) -> None:
    """Apply durable denials to new Codex credentials in a normalized candidate.

    Logout removes live credentials but not their history. Without retained
    baseline evidence, preserve legacy initialization except explicit denials.
    After a baseline, legacy import cannot distinguish a previously manual
    grant from an account-bound grant, so it cannot seed unscoped authority.
    """
    history = {(row['credential_id'], row['creator_id'], row['model_id']): row
               for row in previous.get('revocation_history', ())}
    old_ids = {row['credential_id'] for row in previous['credentials']}
    retained_baselines = {}
    for row in candidate.get('codex_seen_history', ()):
        if row['credential_id'] not in old_ids:
            key = row['credential_id'], row['provider_access_id']
            retained_baselines.setdefault(key, []).append(row)
    access_ids = {row['provider_access_id'] for row in candidate['provider_accesses']
                  if row['provider_id'] == 'oauth_codex'}
    for credential in candidate['credentials']:
        credential_id = credential['credential_id']
        if credential_id in old_ids or credential['provider_access_id'] not in access_ids:
            continue
        authorization = credential['authorization']
        baselines = retained_baselines.get((credential_id, credential['provider_access_id']))
        if baselines:
            for row in baselines:
                row['pending_wire_model_ids'] = []
            authorization['models'] = []
            continue
        retained = []
        for grant in authorization['models']:
            record = history.get((credential_id, grant['creator_id'], grant['model_id']))
            if record:
                if record['model_revoked']:
                    continue
                excluded = set(grant.get('excluded_wire_model_ids', ()))
                excluded.update(record['excluded_wire_model_ids'])
                if excluded:
                    grant['excluded_wire_model_ids'] = sorted(excluded)
            retained.append(grant)
        authorization['models'] = retained
