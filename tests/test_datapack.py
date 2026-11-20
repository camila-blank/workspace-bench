"""Plan 0097: pack / unpack of the bank zips on temp trees (a local directory stands in for the
release through a file:// base URL; no network), plus the real tree's packing rules."""

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from wsbench import cli, datapack
from wsbench.registry import REPO_ROOT

REPO = "owner/bench"
CANARY = datapack.CANARY_PREFIX + "00000000-0000-4000-8000-000000000000"
FILES = {
    "evals/fam/items.json": '{"items": [1, 2, 3]}\n',
    "evals/fam/sub/extra.json": '{"a": "b"}\n',
    "adapted/m1/fam.json": '{"items": ["x"]}\n',
    "adapted/m1/fam/overlay.json": '{"k": 1}\n',
    "adapted/m1/AUDIT.md": "# audit\n",
}
KEEPERS = {"evals/fam/README.md": "# fam\n", "adapted/README.md": "# adapted\n"}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _no_real_release(monkeypatch):
    """No test of this module can reach GitHub, whatever it does to the environment."""
    monkeypatch.setattr(datapack, "BASE_URL", "file:///nonexistent/wsbench-test-release")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A cache, an empty stand-in release directory and instant retries."""
    monkeypatch.setenv("WSB_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("WSB_DATA_URL", (tmp_path / "remote").as_uri())
    monkeypatch.delenv("WSB_DATA_REPO", raising=False)
    monkeypatch.delenv("WSB_NO_UNPACK", raising=False)
    monkeypatch.setattr(datapack, "BACKOFF", 0.0)
    return tmp_path


def _new_root(path: Path) -> Path:
    _write(path, {"data/INDEX.json": json.dumps({"version": 1, "canary": CANARY, "repo": REPO})})
    _write(path, {"data/README.md": "# data\n"})
    return path


@pytest.fixture
def tree(env):
    """A checkout with five packed files in four units; nothing is published yet."""
    root = _new_root(env / "repo")
    _write(root, FILES | KEEPERS)
    datapack.pack(add=["evals/**", "adapted/**"], root=root)
    return root


def _publish_local(root: Path, env: Path, repo: str = REPO) -> None:
    """Copy every sidecar's zip from the cache to the stand-in release."""
    for side in datapack.load_sidecars(root).values():
        dst = env / "remote" / repo / "releases" / "download" / side["release"]
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(datapack.cache_path(side["zip_sha256"]), dst / f"{side['zip_sha256']}.zip")


def _clone(root: Path, dst: Path) -> Path:
    """What a fresh clone holds after the cutover: data/ and the Markdown keepers only."""
    shutil.copytree(root / "data", dst / "data", ignore=shutil.ignore_patterns(".unpacked.json"))
    _write(dst, KEEPERS)
    return dst


def _empty_cache(env: Path) -> None:
    shutil.rmtree(env / "cache")


# ------------------------------------------------------------------------------ layout
def test_units_and_sidecars(tree):
    sides = datapack.load_sidecars(tree)
    assert set(sides) == {
        "evals/fam",
        "adapted/m1/fam.json",
        "adapted/m1/fam",
        "adapted/m1/AUDIT.md",
    }
    assert (tree / "data/adapted/m1/fam.json.json").is_file()  # R1: the full name is kept
    assert (tree / "data/adapted/m1/fam.json").is_file()
    side = sides["evals/fam"]
    assert set(side) == {"unit", "release", "zip_sha256", "zip_bytes", "files"}
    assert side["release"] == "data-0001"
    assert side["files"] == {
        "evals/fam/items.json": _sha(FILES["evals/fam/items.json"]),
        "evals/fam/sub/extra.json": _sha(FILES["evals/fam/sub/extra.json"]),
    }  # the Markdown keeper is not packed
    zip_path = datapack.cache_path(side["zip_sha256"])
    assert zip_path.stat().st_size == side["zip_bytes"]
    assert datapack.file_sha(zip_path) == side["zip_sha256"]
    text = (tree / "data/evals/fam.json").read_text()
    assert text.count("\n") >= 2 + len(side["files"])  # one file per line of the map
    assert datapack.listed_paths(tree) == sorted(FILES)
    assert datapack.unit_of("adapted/m/x/y/z.json") == "adapted/m/x"
    with pytest.raises(datapack.DataError):
        datapack.unit_of("adapted/top.json")


def test_zip_is_encrypted_and_carries_the_canary(tree):
    import zipfile

    import pyzipper

    side = datapack.load_sidecars(tree)["evals/fam"]
    path = datapack.cache_path(side["zip_sha256"])
    assert b'"items"' not in path.read_bytes()
    with zipfile.ZipFile(path) as z, pytest.raises((RuntimeError, NotImplementedError)):
        z.read("evals/fam/items.json")  # no password, and not a legacy-crypto zip
    with pyzipper.AESZipFile(path) as z:
        z.setpassword(datapack.PASSWORD)
        assert z.read(datapack.CANARY_FILE).decode().strip() == CANARY
        assert z.read("evals/fam/items.json").decode() == FILES["evals/fam/items.json"]
        assert all(i.flag_bits & 0x1 for i in z.infolist())


def test_never_packed(tree):
    assert datapack.never_packed("evals/fam/README.md")
    assert datapack.never_packed("adapted/README.md")
    assert datapack.never_packed("adapted/m1/.report.lock")
    assert not datapack.never_packed("adapted/m1/AUDIT.md")
    with pytest.raises(datapack.DataError, match="never packed"):
        datapack.pack(add=["evals/fam/README.md"], root=tree)


# ---------------------------------------------------------------------------- round trip
def test_round_trip_from_the_release(tree, env):
    _publish_local(tree, env)
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    assert datapack.unpack(root=clone) == {"units": 4, "written": 5, "deleted": 0}
    for rel, text in FILES.items():
        assert (clone / rel).read_bytes() == text.encode()
    assert datapack.load_stamp(clone)["sidecars_sha256"] == datapack.sidecars_digest(clone)
    assert not datapack.dirty(datapack.status(root=clone))
    assert len(list((env / "cache" / "data").glob("*.zip"))) == 4  # downloaded into the cache


def test_fork_repo_is_tried_second(tree, env, monkeypatch):
    _publish_local(tree, env, repo="fork/bench")
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    with pytest.raises(datapack.DataError, match="not found"):
        datapack.unpack(root=clone)
    monkeypatch.setenv("WSB_DATA_REPO", "fork/bench")
    assert datapack.unpack(root=clone)["written"] == 5


def test_missing_asset_error_names_unit_url_and_ways_out(tree, env):
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    with pytest.raises(datapack.DataError) as e:
        datapack.unpack(units=["evals/fam"], root=clone)
    msg = str(e.value)
    sha = datapack.load_sidecars(tree)["evals/fam"]["zip_sha256"]
    assert "evals/fam" in msg and f"/{REPO}/releases/download/data-0001/{sha}.zip" in msg
    assert "WSB_NO_UNPACK=1" in msg and str(datapack.cache_path(sha)) in msg
    assert not (clone / "evals/fam/items.json").exists()


def test_wrong_sha_and_corrupt_zip(tree, env):
    _publish_local(tree, env)
    side = datapack.load_sidecars(tree)["evals/fam"]
    remote = env / "remote" / REPO / "releases/download/data-0001" / f"{side['zip_sha256']}.zip"
    remote.write_bytes(remote.read_bytes()[:-7] + b"garbage")
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    with pytest.raises(datapack.DataError, match="the sidecar pins"):
        datapack.unpack(units=["evals/fam"], root=clone)
    assert not datapack.cache_path(side["zip_sha256"]).exists()  # never cached
    # a corrupt cache file is dropped and fetched again
    remote.write_bytes(b"")
    with pytest.raises(datapack.DataError):
        datapack.unpack(units=["evals/fam"], root=clone)
    # a zip whose sha matches a doctored sidecar but is no zip at all
    junk = b"not a zip"
    doctored = {**side, "zip_sha256": hashlib.sha256(junk).hexdigest(), "zip_bytes": len(junk)}
    (clone / "data/evals/fam.json").write_text(json.dumps(doctored))
    datapack.cache_dir().mkdir(parents=True, exist_ok=True)
    datapack.cache_path(doctored["zip_sha256"]).write_bytes(junk)
    with pytest.raises(datapack.DataError, match="cannot read"):
        datapack.unpack(units=["evals/fam"], root=clone)


# ------------------------------------------------------------------- the three-way rule
def _edit_and_pack(tree: Path, rel: str, text: str) -> None:
    (tree / rel).write_text(text, encoding="utf-8")
    assert datapack.pack(root=tree) == [datapack.unit_of(rel)]


