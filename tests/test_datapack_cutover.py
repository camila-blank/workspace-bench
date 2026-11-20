"""Plan 0097: the one-off cutover driver's ``verify`` cannot report IDENTICAL vacuously. This
file leaves the repo together with ``scripts/datapack_cutover.py`` in the cutover PR."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from wsbench import datapack

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "datapack_cutover.py"
FILES = {"evals/fam/items.json": '{"items": [1]}\n', "adapted/m1/fam.json": "[2]\n"}
CANARY = datapack.CANARY_PREFIX + "00000000-0000-4000-8000-000000000000"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="the cutover script is gone")


def _blob_id(path: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "hash-object", "--no-filters", str(path)], capture_output=True, text=True
        )
    except OSError:
        pytest.skip("git unavailable")
    return r.stdout.strip()


@pytest.fixture
def cut(tmp_path, monkeypatch):
    """The script loaded against a temp "repo" whose two bank files are packed and published
    to a local stand-in release; ``tracked()`` answers for the ref ``PARENT``."""
    monkeypatch.setenv("WSB_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("WSB_DATA_URL", (tmp_path / "remote").as_uri())
    monkeypatch.delenv("WSB_DATA_REPO", raising=False)
    monkeypatch.setattr(datapack, "BACKOFF", 0.0)
    repo = tmp_path / "repo"
    index = {"version": 1, "canary": CANARY, "repo": "owner/bench"}
    for rel, text in {**FILES, "data/INDEX.json": json.dumps(index), "data/README.md": "#"}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    (repo / "evals/fam/README.md").write_text("# keeper\n")
    datapack.pack(add=["evals/**", "adapted/**"], root=repo)
    for side in datapack.load_sidecars(repo).values():
        dst = tmp_path / "remote/owner/bench/releases/download" / side["release"]
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(datapack.cache_path(side["zip_sha256"]), dst)
    spec = importlib.util.spec_from_file_location("datapack_cutover_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    want = {rel: _blob_id(repo / rel) for rel in FILES}
    real_git = mod._git

    def fake_git(args, stdin=None):
        if args[0] == "ls-tree":  # the ref's file listing (three files: two banks, one keeper)
            return "".join(f"{p}\n" for p in [*want, "evals/fam/README.md"]).encode()
        return real_git(args, stdin)  # hash-object: read-only, any directory

    monkeypatch.setattr(mod, "REPO", repo)
    monkeypatch.setattr(mod, "_git", fake_git)
    monkeypatch.setattr(mod, "tracked", lambda ref=None: dict(want) if ref == "PARENT" else {})
    mod.want, mod.tmp = want, tmp_path
    return mod


def test_verify_from_the_cache_and_from_the_release(cut, capsys, monkeypatch):
    assert cut.verify(cut.tmp / "scratch", "PARENT", unpack=True) == 0
    out = capsys.readouterr().out
    assert "expected 2; found 2; equal 2; missing 0; extra 0; differing 0" in out
    assert out.strip().endswith("IDENTICAL")
    # --from-release: an empty cache of its own and no token in the environment
    seen: dict[str, object] = {}
    real = datapack.unpack

    def spy(*a, **k):
        seen["cache"] = os.environ["WSB_CACHE"]
        seen["cached_before"] = list(datapack.cache_dir().glob("*.zip"))
        seen["tokens"] = [k for k in datapack.TOKEN_ENVS if k in os.environ]
        return real(*a, **k)

    monkeypatch.setattr(datapack, "unpack", spy)
    for key in datapack.TOKEN_ENVS:
        monkeypatch.setenv(key, "secret")
    home = os.environ["WSB_CACHE"]
    assert cut.verify(cut.tmp / "fresh", "PARENT", unpack=True, from_release=True) == 0
    assert seen["cache"] != home and seen["cached_before"] == [] and seen["tokens"] == []
    assert os.environ["WSB_CACHE"] == home and os.environ["GH_TOKEN"] == "secret"  # restored
    out = capsys.readouterr().out
    assert "downloaded 2 zip(s) for 2 unit(s)" in out and "anonymous download" in out
    assert not Path(str(seen["cache"])).exists()


def test_verify_refuses_a_directory_that_already_holds_the_files(cut, capsys):
    """Otherwise unpack writes nothing, every file "matches", and nothing was proved."""
    scratch = cut.tmp / "scratch"
    for rel, text in FILES.items():
        (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
        (scratch / rel).write_text(text)
    with pytest.raises(SystemExit, match="already holds 2 packable file"):
        cut.verify(scratch, "PARENT", unpack=True)
    assert "IDENTICAL" not in capsys.readouterr().out


def test_verify_requires_every_expected_file_to_be_written_by_the_unpack(cut, monkeypatch, capsys):
    monkeypatch.setattr(datapack, "unpack", lambda **k: {"units": 0, "written": 0, "deleted": 0})
    with pytest.raises(SystemExit, match=r"unpack wrote 0 file.*2 expected"):
        cut.verify(cut.tmp / "scratch", "PARENT", unpack=True)
    assert "IDENTICAL" not in capsys.readouterr().out


def test_verify_refuses_a_ref_without_packable_files(cut):
    with pytest.raises(SystemExit, match="no packable file"):
        cut.verify(cut.tmp / "scratch", "AFTER-THE-CUTOVER", unpack=True)
    with pytest.raises(SystemExit, match="no packable file"):
        cut.verify(cut.tmp / "repo", "AFTER-THE-CUTOVER", unpack=False)


def test_no_unpack_says_that_no_zip_was_read(cut, capsys):
    assert cut.verify(cut.tmp / "repo", "PARENT", unpack=False) == 0
    out = capsys.readouterr().out
    assert "NO ZIP WAS READ" in out.splitlines()[0]
    assert out.strip().endswith("IDENTICAL TO THE REF, NO ZIP WAS READ")
    assert "\nIDENTICAL\n" not in out


def test_verify_reports_a_differing_and_an_extra_file(cut, capsys):
    scratch = cut.tmp / "scratch"
    assert cut.verify(scratch, "PARENT", unpack=True) == 0
    (scratch / "evals/fam/items.json").write_text("changed\n")
    (scratch / "adapted/m1/stray.json").write_text("{}\n")
    capsys.readouterr()
    assert cut.verify(scratch, "PARENT", unpack=False) == 1
    out = capsys.readouterr().out
    assert "expected 2; found 3; equal 1; missing 0; extra 1; differing 1" in out
    assert out.strip().endswith("NOT IDENTICAL")
