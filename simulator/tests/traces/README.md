# Traces from the printer

One `<ID>.jsonl` per row of [docs/printer-behaviors.md](../../../docs/printer-behaviors.md),
recorded with `python -m simulator.probe` and replayed by `test_traces.py`.

The first line of each file is the header: the trace's name, the printer's URL,
when it started, the probe's `interval`, `heartbeat` and `tool`, the printer's
`firmware` version with the full `/api/version` and `/api/v1/info` bodies (model,
hostname, serial, nozzle), the bridge commit the probe ran from, and the `--note`
given. A disagreement between a trace and the firmware source is recorded against
that firmware version in docs/printer-behaviors.md.