def test_three_way_update_noop_conflict_force(tree, env):
    clone = _clone(tree, env / "clone")
    datapack.unpack(root=clone)
    rel = "evals/fam/items.json"
    _edit_and_pack(tree, rel, '{"items": [9]}\n')
    shutil.copy2(tree / "data/evals/fam.json", clone / "data/evals/fam.json")
    # (a) unedited since the last unpack: silently updated
    assert datapack.unpack(root=clone) == {"units": 1, "written": 1, "deleted": 0}
    assert (clone / rel).read_text() == '{"items": [9]}\n'
    # (b) already equal to the zip: nothing to do, and no zip is needed
    _empty_cache(env)
    (clone / "data/.unpacked.json").unlink()
    assert datapack.unpack(root=clone) == {"units": 4, "written": 0, "deleted": 0}
    # (c) a local edit and a changed sidecar: conflict, nothing of the unit is written
    shutil.copytree(tree / "evals", env / "keep")
    _edit_and_pack(tree, rel, '{"items": [10]}\n')
    _edit_and_pack(tree, "evals/fam/sub/extra.json", '{"a": "c"}\n')
    shutil.copy2(tree / "data/evals/fam.json", clone / "data/evals/fam.json")
    (clone / rel).write_text("LOCAL EDIT\n")
    with pytest.raises(datapack.DataError) as e:
        datapack.unpack(root=clone)
    assert rel in str(e.value) and "force=True" in str(e.value) and "wsbench pack" in str(e.value)
    assert (clone / rel).read_text() == "LOCAL EDIT\n"
    assert (clone / "evals/fam/sub/extra.json").read_text() == FILES["evals/fam/sub/extra.json"]
    assert datapack.load_stamp(clone)["sidecars_sha256"] is None
    # (d) force overwrites
    datapack.unpack(force=True, root=clone)
    assert (clone / rel).read_text() == '{"items": [10]}\n'
    assert (clone / "evals/fam/sub/extra.json").read_text() == '{"a": "c"}\n'
    assert datapack.load_stamp(clone)["sidecars_sha256"] == datapack.sidecars_digest(clone)


def test_first_unpack_adopts_equal_files_and_flags_differing_ones(tree, env):
    """A pre-cutover checkout: plaintext present, no stamp."""
    clone = _clone(tree, env / "clone")
    _write(clone, FILES)
    _empty_cache(env)  # equal files need no zip at all
    assert datapack.unpack(root=clone) == {"units": 4, "written": 0, "deleted": 0}
    assert set(datapack.load_stamp(clone)["units"]) == set(datapack.load_sidecars(clone))
    (clone / "data/.unpacked.json").unlink()
    (clone / "adapted/m1/fam.json").write_text("different\n")
    with pytest.raises(datapack.DataError, match=r"adapted/m1/fam\.json"):
        datapack.unpack(root=clone)
    assert (clone / "adapted/m1/fam.json").read_text() == "different\n"


def test_stale_file_is_deleted_unless_edited(tree, env):
    clone = _clone(tree, env / "clone")
    datapack.unpack(root=clone)
    datapack.pack(rm=["evals/fam/sub/extra.json"], root=tree)
    shutil.copy2(tree / "data/evals/fam.json", clone / "data/evals/fam.json")
    datapack.unpack(root=clone)
    assert not (clone / "evals/fam/sub").exists()  # the file and its empty directory
    assert (clone / "evals/fam/items.json").is_file()
    # a whole unit that left the sidecars; its file was edited locally
    clone2 = _clone(tree, env / "clone2")
    datapack.unpack(root=clone2)
    datapack.pack(rm=["adapted/m1/AUDIT.md"], root=tree)
    assert not (tree / "data/adapted/m1/AUDIT.md.json").exists()
    assert (tree / "adapted/m1/AUDIT.md").is_file()  # rm drops the listing, not the file
    (clone2 / "data/adapted/m1/AUDIT.md.json").unlink()
    (clone2 / "adapted/m1/AUDIT.md").write_text("# my notes\n")
    with pytest.raises(datapack.DataError, match=r"adapted/m1/AUDIT\.md"):
        datapack.unpack(root=clone2)
    assert (clone2 / "adapted/m1/AUDIT.md").read_text() == "# my notes\n"
    datapack.unpack(force=True, root=clone2)
    assert not (clone2 / "adapted/m1/AUDIT.md").exists()
    assert "adapted/m1/AUDIT.md" not in datapack.load_stamp(clone2)["units"]


def test_unpublished_zip_is_never_overwritten(tree, env):
    """R5: a packed-but-unpublished change exists only in one cache."""
    rel = "evals/fam/items.json"
    mine = '{"items": ["packed here, never published"]}\n'
    _edit_and_pack(tree, rel, mine)
    packed_side = (tree / "data/evals/fam.json").read_bytes()
    packed_sha = datapack.load_sidecars(tree)["evals/fam"]["zip_sha256"]
    other = '{"items": ["main moved on"]}\n'
    _edit_and_pack(tree, rel, other)
    theirs = (tree / "data/evals/fam.json").read_bytes()
    # back to the state "I packed mine, then pulled their sidecar", with my zip lost
    (tree / rel).write_text(mine)
    (tree / "data/evals/fam.json").write_bytes(packed_side)
    datapack.unpack(root=tree)  # re-stamps the unit on my zip
    assert datapack.load_stamp(tree)["units"]["evals/fam"]["zip_sha256"] == packed_sha
    (tree / "data/evals/fam.json").write_bytes(theirs)
    keep = datapack.cache_path(packed_sha).read_bytes()
    datapack.cache_path(packed_sha).unlink()
    with pytest.raises(datapack.DataError, match="wsbench pack publish"):
        datapack.unpack(root=tree)
    assert (tree / rel).read_text() == mine
    # once the zip is on the release (a HEAD finds it) the update goes through
    dst = env / "remote" / REPO / "releases/download/data-0001"
    dst.mkdir(parents=True)
    (dst / f"{packed_sha}.zip").write_bytes(keep)
    datapack.unpack(root=tree)
    assert (tree / rel).read_text() == other


def test_unpack_restores_a_deleted_file_and_takes_unit_prefixes(tree):
    (tree / "adapted/m1/fam.json").unlink()
    assert datapack.unpack(units=["evals"], root=tree)["written"] == 0
    assert datapack.unpack(units=["adapted/m1"], root=tree)["written"] == 1
    assert (tree / "adapted/m1/fam.json").read_text() == FILES["adapted/m1/fam.json"]
    with pytest.raises(datapack.DataError, match="no such unit"):
        datapack.unpack(units=["adapted/nope"], root=tree)


# ------------------------------------------------------------------------------- pack
def test_pack_refuses_a_tree_that_is_not_fully_unpacked(tree, env):
    clone = _clone(tree, env / "clone")
    with pytest.raises(datapack.DataError, match="run `wsbench unpack` first"):
        datapack.pack(root=clone)
    datapack.unpack(units=["evals"], root=clone)
    with pytest.raises(datapack.DataError, match="run `wsbench unpack` first"):
        datapack.pack(root=clone)  # partly unpacked is not enough
    datapack.unpack(root=clone)
    assert datapack.pack(root=clone) == []


def test_pack_is_a_noop_on_a_clean_tree_and_repacks_only_changed_units(tree):
    before = {u: s["zip_sha256"] for u, s in datapack.load_sidecars(tree).items()}
    assert datapack.pack(root=tree) == []
    _edit_and_pack(tree, "adapted/m1/fam/overlay.json", '{"k": 2}\n')
    after = {u: s["zip_sha256"] for u, s in datapack.load_sidecars(tree).items()}
    assert {u for u in before if before[u] != after[u]} == {"adapted/m1/fam"}
    datapack.ensure(root=tree)  # the stamp follows a pack: nothing to unpack
    assert datapack.load_stamp(tree)["sidecars_sha256"] == datapack.sidecars_digest(tree)


def test_add_and_rm(tree):
    _write(tree, {"adapted/m1/new.json": "[]\n", "adapted/m2/fam.json": "[1]\n"})
    _write(tree, {"evals/fam/work/cache.bin": "x", "evals/fam/NOTES.md": "n\n"})
    report = datapack.status(root=tree)
    assert report["units"] == {
        "evals/fam": {"modified": [], "missing": [], "unlisted": ["evals/fam/work/cache.bin"]}
    }
    assert report["unlisted_entries"] == ["adapted/m1/new.json", "adapted/m2/fam.json"]
    assert not datapack.dirty(report)  # unlisted files are reported, never fatal
    packed = datapack.pack(add=["adapted/m1/new.json", "adapted/m2"], root=tree)
    assert packed == ["adapted/m1/new.json", "adapted/m2/fam.json"]
    assert (tree / "data/adapted/m2/fam.json.json").is_file()
    assert datapack.status(root=tree)["unlisted_entries"] == []
    datapack.ensure(root=tree)  # R8: add stamps the files as packed
    assert datapack.load_stamp(tree)["sidecars_sha256"] == datapack.sidecars_digest(tree)
    assert datapack.pack(add=["adapted/m2"], root=tree) == []  # already listed
    assert datapack.pack(rm=["adapted/m2/*"], root=tree) == ["adapted/m2/fam.json"]
    assert "adapted/m2/fam.json" not in datapack.load_sidecars(tree)
    with pytest.raises(datapack.DataError, match="no listed file"):
        datapack.pack(rm=["adapted/m2/fam.json"], root=tree)
    with pytest.raises(datapack.DataError, match="matches no file"):
        datapack.pack(add=["adapted/m9/*.json"], root=tree)
    with pytest.raises(datapack.DataError, match="not packable"):
        _write(tree, {"adapted/stray.json": "1"})
        datapack.pack(add=["adapted/stray.json"], root=tree)


def test_listed_file_missing_is_an_error_not_a_removal(tree):
    before = (tree / "data/evals/fam.json").read_bytes()
    (tree / "evals/fam/sub/extra.json").unlink()
    with pytest.raises(datapack.DataError, match=r"evals/fam/sub/extra\.json"):
        datapack.pack(root=tree)
    assert (tree / "data/evals/fam.json").read_bytes() == before


