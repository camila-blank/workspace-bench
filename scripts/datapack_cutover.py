"""Plan 0097 section 5, the one-off cutover driver (deleted in the cutover PR). ``build`` packs
every tracked bank file into sidecars + cached zips; ``verify`` unpacks into a directory and
compares ``git hash-object`` with ``git ls-tree``. Neither publishes nor changes git state.

    python scripts/datapack_cutover.py build
    python scripts/datapack_cutover.py verify --dir <empty dir> --ref <commit> [--from-release]
    python scripts/datapack_cutover.py verify --dir . --ref HEAD --no-unpack   # pre-pack self-check
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from wsbench import datapack

REPO = Path(__file__).resolve().parents[1]


def _git(args: list[str], stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], input=stdin, capture_output=True, check=True
    ).stdout


def tracked(ref: str | None = None) -> dict[str, str]:
    """Packable path -> blob id: the index (``ref`` None) or a commit, minus the Markdown
    keepers and the ``.report.lock`` files."""
    if ref is None:
        raw = _git(["ls-files", "-s", "-z", "--", *datapack.SCOPES])
    else:
        raw = _git(["ls-tree", "-r", "-z", ref, "--", *datapack.SCOPES])
    out: dict[str, str] = {}
    for row in raw.decode("utf-8").split("\0"):
        if not row:
            continue
        meta, path = row.split("\t", 1)
        blob = meta.split()[1] if ref is None else meta.split()[2]
        if not datapack.never_packed(path):
            out[path] = blob
    return out


def build() -> int:
    paths = sorted(tracked())
    if datapack.load_sidecars(REPO):
        sys.exit("data/ already holds sidecars: the cutover build runs once, on a tree without")
    absent = [p for p in paths if not (REPO / p).is_file()]
    if absent:
        sys.exit(f"{len(absent)} tracked file(s) are not in the working tree, e.g. {absent[:3]}")
    t0 = time.time()
    units = datapack.pack(add=paths, root=REPO)
    sides = datapack.load_sidecars(REPO)
    n_files = sum(len(s["files"]) for s in sides.values())
    n_bytes = sum(s["zip_bytes"] for s in sides.values())
    print(
        f"packed {len(units)} unit(s), {n_files} file(s) of {len(paths)} tracked, "
        f"{n_bytes / 1e6:.1f} MB of zips in {datapack.cache_dir()} ({time.time() - t0:.0f} s)"
    )
    return 0 if n_files == len(paths) else 1


def _packable_on_disk(directory: Path) -> list[str]:
    found = [
        p.relative_to(directory).as_posix()
        for scope in datapack.SCOPES
        for p in sorted((directory / scope).rglob("*"))
        if p.is_file()
    ]
    return [p for p in found if not datapack.never_packed(p)]


def _unpack_into(directory: Path, n_expected: int, from_release: bool) -> None:
    """Unpack into ``directory``, which must hold no packable file beforehand, and require that
    exactly the expected number of files was written: every compared byte came out of a zip."""
    before = _packable_on_disk(directory)
    if before:
        sys.exit(
            f"{directory} already holds {len(before)} packable file(s) under evals/ / adapted/ "
            f"(e.g. {before[0]}): the check would compare files no zip produced. Remove them "
            "first (the Markdown keepers may stay), or pass --no-unpack for the pre-pack "
            "self-check."
        )
    if directory != REPO:
        for scope in datapack.SCOPES:  # sidecars only; the stamp is the directory's own
            src, dst = REPO / "data" / scope, directory / "data" / scope
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        for name in ("INDEX.json", "README.md"):
            shutil.copy2(REPO / "data" / name, directory / "data" / name)
    (directory / datapack.STAMP).unlink(missing_ok=True)
    saved = dict(os.environ)
    scratch = None
    try:
        if from_release:
            scratch = tempfile.mkdtemp(prefix="wsb-verify-cache-")
            os.environ["WSB_CACHE"] = scratch
            for key in datapack.TOKEN_ENVS:
                os.environ.pop(key, None)
            print(f"--from-release: empty cache {scratch}, no GitHub token in the environment; "
                  "every zip is downloaded anonymously from the release")  # fmt: skip
        else:
            print(f"zips: the local cache {datapack.cache_dir()} (downloads only what it lacks)")
        t0 = time.time()
        done = datapack.unpack(root=directory)
        print(f"unpack: {done} in {time.time() - t0:.0f} s")
        if from_release:
            n_zips = len(list(datapack.cache_dir().glob("*.zip")))
            print(
                f"downloaded {n_zips} zip(s) for {len(datapack.load_sidecars(directory))} unit(s)"
            )
    finally:
        os.environ.clear()
        os.environ.update(saved)
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    if done["written"] != n_expected:
        sys.exit(
            f"unpack wrote {done['written']} file(s), {n_expected} expected: NOT VERIFIED "
            "(a file that was not written by this unpack would be compared as if it were)"
        )


def verify(directory: Path, ref: str, unpack: bool, from_release: bool = False) -> int:
    directory = directory.resolve()
    want = tracked(ref)
    if not want:
        sys.exit(f"{ref}: no packable file under evals/ or adapted/; nothing to verify against "
                 "(pass the last commit that still tracks the plaintext)")  # fmt: skip
    if unpack:
        _unpack_into(directory, len(want), from_release)
    else:
        print("--no-unpack: NO ZIP WAS READ. This compares the directory as it is with the ref "
              "(the pre-pack self-check); it proves nothing about the zips.")  # fmt: skip
    found = _packable_on_disk(directory)
    stdin = "".join(f"{directory / p}\n" for p in found).encode("utf-8")
    ids = _git(["hash-object", "--no-filters", "--stdin-paths"], stdin).decode().split()
    got = dict(zip(found, ids, strict=True))
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    differ = sorted(p for p in set(want) & set(got) if want[p] != got[p])
    equal = len(set(want) & set(got)) - len(differ)
    n_tree = len(_git(["ls-tree", "-r", "--name-only", ref, "--", *datapack.SCOPES]).splitlines())
    print(
        f"{ref}: {n_tree} files under evals/ + adapted/, {len(want)} packable "
        f"({n_tree - len(want)} Markdown keepers / .report.lock files are not packed)"
    )
    print(f"expected {len(want)}; found {len(found)}; equal {equal}; missing {len(missing)}; "
          f"extra {len(extra)}; differing {len(differ)}")  # fmt: skip
    for label, rows in (("missing", missing), ("extra", extra), ("differing", differ)):
        for p in rows[:20]:
            print(f"  {label}: {p}")
    ok = not (missing or extra or differ) and equal == len(want) > 0
    if not ok:
        print("NOT IDENTICAL")
    elif unpack:
        print("IDENTICAL" + (" (anonymous download from the release)" if from_release else ""))
    else:
        print("IDENTICAL TO THE REF, NO ZIP WAS READ")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("build")
    v = sub.add_parser("verify")
    v.add_argument("--dir", required=True, type=Path)
    v.add_argument("--ref", required=True)
    v.add_argument(
        "--no-unpack",
        action="store_true",
        help="pre-pack self-check only: compare the directory as it is (no zip read)",
    )
    v.add_argument(
        "--from-release",
        action="store_true",
        help="fresh temp WSB_CACHE and no GitHub token: zips come from the release",
    )
    args = ap.parse_args()
    try:
        if args.mode == "build":
            return build()
        if args.no_unpack and args.from_release:
            ap.error("--from-release unpacks; it cannot be combined with --no-unpack")
        return verify(args.dir, args.ref, not args.no_unpack, from_release=args.from_release)
    except datapack.DataError as e:
        print(f"datapack_cutover: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
