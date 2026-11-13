"""Compare the copied tables with the firmware source. See `script/firmware-check`.

Exits 0 when every difference is expected, 1 on any other difference or a stale expected
entry, 2 on a usage error and 3 when the firmware can't be fetched or read.
"""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from firmware import compare, source

USAGE = "usage: script/firmware-check [REF | --checkout DIR [REF]]"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--checkout"]:
        if not 2 <= len(args) <= 3:
            return _usage()
        directory = Path(args[1]).resolve()
        if not directory.is_dir():
            return _usage(f"{args[1]} isn't a directory")
        ref = args[2] if len(args) == 3 else "HEAD"
        where = f"{directory} at {ref}"
    else:
        if len(args) > 1:
            return _usage()
        directory = None
        ref = args[0] if args else "master"
        where = f"{source.REPO_URL} at {ref}"
    if ref.startswith("-"):
        return _usage()

    try:
        with _open(directory, ref) as firmware_source:
            print(f"==> Checking {where} ({firmware_source.commit})", flush=True)
            firmware = compare.load(firmware_source)
    except source.Unreadable as e:
        print(e, file=sys.stderr)
        return 3
    lines, ok = compare.report(compare.compare(firmware, compare.Copies.current()))
    print("\n".join(lines))
    return 0 if ok else 1


def _open(directory: Path | None, ref: str) -> AbstractContextManager[source.Source]:
    if directory is None:
        return source.upstream(ref)
    return nullcontext(source.Source(directory, ref))


def _usage(problem: str | None = None) -> int:
    if problem:
        print(problem, file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