def test_status_exit_codes(tree, monkeypatch, capsys):
    monkeypatch.setattr(datapack, "ROOT", tree)
    assert cli.main(["pack", "status"]) == 0
    assert capsys.readouterr().out == ""
    _write(tree, {"evals/fam/untracked.json": "1"})
    assert cli.main(["pack", "status"]) == 0
    assert capsys.readouterr().out.strip() == "? evals/fam/untracked.json"
    (tree / "evals/fam/items.json").write_text("edited\n")
    (tree / "adapted/m1/fam.json").unlink()
    assert cli.main(["pack", "status"]) == 1
    out = capsys.readouterr().out
    assert "M evals/fam/items.json" in out and "! adapted/m1/fam.json" in out
    assert cli.main(["pack", "status", "units=adapted/m1/AUDIT.md"]) == 0


def test_cli_commands_and_error_exit(tree, env, monkeypatch, capsys):
    monkeypatch.setattr(datapack, "ROOT", tree)
    _write(tree, {"adapted/m3/a.json": "1", "adapted/m3/b.json": "2"})
    assert cli.main(["pack", "add=adapted/m3/a.json", "add=adapted/m3/b.json"]) == 0
    out, err = capsys.readouterr()
    assert out.split() == ["adapted/m3/a.json", "adapted/m3/b.json"]
    assert "packed 2 unit(s)" in err  # progress on stderr only
    (tree / "adapted/m3/a.json").unlink()
    assert cli.main(["unpack"]) == 0
    assert (tree / "adapted/m3/a.json").read_text() == "1"
    assert cli.main(["pack", "rm=adapted/m3"]) == 0
    capsys.readouterr()
    assert cli.main(["pack", "rm=adapted/m3"]) == 2
    assert "no listed file" in capsys.readouterr().err
    assert cli.main(["unpack", "units=nope"]) == 2
    assert cli.main(["pack", "status", "--show"]) == 0
    assert "pack <status|diff|publish>" in cli._usage()


def test_deferred_import_error_is_exit_2_except_for_the_data_commands(tree, monkeypatch, capsys):
    monkeypatch.setattr(datapack, "ROOT", tree)
    monkeypatch.setattr(datapack, "DEFERRED", datapack.DataError("conflict: evals/fam"))
    assert cli.main(["list"]) == 2
    assert "conflict: evals/fam" in capsys.readouterr().err
    assert cli.main(["pack", "status"]) == 0


def test_ensure_at_import_defers_only_in_the_cli(tree, env, monkeypatch):
    clone = _clone(tree, env / "clone")
    _write(clone, {"evals/fam/items.json": "local\n"})
    monkeypatch.setattr(datapack, "ROOT", clone)
    monkeypatch.setattr(datapack, "DEFERRED", None)
    monkeypatch.setattr(sys, "argv", ["/venv/bin/pytest"])
    with pytest.raises(datapack.DataError):
        datapack.ensure_at_import()
    monkeypatch.setattr(sys, "argv", ["/venv/bin/wsbench", "unpack", "force=True"])
    datapack.ensure_at_import()
    assert datapack.DEFERRED is None  # the data commands skip the automatic unpack
    monkeypatch.setattr(sys, "argv", ["/venv/bin/wsbench", "list"])
    datapack.ensure_at_import()
    assert "evals/fam/items.json" in str(datapack.DEFERRED)


# ------------------------------------------------------------------------------- diff
def _fake_git(snapshot: dict[str, bytes]):
    """``datapack.git`` over a frozen copy of ``data/`` (what ``ref`` committed)."""
    names = sorted(snapshot)

    def git(root, args, stdin=None):
        if args[0] == "ls-tree":
            return "".join(f"100644 blob {i:040x}\t{n}\0" for i, n in enumerate(names)).encode()
        assert args[:2] == ["cat-file", "blob"]
        return snapshot[names[int(args[2], 16)]]

    return git


def _snapshot(root: Path) -> dict[str, bytes]:
    data = root / "data"
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in [*data.glob("evals/*.json"), *data.glob("adapted/*/*.json")]
    }


def test_diff(tree, env, monkeypatch):
    monkeypatch.setattr(datapack, "git", _fake_git(_snapshot(tree)))
    assert datapack.diff(root=tree) == ([], "")
    _write(tree, {"adapted/m1/new.json": "[]\n"})
    datapack.pack(add=["adapted/m1/new.json"], rm=["adapted/m1/AUDIT.md"], root=tree)
    (tree / "evals/fam/items.json").write_text('{"items": [1, 2, 4]}\n')  # not packed yet
    (tree / "adapted/m1/fam.json").unlink()
    rows, text = datapack.diff(ref="HEAD", root=tree)
    assert rows == [
        ("D", "adapted/m1/AUDIT.md"),
        ("!", "adapted/m1/fam.json"),
        ("A", "adapted/m1/new.json"),
        ("M", "evals/fam/items.json"),
    ]
    assert text == ""
    assert datapack.diff(units=["evals"], root=tree)[0] == [("M", "evals/fam/items.json")]
    # text=True needs ref's zip: from the cache, else from the release
    old = json.loads(_snapshot(tree)["data/evals/fam.json"])["zip_sha256"]
    keep = datapack.cache_path(old).read_bytes()
    datapack.cache_path(old).unlink()
    with pytest.raises(datapack.DataError, match="cannot get its zip"):
        datapack.diff(text=True, root=tree)
    dst = env / "remote" / REPO / "releases/download/data-0001"
    dst.mkdir(parents=True)
    (dst / f"{old}.zip").write_bytes(keep)
    assert "+++ b/evals/fam/items.json" in datapack.diff(text=True, root=tree)[1]


