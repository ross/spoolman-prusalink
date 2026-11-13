"""The command line: arguments, output and exit codes. Git is faked; nothing is fetched."""

from contextlib import contextmanager

import pytest

from firmware import __main__ as cli
from firmware import compare, source
from firmware.compare import MISSING, PRESENT, Difference, Expected
from firmware.tests import fixtures


@pytest.fixture
def calls(monkeypatch):
    """Fake both sources over the fixture files, recording what was opened."""
    opened = []

    @contextmanager
    def upstream(ref):
        opened.append(("upstream", ref))
        yield fixtures.FakeSource()

    def checkout(directory, ref):
        opened.append(("checkout", directory, ref))
        return fixtures.FakeSource()

    monkeypatch.setattr(source, "upstream", upstream)
    monkeypatch.setattr(source, "Source", checkout)
    monkeypatch.setattr(compare, "compare", lambda firmware, copies: [])
    monkeypatch.setattr(compare, "EXPECTED", ())
    return opened


@pytest.mark.parametrize(
    "args",
    [
        ["a", "b"],
        ["-h"],
        ["--checkout"],
        ["--checkout", ".", "a", "b"],
        ["--checkout", ".", "--x"],
    ],
)
def test_usage_errors(args, calls, capsys):
    assert cli.main(args) == 2
    assert capsys.readouterr().err.endswith(f"{cli.USAGE}\n")
    assert calls == []


def test_checkout_must_be_a_directory(tmp_path, calls, capsys):
    missing = tmp_path / "nope"
    assert cli.main(["--checkout", str(missing)]) == 2
    assert capsys.readouterr().err == f"{missing} isn't a directory\n{cli.USAGE}\n"


def test_upstream_defaults_to_master(calls, capsys):
    assert cli.main([]) == 0
    assert calls == [("upstream", "master")]
    out = capsys.readouterr().out
    assert out.startswith(f"==> Checking {source.REPO_URL} at master (c0ffee)\n")
    assert "bridge PRINTER_MODELS: ok" in out


def test_upstream_ref(calls):
    assert cli.main(["v6.9.2"]) == 0
    assert calls == [("upstream", "v6.9.2")]


def test_checkout_resolves_a_relative_directory(tmp_path, calls, monkeypatch, capsys):
    (tmp_path / "fw").mkdir()
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--checkout", "fw"]) == 0
    assert calls == [("checkout", tmp_path / "fw", "HEAD")]
    assert f"==> Checking {tmp_path / 'fw'} at HEAD (c0ffee)" in capsys.readouterr().out
    assert cli.main(["--checkout", "fw", "v1"]) == 0
    assert calls[-1] == ("checkout", tmp_path / "fw", "v1")


def test_differences_exit_1(calls, monkeypatch, capsys):
    found = [Difference("simulator MODELS", "1.4.0", None, "max_tools", 1, 2)]
    monkeypatch.setattr(compare, "compare", lambda firmware, copies: found)
    assert cli.main([]) == 1
    assert "  1.4.0 max_tools: firmware 1, copy 2" in capsys.readouterr().out


def test_expected_differences_exit_0(calls, monkeypatch, capsys):
    found = [
        Difference("bridge PRESETS", "7.1.0", m, "material", PRESENT, MISSING)
        for m in ("PPS", "PPA")
    ]
    monkeypatch.setattr(compare, "compare", lambda firmware, copies: found)
    expected = (Expected("bridge PRESETS", None, None, "material", "a reason"),)
    monkeypatch.setattr(compare, "EXPECTED", expected)
    assert cli.main([]) == 0


def test_unreadable_exits_3(monkeypatch, capsys):
    @contextmanager
    def upstream(ref):
        raise source.Unreadable(ref, "couldn't fetch")
        yield

    monkeypatch.setattr(source, "upstream", upstream)
    assert cli.main(["nope"]) == 3
    assert capsys.readouterr().err == "can't read nope: couldn't fetch\n"


def test_a_file_that_cant_be_read_exits_3(calls, monkeypatch, capsys):
    files = {**fixtures.FILES, "ProjectOptions.cmake": ""}
    monkeypatch.setattr(source, "Source", lambda directory, ref: fixtures.FakeSource(files))
    assert cli.main(["--checkout", "."]) == 3
    assert "can't read ProjectOptions.cmake: no set_feature_for_printers call" in (
        capsys.readouterr().err
    )


def test_a_missing_git_exits_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cli.main([]) == 3
    assert "git isn't installed or isn't on PATH" in capsys.readouterr().err
