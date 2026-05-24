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
    plane = get_plane()
    set_call_observer(plane.record_llm_call)

    http_bind = http_host or host

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
        )

    asyncio.run(main())


if __name__ == "__main__":
    cli()
