"""Content-defined chunking with a 64-bit rolling hash (buzhash family).

Files are packed into one continuous stream (small files share chunks) and
split at content-defined boundaries so that inserting/removing bytes only
re-chunks a local neighbourhood — the property that makes cross-revision
deduplication work.

Algorithm (clean-room, from the published design):

- a 256-entry table of pseudo-random 64-bit values is derived
  deterministically from a per-storage ``seed`` (so chunk boundaries are
  storage-specific and cannot be fingerprinted across storages);
- the rolling window is exactly ``min_size`` bytes wide;
- a boundary is declared when ``hash & (avg_size - 1) == 0`` (``avg_size``
  must be a power of two), which fires on average once per ``avg_size``
  bytes of random data;
- chunks are never smaller than ``min_size`` (the first window) nor larger
  than ``max_size`` (forced cut).

Pure Python: throughput is modest (~10 MB/s on one core), which is fine
for the intended workload — Odoo filestores are content-addressed (files
never mutate, they only appear/disappear), so after the initial backup only
the day's new files are ever chunked. The scan is resumable: the rolling
hash and its position survive between ``update`` calls, so every byte is
hashed once however the stream is split (a filestore feeds it one small
file at a time).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

DEFAULT_MIN_SIZE = 1 * 1024 * 1024
DEFAULT_AVG_SIZE = 4 * 1024 * 1024
DEFAULT_MAX_SIZE = 16 * 1024 * 1024

_MASK64 = (1 << 64) - 1


def derive_table(seed: bytes) -> list[int]:
    """Derive the 256-entry random table from the storage seed.

    A keyed blake2b counter stream: deterministic for a given seed, and
    different seeds produce unrelated tables (secret-dependent boundaries).
    """
    table: list[int] = []
    counter = 0
    while len(table) < 256:
        block = hashlib.blake2b(
            counter.to_bytes(8, "little"), key=seed, digest_size=64
        ).digest()
        for i in range(0, 64, 8):
            if len(table) < 256:
                table.append(int.from_bytes(block[i : i + 8], "little"))
        counter += 1
    return table


def _rotl(value: int, count: int) -> int:
    count %= 64
    return ((value << count) | (value >> (64 - count))) & _MASK64


class Chunker:
    """Streaming chunker: feed bytes with :meth:`update`, finish with
    :meth:`flush`. Emitted chunks are plain ``bytes``.

    The packing behaviour matters to callers: consecutive ``update`` calls
    are one continuous stream — a chunk may span file boundaries. Callers
    that must cut the stream (e.g. before an unchanged file whose chunks
    are reused from the previous revision) call :meth:`flush`.
    """

    def __init__(
        self,
        seed: bytes,
        *,
        min_size: int = DEFAULT_MIN_SIZE,
        avg_size: int = DEFAULT_AVG_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
    ) -> None:
        if avg_size & (avg_size - 1):
            raise ValueError("avg_size must be a power of two")
        if not (0 < min_size <= avg_size <= max_size):
            raise ValueError("expected 0 < min_size <= avg_size <= max_size")
        self.min_size = min_size
        self.avg_size = avg_size
        self.max_size = max_size
        self._mask = avg_size - 1
        self._table = derive_table(seed)
        # Outgoing bytes leave through a window min_size wide: their table
        # entries have been rotated min_size times by the time they exit.
        self._out_table = [_rotl(v, min_size) for v in self._table]
        self._buffer = bytearray()
        self._pending: list[bytes] = []
        # Resumable scan state: _hash is the hash of the window ending at
        # buffer offset _scan_pos, and no boundary fired up to there
        # (_scan_pos == 0: the current chunk's first window is not hashed yet).
        self._scan_pos = 0
        self._hash = 0

    # -- internal ----------------------------------------------------------

    def _scan(self, *, final: bool) -> None:
        """Cut as many chunks out of the buffer as the data allows.

        Resumes where the previous call stopped instead of re-hashing the
        buffer from its start — otherwise a stream fed in small pieces
        (one small file per call) costs O(calls x max_size).
        """
        buf = self._buffer
        min_size = self.min_size
        max_size = self.max_size
        mask = self._mask
        table = self._table
        out_table = self._out_table

        while True:
            n = len(buf)
            if n < min_size:
                break
            pos, h = self._scan_pos, self._hash
            cut = 0
            # Every view (slices included) is released by its with-block, not
            # by refcounting: a live export makes the del/extend of the
            # buffer raise BufferError.
            with memoryview(buf) as view:
                if pos == 0:
                    # First window of a new chunk.
                    with view[:min_size] as first:
                        for b in first:
                            h = (((h << 1) | (h >> 63)) & _MASK64) ^ table[b]
                    pos = min_size
                    if (h & mask) == 0:
                        cut = min_size
                if not cut:
                    # Roll one byte at a time: drop buf[pos-min_size], add buf[pos].
                    upper = min(n, max_size)
                    with (
                        view[pos - min_size : upper - min_size] as outgoing,
                        view[pos:upper] as incoming,
                    ):
                        for out_b, in_b in zip(outgoing, incoming):
                            h = (
                                (((h << 1) | (h >> 63)) & _MASK64)
                                ^ out_table[out_b]
                                ^ table[in_b]
                            )
                            pos += 1
                            if (h & mask) == 0:
                                cut = pos
                                break
                        else:
                            if upper == max_size:
                                cut = max_size
            if cut:
                self._pending.append(bytes(buf[:cut]))
                del buf[:cut]
                self._scan_pos = self._hash = 0
                continue
            # No boundary in the available data: resume here next time.
            self._scan_pos, self._hash = pos, h
            break
        if final:
            if buf:
                # End of stream: the tail is the last (possibly short) chunk.
                self._pending.append(bytes(buf))
                buf.clear()
            self._scan_pos = self._hash = 0

    # -- public ------------------------------------------------------------

    def update(self, data: bytes) -> Iterator[bytes]:
        """Feed data; yield any chunks completed by it."""
        self._buffer.extend(data)
        self._scan(final=False)
        while self._pending:
            yield self._pending.pop(0)

    def flush(self) -> Iterator[bytes]:
        """Cut the stream: emit everything buffered (the last chunk may be
        shorter than min_size, possibly empty stream = no chunk)."""
        self._scan(final=True)
        while self._pending:
            yield self._pending.pop(0)
