# Architecture review

This is a list of design problems found in a review on 2026-11-12, with the
rough shape of a rework for each. Every finding was checked against main
`9402330`. Each workstream is meant to be picked up by a later session as
its own stack of PRs.

Findings name functions, not line numbers, which move with every merge.
Counts are as of `9402330`.

## How to use this document

- Pick a workstream from [Order and dependencies](#order-and-dependencies),
  not a single finding. Several findings share tests and state, so they land
  together or block each other.
- Don't keep backwards compatibility. The project is pre-release with one
  test deployment, so a rework can change the saved state, the ledger
  records, or the config without a migration. Say in the PR what the
  deployment has to do, such as starting from an empty database.
- When a workstream merges, delete its findings from this document. When the
  last one goes, delete the document.

What was checked: every file in `src/spoolman_prusalink` was read in full,
and the bridge findings (B1 to B11, all fixed) came from that reading. The
test (T1 to T8) and simulator (S1 to S6) findings come from two separate
reviews. A second pass then checked every finding against the code.

## The common pattern

Stack #105 removed `Once`, which coupled problem state to log levels and
held both away from the code that owned them. Most findings below are
relatives of that problem:

- State that has no single owner, kept consistent by convention.
- A value stored in its rendered form (G-code, prose) and parsed back.
- One module reaching into another's private fields.
- The same rule or list written out in several places.

The rule from #105 still applies to every rework here: state lives at the
code that owns it, and the call site that detects a change logs it with a
literal level. Don't add a central object that owns levels.

## Tests and tooling

### The test layers

Each layer keeps the one below it honest. The findings in this section are
judged against these roles.

| Layer | Role | Runs |
| --- | --- | --- |
| Unit suite (`tests/`, `simulator/tests/`) | Covers everything. The 99.4% floor counts this suite only, as a cheap check that nothing lacks a test. | Constantly during development, so it stays fast and needs no container. |
| Integration suite (`integration/`) | Checks that the unit suite's fakes and state setup haven't diverged from a real Spoolman. | In CI and before a merge. Development doesn't depend on it. |
| Live checks and firmware reading | Keep the simulator behaving like a real printer, which can't be put in a test loop. | By hand, at the printer. |

Some scenarios are tested in both suites. That's accepted. Don't move
coverage out of the unit suite to remove it.

### T3. A behavior change is made in two suites

Most W rows of `docs/scenarios.md` are tested in both
`tests/test_writes.py` and `integration/test_writes.py`.

The duplication is accepted (see [the test layers](#the-test-layers)).
Nothing to do now. If changing both suites for each behavior change becomes
a burden, narrow the integration suite to the scenarios where Spoolman's
own behavior decides the outcome. Don't narrow the unit suite.

## Order and dependencies

| # | Workstream | Findings | Depends on |
| --- | --- | --- | --- |

T3 has no work, so it isn't in a workstream.
