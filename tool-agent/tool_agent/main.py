"""Tool Agent entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
import structlog
from dotenv import load_dotenv

from .registry import ToolRegistry
from .server import serve


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
    """Same search order as cortex/main.py — picks up OPENAI_API_KEY for twin_query."""
    repo_root = Path(__file__).resolve().parents[2]
    for p in [repo_root / ".env", repo_root / "tool-agent" / ".env", repo_root / "cortex" / ".env"]:
        if p.exists():
            load_dotenv(p, override=False)
            return p
    return None


@click.command()
@click.option("--host", default="127.0.0.1", help="Bind address (localhost only)")
@click.option("--port", default=8889, type=int, help="Port for Cortex IPC")
@click.option(
    "--adapters-config",
    default=None,
    type=click.Path(),
    help="Path to adapters.yaml (default: ./adapters.yaml next to script)",
)
@click.option("--log-level", default="INFO", help="Log level")
def cli(host: str, port: int, adapters_config: str | None, log_level: str) -> None:
    """Start Tool Agent.

    Phase 1: minimum-viable. Echo adapter only. See HANDOFF.md §6.
    """
    _configure_logging(log_level)
    log = structlog.get_logger(__name__)
    env_path = _load_env()
    if env_path:
        log.info("env.loaded", path=str(env_path))
    cfg_path = Path(adapters_config) if adapters_config else Path(__file__).resolve().parents[1] / "adapters.yaml"
    registry = ToolRegistry()
    registry.load(cfg_path)
    asyncio.run(serve(host, port, registry))


if __name__ == "__main__":
    cli()
