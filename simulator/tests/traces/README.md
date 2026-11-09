# Traces from the printer

One `<ID>.jsonl` per row of [docs/printer-behaviors.md](../../../docs/printer-behaviors.md),
recorded with `python -m simulator.probe` and replayed by `test_traces.py`. A row
recorded under different conditions gets a variant, `<ID><letter>-<what>.jsonl`, with
its own replay: `F10b-sync-job-rewrite` is the sync job writing the spool already
loaded, from the FINISHED screen, and `F10c-sync-job-rewrite-idle` the same job from
IDLE after a boot, with Z not homed.

The first line of each file is the header: the trace's name, the printer's URL,
when it started, the probe's `interval`, `heartbeat` and `tool`, the printer's
`firmware` version with the full `/api/version` and `/api/v1/info` bodies (model,
hostname, serial, nozzle), the bridge commit the probe ran from, and the `--note`
given. A disagreement between a trace and the firmware source is recorded against
that firmware version in docs/printer-behaviors.md.
