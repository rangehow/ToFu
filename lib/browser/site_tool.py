"""Explicit read-capability discovery/execution over the existing adapter owner.

No website is implicitly enrolled as a web engine. Model-visible schemas are
projected from AdapterCommand; this entry never creates a registry or session.
"""
from __future__ import annotations

import json


def site_tool(args: dict, runtime) -> str:
    from .adapters import (
        AdapterExecutionError, AdapterValidationError, adapter_health,
        get_adapter, invoke_adapter, list_adapters,
    )
    from .access import is_read_allowed
    from .network_evidence import redact_value
    from lib.tools.result_envelope import typed_tool_error


    def failure(code, message, *, retryable=False):
        return typed_tool_error(code, message=message, retryable=retryable,
                                next_action='Inspect the site operation schema and current browser availability.')

    site = str(args.get('site') or '').strip()
    operation = str(args.get('operation') or '').strip()
    if not site and (operation or args.get('params')):
        return failure('invalid_arguments', 'site is required for an operation or params')
    if args.get('params') and not operation:
        return failure('invalid_arguments', 'operation is required with params')
    try:
        if not site:
            sources = []
            for adapter in list_adapters():
                if not all(is_read_allowed(runtime.owner_user_id, 'https://' + domain)
                           for domain in adapter.domains):
                    continue
                sources.append({
                    'site': adapter.id, 'name': adapter.name,
                    'operations': [
                        {'operation': cmd.name, 'purpose': cmd.description,
                         'available': adapter_health(adapter, client_id=runtime.client_id,
                                                     command_name=cmd.name)['healthy']}
                        for cmd in adapter.commands if cmd.access == 'read'],
                })
            return json.dumps({'sources': sources[:32],
                               'next': 'Inspect a site for its exact operation input schemas.'},
                              ensure_ascii=False)
        adapter = get_adapter(site)
        if adapter is None:
            raise AdapterValidationError('Unknown site; call browser_site without arguments to discover sources')
        if not all(is_read_allowed(runtime.owner_user_id, 'https://' + domain)
                   for domain in adapter.domains):
            return failure('browser_access_denied', 'Site is denied by current browser policy')
        if not operation:
            return json.dumps({
                'site': adapter.id, 'name': adapter.name, 'domains': adapter.domains,
                'risk': adapter.risk_notice,
                'operations': [cmd.public_dict() for cmd in adapter.commands if cmd.access == 'read'],
            }, ensure_ascii=False)
        command = adapter.command(operation)
        if command.access != 'read':
            raise AdapterValidationError('browser_site exposes only declared read operations')
        if not isinstance(args.get('params', {}), dict):
            raise AdapterValidationError('params must be an object')
        result = invoke_adapter(
            adapter.id, command.name, args.get('params', {}),
            owner_user_id=runtime.owner_user_id, client_id=runtime.client_id)
        return json.dumps(redact_value(result), ensure_ascii=False)[:80000]
    except AdapterValidationError as exc:
        return failure('invalid_arguments', str(exc))
    except AdapterExecutionError as exc:
        return failure('browser_site_' + exc.code, str(exc), retryable=exc.retryable)
