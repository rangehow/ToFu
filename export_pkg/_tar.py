"""Streaming ``tar c | tar x`` copy helpers (split out of ``export.py``)."""
import os
import shutil
import subprocess
import tempfile
import sys
import threading
import time
from pathlib import Path

from export_pkg._lists import C_CYAN, C_DIM, C_END, ROOT, logger
def _ellipsize(text: str, width: int) -> str:
    """Trim ``text`` to ``width`` chars, keeping the tail (the filename)."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return '…' + text[-(width - 1):]


def _tar_progress_consumer(pipe, dst: Path) -> None:
    """Render live per-file progress from ``tar -v`` verbose output.

    ``tar cvf -`` writes the archive to stdout and the name of each member
    to stderr, one path per line, as it descends the tree. We read that
    stream and show:
      - a permanent line the first time tar enters each TOP-LEVEL entry
        (``./lib``, ``./static``, …) so an unwanted dir that slipped past
        the ``--exclude`` rules is immediately visible; and
      - a live, in-place ``transferred N · <current file>`` counter.

    The counter is only animated on a real TTY; when output is piped/
    captured we just emit the per-top-level lines (no carriage-return
    spam in logs).
    """
    is_tty = sys.stdout.isatty()
    seen_top: set[str] = set()
    count = 0
    last_paint = 0.0
    try:
        term_w = shutil.get_terminal_size((100, 24)).columns
    except OSError:
        term_w = 100

    for raw in iter(pipe.readline, b''):
        line = raw.decode('utf-8', 'replace').rstrip('\n')
        if not line:
            continue
        rel = line[2:] if line.startswith('./') else line
        if not rel:
            # tar emits the archive root ('./') as its first member — skip.
            continue
        count += 1
        top = rel.split('/', 1)[0]
        if top and top not in seen_top:
            seen_top.add(top)
            src_item = (ROOT / top)
            kind = 'dir ' if src_item.is_dir() else 'file'
            # Clear any live counter line before printing a permanent row.
            if is_tty:
                sys.stdout.write('\r\033[K')
            print(f"    {C_DIM}↳ {kind} {C_END}{C_CYAN}{top}{C_END}")
            if is_tty:
                sys.stdout.flush()
        if is_tty:
            now = time.monotonic()
            if now - last_paint >= 0.08:
                last_paint = now
                shown = _ellipsize(rel, max(20, term_w - 24))
                sys.stdout.write(
                    f"\r\033[K    {C_DIM}transferred {count} · {shown}{C_END}")
                sys.stdout.flush()

    if is_tty:
        sys.stdout.write(f"\r\033[K    {C_DIM}transferred {count} files{C_END}\n")
        sys.stdout.flush()
    else:
        print(f"    {C_DIM}transferred {count} files{C_END}")


def _stream_tar_copy(src: Path, dst: Path, excludes: list[str],
                     progress: bool = False) -> None:
    """Run ``tar c <excludes> . | tar x`` from src into dst.

    When ``progress`` is True, ``tar`` runs in verbose mode and each
    archived path is streamed live (see ``_tar_progress_consumer``) so the
    operator can watch the transfer and spot files that shouldn't be
    carried. Without it, the copy is silent (the historical behavior).

    Raises RuntimeError on real failure. GNU tar rc=1 ("file changed as
    we read it") is treated as a non-fatal warning — the source tree may
    be live (running server appending to logs, .project_sessions, etc.).
    """
    dst.mkdir(parents=True, exist_ok=True)
    # Thousands of gitignored/untracked entries outgrow the OS argv limit —
    # feed patterns through --exclude-from instead of individual argv items.
    exclude_file = None
    if excludes:
        fd, tmp = tempfile.mkstemp(prefix='tofu-export-excludes-', text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(
                e.removeprefix('--exclude=') for e in excludes) + '\n')
        exclude_file = tmp
    exclude_args = [f'--exclude-from={exclude_file}'] if exclude_file else []
    # Verbose mode writes member names to stderr (stdout carries the archive).
    create_cmd = ['tar', 'cvf', '-', *exclude_args, '.'] if progress \
        else ['tar', 'cf', '-', *exclude_args, '.']
    try:
        tar_c = subprocess.Popen(
            create_cmd,
            cwd=str(src),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tar_x = subprocess.Popen(
            ['tar', 'xf', '-'],
            cwd=str(dst),
            stdin=tar_c.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if tar_c.stdout is not None:
            tar_c.stdout.close()

        # When showing progress, drain tar_c's stderr (the verbose file
        # list) in a thread so it can't block on a full pipe buffer while
        # we wait on the extract side. We capture the rendered lines back
        # into ``cerr`` only as needed for the rc=1 warning below.
        progress_thread = None
        if progress and tar_c.stderr is not None:
            progress_thread = threading.Thread(
                target=_tar_progress_consumer,
                args=(tar_c.stderr, src),
                daemon=True,
            )
            progress_thread.start()

        # No timeout — cross-FUSE copies can take minutes; user requested "no timeout".
        _, xerr = tar_x.communicate()
        if progress_thread is not None:
            # stderr is consumed by the thread; just reap the process.
            tar_c.wait()
            progress_thread.join(timeout=5)
            cerr = b''
        else:
            _, cerr = tar_c.communicate()
        if tar_c.returncode not in (0, 1, None):
            raise RuntimeError(
                f'tar create failed (rc={tar_c.returncode}): '
                f'{(cerr or b"").decode("utf-8", "replace")[:500]}'
            )
        elif tar_c.returncode == 1 and cerr:
            logger.info('tar warnings (non-fatal): %s',
                        (cerr or b'').decode('utf-8', 'replace')[:500])
        if tar_x.returncode != 0:
            raise RuntimeError(
                f'tar extract failed (rc={tar_x.returncode}): '
                f'{(xerr or b"").decode("utf-8", "replace")[:500]}'
            )
    except FileNotFoundError:
        raise RuntimeError(
            'tar binary not found — fast copy unavailable. '
            'Install GNU tar (conda install -c conda-forge tar).'
        ) from None
    finally:
        if exclude_file is not None:
            os.unlink(exclude_file)
