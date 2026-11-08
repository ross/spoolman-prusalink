"""The server's clock."""

from __future__ import annotations

import asyncio

from simulator import __main__ as main
from simulator.tests.util import make


async def test_clock_survives_a_failing_tick(monkeypatch, capsys):
    sim, p = make()
    server = main.Server(sim, "127.0.0.1", 0)
    calls = []

    def tick(dt):
        calls.append(dt)
        if len(calls) == 1:
            raise RuntimeError("boom")

    async def sync_sites():
        if len(calls) == 2:
            raise OSError("port in use")

    monkeypatch.setattr(main, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(sim, "tick", tick)
    monkeypatch.setattr(server, "sync_sites", sync_sites)
    clock = asyncio.create_task(server.run_clock())
    await asyncio.sleep(0.2)
    clock.cancel()
    assert len(calls) > 3
    err = capsys.readouterr().err
    assert "RuntimeError: boom" in err and "OSError: port in use" in err
