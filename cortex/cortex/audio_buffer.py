"""Per-stream PCM assembly for Glass `audio_chunk` events.

Glass uploads ~250 ms PCM frames over WSS as `audio_chunk` events keyed by
`stream_id`. This module:
  - accumulates frames per stream_id
  - drops late chunks for streams that already ended (race with closing)
  - bounds memory growth (caps individual streams at MAX_STREAM_BYTES and
    drops streams that have been idle for STREAM_IDLE_TIMEOUT_S)
  - returns the assembled PCM byte string when `audio_end` fires

The buffer is in-memory only — if cortex restarts mid-stream, the user just
re-speaks. Streams are tiny (~30 KB/s; a 10-second utterance is ~300 KB).
"""

from __future__ import annotations

import base64
import time
from collections import OrderedDict
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# A single utterance is rarely > 30 s; 1 MB ≈ 30 s of 16-kHz / 16-bit mono PCM.
# Cap at 2 MB so a stuck stream doesn't eat memory unbounded.
MAX_STREAM_BYTES = 2 * 1024 * 1024

# If we haven't received a chunk for this long, garbage-collect the stream.
STREAM_IDLE_TIMEOUT_S = 60.0

# Bound the total number of active streams to defend against a misbehaving
# client (each open stream uses up to MAX_STREAM_BYTES of memory).
MAX_ACTIVE_STREAMS = 8


class StreamEntry:
    __slots__ = ("stream_id", "buffer", "last_seq", "n_chunks", "started_at", "last_update_at", "sample_rate", "channels")

    def __init__(self, stream_id: str, sample_rate: int, channels: int) -> None:
        self.stream_id = stream_id
        self.buffer = bytearray()
        self.last_seq: int = -1
        self.n_chunks: int = 0
        self.started_at: float = time.time()
        self.last_update_at: float = time.time()
        self.sample_rate = sample_rate
        self.channels = channels

    def append(self, b64_pcm: str, seq: int) -> None:
        try:
            chunk = base64.b64decode(b64_pcm)
        except Exception as e:
            log.warning("audio_buffer.bad_b64", stream_id=self.stream_id, seq=seq, error=str(e))
            return
        # Allow out-of-order delivery but reject duplicates and unbounded growth.
        if seq <= self.last_seq:
            log.info("audio_buffer.dropped_dup_or_older",
                     stream_id=self.stream_id, seq=seq, last=self.last_seq)
            return
        if len(self.buffer) + len(chunk) > MAX_STREAM_BYTES:
            log.warning("audio_buffer.over_cap",
                        stream_id=self.stream_id, size=len(self.buffer))
            return
        self.buffer.extend(chunk)
        self.last_seq = seq
        self.n_chunks += 1
        self.last_update_at = time.time()


class AudioStreamBuffer:
    """LRU-bounded set of active streams keyed by stream_id."""

    def __init__(self) -> None:
        self._streams: OrderedDict[str, StreamEntry] = OrderedDict()

    def on_chunk(
        self,
        stream_id: str,
        seq: int,
        b64_pcm: str,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self._gc_expired()
        entry = self._streams.get(stream_id)
        if entry is None:
            if len(self._streams) >= MAX_ACTIVE_STREAMS:
                # Evict oldest
                oldest_id, _ = self._streams.popitem(last=False)
                log.warning("audio_buffer.evicted_for_capacity", evicted=oldest_id)
            entry = StreamEntry(stream_id, sample_rate, channels)
            self._streams[stream_id] = entry
        entry.append(b64_pcm, seq)
        # Move to MRU position
        self._streams.move_to_end(stream_id, last=True)

    def finalize(self, stream_id: str) -> StreamEntry | None:
        """Pop the stream's buffer; return it for STT. Returns None if the
        stream was never started or already finalized."""
        return self._streams.pop(stream_id, None)

    def discard(self, stream_id: str) -> None:
        """Cancel a stream without returning its bytes (e.g. user said 'cancel')."""
        self._streams.pop(stream_id, None)

    def _gc_expired(self) -> None:
        now = time.time()
        expired = [
            sid for sid, e in self._streams.items()
            if (now - e.last_update_at) > STREAM_IDLE_TIMEOUT_S
        ]
        for sid in expired:
            log.warning("audio_buffer.gc_idle", stream_id=sid)
            self._streams.pop(sid, None)

    def __len__(self) -> int:
        return len(self._streams)

    def snapshot(self) -> list[dict[str, Any]]:
        """Diagnostic: list active streams."""
        return [
            {
                "stream_id": e.stream_id,
                "n_chunks": e.n_chunks,
                "bytes": len(e.buffer),
                "duration_s": round(time.time() - e.started_at, 2),
                "sample_rate": e.sample_rate,
            }
            for e in self._streams.values()
        ]
