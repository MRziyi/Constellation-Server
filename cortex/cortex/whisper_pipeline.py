"""Whisper.cpp STT pipeline for Glass audio streams.

Runs `whisper-cli` (via Homebrew) on the Mac mini against PCM frames the
Glass uploads over WSS. Picked over Rokid cloud + OpenAI Whisper API per
the benchmark in `/tmp/asr-bench/results.json` (and documented in
GLASS-CLIENT-DESIGN.md §2.4).

Pre-warm: import time spawns one dummy transcription so the first real
call doesn't pay the model-load tax (~3s for `small` on M2).

Public API:
    pipeline = WhisperPipeline(model="small")
    transcript = await pipeline.transcribe(pcm_bytes, lang="auto")
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
import structlog


log = structlog.get_logger(__name__)


WHISPER_CLI = os.environ.get("WHISPER_CLI", "/opt/homebrew/bin/whisper-cli")
# Persistent server binary (whisper.cpp). When a server is up, transcription
# POSTs to it (model already resident) instead of spawning whisper-cli per call
# — the spawn+model-load was ~0.6s (base) / ~1.5s (small) of fixed overhead on
# EVERY call, paid on the audio_end→STT-card critical path (Zack 2026-06-02).
WHISPER_SERVER = os.environ.get("WHISPER_SERVER", "/opt/homebrew/bin/whisper-server")
WHISPER_MODELS_DIR = Path(os.environ.get(
    "WHISPER_MODELS_DIR",
    str(Path.home() / "constellation" / "whisper-models"),
))

# Supported model names (must have ggml-<name>.bin in WHISPER_MODELS_DIR).
SUPPORTED_MODELS = ("tiny", "base", "small", "medium", "large")


class WhisperError(Exception):
    """Raised when whisper-cli fails or returns unparseable output."""


class WhisperPipeline:
    """Wrapper around the `whisper-cli` binary. One instance per Cortex
    process is plenty; calls are serialised by the binary itself (model
    load is held in memory between calls within whisper-cli's own session)."""

    def __init__(
        self,
        model: str = "small",
        binary: str = WHISPER_CLI,
        models_dir: Path = WHISPER_MODELS_DIR,
        server_port: int | None = None,
        server_binary: str = WHISPER_SERVER,
    ):
        if model not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported model '{model}', expected one of {SUPPORTED_MODELS}")
        self.model = model
        self.binary = binary
        self.model_path = models_dir / f"ggml-{model}.bin"
        if not self.model_path.exists():
            log.warning("whisper.model_missing", path=str(self.model_path))
        # Persistent-server transport (Zack 2026-06-02). When server_port is set
        # and a whisper-server is reachable there, transcribe() POSTs to it
        # (no per-call model load). Falls back to the whisper-cli subprocess on
        # any server failure, so STT never breaks.
        self.server_port = server_port
        self.server_binary = server_binary
        self._server_proc: asyncio.subprocess.Process | None = None
        self._server_ready = False
        self._server_lock = asyncio.Lock()

    async def transcribe(
        self,
        pcm_bytes: bytes,
        sample_rate: int = 16000,
        channels: int = 1,
        lang: str = "auto",
    ) -> str:
        """Transcribe a chunk of PCM → transcript text (utf-8).

        Prefers the persistent whisper-server (model already resident → no
        per-call load); falls back to the whisper-cli subprocess on ANY server
        failure so STT never breaks. The decoder is never language-biased."""
        if not pcm_bytes:
            return ""
        if self._server_ready:
            text = await self._transcribe_server(pcm_bytes, sample_rate, channels)
            if text is not None:
                return text
            log.warning("whisper.server.fallback_to_cli", model=self.model)
        return await self._transcribe_cli(pcm_bytes, sample_rate, channels, lang)

    async def _transcribe_server(
        self, pcm_bytes: bytes, sample_rate: int, channels: int,
    ) -> str | None:
        """POST the PCM (wrapped as WAV) to the resident whisper-server. Returns
        the transcript, or None on any failure (caller falls back to the CLI)."""
        wav = _pcm_to_wav(pcm_bytes, sample_rate, channels)
        url = f"http://127.0.0.1:{self.server_port}/inference"
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
        form.add_field("response_format", "json")
        form.add_field("language", "auto")   # never bias the language
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, data=form) as resp:
                    if resp.status != 200:
                        log.warning("whisper.server.http_status",
                                    status=resp.status, model=self.model)
                        return None
                    body = await resp.text()
        except Exception as e:
            log.warning("whisper.server.request_failed", model=self.model, error=str(e))
            self._server_ready = False   # re-probe / relaunch on next start_server()
            return None
        try:
            data = _json.loads(body)
            text = (data.get("text") if isinstance(data, dict) else None) or ""
        except Exception:
            text = body
        return text.strip()

    async def start_server(self) -> bool:
        """Ensure a whisper-server for this model is up + reachable. Reuses one
        already listening (it survives a cortex restart → stays warm), else
        launches it DETACHED (start_new_session) and polls until ready.
        Idempotent. Returns True if the server is usable."""
        if self.server_port is None:
            return False
        async with self._server_lock:
            if self._server_ready:
                return True
            if await self._server_alive():
                self._server_ready = True
                log.info("whisper.server.reuse", model=self.model, port=self.server_port)
                return True
            if not Path(self.server_binary).exists():
                log.warning("whisper.server.binary_missing", binary=self.server_binary)
                return False
            if not self.model_path.exists():
                return False
            try:
                self._server_proc = await asyncio.create_subprocess_exec(
                    self.server_binary,
                    "-m", str(self.model_path),
                    "--host", "127.0.0.1", "--port", str(self.server_port),
                    "-nt",   # no timestamps (NOT -np: that's a whisper-cli-only flag
                             # and whisper-server exits with usage if it sees it)
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,   # detach → survive cortex restart, reused warm
                )
            except Exception as e:
                log.warning("whisper.server.launch_failed", model=self.model, error=str(e))
                return False
            for _ in range(60):              # up to ~30s for the model to load
                if await self._server_alive():
                    self._server_ready = True
                    log.info("whisper.server.ready", model=self.model, port=self.server_port)
                    return True
                await asyncio.sleep(0.5)
            log.warning("whisper.server.never_ready", model=self.model, port=self.server_port)
            return False

    async def _server_alive(self) -> bool:
        """True iff something is listening on the server port. whisper-server
        binds only after the model finishes loading, so this also implies ready."""
        if self.server_port is None:
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.server_port), timeout=1.0,
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _transcribe_cli(
        self,
        pcm_bytes: bytes,
        sample_rate: int = 16000,
        channels: int = 1,
        lang: str = "auto",
    ) -> str:
        """Fallback path: spawn whisper-cli per call (pays model load each time)."""
        if not pcm_bytes:
            return ""

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = Path(f.name)
            f.write(_pcm_to_wav(pcm_bytes, sample_rate, channels))

        try:
            args = [
                self.binary,
                "-m", str(self.model_path),
                "-nt", "-np",            # no timestamps, no progress
                "-f", str(wav_path),
            ]
            # NEVER force/bias a language (Zack 2026-06-01: spoke English, got
            # Chinese — a forced/biased language is wrong). Always `-l auto` so
            # whisper detects what was actually spoken. (`lang` is kept in the
            # signature for callers but no longer pins the decoder.)
            args += ["-l", "auto"]

            log.info("whisper.transcribe.start",
                     bytes=len(pcm_bytes), lang=lang, model=self.model)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=60.0,
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise WhisperError(f"whisper-cli timed out (>60s) for {len(pcm_bytes)}B")

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:500]
                raise WhisperError(f"whisper-cli rc={proc.returncode}: {err}")

            transcript = _parse_transcript(stdout.decode("utf-8", errors="replace"))
            log.info("whisper.transcribe.done",
                     n_chars=len(transcript), lang=lang, model=self.model)
            return transcript
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass

    async def prewarm(self) -> None:
        """Boot the persistent whisper-server (or reuse a warm one) so the first
        real call — and every call after — skips the per-call model load, then
        run one dummy transcription. Called from cortex.main during boot."""
        log.info("whisper.prewarm.start", model=self.model, server_port=self.server_port)
        try:
            started = await self.start_server()
            # 100 ms of silence at 16 kHz / 16-bit mono = 3200 bytes
            silence = bytes(3200)
            t = await self.transcribe(silence, lang="en")
            log.info("whisper.prewarm.done", n_chars=len(t), server=started)
        except Exception as e:
            log.warning("whisper.prewarm.failed", error=str(e))


# ── module helpers ─────────────────────────────────────────────────────────


def _pcm_to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw 16-bit PCM in a WAV header. whisper-cli supports flac / mp3 /
    ogg / wav — wav is the easiest (no encode step)."""
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16)
        + b"data"
        + struct.pack("<I", data_size)
    )
    return header + pcm


def _parse_transcript(stdout: str) -> str:
    """Pull the transcript text out of whisper-cli's stdout. The CLI prints
    a bunch of metal/ggml/whisper init lines first, then blank line(s), then
    the transcript. With `-nt -np`, only the transcript should appear."""
    lines = [
        l.strip() for l in stdout.splitlines()
        if l.strip()
        and not l.startswith(("ggml_", "whisper_", "load_", "system_info", "main:"))
    ]
    return " ".join(lines).strip()
