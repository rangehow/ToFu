"""Pinned no-follow directory handles for owned job output IO (POSIX only)."""
import os
from contextlib import contextmanager
from pathlib import Path
from .contracts import EnvironmentError


class OutputDirectory:
    def __init__(self, path, *, project_root=None, create=False):
        if not all(hasattr(os, flag) for flag in ('O_DIRECTORY', 'O_NOFOLLOW')):
            raise EnvironmentError('filesystem_unsupported', 'Pinned no-follow directory IO is unavailable')
        self.path = Path(path)
        if not self.path.is_absolute() or '..' in self.path.parts:
            raise EnvironmentError('invalid_destination', 'Absolute owned destination required')
        root = Path(project_root) if project_root is not None else self.path
        if not root.is_absolute() or '..' in root.parts or not self.path.is_relative_to(root):
            raise EnvironmentError('invalid_destination', 'Destination outside project root')
        self.fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(self.path.parts[1:], 1):
                if create and index > len(root.parts) - 1:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=self.fd)
                    except FileExistsError:
                        pass
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = new
        except OSError as exc:
            os.close(self.fd)
            raise EnvironmentError('invalid_destination', 'Output directory ancestor is missing or not a real directory') from exc
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def child(self, parts, *, create=False):
        result = object.__new__(OutputDirectory)
        result.path, result.fd = self.path.joinpath(*parts), os.dup(self.fd)
        try:
            for part in parts:
                if part in {'', '.', '..'} or '/' in part or '\\' in part:
                    raise EnvironmentError('invalid_output', 'Relative output component required')
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=result.fd)
                    except FileExistsError:
                        pass
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=result.fd)
                os.close(result.fd)
                result.fd = new
            return result
        except BaseException:
            os.close(result.fd)
            raise

    def remove_tree(self, name):
        """Remove one owned snapshot relative to this handle (Python 3.10 APIs)."""
        with self.child([name]) as child:
            with os.scandir(child.fd) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        child.remove_tree(entry.name)
                    else:
                        os.unlink(entry.name, dir_fd=child.fd)
        os.rmdir(name, dir_fd=self.fd)

    @contextmanager
    def cleanup_child(self, name):
        try:
            yield
        except BaseException as primary:
            try:
                self.remove_tree(name)
            except BaseException as failure:
                raise primary from failure
            raise
        else:
            self.remove_tree(name)
