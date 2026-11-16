"""Receive-side frame bytes shared by v1/v2 storage transports.

Owns only reconstructible frame bytes. A received body is decoded inside this
context; callers must return owned decoded values. Temporary files are private,
never durable authority state, and close on read/decode/checksum failures.
``ReceiveBuffer`` reads the consecutive response frames of one pipelined
exchange with as few receives as the kernel's queued bytes allow.
"""
from contextlib import contextmanager
import mmap
import socket
import tempfile


@contextmanager
def received_frame_body(read_exact, size: int, reservation=None):
    if not getattr(reservation, 'spilled', False):
        yield read_exact(size)
        return
    with tempfile.TemporaryFile(mode='w+b') as file:
        remaining = size
        chunk_bytes = getattr(reservation, 'chunk_bytes', 64 * 1024)
        while remaining:
            count = min(remaining, chunk_bytes)
            data = read_exact(count)
            if len(data) != count:
                raise EOFError('short storage overflow frame read')
            file.write(data)
            remaining -= count
        file.flush()
        with mmap.mmap(file.fileno(), size, access=mmap.ACCESS_READ) as body:
            yield body


class ReceiveBuffer:
    """Exact reads over one socket for the consecutive frames of a pipelined exchange.

    Each receive takes whatever the kernel already holds, up to the socket's
    own receive-buffer size, so a run of queued frames costs one receive
    instead of one per header, body and trailer. A remainder at least that
    large is received straight into its exact allocation. Unconsumed bytes stay
    staged for the next read; ``drained`` says whether any remain. ``closed``
    maps EOF to the transport's error: ``closed(True)`` at a frame boundary,
    ``closed(False)`` mid-frame.
    """

    def __init__(self, sock: socket.socket, closed):
        self._sock = sock
        self._closed = closed
        self._staged = bytearray(sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
        self._start = self._end = 0

    @property
    def drained(self) -> bool:
        return self._start == self._end

    def read_exact(self, size: int, *, frame_start: bool = False) -> bytearray:
        result = bytearray(size)
        capacity = len(self._staged)
        with memoryview(result) as view, memoryview(self._staged) as staged:
            filled = min(size, self._end - self._start)
            view[:filled] = staged[self._start:self._start + filled]
            self._start += filled
            while filled < size:
                # Every staged byte is consumed here, so the staging area is empty.
                missing = size - filled
                if missing >= capacity:
                    received = self._sock.recv_into(view[filled:], missing)
                    taken = received
                else:
                    received = self._sock.recv_into(staged, capacity)
                    taken = min(missing, received)
                    view[filled:filled + taken] = staged[:taken]
                    self._start, self._end = taken, received
                if not received:
                    raise self._closed(frame_start and not filled)
                filled += taken
        return result
