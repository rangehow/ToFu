"""Owned count/byte-bounded Python event chunks; offline retention experiment.

Membership changes discard the optional full-window pointer index immediately.
Payload dictionaries remain the original objects; snapshots own membership only.
No production storage/publication/default is changed.
"""
from itertools import chain


class ChunkedWindow:
    block_size = 64  # data layout granularity, not a system resource cap

    def __init__(self, max_events, max_bytes):
        if type(max_events) is not int or type(max_bytes) is not int or min(max_events, max_bytes) < 1:
            raise ValueError('positive integer event/byte limits required')
        self.max_events, self.max_bytes = max_events, max_bytes
        self.clear()

    def clear(self):
        self.blocks = []
        self.size_blocks = []
        self.head = 0
        self.count = 0
        self.retained_bytes = 0
        self.flat_index = None

    def __len__(self):
        return self.count

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self.tail(0)[key]
        index = key + self.count if key < 0 else key
        if not 0 <= index < self.count:
            raise IndexError('event index out of range')
        block, offset = divmod(self.head + index, self.block_size)
        return self.blocks[block][offset]

    def __iter__(self):
        return iter(self.tail(0))

    def append(self, event, event_bytes):
        if type(event_bytes) is not int or event_bytes < 0:
            raise ValueError('event byte size must be a nonnegative integer')
        self.flat_index = None
        if not self.blocks or len(self.blocks[-1]) == self.block_size:
            self.blocks.append([])
            self.size_blocks.append([])
        self.blocks[-1].append(event)
        self.size_blocks[-1].append(event_bytes)
        self.count += 1
        self.retained_bytes += event_bytes
        trimmed = 0
        while self.count > self.max_events or (self.retained_bytes > self.max_bytes and self.count > 1):
            self.retained_bytes -= self.size_blocks[0][self.head]
            # Drop evicted payload references now, including within a block.
            self.blocks[0][self.head] = None
            self.size_blocks[0][self.head] = 0
            self.head += 1
            self.count -= 1
            trimmed += 1
            if self.head == len(self.blocks[0]):
                del self.blocks[0]
                del self.size_blocks[0]
                self.head = 0
        return trimmed

    def tail(self, start):
        start = max(0, start)
        if start >= self.count:
            return []
        if self.flat_index is not None:
            return self.flat_index[start:]
        block, offset = divmod(self.head + start, self.block_size)
        result = self.blocks[block][offset:]
        if block + 1 < len(self.blocks):
            result.extend(chain.from_iterable(self.blocks[block + 1:]))
        if start == 0:
            self.flat_index = result
            return list(result)
        return result
