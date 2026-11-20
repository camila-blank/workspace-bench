"""Plan 0097: pack / unpack of the bank zips on temp trees (a local directory stands in for the
release through a file:// base URL; no network), plus the real tree's packing rules."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
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
    """An in-memory ``gh`` whose uploaded assets land in the stand-in release directory."""

    def __init__(self, env: Path) -> None:
        self.remote = env / "remote"
        self.releases: dict[str, dict[str, dict[str, dict]]] = {}  # repo -> tag -> name -> asset
        self.calls: list[list[str]] = []
        self.tags: dict[str, str] = {}
        self.objects: dict[str, dict] = {}
        self.draft_uploads = False

    def __call__(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append(args)
        if args[0] == "api":
            body = json.loads(stdin or "{}")
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            self.objects[sha] = body
            if args[1].endswith("/git/refs"):
                self.tags[body["ref"]] = body["sha"]
            return 0, json.dumps({"sha": sha}), ""
        repo = args[args.index("--repo") + 1]
        rel = self.releases.setdefault(repo, {})
        if args[:2] == ["release", "list"]:
            return 0, json.dumps([{"tagName": t} for t in [*rel, "v1.2.0"]]), ""
        tag = args[2]
        if args[:2] == ["release", "create"]:
            assert f"refs/tags/{tag}" in self.tags and "--latest=false" in args
            assert "--draft" not in args and args[args.index("--target") + 1] in self.objects
            rel[tag] = {}
            return 0, "", ""
        if tag not in rel:
            return 1, "", "release not found"
        if args[:2] == ["release", "view"]:
            return 0, json.dumps({"assets": list(rel[tag].values())}), ""
        if args[:2] == ["release", "delete-asset"]:
            del rel[tag][args[3]]
            return 0, "", ""
        assert args[:2] == ["release", "upload"]
        for path in args[3 : args.index("--repo")]:
            name = Path(path).name
            if name in rel[tag]:
                return 1, "", f"asset {name} already exists"
            rel[tag][name] = {"name": name, "size": Path(path).stat().st_size, "state": "uploaded"}
            if not self.draft_uploads:
                dst = self.remote / repo / "releases" / "download" / tag
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst / name)
        return 0, "", ""

    def uploads(self) -> list[str]:
        return [Path(p).name for c in self.calls if c[:2] == ["release", "upload"] for p in c[3:-2]]


def test_publish_creates_the_release_uploads_once_and_verifies(tree, env, monkeypatch):
    hub = FakeGitHub(env)
    monkeypatch.setattr(datapack, "gh", hub)
    done = datapack.publish(root=tree)
    assert len(done["uploaded"]) == 4 and done["rewritten"] == []
    assert set(hub.releases[REPO]) == {"data-0001"}
    # R2: an orphan commit holding only data/README.md, reachable from the tag alone
    commit = hub.objects[hub.tags["refs/tags/data-0001"]]
    assert commit["parents"] == []
    assert [e["path"] for e in hub.objects[commit["tree"]]["tree"]] == ["data/README.md"]
    create = next(c for c in hub.calls if c[:2] == ["release", "create"])
    assert CANARY in create[create.index("--notes") + 1]
    # a fresh clone now unpacks from the release
    _empty_cache(env)
    clone = _clone(tree, env / "clone")
    assert datapack.unpack(root=clone)["written"] == 5
    # nothing to do the second time; an edit uploads one zip
    hub.calls.clear()
    assert datapack.publish(root=clone) == {"uploaded": [], "rewritten": []}
    assert hub.uploads() == []
    _edit_and_pack(clone, "evals/fam/items.json", "[2]\n")
    assert datapack.publish(root=clone)["uploaded"] == ["evals/fam"]
    assert hub.uploads() == [f"{datapack.load_sidecars(clone)['evals/fam']['zip_sha256']}.zip"]


def test_publish_replaces_a_broken_asset_and_fails_on_an_unreadable_one(tree, env, monkeypatch):
    hub = FakeGitHub(env)
    monkeypatch.setattr(datapack, "gh", hub)
    side = datapack.load_sidecars(tree)["evals/fam"]
    name = f"{side['zip_sha256']}.zip"
    hub.releases[REPO] = {"data-0001": {name: {"name": name, "size": 3, "state": "starter"}}}
    datapack.publish(root=tree)
    assert ["release", "delete-asset", "data-0001", name, "--repo", REPO, "--yes"] in hub.calls
    assert hub.releases[REPO]["data-0001"][name]["size"] == side["zip_bytes"]
    # an asset the anonymous download cannot read (a draft release) fails the publish
    hub.draft_uploads = True
    _edit_and_pack(tree, "evals/fam/items.json", "[3]\n")
    with pytest.raises(datapack.DataError, match="anonymous download check failed"):
        datapack.publish(root=tree)


def test_publish_rolls_to_the_next_release_and_rewrites_the_sidecar(tree, env, monkeypatch):
    hub = FakeGitHub(env)
    monkeypatch.setattr(datapack, "gh", hub)
    monkeypatch.setattr(datapack, "RELEASE_CAP", 3)
    done = datapack.publish(root=tree)
    assert len(done["rewritten"]) == 1 and set(hub.releases[REPO]) == {"data-0001", "data-0002"}
    moved = datapack.load_sidecars(tree)[done["rewritten"][0]]
    assert moved["release"] == "data-0002"
    assert f"{moved['zip_sha256']}.zip" in hub.releases[REPO]["data-0002"]
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


def test_publish_rebuilds_a_lost_zip_and_mirrors_a_forks(tree, env, monkeypatch):
    hub = FakeGitHub(env)
    monkeypatch.setattr(datapack, "gh", hub)
    old = datapack.load_sidecars(tree)["evals/fam"]["zip_sha256"]
    datapack.cache_path(old).unlink()
    done = datapack.publish(root=tree)  # R4: in neither the cache nor a release
    assert done["rewritten"] == ["evals/fam"]
    new = datapack.load_sidecars(tree)["evals/fam"]["zip_sha256"]
    assert new != old and f"{new}.zip" in hub.releases[REPO]["data-0001"]
    (tree / "evals/fam/items.json").write_text("edited, not packed\n")
    datapack.cache_path(new).unlink()
    hub.releases[REPO]["data-0001"].pop(f"{new}.zip")
    (env / "remote" / REPO / "releases/download/data-0001" / f"{new}.zip").unlink()
    with pytest.raises(datapack.DataError, match="run `wsbench pack` first"):
        datapack.publish(root=tree)
    # R3: publishing to a fork, then a maintainer mirrors the fork's assets upstream
    (tree / "evals/fam/items.json").write_text(FILES["evals/fam/items.json"])
    datapack.publish(repo="fork/bench", root=tree)
    assert set(hub.releases["fork/bench"]) == {"data-0001"}
    _empty_cache(env)
    monkeypatch.setenv("WSB_DATA_REPO", "fork/bench")
    before = _snapshot(tree)
    assert datapack.publish(root=tree)["uploaded"] == ["evals/fam"]
    assert _snapshot(tree) == before  # mirrored byte-identical: no sidecar moved


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
