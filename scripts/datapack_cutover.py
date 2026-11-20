"""Plan 0097 section 5, the one-off cutover driver (deleted in the cutover PR). ``build`` packs
every tracked bank file into sidecars + cached zips; ``verify`` unpacks into a directory and
compares ``git hash-object`` with ``git ls-tree``. Neither publishes nor changes git state.

    python scripts/datapack_cutover.py build
    python scripts/datapack_cutover.py verify --dir <scratch or clone> --ref <commit> [--no-unpack]
"""

import argparse
import shutil
import subprocess
import sys
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


def verify(directory: Path, ref: str, unpack: bool) -> int:
    directory = directory.resolve()
    want = tracked(ref)
    if unpack:
        if directory != REPO:
            for scope in datapack.SCOPES:  # sidecars only; the stamp is the directory's own
                src, dst = REPO / "data" / scope, directory / "data" / scope
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            for name in ("INDEX.json", "README.md"):
                shutil.copy2(REPO / "data" / name, directory / "data" / name)
        t0 = time.time()
        print(f"unpack: {datapack.unpack(root=directory)} in {time.time() - t0:.0f} s")
    found = [
        p.relative_to(directory).as_posix()
        for scope in datapack.SCOPES
        for p in sorted((directory / scope).rglob("*"))
        if p.is_file()
    ]
    found = [p for p in found if not datapack.never_packed(p)]
    stdin = "".join(f"{directory / p}\n" for p in found).encode("utf-8")
    got = dict(
        zip(
            found,
            _git(["hash-object", "--no-filters", "--stdin-paths"], stdin).decode().split(),
            strict=True,
        )
    )
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    differ = sorted(p for p in set(want) & set(got) if want[p] != got[p])
    equal = len(set(want) & set(got)) - len(differ)
    n_tree = len(_git(["ls-tree", "-r", "--name-only", ref, "--", *datapack.SCOPES]).splitlines())
    print(
        f"{ref}: {n_tree} files under evals/ + adapted/, {len(want)} packable "
        f"({n_tree - len(want)} Markdown keepers / .report.lock files are not packed)"
    )
    print(f"equal {equal} / {len(want)}; missing {len(missing)}; extra {len(extra)}; "
          f"differing {len(differ)}")  # fmt: skip
    for label, rows in (("missing", missing), ("extra", extra), ("differing", differ)):
        for p in rows[:20]:
            print(f"  {label}: {p}")
    ok = not (missing or extra or differ)
    print("IDENTICAL" if ok else "NOT IDENTICAL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("build")
    v = sub.add_parser("verify")
    v.add_argument("--dir", required=True, type=Path)
    v.add_argument("--ref", required=True)
    v.add_argument("--no-unpack", action="store_true", help="compare the directory as it is")
    args = ap.parse_args()
    try:
        if args.mode == "build":
            return build()
        return verify(args.dir, args.ref, unpack=not args.no_unpack)
    except datapack.DataError as e:
        print(f"datapack_cutover: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
