"""Stdlib-only, no-source-execution qualification for desktop node lifecycle.

Run by absolute path with the project's Python and ``-I -S``. This does not
import or launch the node, a server, a transport, or optional dependencies.
Namespace availability is necessary, not sufficient, for safe node execution.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


_NAMESPACE_PROBE = '''
import json, os
from pathlib import Path
names = {key: os.readlink('/proc/self/ns/' + key) for key in ('user', 'net', 'pid')}
interfaces = [line.split(':')[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:]]
print(json.dumps({'namespaces': names, 'interfaces': interfaces,
                  'isolated_python': bool(__import__('sys').flags.isolated),
                  'no_site': bool(__import__('sys').flags.no_site)}))
'''


def qualify():
    if not sys.flags.isolated or not sys.flags.no_site:
        raise SystemExit('Refused: invoke with -I -S before any project/site imports')
    checkout = Path(__file__).resolve().parents[2]
    owners = ('tests/chaos/node.py', 'tests/chaos/node_main.py',
              'lib/desktop_agent/__init__.py', 'lib/desktop_agent/_run.py',
              'lib/desktop_agent/links.py', 'lib/desktop_agent/_gui.py')
    hashes = {name: hashlib.sha256((checkout / name).read_bytes()).hexdigest()
              for name in owners}
    unshare = shutil.which('unshare', path='/usr/bin:/bin')
    preflight = {'available': False, 'reason': 'unshare executable unavailable'}
    if unshare and sys.platform == 'linux':
        before = {key: os.readlink('/proc/self/ns/' + key) for key in ('user', 'net', 'pid')}
        result = subprocess.run(
            [unshare, '--user', '--map-root-user', '--net', '--pid', '--fork',
             sys.executable, '-I', '-S', '-c', _NAMESPACE_PROBE],
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, cwd='/',
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        preflight = {'available': False, 'exit_code': result.returncode}
        if result.returncode == 0:
            probe = json.loads(result.stdout)
            changed = all(probe['namespaces'][key] != before[key] for key in before)
            preflight.update({'distinct_user_net_pid_namespaces': changed,
                              'interfaces': probe['interfaces'],
                              'isolated_python': probe['isolated_python'], 'no_site': probe['no_site']})
            preflight['available'] = (changed and probe['interfaces'] == ['lo']
                                      and probe['isolated_python'] and probe['no_site'])
        else:
            preflight['reason'] = 'kernel namespace creation refused; no source executed'
    return {
        'status': 'NOT_QUALIFIED_NO_SOURCE_EXECUTION',
        'source_sha256': hashes,
        'kernel_preflight': preflight,
        'node_generations_started': 0,
        'services_started': 0,
        'application_or_third_party_imports': False,
        'network_operations': False,
        'allow_exec_set': False,
        'reason': ('Existing ChaosNode.start unconditionally appends --allow-exec. '
                   'node_main imports the desktop-agent facade before synthetic identity/setup; '
                   'the facade imports optional GUI dependencies. Neither entry establishes '
                   'an early-process confinement and command-free dependency fixture. '
                   'Namespace availability alone does not supply that missing fixture. '
                   'No new security boundary or weaker audit-hook clone is introduced.'),
        'unverified': ['distinct node generation receipts', '0600 shared config',
                       'two peers resume without re-pairing', 'credential separation',
                       'node worker shutdown'],
        'limits': 'No native OS, production transport, installer, real app, or rollout claim.',
    }


if __name__ == '__main__':
    print(json.dumps(qualify(), indent=2, sort_keys=True))