def test_diff_text(tree, monkeypatch, capsys):
    monkeypatch.setattr(datapack, "git", _fake_git(_snapshot(tree)))
    monkeypatch.setattr(datapack, "ROOT", tree)
    (tree / "evals/fam/items.json").write_text('{"items": [1, 2, 4]}\n')
    assert cli.main(["pack", "diff", "text=True"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "M evals/fam/items.json"
    assert "--- a/evals/fam/items.json" in out and "+++ b/evals/fam/items.json" in out
    assert '-{"items": [1, 2, 3]}' in out and '+{"items": [1, 2, 4]}' in out


# ---------------------------------------------------------------------------- publish
class FakeGitHub:
    """An in-memory ``gh`` (REST shapes); uploaded assets land in the stand-in release
    directory. Knobs model the failures ``publish`` must survive."""

    def __init__(self, env: Path) -> None:
        self.remote = env / "remote"
        self.repos: dict[str, dict[str, dict]] = {}  # repo -> tag -> release (with "assets")
        self.refs: dict[str, dict[str, str]] = {}  # repo -> ref -> commit sha
        self.objects: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.page = 100  # assets per listing page
        self.draft_uploads = False  # uploads never become anonymously readable
        self.fail_upload_after: int | None = None  # one upload call dies after N files
        self.upload_failures = 1
        self._ids = iter(range(1000, 10**6))

    # -- helpers the tests use to stage a state
    def add_release(self, repo: str, tag: str, **fields) -> dict:
        rel = {"tag_name": tag, "id": next(self._ids), "draft": False, "assets": {}, **fields}
        self.repos.setdefault(repo, {})[tag] = rel
        return rel

    def add_asset(self, repo: str, tag: str, name: str, size: int, state: str = "uploaded"):
        self.repos[repo][tag]["assets"][name] = {"name": name, "size": size, "state": state}

    def orphan_tag(self, repo: str, tag: str, paths: list[str]) -> None:
        tree = self._put({"tree": [{"path": p, "type": "blob", "sha": "b" * 40} for p in paths]})
        commit = self._put({"message": "earlier run", "tree": tree, "parents": []})
        self.refs.setdefault(repo, {})[f"refs/tags/{tag}"] = commit

    def uploads(self) -> list[str]:
        return [Path(p).name for c in self.calls if c[:2] == ["release", "upload"] for p in c[3:-3]]

    def _put(self, body: dict) -> str:
        sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
        self.objects[sha] = body
        return sha

    def _api(self, args: list[str], stdin: str | None) -> tuple[int, str, str]:
        paginate = "--paginate" in args
        path = next(a for a in args[1:] if a.startswith("repos/"))
        repo, rest = "/".join(path.split("/")[1:3]), "/".join(path.split("/")[3:])
        rels = self.repos.setdefault(repo, {})
        refs = self.refs.setdefault(repo, {})
        if "--method" in args:  # POST git/blobs|trees|commits|refs
            body = json.loads(stdin or "{}")
            if rest == "git/refs":
                if body["ref"] in refs:
                    return 1, '{"message": "Reference already exists"}', "gh: HTTP 422"
                refs[body["ref"]] = body["sha"]
                return 0, json.dumps({"ref": body["ref"]}), ""
            return 0, json.dumps({"sha": self._put(body)}), ""
        if rest.startswith("releases?"):
            rows = [{k: v for k, v in r.items() if k != "assets"} for r in rels.values()]
            rows.append({"tag_name": "v1.2.0", "id": 1, "draft": False})
            return 0, json.dumps(rows), ""
        if rest.startswith("releases/tags/"):
            tag = rest.rsplit("/", 1)[1]
            if tag not in rels:
                return 1, "", "gh: Not Found (HTTP 404)"
            return 0, json.dumps({k: v for k, v in rels[tag].items() if k != "assets"}), ""
        if rest.startswith("releases/") and "/assets" in rest:
            rid = int(rest.split("/")[1])
            rows = list(next(r for r in rels.values() if r["id"] == rid)["assets"].values())
            pages = [rows[i : i + self.page] for i in range(0, len(rows), self.page)] or [[]]
            return 0, "".join(json.dumps(p) for p in (pages if paginate else pages[:1])), ""
        if rest.startswith("git/ref/tags/"):
            ref = "refs/tags/" + rest.rsplit("/", 1)[1]
            if ref not in refs:
                return 1, "", "gh: Not Found (HTTP 404)"
            return 0, json.dumps({"object": {"type": "commit", "sha": refs[ref]}}), ""
        if rest.startswith("git/commits/"):
            return (
                0,
                json.dumps({"tree": {"sha": self.objects[rest.rsplit("/", 1)[1]]["tree"]}}),
                "",
            )
        if rest.startswith("git/trees/"):
            sha = rest.rsplit("/", 1)[1].split("?")[0]
            return 0, json.dumps({"tree": self.objects[sha]["tree"], "truncated": False}), ""
        raise AssertionError(f"unexpected gh api call: {args}")

    def __call__(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append(args)
        if args[0] == "api":
            return self._api(args, stdin)
        repo = args[args.index("--repo") + 1]
        self.repos.setdefault(repo, {})
        tag = args[2]
        if args[:2] == ["release", "create"]:
            assert "--verify-tag" in args and "--latest=false" in args and "--draft" not in args
            commit = self.refs[repo][f"refs/tags/{tag}"]
            assert args[args.index("--target") + 1] == commit
            self.add_release(repo, tag, notes=args[args.index("--notes") + 1])
            return 0, "", ""
        assert args[:2] == ["release", "upload"] and args[-1] == "--clobber", args
        for done, path in enumerate(args[3 : args.index("--repo")]):
            if self.fail_upload_after is not None and done == self.fail_upload_after:
                self.upload_failures -= 1
                if self.upload_failures <= 0:
                    self.fail_upload_after = None
                return 1, "", "HTTP 502: Server Error"
            name = Path(path).name
            self.add_asset(repo, tag, name, Path(path).stat().st_size)
            if not self.draft_uploads:
                dst = self.remote / repo / "releases" / "download" / tag
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst / name)
        return 0, "", ""


@pytest.fixture
def hub(env, monkeypatch):
    fake = FakeGitHub(env)
    monkeypatch.setattr(datapack, "gh", fake)
    return fake


def _spy(monkeypatch, name: str) -> list[str]:
    """Record the URLs one of the two network seams is asked for."""
    seen: list[str] = []
    real = getattr(datapack, name)

    def wrapped(url, *a, **k):
        seen.append(url)
        return real(url, *a, **k)

    monkeypatch.setattr(datapack, name, wrapped)
    return seen


def test_publish_creates_the_release_uploads_once_and_verifies(tree, env, hub, monkeypatch):
    done = datapack.publish(root=tree)
    assert len(done["uploaded"]) == 4 and done["rewritten"] == []
    assert set(hub.repos[REPO]) == {"data-0001"}
    # R2: an orphan commit holding only data/README.md, reachable from the tag alone
    commit = hub.objects[hub.refs[REPO]["refs/tags/data-0001"]]
    assert commit["parents"] == []
    assert [e["path"] for e in hub.objects[commit["tree"]]["tree"]] == ["data/README.md"]
    assert CANARY in hub.repos[REPO]["data-0001"]["notes"]
    assert not json.loads((env / "cache/data/publish-unverified.json").read_text())
    # a fresh clone now unpacks from the release
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    assert datapack.unpack(root=clone)["written"] == 5
    # the second run uploads nothing, HEADs EVERY sidecar's asset and downloads none
    hub.calls.clear()
    heads, gets = _spy(monkeypatch, "head"), _spy(monkeypatch, "fetch")
    assert datapack.publish(root=clone) == {"uploaded": [], "rewritten": []}
    assert hub.uploads() == [] and gets == []
    sides = datapack.load_sidecars(clone)
    assert sorted(heads) == sorted(
        datapack.asset_url(REPO, s["release"], s["zip_sha256"]) for s in sides.values()
    )
    # an edit uploads one zip, and only that one is downloaded for the sha check
    _edit_and_pack(clone, "evals/fam/items.json", "[2]\n")
    assert datapack.publish(root=clone)["uploaded"] == ["evals/fam"]
    new = datapack.load_sidecars(clone)["evals/fam"]["zip_sha256"]
    assert hub.uploads() == [f"{new}.zip"]
    assert gets == [datapack.asset_url(REPO, "data-0001", new)]


def test_publish_reuses_a_tag_left_without_its_release(tree, env, hub):
    """7a: an earlier run died between creating the ref and the release."""
    hub.orphan_tag(REPO, "data-0001", ["data/README.md"])
    assert len(datapack.publish(root=tree)["uploaded"]) == 4
    assert set(hub.repos[REPO]) == {"data-0001"}
    create = next(c for c in hub.calls if c[:2] == ["release", "create"])
    assert "--verify-tag" in create


def test_publish_refuses_a_foreign_tag_of_the_same_name(tree, env, hub):
    hub.orphan_tag(REPO, "data-0001", ["data/README.md", "evals/fam/items.json"])
    with pytest.raises(datapack.DataError, match="its tree is not the single file"):
        datapack.publish(root=tree)
    assert hub.uploads() == [] and not hub.repos.get(REPO)


def test_publish_reads_every_page_of_the_asset_listing(tree, env, hub):
    """7c: a listing cut at its first page would re-upload what the release already holds."""
    hub.page = 3
    datapack.publish(root=tree)
    hub.calls.clear()
    assert datapack.publish(root=tree)["uploaded"] == []
    assert hub.uploads() == []
    listing = next(c for c in hub.calls if "/assets?per_page=100" in " ".join(c))
    assert "--paginate" in listing


def test_publish_retries_a_failed_batch_and_a_rerun_converges(tree, env, hub, monkeypatch):
    """7d / 7f: a batch that dies midway is retried with --clobber; after a hard failure the
    next run uploads only what the listing says is missing."""
    hub.fail_upload_after = 2
    assert len(datapack.publish(root=tree)["uploaded"]) == 4
    assert len(hub.repos[REPO]["data-0001"]["assets"]) == 4
    assert sum(c[:2] == ["release", "upload"] for c in hub.calls) == 2
    # a hard failure: three tries, then a DataError; the release holds two of the next four
    for i in range(4):
        _write(tree, {f"adapted/m{i + 5}/fam.json": f"[{i}]\n"})
    datapack.pack(add=[f"adapted/m{i + 5}" for i in range(4)], root=tree)
    hub.fail_upload_after, hub.upload_failures = 2, datapack.TRIES
    hub.calls.clear()
    with pytest.raises(datapack.DataError, match="HTTP 502"):
        datapack.publish(root=tree)
    assert sum(c[:2] == ["release", "upload"] for c in hub.calls) == datapack.TRIES
    assert len(hub.repos[REPO]["data-0001"]["assets"]) == 6
    hub.calls.clear()
    gets = _spy(monkeypatch, "fetch")
    assert len(datapack.publish(root=tree)["uploaded"]) == 2
    assert len(hub.uploads()) == 2 and len(hub.repos[REPO]["data-0001"]["assets"]) == 8
    assert len(gets) == 4  # the two uploaded before the failure were never verified: now they are


def test_publish_refuses_a_draft_and_rolls_past_an_immutable_release(tree, env, hub, capsys):
    hub.add_release(REPO, "data-0001", draft=True)
    with pytest.raises(datapack.DataError, match="DRAFT"):
        datapack.publish(root=tree)
    # an immutable release takes no new asset: it counts as full, and publish moves on
    hub.repos[REPO]["data-0001"]["draft"] = False
    hub.repos[REPO]["data-0001"]["immutable"] = True
    done = datapack.publish(root=tree)
    assert len(done["uploaded"]) == 4 and len(done["rewritten"]) == 4
    assert hub.repos[REPO]["data-0001"]["assets"] == {}
    assert len(hub.repos[REPO]["data-0002"]["assets"]) == 4
    assert {s["release"] for s in datapack.load_sidecars(tree).values()} == {"data-0002"}
    assert "release data-0001 on owner/bench is immutable" in capsys.readouterr().err
    # what an immutable release already holds stays where it is
    hub.repos[REPO]["data-0002"]["immutable"] = True
    hub.calls.clear()
    assert datapack.publish(root=tree) == {"uploaded": [], "rewritten": []}
    _edit_and_pack(tree, "evals/fam/items.json", "[5]\n")
    assert datapack.publish(root=tree) == {"uploaded": ["evals/fam"], "rewritten": ["evals/fam"]}
    assert len(hub.repos[REPO]["data-0003"]["assets"]) == 1


def test_publish_never_takes_a_size_from_an_unverified_cache_file(tree, env, hub):
    datapack.publish(root=tree)
    side = datapack.load_sidecars(tree)["evals/fam"]
    datapack.cache_path(side["zip_sha256"]).write_bytes(b"a truncated download")
    hub.calls.clear()
    assert datapack.publish(root=tree) == {"uploaded": [], "rewritten": []}
    assert hub.uploads() == []


def test_the_error_for_an_asset_that_only_a_fork_holds_names_the_variable(tree, env, hub):
    datapack.publish(repo="fork/bench", root=tree)
    _empty_cache(env)
    clone = _clone(tree, env / "maintainer")  # nothing unpacked: the zips are on the fork only
    with pytest.raises(datapack.DataError, match="set WSB_DATA_REPO=<owner>/<name> to that fork"):
        datapack.publish(root=clone)
    with pytest.raises(datapack.DataError, match="set WSB_DATA_REPO=<owner>/<name> to that fork"):
        datapack.unpack(root=clone)


def test_publish_replaces_a_half_uploaded_asset_and_compares_the_cached_size(tree, env, hub):
    datapack.publish(root=tree)
    side = datapack.load_sidecars(tree)["evals/fam"]
    name = f"{side['zip_sha256']}.zip"
    # the sidecar's zip_bytes is not what decides: the cached zip's size is
    doctored = {**side, "zip_bytes": side["zip_bytes"] + 1}
    (tree / "data/evals/fam.json").write_text(json.dumps(doctored))
    hub.calls.clear()
    assert datapack.publish(root=tree)["uploaded"] == []
    (tree / "data/evals/fam.json").write_text(json.dumps(side))
    # an asset that never finished uploading is replaced
    hub.add_asset(REPO, "data-0001", name, side["zip_bytes"], state="starter")
    assert datapack.publish(root=tree)["uploaded"] == ["evals/fam"]
    assert hub.repos[REPO]["data-0001"]["assets"][name]["state"] == "uploaded"
    assert hub.calls[-1][-1] == "--clobber"


def test_publish_fails_on_an_unreadable_asset_and_checks_it_again_next_time(
    tree, env, hub, monkeypatch
):
    """7b: an upload the anonymous download cannot read fails the run and stays owed."""
    hub.draft_uploads = True
    with pytest.raises(datapack.DataError, match="anonymous download check failed"):
        datapack.publish(root=tree)
    owed = json.loads((env / "cache/data/publish-unverified.json").read_text())
    assert len(owed) == 4
    _publish_local(tree, env)  # the assets become readable
    hub.calls.clear()
    gets = _spy(monkeypatch, "fetch")
    assert datapack.publish(root=tree)["uploaded"] == []  # the listing says they are there
    assert len(gets) == 4  # ... and each is downloaded and sha-checked, since it failed before
    assert json.loads((env / "cache/data/publish-unverified.json").read_text()) == []
    gets.clear()
    datapack.publish(root=tree)
    assert gets == []


def test_publish_rolls_to_the_next_release_and_rewrites_the_sidecar(tree, env, hub, monkeypatch):
    monkeypatch.setattr(datapack, "RELEASE_CAP", 3)
    done = datapack.publish(root=tree)
    assert len(done["rewritten"]) == 1 and set(hub.repos[REPO]) == {"data-0001", "data-0002"}
    moved = datapack.load_sidecars(tree)[done["rewritten"][0]]
    assert moved["release"] == "data-0002"
    assert f"{moved['zip_sha256']}.zip" in hub.repos[REPO]["data-0002"]["assets"]
    datapack.ensure(root=tree)  # the stamp followed the rewrite
    assert datapack.load_stamp(tree)["sidecars_sha256"] == datapack.sidecars_digest(tree)
    # the next new unit is recorded on the newest release from the start
    _write(tree, {"adapted/m2/fam.json": "[1]\n"})
    datapack.pack(add=["adapted/m2/fam.json"], root=tree)
    assert datapack.load_sidecars(tree)["adapted/m2/fam.json"]["release"] == "data-0002"
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    with pytest.raises(datapack.DataError, match=r"adapted/m2/fam\.json"):
        datapack.unpack(root=clone)  # packed, never published: only the other units arrive
    assert (clone / "evals/fam/items.json").is_file()


def test_publish_rebuilds_a_zip_only_when_it_exists_nowhere(tree, env, hub, monkeypatch):
    """R4 + item 12: absent from the cache AND from every repo -> rebuilt (sidecar rewritten);
    a network error or a wrong sha on the release is an error, never a rewrite."""
    datapack.publish(root=tree)
    side = datapack.load_sidecars(tree)["evals/fam"]
    old = side["zip_sha256"]
    before = (tree / "data/evals/fam.json").read_bytes()
    remote = env / "remote" / REPO / "releases/download/data-0001" / f"{old}.zip"
    datapack.cache_path(old).unlink()
    del hub.repos[REPO]["data-0001"]["assets"][f"{old}.zip"]  # the listing lost it; the URL not

    def down(url, *a, **k):
        raise OSError("connection reset")

    with monkeypatch.context() as m:
        m.setattr(datapack, "_retrying", down)
        with pytest.raises(datapack.DataError, match="could not be asked"):
            datapack.publish(root=tree)
    assert (tree / "data/evals/fam.json").read_bytes() == before
    good = remote.read_bytes()
    remote.write_bytes(good[:-5] + b"wrong")
    with pytest.raises(datapack.DataError, match="the sidecar pins"):
        datapack.publish(root=tree)
    assert (tree / "data/evals/fam.json").read_bytes() == before
    # present on the release: fetched, re-uploaded under the same name, no rewrite
    remote.write_bytes(good)
    assert datapack.publish(root=tree) == {"uploaded": ["evals/fam"], "rewritten": []}
    assert (tree / "data/evals/fam.json").read_bytes() == before
    # absent everywhere: rebuilt from the tree
    datapack.cache_path(old).unlink()
    del hub.repos[REPO]["data-0001"]["assets"][f"{old}.zip"]
    remote.unlink()
    (tree / "evals/fam/items.json").write_text("edited, not packed\n")
    with pytest.raises(datapack.DataError, match="run `wsbench pack` first"):
        datapack.publish(root=tree)
    (tree / "evals/fam/items.json").write_text(FILES["evals/fam/items.json"])
    assert datapack.publish(root=tree)["rewritten"] == ["evals/fam"]
    new = datapack.load_sidecars(tree)["evals/fam"]["zip_sha256"]
    assert new != old and f"{new}.zip" in hub.repos[REPO]["data-0001"]["assets"]


def test_publish_to_a_fork_skips_what_upstream_serves_and_upstream_mirrors(
    tree, env, hub, monkeypatch
):
    """R3 + item 8."""
    datapack.publish(root=tree)
    monkeypatch.setattr(datapack, "RELEASE_CAP", 1)  # any upload to the fork would roll + rewrite
    _edit_and_pack(tree, "evals/fam/items.json", "[7]\n")
    before = _snapshot(tree)
    hub.calls.clear()
    assert datapack.publish(repo="fork/bench", root=tree)["uploaded"] == ["evals/fam"]
    assert len(hub.uploads()) == 1 and set(hub.repos["fork/bench"]) == {"data-0001"}
    assert _snapshot(tree) == before  # the three units upstream serves kept their sidecars
    # a maintainer with an empty cache mirrors the fork's asset upstream, byte-identical
    monkeypatch.setattr(datapack, "RELEASE_CAP", 900)
    _empty_cache(env)
    monkeypatch.setenv("WSB_DATA_REPO", "fork/bench")
    assert datapack.publish(root=tree)["uploaded"] == ["evals/fam"]
    assert _snapshot(tree) == before
    with pytest.raises(datapack.DataError, match="not an <owner>/<name>"):
        datapack.publish(repo="../../elsewhere", root=tree)


# ---------------------------------------------------------------------------- the fetch seam
class _Body:
    def __init__(self, blob: bytes) -> None:
        self.blob = blob

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self.blob


def _scripted(monkeypatch, steps: list):
    """``datapack._open`` answering from a script of exceptions and bodies."""
    calls: list[tuple[str, str, dict]] = []

    def fake(url, method, headers=None):
        calls.append((url, method, headers or {}))
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return _Body(step)

    monkeypatch.setattr(datapack, "_open", fake)
    monkeypatch.setattr(datapack, "BACKOFF", 0.0)
    return calls


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x/y.zip", code, "status", None, None)


def test_fetch_retries_a_short_read_and_not_a_404(monkeypatch):
    calls = _scripted(monkeypatch, [http.client.IncompleteRead(b"par"), _http(503), b"zip"])
    assert datapack.fetch("https://x/y.zip") == b"zip" and len(calls) == 3
    calls = _scripted(monkeypatch, [_http(404), b"zip"])
    assert datapack.fetch("https://x/y.zip") is None and len(calls) == 1
    # after an upload the asset may lag: there, and only there, a 404 is retried
    calls = _scripted(monkeypatch, [_http(404), b"zip"])
    assert datapack.fetch("https://x/y.zip", retry_missing=True) == b"zip" and len(calls) == 2
    calls = _scripted(monkeypatch, [_http(404)] * 3)
    assert datapack.fetch("https://x/y.zip", retry_missing=True) is None and len(calls) == 3
    calls = _scripted(monkeypatch, [_http(401)])
    with pytest.raises(OSError, match="HTTP 401"):
        datapack.fetch("https://x/y.zip")
    calls = _scripted(monkeypatch, [http.client.IncompleteRead(b"")] * 3)
    with pytest.raises(OSError, match="IncompleteRead"):
        datapack.fetch("https://x/y.zip")


def test_head_falls_back_to_a_ranged_get_and_is_strict_on_request(monkeypatch):
    calls = _scripted(monkeypatch, [_http(403), b"z"])
    assert datapack.head("https://x/y.zip") is True
    assert [c[1] for c in calls] == ["HEAD", "GET"] and calls[1][2] == {"Range": "bytes=0-0"}
    _scripted(monkeypatch, [TimeoutError("slow")] * 3)
    assert datapack.head("https://x/y.zip") is False
    _scripted(monkeypatch, [TimeoutError("slow")] * 3)
    with pytest.raises(OSError):
        datapack.head("https://x/y.zip", strict=True)


# ------------------------------------------------------- review round 1 (unpack / pack)
def test_a_stamp_without_sidecars_deletes_nothing(tree, env, capsys):
    """Review 2 A: redoing the cutover build, or checking out a commit that has data/INDEX.json
    and no sidecars, leaves a stale stamp over banks that git tracks again."""
    shutil.rmtree(tree / "data/evals")
    shutil.rmtree(tree / "data/adapted")
    assert set(datapack.load_stamp(tree)["units"]) and not datapack.load_sidecars(tree)
    datapack.ensure(root=tree)
    assert all((tree / rel).read_text() == text for rel, text in FILES.items())
    capsys.readouterr()
    assert datapack.unpack(root=tree) == {"units": 0, "written": 0, "deleted": 0}
    assert datapack.unpack(force=True, root=tree) == {"units": 0, "written": 0, "deleted": 0}
    assert all((tree / rel).read_text() == text for rel, text in FILES.items())
    assert "nothing is deleted" in capsys.readouterr().err


def test_the_import_time_path_never_deletes_a_unit_that_left_the_sidecars(
    tree, env, capsys, monkeypatch
):
    (tree / "data/adapted/m1/AUDIT.md.json").unlink()
    (tree / "data/adapted/m1/fam.json").unlink()  # the sidecar of the directory unit
    datapack.ensure(root=tree)
    assert (tree / "adapted/m1/AUDIT.md").is_file() and (tree / "adapted/m1/fam").is_dir()
    err = capsys.readouterr().err
    assert err.count("no longer have a sidecar") == 1 and "adapted/m1/AUDIT.md" in err
    # the stamp is current again: the next import does not unpack, and says nothing
    real = datapack.unpack
    monkeypatch.setattr(datapack, "unpack", lambda **k: pytest.fail("no unpack expected"))
    datapack.ensure(root=tree)
    assert capsys.readouterr().err == ""
    # the explicit command is what removes them (other sidecars exist)
    monkeypatch.setattr(datapack, "unpack", real)
    assert datapack.unpack(root=tree)["deleted"] == 2
    assert not (tree / "adapted/m1/AUDIT.md").exists() and not (tree / "adapted/m1/fam").exists()
    assert (tree / "adapted/m1/fam.json").is_file()


def test_a_missing_file_does_not_turn_an_unpacked_edit_into_a_conflict(tree, env, capsys):
    """Review 2 C: the unit's sidecar did not change, so a differing file is just an edit."""
    (tree / "evals/fam/items.json").write_text("my un-packed edit\n")
    (tree / "evals/fam/sub/extra.json").unlink()
    datapack.ensure(root=tree)  # the probe sees nothing; the explicit / stale paths must agree
    assert datapack.unpack(root=tree) == {"units": 1, "written": 1, "deleted": 0}
    assert (tree / "evals/fam/sub/extra.json").read_text() == FILES["evals/fam/sub/extra.json"]
    assert (tree / "evals/fam/items.json").read_text() == "my un-packed edit\n"
    # the import-time path with a stale stamp behaves the same
    (tree / "evals/fam/sub/extra.json").unlink()
    (tree / "data/evals/fam.json").write_text((tree / "data/evals/fam.json").read_text() + "\n")
    datapack.ensure(root=tree)
    assert (tree / "evals/fam/sub/extra.json").is_file()
    assert (tree / "evals/fam/items.json").read_text() == "my un-packed edit\n"
    # force without units= restores, and still leaves the edit; naming the unit discards it
    (tree / "evals/fam/sub/extra.json").unlink()
    datapack.unpack(force=True, root=tree)
    assert (tree / "evals/fam/items.json").read_text() == "my un-packed edit\n"
    capsys.readouterr()
    datapack.unpack(units=["evals/fam"], force=True, root=tree)
    assert (tree / "evals/fam/items.json").read_text() == FILES["evals/fam/items.json"]
    assert "force: overwrote evals/fam/items.json" in capsys.readouterr().err


@pytest.mark.parametrize(
    "fields",
    [
        {"zip_sha256": "a" * 64 + "\n"},
        {"release": "data-0001\n"},
        {"release": "data-\u0660\u0660\u0660\u0661"},  # Arabic-Indic digits
        {"files": {}},
        {"files": {"evals/fam/items.json": "\uff11" * 64}},  # fullwidth digits
    ],
)
def test_sidecar_patterns_are_full_ascii_matches(tree, fields):
    _doctor(tree, "evals/fam", **fields)
    with pytest.raises(datapack.DataError, match=r"malformed sidecar data/evals/fam\.json"):
        datapack.load_sidecars(tree)


@pytest.mark.parametrize("repo", ["./bench", "../bench", "owner/..", "owner/.", "owner/bench\n"])
def test_repo_owner_and_name_cannot_be_dots(repo):
    with pytest.raises(datapack.DataError, match="not an <owner>/<name>"):
        datapack.check_repo(repo, "test")
    assert datapack.check_repo("camila-blank/workspace-bench", "test")
    assert datapack.check_repo("a.b/c_d.e", "test")


def test_markdown_under_evals_is_never_packed_in_any_case():
    assert datapack.never_packed("evals/fam/README.MD") and datapack.never_packed("evals/f/a.Md")
    assert not datapack.never_packed("adapted/m1/AUDIT.MD")
    with pytest.raises(datapack.DataError):
        datapack.cache_path("a" * 64 + "\n")


def test_ensure_notices_plaintext_that_git_removed_under_a_current_stamp(tree, env):
    """Checking out a pre-cutover commit and coming back deletes the (then tracked) banks."""
    clone = _clone(tree, env / "clone")
    datapack.ensure(root=clone)
    shutil.rmtree(clone / "evals/fam")
    shutil.rmtree(clone / "adapted/m1")
    assert datapack.load_stamp(clone)["sidecars_sha256"] == datapack.sidecars_digest(clone)
    datapack.ensure(root=clone)
    assert not datapack.dirty(datapack.status(root=clone))
    assert (clone / "evals/fam/sub/extra.json").is_file()


def test_force_touches_only_units_that_are_behind_or_named(tree, env, capsys):
    """A forced unpack must not destroy an un-packed edit in a unit that was not in conflict."""
    clone = _clone(tree, env / "clone")
    datapack.unpack(root=clone)
    _edit_and_pack(tree, "evals/fam/items.json", '{"items": [9]}\n')
    shutil.copy2(tree / "data/evals/fam.json", clone / "data/evals/fam.json")
    (clone / "evals/fam/items.json").write_text("CONFLICTING EDIT\n")
    (clone / "adapted/m1/fam.json").write_text("WORK IN PROGRESS\n")  # its sidecar did not move
    with pytest.raises(datapack.DataError) as e:
        datapack.unpack(root=clone)
    assert "`wsbench unpack units=evals/fam force=True`" in str(e.value)
    capsys.readouterr()
    datapack.unpack(force=True, root=clone)
    assert (clone / "evals/fam/items.json").read_text() == '{"items": [9]}\n'
    assert (clone / "adapted/m1/fam.json").read_text() == "WORK IN PROGRESS\n"
    err = capsys.readouterr().err
    assert "force: overwrote evals/fam/items.json" in err and "adapted/m1/fam.json" not in err
    # naming the unit is how an un-packed edit is discarded, and it is printed
    datapack.unpack(units=["adapted/m1/fam.json"], force=True, root=clone)
    assert (clone / "adapted/m1/fam.json").read_text() == FILES["adapted/m1/fam.json"]
    assert "force: overwrote adapted/m1/fam.json" in capsys.readouterr().err


def test_forced_delete_of_an_edited_stale_file_is_printed(tree, env, capsys):
    clone = _clone(tree, env / "clone")
    datapack.unpack(root=clone)
    (clone / "data/adapted/m1/AUDIT.md.json").unlink()
    (clone / "adapted/m1/AUDIT.md").write_text("# mine\n")
    with pytest.raises(datapack.DataError, match=r"units=adapted/m1/AUDIT\.md force=True"):
        datapack.unpack(root=clone)
    datapack.unpack(units=["adapted/m1/AUDIT.md"], force=True, root=clone)
    assert not (clone / "adapted/m1/AUDIT.md").exists()
    assert "force: deleted adapted/m1/AUDIT.md" in capsys.readouterr().err


def _doctor(root: Path, name: str, /, **fields) -> None:
    path = datapack.sidecar_path(root, name)
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


@pytest.mark.parametrize(
    "bad",
    [
        "../x.json",
        "/etc/passwd",
        "src/evil.py",
        "evals/other/items.json",
        "evals/fam/../../src/evil.py",
        "evals/fam//items.json",
        "evals/fam/README.md",
    ],
)
def test_sidecar_file_paths_are_validated(tree, env, bad):
    clone = _clone(tree, env / "clone")
    side = datapack.load_sidecars(tree)["evals/fam"]
    _doctor(clone, "evals/fam", files={**side["files"], bad: "0" * 64})
    victim = env / "x.json"
    victim.write_text("keep me")
    for call in (datapack.load_sidecars, datapack.unpack, datapack.status, datapack.ensure):
        with pytest.raises(datapack.DataError, match=r"malformed sidecar data/evals/fam\.json"):
            call(root=clone)
    assert victim.read_text() == "keep me" and not (clone / "src").exists()
    assert not (clone / "evals/fam/items.json").exists()  # nothing of any unit was written


@pytest.mark.parametrize(
    "fields",
    [
        {"zip_sha256": "../../victim"},
        {"zip_sha256": "ABC"},
        {"release": "data-1/../../x"},
        {"release": "v1.2.0"},
        {"files": {"evals/fam/items.json": "../../victim"}},
        {"files": ["evals/fam/items.json"]},
        {"unit": "evals/other"},
        {"zip_bytes": "12"},
    ],
)
def test_sidecar_fields_are_validated_and_nothing_is_unlinked(tree, env, fields):
    clone = _clone(tree, env / "clone")
    _doctor(clone, "evals/fam", **fields)
    victim = env / "cache" / "victim.zip"  # what cache/data/../../victim.zip would resolve near
    for v in (victim, env / "victim.zip", env / "cache/data/victim.zip"):
        v.parent.mkdir(parents=True, exist_ok=True)
        v.write_text("keep me")
    with pytest.raises(datapack.DataError, match=r"malformed sidecar data/evals/fam\.json"):
        datapack.unpack(root=clone)
    with pytest.raises(datapack.DataError):
        datapack.cache_path("../../victim")
    assert all(v.read_text() == "keep me" for v in (victim, env / "victim.zip"))
    assert (env / "cache/data/victim.zip").read_text() == "keep me"


def test_a_doctored_stamp_cannot_make_unpack_delete(tree, env, capsys):
    clone = _clone(tree, env / "clone")
    datapack.unpack(root=clone)
    victim = clone / "src/keep.py"
    _write(clone, {"src/keep.py": "x = 1\n", "evals/other/items.json": "{}\n"})
    stamp = json.loads((clone / "data/.unpacked.json").read_text())
    entry = stamp["units"]["evals/fam"]
    entry["files"]["src/keep.py"] = _sha("x = 1\n")  # "unchanged since the stamp": deletable
    entry["files"]["evals/other/items.json"] = _sha("{}\n")
    stamp["units"]["adapted/m1/fam"]["zip_sha256"] = "../../victim"
    (clone / "data/.unpacked.json").write_text(json.dumps(stamp))
    datapack.unpack(root=clone)  # the bad entries are dropped: those units are simply redone
    assert victim.read_text() == "x = 1\n" and (clone / "evals/other/items.json").is_file()
    assert "malformed" in capsys.readouterr().err
    assert datapack.load_stamp(clone)["sidecars_sha256"] == datapack.sidecars_digest(clone)


def test_repo_names_are_validated(tree, env, monkeypatch):
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    monkeypatch.setenv("WSB_DATA_REPO", "../../etc")
    with pytest.raises(datapack.DataError, match=r"\$WSB_DATA_REPO"):
        datapack.unpack(root=clone)
    monkeypatch.delenv("WSB_DATA_REPO")
    idx = json.loads((clone / "data/INDEX.json").read_text())
    (clone / "data/INDEX.json").write_text(json.dumps({**idx, "repo": "evil.example/a/b"}))
    with pytest.raises(datapack.DataError, match=r"data/INDEX\.json `repo`"):
        datapack.unpack(root=clone)


def test_python_dash_m_is_the_cli(tree, env, monkeypatch):
    """`python -m wsbench` (what `run` spawns per family) defers like the script."""
    clone = _clone(tree, env / "clone")
    _write(clone, {"evals/fam/items.json": "local\n"})
    monkeypatch.setattr(datapack, "ROOT", clone)
    monkeypatch.setattr(datapack, "DEFERRED", None)
    main = str(REPO_ROOT / "src/wsbench/__main__.py")
    monkeypatch.setattr(sys, "argv", [main, "pack", "status"])
    datapack.ensure_at_import()
    assert datapack.DEFERRED is None
    monkeypatch.setattr(sys, "argv", [main, "judge", "family=poetry"])
    datapack.ensure_at_import()
    assert "evals/fam/items.json" in str(datapack.DEFERRED)
    monkeypatch.setattr(sys, "argv", ["/somewhere/else/__main__.py", "judge"])
    with pytest.raises(datapack.DataError):
        datapack.ensure_at_import()


def test_a_judge_child_with_unpackable_banks_exits_2_with_the_message(tree, env):
    """End to end in a subprocess: a copy of the package beside conflicting sidecars."""
    co = _clone(tree, env / "checkout")
    shutil.copytree(
        REPO_ROOT / "src/wsbench",
        co / "src/wsbench",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _write(co, {"evals/fam/items.json": "a local edit\n"})
    child_env = {**os.environ, "PYTHONPATH": str(co / "src")}
    for argv in (
        ["judge", "family=poetry", "readouts=none.jsonl", f"out={env / 'out'}"],
        ["list"],
    ):
        r = subprocess.run(
            [sys.executable, "-m", "wsbench", *argv], env=child_env, capture_output=True, text=True
        )
        assert r.returncode == 2, r.stderr
        assert "conflict" in r.stderr and "evals/fam/items.json" in r.stderr
        assert "Traceback" not in r.stderr
    # the data commands run (they are how the conflict is resolved)
    r = subprocess.run(
        [sys.executable, "-m", "wsbench", "unpack", "units=evals/fam", "force=True"],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (co / "evals/fam/items.json").read_text() == FILES["evals/fam/items.json"]
    r = subprocess.run(
        [sys.executable, "-m", "wsbench", "unpack", "units=nope"],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2 and "no such unit" in r.stderr and "Traceback" not in r.stderr


def test_stale_temp_files_are_swept_at_unpack(tree, env):
    clone = _clone(tree, env / "clone")
    datapack.unpack(root=clone)
    old = [
        clone / "evals/fam/.items.json.wsbtmp.1.2",
        clone / "data/..unpacked.json.wsbtmp.1.2",
        env / "cache/data/.build.zip.wsbtmp.1.2",
    ]
    fresh = clone / "evals/fam/.items.json.wsbtmp.3.4"
    for p in [*old, fresh]:
        p.write_text("partial")
    for p in old:
        os.utime(p, (0, 0))
    (clone / "evals/fam/items.json").unlink()  # gives unpack something to do in that directory
    datapack.unpack(root=clone)
    assert not any(p.exists() for p in old) and fresh.exists()
    assert (
        "evals/fam/.items.json.wsbtmp.3.4"
        not in datapack.status(root=clone)["units"].get("evals/fam", {"unlisted": []})["unlisted"]
    )


def test_nothing_tracked_under_data_is_local_state():
    tracked = _git_ls_files("data")
    assert tracked, "data/README.md and data/INDEX.json are tracked"
    assert [p for p in tracked if p.endswith(".unpacked.json") or ".wsbtmp." in p] == []
    try:
        ignored = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "data/.unpacked.json"],
            capture_output=True,
        ).returncode
    except OSError:
        pytest.skip("git unavailable")
    assert ignored == 0, "data/.unpacked.json must be git-ignored"


def test_unpack_refreshes_the_stamps_release_when_only_the_tag_moved(tree):
    assert datapack.load_stamp(tree)["units"]["evals/fam"]["release"] == "data-0001"
    _doctor(tree, "evals/fam", release="data-0002")
    assert datapack.unpack(root=tree) == {"units": 0, "written": 0, "deleted": 0}
    stamp = datapack.load_stamp(tree)
    assert stamp["units"]["evals/fam"]["release"] == "data-0002"
    assert stamp["sidecars_sha256"] == datapack.sidecars_digest(tree)


def test_the_sidecar_records_the_bytes_that_are_in_the_zip(tree, monkeypatch):
    """A file read once: a hash taken before the zip was written would describe other bytes."""
    rel = "evals/fam/items.json"
    (tree / rel).write_text("the bytes that get zipped\n")
    real = datapack._sha_or_none

    def racing(path):  # the planning pass saw an older version of this one file
        return "f" * 64 if Path(path).name == "items.json" else real(path)

    monkeypatch.setattr(datapack, "_sha_or_none", racing)
    datapack.pack(root=tree)
    monkeypatch.setattr(datapack, "_sha_or_none", real)
    side = datapack.load_sidecars(tree)["evals/fam"]
    assert side["files"][rel] == _sha("the bytes that get zipped\n")
    blobs = datapack._read_zip("evals/fam", datapack.cache_path(side["zip_sha256"]), [rel])
    assert hashlib.sha256(blobs[rel]).hexdigest() == side["files"][rel]


def test_pack_reads_the_zip_back_before_it_writes_the_sidecar(tree, monkeypatch):
    before = (tree / "data/evals/fam.json").read_bytes()
    (tree / "evals/fam/items.json").write_text("new\n")
    real = datapack._read_zip
    monkeypatch.setattr(
        datapack, "_read_zip", lambda u, p, names: dict.fromkeys(real(u, p, names), b"damaged")
    )
    with pytest.raises(datapack.DataError, match="does not read back"):
        datapack.pack(root=tree)
    assert (tree / "data/evals/fam.json").read_bytes() == before
    assert not list(datapack.cache_dir().glob("*wsbtmp*"))


def test_unpack_verify_restores_missing_and_reports_differing(tree, monkeypatch, capsys):
    monkeypatch.setattr(datapack, "ROOT", tree)
    assert cli.main(["unpack", "verify=True"]) == 0
    (tree / "adapted/m1/fam.json").unlink()
    (tree / "evals/fam/items.json").write_text("bit rot, or an edit\n")
    assert cli.main(["unpack"]) == 0  # the stamp matches: a plain unpack only restores
    assert (tree / "adapted/m1/fam.json").is_file()
    (tree / "adapted/m1/fam.json").unlink()
    capsys.readouterr()
    assert cli.main(["unpack", "verify=True"]) == 2
    err = capsys.readouterr().err
    assert (tree / "adapted/m1/fam.json").read_text() == FILES["adapted/m1/fam.json"]
    assert "M evals/fam/items.json" in err and "units=evals/fam force=True" in err
    assert (tree / "evals/fam/items.json").read_text() == "bit rot, or an edit\n"


def test_atomic_write_fsyncs_before_the_rename(tmp_path, monkeypatch):
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1]
    )
    datapack._atomic_write(tmp_path / "d/f.json", b"x")
    assert order == ["fsync", "replace"] and (tmp_path / "d/f.json").read_bytes() == b"x"


def test_a_unit_that_turns_from_a_file_into_a_directory_and_back(tree, env):
    clone = _clone(tree, env / "clone")
    datapack.unpack(root=clone)
    unit = "adapted/m1/AUDIT.md"
    # file -> directory of the same name
    (tree / unit).unlink()
    _write(tree, {f"{unit}/part1.md": "one\n", f"{unit}/part2.md": "two\n"})
    assert datapack.pack(add=[unit], rm=[unit], root=tree) == [unit]
    listed = set(datapack.load_sidecars(tree)[unit]["files"])
    assert listed == {f"{unit}/part1.md", f"{unit}/part2.md"}
    shutil.copy2(tree / f"data/{unit}.json", clone / f"data/{unit}.json")
    datapack.unpack(root=clone)
    assert (clone / unit / "part2.md").read_text() == "two\n"
    # ... and back
    shutil.rmtree(tree / unit)
    _write(tree, {unit: "# one file again\n"})
    datapack.pack(rm=[f"{unit}/part1.md", f"{unit}/part2.md"], add=[unit], root=tree)
    shutil.copy2(tree / f"data/{unit}.json", clone / f"data/{unit}.json")
    datapack.unpack(root=clone)
    assert (clone / unit).read_text() == "# one file again\n"
    assert not datapack.dirty(datapack.status(root=clone))


# ----------------------------------------------------------------------------- ensure
def test_ensure_noops(tree, env, monkeypatch):
    bare = env / "wheel"
    _write(bare, {"evals/fam/items.json": "x"})
    datapack.ensure(root=bare)  # no data/INDEX.json
    pre = _new_root(env / "pre-cutover")
    _write(pre, FILES)
    datapack.ensure(root=pre)  # an index but no sidecar yet
    assert datapack.unpack(root=pre) == {"units": 0, "written": 0, "deleted": 0}
    assert not (pre / "data/.unpacked.json").exists()
    clone = _clone(tree, env / "clone")
    _empty_cache(env)
    monkeypatch.setenv("WSB_NO_UNPACK", "1")
    datapack.ensure(root=clone)
    assert not (clone / "evals/fam/items.json").exists()
    monkeypatch.delenv("WSB_NO_UNPACK")
    with pytest.raises(datapack.DataError):
        datapack.ensure(root=clone)  # nothing cached, nothing published


def test_ensure_unpacks_then_the_stamp_makes_it_a_noop(tree, env, monkeypatch):
    clone = _clone(tree, env / "clone")
    datapack.ensure(root=clone)
    assert (clone / "evals/fam/items.json").read_text() == FILES["evals/fam/items.json"]

    def boom(*a, **k):
        raise AssertionError("the stamp is current: no unpack")

    monkeypatch.setattr(datapack, "unpack", boom)
    datapack.ensure(root=clone)
    side = clone / "data/evals/fam.json"
    side.write_text(side.read_text() + "\n")  # any sidecar byte moves the digest
    with pytest.raises(AssertionError, match="no unpack"):
        datapack.ensure(root=clone)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root posix user")
def test_ensure_in_a_read_only_checkout_non_root(tree, env):
    _read_only_checks(tree, env)


def test_ensure_in_a_read_only_checkout(tree, env, monkeypatch):
    """The same rule with the write failure simulated (root ignores directory modes)."""
    real = datapack._atomic_write

    def refuse(path, blob):
        if "clone" in Path(path).parts:
            raise PermissionError(13, "Read-only file system", str(path))
        real(path, blob)

    monkeypatch.setattr(datapack, "_atomic_write", refuse)
    _read_only_checks(tree, env, chmod=False)


def _read_only_checks(tree: Path, env: Path, chmod: bool = True) -> None:
    clone = _clone(tree, env / "clone")
    _write(clone, FILES)
    dirs = [clone, *[p for p in clone.rglob("*") if p.is_dir()]]
    try:
        if chmod:
            for d in dirs:
                d.chmod(0o555)
        datapack.ensure(root=clone)  # the plaintext is present and matches: no stamp, no error
        assert not (clone / "data/.unpacked.json").exists()
        if chmod:
            (clone / "evals/fam").chmod(0o755)
        (clone / "evals/fam/items.json").unlink()
        if chmod:
            (clone / "evals/fam").chmod(0o555)
        with pytest.raises(datapack.DataError, match="not writable"):
            datapack.ensure(root=clone)
    finally:
        for d in dirs:
            d.chmod(0o755)


def test_two_processes_unpacking_at_once_leave_a_correct_tree(tree, env):
    _write(tree, {f"evals/big{i}/items.json": f"{i}" * 200_000 for i in range(6)})
    datapack.pack(add=["evals/big*"], root=tree)
    _publish_local(tree, env)
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    code = (
        "import sys; from wsbench import datapack; "
        "datapack.ensure(root=sys.argv[1]); datapack.unpack(root=sys.argv[1])"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(clone)], env=dict(os.environ), stderr=subprocess.PIPE
        )
        for _ in range(3)
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0, p.stderr.read().decode()
    want = datapack.listed_paths(tree)
    assert datapack.tree_shas("evals", clone) | datapack.tree_shas("adapted", clone) == {
        p: datapack.file_sha(tree / p) for p in want
    } | {k: _sha(v) for k, v in KEEPERS.items()}
    assert not datapack.dirty(datapack.status(root=clone))
    assert datapack.load_stamp(clone)["sidecars_sha256"] == datapack.sidecars_digest(clone)
    assert not [p for p in clone.rglob("*") if ".wsbtmp." in p.name]


