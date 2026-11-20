"""Offline admission planner only: never starts a manager or opens a store."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

CANDIDATE = 'a28b2b7bf4cc28b981720fbf36e535ea7034fc5c'
TREE = '552cf8f87cab3905b7f94afc70726ecac97bcfa6'
MANAGER_TREE = '40c9eab67085a2112b8c5f28e5d74722065dec04'
BASE = 'tests/server/rust_manager_parity/'
CASES = [BASE + 'test_storage_rollback_parity.py::' + name + '[rust]' for name in (
    'test_a_new_engine_whose_first_start_fails_after_stop_is_rolled_back_and_service_restored',
    'test_without_a_known_good_engine_the_failed_host_is_replaced_and_bounded',
    'test_a_new_engine_that_cannot_start_on_a_disposable_authority_refuses_before_stop',
    'test_unknown_existing_authority_leaves_both_serving_processes_unchanged',
)] + [BASE + 'test_sql_storage_parity.py::test_the_sql_authority_is_hosted_exported_and_outlives_the_api_worker[rust]']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    args = parser.parse_args()
    def git(*parts):
        return subprocess.check_output(['git', '-C', str(args.repo), *parts])
    assert git('rev-parse', CANDIDATE + '^{tree}').decode().strip() == TREE
    assert git('rev-parse', CANDIDATE + ':packages/tofu-manager').decode().strip() == MANAGER_TREE
    paths = [BASE + name for name in ('test_storage_rollback_parity.py', 'test_sql_storage_parity.py',
                                    'conftest.py', 'fake_project.py')]
    paths += ['packages/tofu-manager/src/fingerprint.rs', 'Cargo.lock']
    print(json.dumps({
        'status': 'BLOCKED_NO_REVIEWED_EXACT_SOURCE_MANAGER_EXECUTABLE',
        'candidate': CANDIDATE, 'sourceTree': TREE, 'managerTree': MANAGER_TREE,
        'sourceFileSha256': {p: hashlib.sha256(git('show', CANDIDATE + ':' + p)).hexdigest() for p in paths},
        'nodeids': CASES,
        'futureArgvOnly': ['<verified-project-python>', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *CASES],
        'execution': False,
        'requirements': [
            'Reviewed build-time provenance binds the exact full source archive, manager tree, Cargo.lock, dependencies, build command and executable SHA-256; /health sourceFingerprint is not compiled Rust attestation.',
            'Materialize exact git archive into a new isolated directory, verify every tracked blob and executable digest before launch; do not use the current checkout as the candidate.',
            'Use scrubbed env, isolated HOME/TMPDIR/cache and pytest basetemp, explicit TOFU_MANAGER_BINARY; never default to target/debug or a live manager.',
            'Preserve frozen suite, fake-project fixtures, tests/conftest.py and dependencies unchanged; all data and processes must belong only to disposable fixture project roots.',
            'Execute only the five listed rust IDs; require exactly five passed and zero skipped/errors/failures, plus fixture cleanup; retain sanitized JUnit and source/executable receipts.',
            'No result establishes formal schema71 rollback, real native crash recovery or release readiness. No execution is authorized by this planner.'
        ]
    }, indent=2))


if __name__ == '__main__':
    main()
