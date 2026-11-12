"""Run the simulator, print the bridge config for it, or seed Spoolman with its spools."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import traceback
from dataclasses import replace

from aiohttp import web

from simulator import config, control, prusalink
from simulator.model import Event, Power, Simulation

TICK_SECONDS = 0.1
SHUTDOWN_SECONDS = 1.0  # a hung request or open stream mustn't hold up exit


def _print_event(event: Event) -> None:
    # Requests are the bridge's polls: too many for stdout, so only the page shows them.
    if event.kind == "request":
        return
    where = event.printer or "-"
    print(f"{event.t:9.1f} {where} {event.kind}: {event.text}", flush=True)


class Server:
    """The control app and one PrusaLink listener per printer, opened and closed with power."""

    def __init__(self, sim: Simulation, host: str, control_port: int) -> None:
        self.sim, self.host, self.control_port = sim, host, control_port
        self.runners: dict[str, web.AppRunner] = {}
        self.sites: dict[str, web.TCPSite] = {}

    async def start(self) -> None:
        runner = web.AppRunner(control.make_app(self.sim), shutdown_timeout=SHUTDOWN_SECONDS)
        await runner.setup()
        await web.TCPSite(runner, self.host, self.control_port).start()
        self.runners["/control"] = runner
        for name in self.sim.printers:
            printer_runner = web.AppRunner(
                prusalink.make_app(self.sim, name), shutdown_timeout=SHUTDOWN_SECONDS
            )
            await printer_runner.setup()
            self.runners[name] = printer_runner
        await self.sync_sites()
        print(f"control page on http://{self.host}:{self.control_port}", flush=True)

    async def sync_sites(self) -> None:
        for name, printer in self.sim.printers.items():
            site = self.sites.get(name)
            if printer.power is Power.OFF and site is not None:
                await site.stop()
                del self.sites[name]
            elif printer.power is not Power.OFF and site is None:
                site = web.TCPSite(self.runners[name], self.host, printer.port)
                await site.start()
                self.sites[name] = site

    async def run_clock(self) -> None:
        """Tick the model. A failure is printed and the clock keeps going: a dead clock would
        freeze every printer with no sign of why."""
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            now = loop.time()
            try:
                self.sim.tick((now - last) * self.sim.settings.time_scale)
            except Exception:
                traceback.print_exc()
            last = now
            try:
                await self.sync_sites()
            except Exception:
                # Say rebinding a port failed: sync_sites tries again on the next tick.
                traceback.print_exc()

    async def stop(self) -> None:
        for runner in self.runners.values():
            await runner.cleanup()


async def _run(cfg: config.SimConfig) -> None:
    sim = config.build(cfg)
    sim.listeners.append(_print_event)
    server = Server(sim, cfg.host, cfg.control_port)
    await server.start()
    for p in sim.printers.values():
        print(f"{p.name}: {p.model.name} ({p.model.code}) on port {p.port}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    clock = asyncio.create_task(server.run_clock())
    try:
        await stop.wait()
    finally:
        clock.cancel()
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulator")
    parser.add_argument("-c", "--config", default="simulator/sim.example.toml")
    parser.add_argument("--host", help="listen on this address instead of the config's host")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the printers and the control page (the default)")
    sub.add_parser("bridge-config", help="print the bridge's [[printers]] tables")
    seed_parser = sub.add_parser("seed", help="create spools and locations in Spoolman")
    seed_parser.add_argument("--spoolman", required=True, help="Spoolman's URL")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except (OSError, config.ConfigError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    if args.host:
        cfg = replace(cfg, host=args.host)

    if args.command == "bridge-config":
        print(config.bridge_config(cfg))
    elif args.command == "seed":
        from simulator import seed  # only this command needs it

        asyncio.run(seed.seed(args.spoolman, config.locations(cfg)))
    else:
        asyncio.run(_run(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
