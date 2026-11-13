"""Reading firmware files from git, against a local repository instead of GitHub."""

import subprocess

import pytest

from firmware import source
from firmware.source import Source, Unreadable


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository with one file over two commits, tagged `v1` and `v2`."""
    # Keep the user's git config (signing, tag settings) out of it.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "fw"
    path.mkdir()
    git(path, "init", "-q", "-b", "master")
    for version in ("1", "2"):
        (path / "file.txt").write_text(f"version {version}\n")
        git(path, "add", "file.txt")
        git(
            path,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            version,
        )
        git(path, "tag", f"v{version}")
    return path


def test_checkout_reads_a_ref(repo):
    old = Source(repo, "v1")
    assert old.commit == git(repo, "rev-parse", "v1")
    assert old.read("file.txt") == "version 1\n"
    assert Source(repo, "HEAD").read("file.txt") == "version 2\n"


def test_checkout_fails_closed(repo, tmp_path):
    with pytest.raises(Unreadable, match="can't read nope in "):
        Source(repo, "nope")
    with pytest.raises(Unreadable, match="can't read missing.txt: "):
        Source(repo, "HEAD").read("missing.txt")
    with pytest.raises(Unreadable, match="not a git repository"):
        Source(tmp_path, "HEAD")


def test_upstream_fetches_a_ref(repo, monkeypatch):
    monkeypatch.setattr(source, "REPO_URL", repo.as_uri())
    with source.upstream("v1") as fetched:
        assert fetched.commit == git(repo, "rev-parse", "v1")
        assert fetched.read("file.txt") == "version 1\n"
        tmp = fetched.repo
    assert not tmp.exists()


def test_upstream_fails_closed(repo, monkeypatch):
    monkeypatch.setattr(source, "REPO_URL", repo.as_uri())
    with pytest.raises(Unreadable, match="can't read nope: "), source.upstream("nope"):
        pass
