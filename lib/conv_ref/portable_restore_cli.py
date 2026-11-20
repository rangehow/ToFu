"""Preview or explicitly apply a selected-chat archive to an attached authority."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys

from .portable import PortableChatError, validate_package
from .portable_import import check_text_import_frame, preview_selected, restore_selected

_WARNING = ('Passive history only; no Continue or execution. '
            'Selected prose and files are not scanned for secrets.')


@contextmanager
def _attached(connection_file):
    from lib.storage.supervisor import StorageSupervisor

    # An explicit private connection file forces attach mode; never spawn an
    # authority or fall back to a different backend when attachment fails.
    supervisor = StorageSupervisor(connection_file=str(connection_file))
    try:
        client = supervisor.start()
        status = supervisor.status()
        host, port = client.endpoint
        target = {'backend': status['backend'], 'host': host, 'port': port,
                  'pid': status['pid'], 'connection_file': str(connection_file)}
        yield client, target, supervisor.attached_confirmation
    finally:
        supervisor.close()


def main(argv=None, *, attach=_attached):
    parser = argparse.ArgumentParser(description=__doc__, epilog=_WARNING)
    parser.add_argument('mode', choices=('preview', 'apply'))
    parser.add_argument('archive', type=Path)
    parser.add_argument('--owner', required=True, type=int, help='destination owner ID')
    parser.add_argument('--connection-file', required=True, type=Path,
                        help='absolute path to the existing private storage connection file')
    parser.add_argument('--confirm', help='required for apply; copy confirmation from preview')
    parser.add_argument('--data-directory', required=True, type=Path,
                        help='absolute existing local data/ directory from CLI configuration')
    args = parser.parse_args(argv)
    if args.owner <= 0 or not args.connection_file.is_absolute():
        parser.error('positive owner and absolute connection file required')
    from lib.storage.errors import StorageError

    try:
        raw = args.archive.read_bytes()
        package = validate_package(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if args.mode == 'apply' and not args.confirm:
            raise PortableChatError('apply_requires_preview_confirmation')
        from runtime_path_policy import get_runtime_base

        expected_data = Path(get_runtime_base()).resolve() / 'data'
        if (not args.data_directory.is_absolute() or not expected_data.is_dir()
                or args.data_directory.resolve() != expected_data.resolve()):
            raise PortableChatError('local_data_directory_does_not_match_configuration')
        with attach(args.connection_file) as (client, target, confirm):
            target = {**target, 'owner': args.owner, 'local_config_data_directory': str(expected_data),
                      'authorization_scope': ('native_owner_tenant_scope' if target['backend'] == 'tofudb'
                                              else 'privileged_sql_storage_operator')}
            confirmation = confirm(json.dumps([target, digest], sort_keys=True).encode())
            plan = preview_selected(package, user_id=args.owner, client=client)
            blocked = 'attachment_volume_unverified' if package.originals else None
            if not blocked:
                try:
                    check_text_import_frame(package, user_id=args.owner, client=client)
                except StorageError as error:
                    if error.code != 'database_protocol_error':
                        raise
                    blocked = 'storage_command_frame_not_encodable'
            report = {'target': target, 'sha256': digest, 'warning': _WARNING,
                      'preview': plan,
                      'apply_blocked': blocked}
            if args.mode == 'preview' and not blocked and not plan['conflicts']:
                report['confirmation'] = confirmation
            print(json.dumps(report), flush=True)
            if plan['conflicts']:
                return 1
            if args.mode == 'apply':
                if blocked:
                    raise PortableChatError(blocked)
                if args.confirm != confirmation:
                    raise PortableChatError('apply_confirmation_does_not_match_target_and_archive')
                receipt = restore_selected(raw, user_id=args.owner, client=client)
                print(json.dumps({'applied': receipt}))
    except PortableChatError as error:
        print(str(error), file=sys.stderr)
        return 1
    except StorageError as error:
        print(f'Storage rejected or did not confirm the request: {error.code}. '
              'Preview again before retrying.', file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError):
        print('Could not read the archive or attach to the configured authority.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
