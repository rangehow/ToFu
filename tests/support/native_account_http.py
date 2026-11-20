"""Off-pod synthetic native account HTTP probe; no external endpoint."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_content_contract import NATIVE_STDIO_CONTRACT
from lib.agent_core.native_process import Budget, NativeProcessClient, NativeProcessError
from lib.model_routing.native_route import run_native_route
from lib.model_routing.repository import InMemoryModelRoutingRepository
from lib.oauth.accounts import SubscriptionAccounts
from lib.tasks_pkg.native_execution import compile_native_application_route
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.model_routing.test_native_route import Repository, principal, selection
from tests.oauth.test_codex_account_flow import bearer
from tests.support.native_process_probe import config


def main(binary):
    from dataclasses import replace
    base = Repository()
    base.document['connections'][0].update(protocol='openai_responses', responses_profile='codex',
                                            base_url='https://chatgpt.com/backend-api/codex')
    base.document['credentials'][0]['kind'] = 'oauth'

    class Repo(InMemoryModelRoutingRepository):
        def get(self, boundary):
            row = super().get(boundary)
            return replace(row, document={**base.document, **{k:v for k,v in row.document.items() if k == 'subscription_accounts'}})

        def resolve_secret(self, boundary, reference, **kwargs):
            if reference == 'secret-a':
                return 'oauth:codex'
            return super().resolve_secret(boundary, reference, **kwargs)

    repo, owner = Repo(), base.boundary
    accounts = SubscriptionAccounts(repo)
    a, b = [accounts.complete(accounts.begin(owner, 'codex', n), verified_subject=subject,
        token={'access_token': bearer(subject), 'account_id': subject}, expires_at=time.time()+300)
        for n, subject in enumerate(('A', 'B'))]
    accounts.update(owner, 'codex', a, 2, action='default')
    first = compile_native_application_route(repo, principal(), selection())
    blocked = compile_native_application_route(repo, principal(), selection())
    accounts.update(owner, 'codex', b, 3, action='default')
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            seen.append(self.headers['Authorization'])
            self.send_response(401)
            self.send_header('Content-Length', '0')
            self.end_headers()

    with HTTPServer(('127.0.0.1', 0), Handler) as server, tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=1) as executor:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        loopback = f'http://127.0.0.1:{server.server_port}/responses'

        class LoopbackClient(NativeProcessClient):
            async def run(self, configuration, **kwargs):
                # Redirect only the physical fixture transport after the trusted
                # route/resolver have bound the first-party endpoint.
                assert configuration['endpoint'] == 'https://chatgpt.com/backend-api/codex/responses'
                return await super().run(dict(configuration, endpoint=loopback), **kwargs)

        async def run(route, task_id):
            task = dict(id=task_id, _userId=7, status='running', abort_event=threading.Event(),
                        config={'executionLimits': dict(max_tokens=1000, max_tool_calls=0, deadline=None)})
            task['_executionBudget'] = ExecutionBudget(task)
            policy = NativeModelAccountPolicy(principal=principal(), task=task, executor=executor)
            client = LoopbackClient(binary=Path(binary), sha256=os.environ['TOFU_REMOTE_BINARY_SHA256'],
                protocol_version=NATIVE_STDIO_CONTRACT, principal=principal(), budget=Budget(1,16*1024*1024,65536,65536,5))
            cfg = config(loopback, task_id)
            cfg['tools'] = {}
            for key in ('endpoint', 'headers', 'template'):
                cfg.pop(key)
            cfg['model_control'] = {'ack_ms': 2000}
            cfg['model_counter'] = dict(encoding='cl100k_base', output_field='max_tokens', input_bytes=65536,
                text_bytes=65536, tool_json_bytes=65536, depth=128, worker_stack_bytes=1048576)
            try:
                return await run_native_route(client, route, cfg, prepare_template=lambda meta: {
                    'model': meta['wire_model'], 'max_tokens': 100}, callbacks={}, working_directory=Path(tmp), model_policy=policy)
            finally:
                await client.close()

        try:
            event = asyncio.run(run(first, 'fixture-401')).event
            assert event['status'] == 'error' and event['error']['statusCode'] == 401, event
            inventory = accounts.inventory(owner, 'codex')
            assert inventory['default_account_id'] == b
            assert {row['account_id']: row['status'] for row in inventory['accounts']} == {a:'needs_reauth', b:'authorized'}
            accounts.update(owner, 'codex', a, inventory['revision'], action='remove')
            try:
                asyncio.run(run(blocked, 'fixture-removed'))
            except NativeProcessError:
                pass
            else:
                raise AssertionError('removed generation was not refused')
            assert seen == ['Bearer ' + bearer('A')], 'unexpected physical request or credential substitution'
            print('Native physical HTTP: pinned A after default B; exact 401 marked A only; removed generation sent nothing')
        finally:
            server.shutdown()
            thread.join()


if __name__ == '__main__':
    main(sys.argv[1])
