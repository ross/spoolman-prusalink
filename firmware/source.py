"""Read files from the firmware's git history: upstream on GitHub, or a local checkout."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_URL = "https://github.com/prusa3d/Prusa-Firmware-Buddy.git"


class Unreadable(Exception):
    """A firmware file can't be fetched or read, or doesn't have the expected structure."""

    def __init__(self, path: str, what: str) -> None:
        super().__init__(f"can't read {path}: {what}")


class Source:
    """The files of one commit, given as a branch, tag or commit, in a git repository."""

    def __init__(self, repo: Path, treeish: str) -> None:
        self.repo = repo
        what = treeish if treeish == "FETCH_HEAD" else f"{treeish} in {repo}"
        commit = _git(
            repo, "rev-parse", "--verify", "--end-of-options", f"{treeish}^{{commit}}", what=what
        )
        self.commit = commit.strip()

    def read(self, path: str) -> str:
        return _git(self.repo, "show", f"{self.commit}:{path}", what=path)


@contextmanager
def upstream(ref: str) -> Iterator[Source]:
    """Fetch `ref` from GitHub into a temporary repository and yield its files.

    A shallow, blob-less fetch needs no API or token. Each `read` then fetches its blob on
    demand. A short commit hash can't be fetched; a branch, tag or full hash can.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q")
        _git(repo, "fetch", "-q", "--depth", "1", "--filter=blob:none", REPO_URL, ref, what=ref)
        yield Source(repo, "FETCH_HEAD")


def _git(repo: Path, *args: str, what: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise Unreadable(what or str(repo), "git isn't installed or isn't on PATH") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git {args[0]} exited with {result.returncode}"
        raise Unreadable(what or str(repo), detail)
    return result.stdout
