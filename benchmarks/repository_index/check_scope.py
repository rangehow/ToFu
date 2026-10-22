"""Terminal lifecycle, blocked close, authority denial and epoch-owned reuse."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import tempfile
from unittest.mock import patch

from index import outline
from owned_scope import OwnedIndexScope

OWNER = 900001


def main():
    with tempfile.TemporaryDirectory(prefix='tofu-index-scope-') as folder:
        root = Path(folder)
        (root / 'module.py').write_text('def original():\n    return 1\n')
        epoch = [0]
        calls = []
        def epoch_source():
            calls.append(epoch[0])
            return epoch[0]
        scope = OwnedIndexScope(OWNER, str(root), 1024 * 1024, epoch_source=epoch_source)
        assert scope.scan(OWNER) == outline.scan(str(root))
        assert scope.retained_bytes(OWNER) > 0
        before = len(calls)
        for action in (scope.scan, scope.close, scope.retained_bytes):
            try:
                action(OWNER + 1)
            except PermissionError:
                pass
            else:
                raise AssertionError('foreign scope authority accepted')
        assert len(calls) == before
        def changed(relative, text):
            return [outline.Tag(relative, 'newBackend', 1, 'def', 'def newBackend():', 0)]
        with patch.object(outline, '_collect_source_tags', new=changed):
            epoch[0] = 1
            assert scope.scan(OWNER) == outline.scan(str(root))
        epoch[0] = 2
        assert scope.scan(OWNER) == outline.scan(str(root))
        entered = Event(); release = Event(); close_started = Event(); closed = Event()
        original = outline._collect_source_tags
        def blocked(relative, text):
            entered.set(); assert release.wait(10)
            return original(relative, text)
        def close():
            close_started.set(); scope.close(OWNER); closed.set()
        epoch[0] = 3
        with patch.object(outline, '_collect_source_tags', new=blocked), ThreadPoolExecutor(max_workers=3) as pool:
            scan = pool.submit(scope.scan, OWNER)
            try:
                assert entered.wait(10)
                closing = pool.submit(close)
                assert close_started.wait(10)
                assert not closed.wait(.05)
                another = pool.submit(scope.close, OWNER)
                assert not another.done()
            finally:
                release.set()
            scan.result(timeout=10); closing.result(timeout=10); another.result(timeout=10)
        assert scope.retained_bytes(OWNER) == 0
        before = len(calls)
        for _ in range(2):
            scope.close(OWNER)
            try:
                scope.scan(OWNER)
            except RuntimeError:
                pass
            else:
                raise AssertionError('closed scope reopened')
        assert len(calls) == before
        try:
            with OwnedIndexScope(OWNER, str(root), 1024 * 1024, epoch_source=epoch_source) as owned:
                owned.scan(OWNER)
                raise ValueError('caller failure')
        except ValueError:
            pass
        assert owned.retained_bytes(OWNER) == 0
        failed = OwnedIndexScope(OWNER, str(root), 1024 * 1024, epoch_source=epoch_source)
        failed.scan(OWNER)
        with patch.object(failed._index, 'close', side_effect=RuntimeError('cleanup fault')):
            try:
                failed.close(OWNER)
            except RuntimeError as error:
                assert str(error) == 'cleanup fault'
            else:
                raise AssertionError('cleanup fault hidden')
            try:
                failed.scan(OWNER)
            except RuntimeError:
                pass
            else:
                raise AssertionError('failed cleanup reopened scope')
        assert failed.retained_bytes(OWNER) > 0
        failed.close(OWNER)
        assert failed.retained_bytes(OWNER) == 0
        primary = OwnedIndexScope(OWNER, str(root), 1024 * 1024, epoch_source=epoch_source)
        primary.scan(OWNER)
        with patch.object(primary._index, 'close', side_effect=RuntimeError('secondary cleanup fault')):
            try:
                with primary:
                    raise ValueError('primary caller failure')
            except ValueError as error:
                assert str(error) == 'primary caller failure'
            else:
                raise AssertionError('primary caller failure lost')
        primary.close(OWNER)
        assert primary.retained_bytes(OWNER) == 0
        reentrant = OwnedIndexScope(OWNER, str(root), 1024 * 1024, epoch_source=lambda: 0)
        def close_from_epoch():
            reentrant.close(OWNER)
            return 0
        reentrant._epoch_source = close_from_epoch
        try:
            reentrant.scan(OWNER)
        except RuntimeError as error:
            assert 'active scan' in str(error)
        else:
            raise AssertionError('reentrant close falsely completed before scan quiescence')
        reentrant.close(OWNER)
        assert reentrant.retained_bytes(OWNER) == 0
    print('explicit epoch authority, foreign denial, blocked concurrent close, terminal admission and exception cleanup passed')


if __name__ == '__main__':
    main()
