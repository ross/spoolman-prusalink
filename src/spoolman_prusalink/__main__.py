"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import config as config_mod
from .app import Bridge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spoolman-prusalink")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("SPOOLMAN_PRUSALINK_CONFIG", "/config/config.toml"),
        help="TOML config file (default: $SPOOLMAN_PRUSALINK_CONFIG or /config/config.toml)",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="poll until stopped (default)")
    sub.add_parser("once", help="poll once and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        cfg = config_mod.load(args.config)
    except (OSError, config_mod.ConfigError) as e:
        logging.error("config: %s", e)
        return 2
    return asyncio.run(_run(cfg, once=args.command == "once"))


async def _run(cfg: config_mod.Config, *, once: bool) -> int:
    bridge = Bridge.from_config(cfg)
    try:
        if once:
            await bridge.poll()
            return 0
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await bridge.run(stop)
        return 0
    finally:
        await bridge.aclose()


if __name__ == "__main__":
    sys.exit(main())
