"""Cortex Agent entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import click
import structlog
from dotenv import load_dotenv

from .control_plane import get_plane
from .http import serve_http
from .llm_cache import set_call_observer
from .server import serve
from .twin import Twin


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ]
    )


def _load_env() -> Path | None:
    """Find and load the project's .env. Returns the path loaded, or None.

    Search order (first existing wins; later ones do NOT override):
      1. <repo_root>/.env
      2. <repo_root>/cortex/.env
      3. <repo_root>/tool-agent/.env   (current location of Zack's OPENAI_API_KEY)
    """
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / ".env",
        repo_root / "cortex" / ".env",
        repo_root / "tool-agent" / ".env",
    ]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)
            return p
    return None


@click.command()
@click.option("--host", default="127.0.0.1", help="Bind address for Glass WSS endpoint")
@click.option("--port", default=8888, type=int, help="Port for Glass WSS endpoint")
@click.option("--http-host", default=None, help="Bind address for management HTTP surface (defaults to --host)")
@click.option("--http-port", default=8890, type=int, help="Port for management HTTP surface (Phase 3a.1)")
@click.option(
    "--tool-agent-url",
    default="ws://localhost:8889",
    help="WebSocket URL of the Tool Agent",
)
@click.option(
    "--twin-root",
    default=None,
    type=click.Path(),
    help="Twin root directory (default: ~/constellation/twin)",
)
@click.option(
    "--router-model",
    default="gpt-5.2",
    envvar="CORTEX_ROUTER_MODEL",
    help="OpenAI model id for the Router. Override via CORTEX_ROUTER_MODEL env var.",
)
@click.option(
    "--use-stub-router",
    is_flag=True,
    default=False,
    help="Force use of the Phase 1 echo stub (skip GPT call even if OPENAI_API_KEY is set).",
)
@click.option("--log-level", default="INFO", help="Log level (DEBUG/INFO/WARN/ERROR)")
def cli(
    host: str,
    port: int,
    http_host: str | None,
    http_port: int,
    tool_agent_url: str,
    twin_root: str | None,
    router_model: str,
    use_stub_router: bool,
    log_level: str,
) -> None:
    """Start Cortex Agent."""
    _configure_logging(log_level)
    log = structlog.get_logger(__name__)

    env_path = _load_env()
    if env_path:
        log.info("env.loaded", path=str(env_path))
    else:
        log.info("env.none_found")

    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    use_stub = use_stub_router or not has_key
    log.info(
        "router.mode",
        mode="stub" if use_stub else "real",
        model=None if use_stub else router_model,
    )

    twin = Twin(Path(twin_root) if twin_root else None)

    # Control plane wiring (Phase 3a.1): single in-memory state mirror, populated
    # by server + llm_cache observer, read by the HTTP management surface.
    # P0.3 — also forward the LLM call to the session log so per-session
    # cost/latency totals are queryable from /api/sessions.
    plane = get_plane()

    def _on_llm_call(info: dict[str, object]) -> None:
        plane.record_llm_call(info)
        sid = info.get("session_id") if isinstance(info, dict) else None
        srv = plane.server
        if sid and srv is not None and getattr(srv, "sessions", None):
            try:
                srv.sessions.append(
                    sid, "llm_call",
                    purpose=info.get("purpose", "?"),
                    model=info.get("model", "?"),
                    provider=info.get("provider", "openai"),
                    cache_hit=bool(info.get("cache_hit", False)),
                    latency_ms=int(info.get("latency_ms", 0) or 0),
                    prompt_chars=int(info.get("prompt_chars", 0) or 0),
                    completion_chars=int(info.get("completion_chars", 0) or 0),
                )
            except Exception as e:
                log.warning("session.llm_call_append_failed", error=str(e))

    set_call_observer(_on_llm_call)

    http_bind = http_host or host

    async def _prewarm_whisper() -> None:
        """Phase 3b — preload the whisper.cpp model so the first real Glass
        audio_end doesn't pay the ~3 s model-load tax. Runs in the background;
        prewarm failures are logged but non-fatal (we may be running on a box
        without whisper-cli — e.g. CI)."""
        await asyncio.sleep(2.0)  # let serve() bind first
        srv = plane.server
        if srv is None:
            log.warning("whisper.prewarm.no_server")
            return
        whisper = getattr(srv, "_whisper", None)
        if whisper is None:
            log.warning("whisper.prewarm.no_whisper_attr")
            return
        try:
            await whisper.prewarm()
        except Exception as e:
            log.warning("whisper.prewarm.failed", error=str(e))
        # Also prewarm the partial-transcription model (Level 2 streaming).
        partial = getattr(srv, "_whisper_partial", None)
        if partial is not None:
            try:
                await partial.prewarm()
            except Exception as e:
                log.warning("whisper.prewarm_partial.failed", error=str(e))

    async def _prewarm_face() -> None:
        """People-Recall (Zack 2026-06-01): preload the InsightFace model so the
        first recall doesn't pay the multi-second model-load tax. Off the event
        loop (CPU/model load is blocking); non-fatal if deps/model are missing."""
        await asyncio.sleep(3.0)  # after serve() binds; let whisper warm first
        srv = plane.server
        fi = getattr(srv, "face_index", None) if srv else None
        if fi is None or not fi.available():
            log.info("face.prewarm.skipped")
            return
        try:
            await asyncio.to_thread(fi.warm)
        except Exception as e:
            log.warning("face.prewarm.failed", error=str(e))

    async def main() -> None:
        await asyncio.gather(
            serve(
                host=host,
                port=port,
                twin=twin,
                tool_agent_url=tool_agent_url,
                router_model=router_model,
                use_stub_router=use_stub,
                plane=plane,
            ),
            serve_http(host=http_bind, port=http_port, plane=plane),
            _prewarm_whisper(),
            _prewarm_face(),
        )

    asyncio.run(main())


if __name__ == "__main__":
    cli()
