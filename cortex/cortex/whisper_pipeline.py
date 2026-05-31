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
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

import structlog

from .prompts import WHISPER_ZH_PROMPT as ZH_PROMPT

log = structlog.get_logger(__name__)


WHISPER_CLI = os.environ.get("WHISPER_CLI", "/opt/homebrew/bin/whisper-cli")
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
    ):
        if model not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported model '{model}', expected one of {SUPPORTED_MODELS}")
        self.model = model
        self.binary = binary
        self.model_path = models_dir / f"ggml-{model}.bin"
        if not self.model_path.exists():
            log.warning("whisper.model_missing", path=str(self.model_path))

    async def transcribe(
        self,
        pcm_bytes: bytes,
        sample_rate: int = 16000,
        channels: int = 1,
        lang: str = "auto",
    ) -> str:
        """Transcribe a chunk of PCM. Returns the transcript text (utf-8).
        `lang` accepts "auto" / "zh" / "en" — others are passed through.
        For zh we also inject the simplified-output prompt."""
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
            if lang == "zh":
                args += ["-l", "zh", "--prompt", ZH_PROMPT]
            elif lang in ("en", "auto"):
                args += ["-l", lang]
            else:
                args += ["-l", lang]

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
        """Spawn one dummy transcription so first real call doesn't load model.
        Called from cortex.main during boot. ~3s for `small` on M2."""
        log.info("whisper.prewarm.start", model=self.model)
        try:
            # 100 ms of silence at 16 kHz / 16-bit mono = 3200 bytes
            silence = bytes(3200)
            t = await self.transcribe(silence, lang="en")
            log.info("whisper.prewarm.done", n_chars=len(t))
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