# ---------------------------------------------------------------- pyzipper stays optional
def test_wsbench_imports_and_ensures_without_pyzipper():
    code = (
        "import sys; sys.modules['pyzipper'] = None\n"
        "import wsbench, wsbench.registry, wsbench.cli\n"
        "from wsbench import datapack\n"
        "datapack.ensure()\n"
        "assert sys.modules['pyzipper'] is None\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_datapack_module_never_imports_pyzipper_at_module_level():
    src = (REPO_ROOT / "src/wsbench/datapack.py").read_text(encoding="utf-8")
    assert [ln for ln in src.splitlines() if "import pyzipper" in ln and ln[0] != " "] == []


# --------------------------------------------------------------------- the real tree
def _git_ls_files(*paths: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", *paths],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode("utf-8").split("\0") if p]


def test_only_the_markdown_keepers_are_tracked_under_evals_and_adapted():
    if not datapack.load_sidecars():
        pytest.skip("before the cutover: the banks are still tracked")
    tracked = _git_ls_files("evals", "adapted")
    assert [p for p in tracked if not datapack.never_packed(p)] == []
    assert [p for p in tracked if p.endswith(".report.lock")] == []
    assert set(tracked).isdisjoint(datapack.listed_paths())


def test_pack_status_is_clean_on_the_real_tree():
    """R6, the "forgot to pack" check: a listed file that differs from its sidecar."""
    if not (REPO_ROOT / "data").is_dir():
        pytest.skip("no data/ (not a source checkout)")
    report = datapack.status()
    beside = [p for r in report["units"].values() for p in r["unlisted"]]
    assert not datapack.dirty(report), (
        datapack.format_status(report)
        + "\nunlisted files beside listed ones (new bank files need `wsbench pack add=`):\n"
        + "\n".join(beside)
    )


def test_real_sidecars_are_well_formed():
    for unit, side in datapack.load_sidecars().items():
        assert side["release"].startswith("data-") and len(side["zip_sha256"]) == 64
        assert side["files"], unit
        assert all(
            datapack.unit_of(p) == unit and not datapack.never_packed(p) for p in side["files"]
        )


def test_canary_placement():
    idx = json.loads((REPO_ROOT / "data/INDEX.json").read_text(encoding="utf-8"))
    line = idx["canary"]
    assert line.startswith(datapack.CANARY_PREFIX) and len(line) == len(datapack.CANARY_PREFIX) + 36
    assert datapack.canary() == line and idx["version"] == 1 and idx["repo"].count("/") == 1
    for doc in ("data/README.md", "README.md", "NOTICE.md"):
        assert line in (REPO_ROOT / doc).read_text(encoding="utf-8"), doc
    assert datapack.PASSWORD.decode() in (REPO_ROOT / "data/README.md").read_text(encoding="utf-8")
    # never inside a bank file (it would move every bank_sha): every non-Markdown file of evals/
    needle = datapack.CANARY_PREFIX.encode()
    for path in sorted((REPO_ROOT / "evals").rglob("*")):
        if path.is_file() and path.suffix != ".md":
            assert needle not in path.read_bytes(), path
