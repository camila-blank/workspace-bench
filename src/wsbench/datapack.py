"""Plan 0097: the banks under ``evals/`` and ``adapted/`` ship as password zips (release assets);
git keeps one small sidecar per unit under ``data/`` and ``unpack`` writes the plaintext back at
the same paths. ``data/README.md`` is the user-facing statement."""

import contextlib
import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

# src layout: the checkout that holds this file (``registry.REPO_ROOT`` is the same path; this
# module must not import the registry, which calls ``ensure_at_import`` while it loads)
ROOT = Path(__file__).resolve().parents[2]
# published in data/README.md: it keeps crawlers out, it is not a secret
PASSWORD = b"workspace-bench-evaluation-only"
SCOPES = ("evals", "adapted")
STAMP = "data/.unpacked.json"
INDEX = "data/INDEX.json"
CANARY_FILE = "CANARY.txt"
CANARY_PREFIX = "WORKSPACEBENCH CANARY GUID "
FIRST_RELEASE = "data-0001"
RELEASE_CAP = 900  # GitHub caps a release at 1,000 assets; publish rolls to the next tag here
BASE_URL = "https://github.com"  # $WSB_DATA_URL replaces it (a mirror; tests use file://)
USER_AGENT = "wsbench-datapack (+https://github.com/camila-blank/workspace-bench)"
TIMEOUT = 60
TRIES = 3
BACKOFF = 2.0  # seconds before the 2nd try, doubled before the 3rd
WORKERS = 8
UPLOAD_BATCH = 20
NO_UNPACK_ENV = "WSB_NO_UNPACK"
DATA_REPO_ENV = "WSB_DATA_REPO"
DATA_COMMANDS = ("unpack", "pack")
_RELEASE_RE = re.compile(r"^data-(\d{4})$")
_GLOB_CHARS = frozenset("*?[")

# a DataError met by the import-time ensure() of a `wsbench` CLI process; cli.main reports it
DEFERRED: "DataError | None" = None


class DataError(Exception):
    """A pack / unpack failure; the CLI prints it and exits 2."""


# ------------------------------------------------------------------ paths, sidecars, the stamp
def _root(root: Path | str | None) -> Path:
    return ROOT if root is None else Path(root)


def cache_dir() -> Path:
    base = os.environ.get("WSB_CACHE")
    return (Path(base) if base else Path.home() / ".cache" / "wsbench") / "data"


def cache_path(sha: str) -> Path:
    return cache_dir() / f"{sha}.zip"


def never_packed(path: str) -> bool:
    """The Markdown keepers that stay tracked, and the adapt store's lock files."""
    if path == "adapted/README.md" or PurePosixPath(path).name == ".report.lock":
        return True
    return path.startswith("evals/") and path.endswith(".md")


def unit_of(path: str) -> str:
    """``evals/<entry>`` or ``adapted/<model>/<entry>`` (ruling R1) of a repo-relative path."""
    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[0] == "evals":
        return "/".join(parts[:2])
    if len(parts) >= 3 and parts[0] == "adapted":
        return "/".join(parts[:3])
    raise DataError(
        f"{path}: not packable; a unit is evals/<entry> or adapted/<model>/<entry> (data/README.md)"
    )


def sidecar_path(root: Path, unit: str) -> Path:
    return root / "data" / f"{unit}.json"  # the entry's full name is kept: poetry.json.json


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _sha_or_none(path: Path) -> str | None:
    try:
        return file_sha(path)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _sidecar_files(root: Path) -> list[Path]:
    data = root / "data"
    return sorted([*data.glob("evals/*.json"), *data.glob("adapted/*/*.json")])


def load_sidecars(root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Unit -> sidecar, from ``data/evals/*.json`` and ``data/adapted/*/*.json``."""
    root = _root(root)
    out: dict[str, dict[str, Any]] = {}
    for p in _sidecar_files(root):
        unit = p.relative_to(root / "data").as_posix()[: -len(".json")]
        try:
            side = json.loads(p.read_text(encoding="utf-8"))
            ok = side["unit"] == unit and isinstance(side["files"], dict)
            ok = ok and isinstance(side["zip_sha256"], str) and isinstance(side["release"], str)
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            raise DataError(
                f"{unit}: malformed sidecar {p.relative_to(root).as_posix()} (see data/README.md)"
            )
        out[unit] = side
    return out


def listed_paths(root: Path | str | None = None) -> list[str]:
    """Every repo-relative path a sidecar lists: the packed counterpart of ``git ls-files``."""
    return sorted(p for side in load_sidecars(root).values() for p in side["files"])


def tree_shas(prefix: str, root: Path | str | None = None) -> dict[str, str]:
    """sha256 of every file under ``<root>/<prefix>``, by repo-relative path."""
    root = _root(root)
    return {p.relative_to(root).as_posix(): file_sha(p) for p in _walk(root / prefix)}


def sidecars_digest(root: Path | str | None = None) -> str:
    """One sha256 over every sidecar's path and bytes: the stamp's no-op key."""
    root = _root(root)
    h = hashlib.sha256()
    for p in _sidecar_files(root):
        blob = p.read_bytes()
        h.update(f"{p.relative_to(root).as_posix()}\0{len(blob)}\0".encode())
        h.update(blob)
    return h.hexdigest()


def index(root: Path | str | None = None) -> dict[str, Any] | None:
    try:
        return json.loads((_root(root) / INDEX).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def canary(root: Path | str | None = None) -> str:
    idx = index(root)
    if not idx or not str(idx.get("canary", "")).startswith(CANARY_PREFIX):
        raise DataError(f"{INDEX}: missing, or it has no canary; packing needs it")
    return idx["canary"]


def load_stamp(root: Path | str | None = None) -> dict[str, Any]:
    try:
        stamp = json.loads((_root(root) / STAMP).read_text(encoding="utf-8"))
        if isinstance(stamp.get("units"), dict):
            return stamp
    except (OSError, ValueError, AttributeError):
        pass  # absent or torn: the same as never unpacked
    return {"version": 1, "sidecars_sha256": None, "units": {}}


def _tmp_name(path: Path) -> Path:
    return path.with_name(f".{path.name}.wsbtmp.{os.getpid()}.{threading.get_ident()}")


def _atomic_write(path: Path, blob: bytes) -> None:
    """Temp name + ``os.replace``: a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(path)
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _dump(obj: Any) -> bytes:
    # sorted keys, one file per line of the map: a bank PR shows which files changed
    return (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_stamp(root: Path, stamp: dict[str, Any], sides: dict[str, dict[str, Any]]) -> bool:
    """Write the stamp, current only when every sidecar's unit is stamped as it is; False in a
    read-only checkout (the caller decides whether that matters)."""
    units = stamp["units"]
    current = set(units) == set(sides) and all(not _differs(sides[u], units[u]) for u in sides)
    stamp["version"] = 1
    stamp["sidecars_sha256"] = sidecars_digest(root) if current else None
    try:
        _atomic_write(root / STAMP, _dump(stamp))
    except OSError:
        return False
    return True


def _entry(side: dict[str, Any]) -> dict[str, Any]:
    return {
        "release": side["release"],
        "zip_sha256": side["zip_sha256"],
        "files": dict(side["files"]),
    }


def _differs(side: dict[str, Any] | None, entry: dict[str, Any] | None) -> bool:
    if side is None or entry is None:
        return side is not entry
    return side["zip_sha256"] != entry.get("zip_sha256") or side["files"] != entry.get("files")


def _select(units: Iterable[str] | None, known: Iterable[str]) -> list[str]:
    """``units=`` as exact units or prefixes (``adapted/glm-5.3`` = every unit of that set)."""
    known = sorted(known)
    if not units:
        return known
    out: set[str] = set()
    for u in units:
        u = u.strip("/")
        hit = [k for k in known if k == u or k.startswith(u + "/")]
        if not hit:
            raise DataError(f"{u}: no such unit (no sidecar under data/ matches it)")
        out.update(hit)
    return sorted(out)


def _note(msg: str) -> None:
    print(f"wsbench data: {msg}", file=sys.stderr)  # progress never goes to stdout (R7)


# ----------------------------------------------------------------------------------- fetching
def _base_url() -> str:
    return os.environ.get("WSB_DATA_URL") or BASE_URL


def repos(root: Path | str | None = None) -> list[str]:
    """Where a zip is looked for: ``INDEX.json``'s repo, then ``$WSB_DATA_REPO`` (a fork)."""
    idx = index(root) or {}
    out = [r for r in (idx.get("repo"), os.environ.get(DATA_REPO_ENV)) if r]
    return list(dict.fromkeys(out))


def asset_url(repo: str, release: str, sha: str) -> str:
    return f"{_base_url()}/{repo}/releases/download/{release}/{sha}.zip"


def _open(url: str, method: str) -> Any:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT)  # anonymous: no token is ever sent


def _retrying(url: str, method: str) -> bytes | None:
    """The body (b"" for HEAD), or None when the URL does not exist; other failures are retried
    and then raised."""
    last: Exception | None = None
    for attempt in range(TRIES):
        if attempt:
            time.sleep(BACKOFF * 2 ** (attempt - 1))
        try:
            with _open(url, method) as r:
                return b"" if method == "HEAD" else bytes(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            last = e
        except urllib.error.URLError as e:
            if isinstance(e.reason, FileNotFoundError):
                return None  # a file:// mirror without the asset
            last = e
        except OSError as e:
            last = e
    raise OSError(f"{type(last).__name__}: {last}")


def fetch(url: str) -> bytes | None:
    """GET ``url`` (the download seam): bytes, None when absent, ``OSError`` when unreachable."""
    return _retrying(url, "GET")


def head(url: str) -> bool:
    """Whether ``url`` exists (the HEAD seam); unreachable counts as absent."""
    try:
        return _retrying(url, "HEAD") is not None
    except OSError:
        return False


def _cached(sha: str) -> Path | None:
    path = cache_path(sha)
    if not path.is_file():
        return None
    if file_sha(path) == sha:
        return path
    with contextlib.suppress(OSError):
        path.unlink()  # a corrupt / partial cache file: drop it and fetch again
    return None


def _ways_out(sha: str) -> str:
    return (
        f"Ways out: seed the cache with the zip at {cache_path(sha)}; or set {NO_UNPACK_ENV}=1 "
        "to run on the plaintext that is already in the tree."
    )


def get_zip(unit: str, release: str, sha: str, root: Path | str | None = None) -> Path:
    """The unit's zip in the cache, downloaded when missing; its sha256 must be ``sha``."""
    path = _cached(sha)
    if path is not None:
        return path
    tried: list[str] = []
    for repo in repos(root):
        url = asset_url(repo, release, sha)
        try:
            blob = fetch(url)
        except OSError as e:
            tried.append(f"{url} ({e})")
            continue
        if blob is None:
            tried.append(f"{url} (not found)")
            continue
        got = hashlib.sha256(blob).hexdigest()
        if got != sha:
            raise DataError(
                f"{unit}: the zip at {url} has sha256 {got}, the sidecar pins {sha}. "
                + _ways_out(sha)
            )
        path = cache_path(sha)
        _atomic_write(path, blob)
        return path
    where = "; ".join(tried) or f"no repo in {INDEX} and no ${DATA_REPO_ENV}"
    raise DataError(
        f"{unit}: cannot get its zip {sha}.zip: {where}. If the sidecar was packed on another "
        "machine and never published, run `wsbench pack publish` there. " + _ways_out(sha)
    )


# ------------------------------------------------------------------------------------ zips
def _read_zip(unit: str, path: Path, names: Iterable[str]) -> dict[str, bytes]:
    import pyzipper  # lazy: Modal images mount src/wsbench without it

    try:
        with pyzipper.AESZipFile(path) as z:
            z.setpassword(PASSWORD)
            return {n: z.read(n) for n in names}
    except (KeyError, RuntimeError, OSError, ValueError, pyzipper.BadZipFile) as e:
        raise DataError(f"{unit}: cannot read {path}: {type(e).__name__}: {e}") from e


def _build_zip(root: Path, files: Iterable[str], canary_line: str) -> tuple[str, int]:
    """An AES-256 zip of ``files`` plus ``CANARY.txt`` in the cache; ``(sha256, bytes)``."""
    import pyzipper  # lazy, as in _read_zip

    cache_dir().mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(cache_dir() / "build.zip")
    try:
        with pyzipper.AESZipFile(
            tmp, "w", compression=pyzipper.ZIP_DEFLATED, compresslevel=9, encryption=pyzipper.WZ_AES
        ) as z:
            z.setpassword(PASSWORD)
            z.writestr(CANARY_FILE, canary_line + "\n")
            for rel in sorted(files):
                z.write(root / rel, rel)
        sha = file_sha(tmp)
        size = tmp.stat().st_size
        os.replace(tmp, cache_path(sha))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return sha, size


# ---------------------------------------------------------------------------------- unpack
CONFLICT_RECIPE = (
    "Recipe: copy the edited files aside, `wsbench unpack force=True`, copy them back, "
    "`wsbench pack`."
)
UNPUBLISHED = (
    "the zip these files were last unpacked / packed from is in neither the cache nor a "
    "release, so overwriting them would lose it: run `wsbench pack publish` on the branch "
    "that packed it (or `wsbench unpack force=True` to discard it)"
)


def _durable(entry: dict[str, Any], root: Path) -> bool:
    """Ruling R5: is the stamp's zip of a unit recoverable (cache, else a HEAD on a release)?"""
    sha, release = entry.get("zip_sha256"), entry.get("release")
    if not sha:
        return False
    if _cached(sha) is not None:
        return True
    return bool(release) and any(head(asset_url(r, release, sha)) for r in repos(root))


def _unpack_unit(
    root: Path, unit: str, side: dict[str, Any] | None, old: dict[str, Any] | None, force: bool
) -> tuple[list[str], list[str], int, int]:
    """One unit's three-way update: ``(edited, unpublished, n_written, n_deleted)``; nothing of
    the unit is written when either conflict list is non-empty."""
    files: dict[str, str] = side["files"] if side else {}
    old_files: dict[str, str] = (old or {}).get("files", {})
    writes: list[str] = []
    deletes: list[str] = []
    replaced: list[str] = []  # stamp-unchanged files to overwrite or delete: R5 applies
    edited: list[str] = []
    for path, want in files.items():
        have = _sha_or_none(root / path)
        if have == want:
            continue
        if have is None or force:
            writes.append(path)
        elif have == old_files.get(path):
            writes.append(path)
            replaced.append(path)
        else:
            edited.append(path)
    for path, was in old_files.items():
        if path in files:
            continue  # a stale path: the new sidecar no longer lists it
        have = _sha_or_none(root / path)
        if have is None:
            continue
        if force:
            deletes.append(path)
        elif have == was:
            deletes.append(path)
            replaced.append(path)
        else:
            edited.append(path)
    if edited:
        return sorted(edited), [], 0, 0
    if replaced and not _durable(old or {}, root):
        return [], sorted(replaced), 0, 0
    if writes:
        assert side is not None
        blobs = _read_zip(unit, get_zip(unit, side["release"], side["zip_sha256"], root), writes)
        for path in writes:
            if hashlib.sha256(blobs[path]).hexdigest() != files[path]:
                raise DataError(f"{unit}: {path} in its zip does not match the sidecar's sha256")
        for path in writes:
            try:
                _atomic_write(root / path, blobs[path])
            except OSError as e:
                raise DataError(
                    f"{unit}: cannot write {path} ({e.strerror or e}): the plaintext is absent "
                    "or stale and this checkout is not writable. Run `wsbench unpack` in a "
                    "writable checkout."
                ) from e
    for path in deletes:
        p = root / path
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
        for parent in p.parents:  # leave no empty directory behind, up to the scope
            if parent == root / PurePosixPath(path).parts[0]:
                break
            try:
                parent.rmdir()
            except OSError:
                break
    return [], [], len(writes), len(deletes)


def _has_missing(root: Path, side: dict[str, Any] | None) -> bool:
    return side is not None and any(not (root / p).is_file() for p in side["files"])


def unpack(
    units: Iterable[str] | None = None, force: bool = False, root: Path | str | None = None
) -> dict[str, int]:
    """Bring the working tree to the sidecars (plan 0097 section 2 + rulings R5 / R7): every unit
    whose sidecar differs from the stamp, or that misses a listed file, or all with ``force``."""
    root = _root(root)
    sides = load_sidecars(root)
    stamp = load_stamp(root)
    done: dict[str, Any] = stamp["units"]
    todo = [
        u
        for u in _select(units, set(sides) | set(done))
        if force or _differs(sides.get(u), done.get(u)) or _has_missing(root, sides.get(u))
    ]
    edited: dict[str, list[str]] = {}
    unpublished: dict[str, list[str]] = {}
    errors: list[str] = []
    written = deleted = 0
    if len(todo) > 20:
        _note(f"unpacking {len(todo)} unit(s) into {root} ...")

    def one(unit: str) -> tuple[str, Any]:
        try:
            return unit, _unpack_unit(root, unit, sides.get(unit), done.get(unit), force)
        except DataError as e:
            return unit, e

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, todo))
    for unit, res in results:
        if isinstance(res, DataError):
            errors.append(str(res))
            continue
        ed, unp, w, d = res
        if ed:
            edited[unit] = ed
        elif unp:
            unpublished[unit] = unp
        else:
            written += w
            deleted += d
            if unit in sides:
                done[unit] = _entry(sides[unit])
            else:
                done.pop(unit, None)
    # before the cutover (no sidecar, nothing ever unpacked) no stamp file is written
    tracked = bool(sides or done or todo)
    changed = tracked and (bool(todo) or stamp.get("sidecars_sha256") != sidecars_digest(root))
    if changed and not _write_stamp(root, stamp, sides) and (written or deleted):
        errors.append(f"cannot write {STAMP} (read-only checkout?)")
    if written or deleted:
        _note(f"unpacked {len(todo)} unit(s): {written} file(s) written, {deleted} deleted")
    problems = list(errors)
    if edited:
        lines = [f"  {u}: {p}" for u, ps in sorted(edited.items()) for p in ps]
        problems.append(
            "conflict: these files were edited locally and their sidecar changed too; nothing "
            "of their units was written.\n" + "\n".join(lines) + "\n" + CONFLICT_RECIPE
        )
    if unpublished:
        lines = [f"  {u}: {p}" for u, ps in sorted(unpublished.items()) for p in ps]
        problems.append(f"conflict: {UNPUBLISHED}.\n" + "\n".join(lines))
    if problems:
        raise DataError("\n".join(problems))
    return {"units": len(todo), "written": written, "deleted": deleted}


@contextlib.contextmanager
def _lock(root: Path) -> Iterator[None]:
    """Best-effort ``flock`` (in the cache, not the tree); correctness never depends on it."""
    handle = None
    try:
        import fcntl

        cache_dir().mkdir(parents=True, exist_ok=True)
        tag = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
        handle = (cache_dir() / f"unpack-{tag}.lock").open("w")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass
    try:
        yield
    finally:
        if handle is not None:
            handle.close()


def _stamp_current(root: Path) -> bool:
    return load_stamp(root).get("sidecars_sha256") == sidecars_digest(root)


def ensure(root: Path | str | None = None) -> None:
    """Unpack when the stamp is not current. A no-op without ``data/INDEX.json`` (a wheel, a
    Modal container with only ``src/``), under ``WSB_NO_UNPACK=1``, and before the cutover (no
    sidecar, never unpacked). Raises ``DataError``, never ``SystemExit``."""
    root = _root(root)
    if os.environ.get(NO_UNPACK_ENV) == "1" or not (root / INDEX).is_file():
        return
    if not _sidecar_files(root) and not (root / STAMP).is_file():
        return
    if _stamp_current(root):
        return
    with _lock(root):
        if not _stamp_current(root):  # another process may have finished meanwhile
            unpack(root=root)


def _is_cli() -> bool:
    return Path(sys.argv[0] or "").name in ("wsbench", "wsbench.exe") if sys.argv else False


def ensure_at_import() -> None:
    """``wsbench.registry``'s import-time call. In a ``wsbench`` CLI process a ``DataError`` is
    kept in ``DEFERRED`` for ``cli.main`` (exit 2 with the message), and ``wsbench unpack`` /
    ``wsbench pack`` skip the automatic unpack: they are how a conflict is resolved."""
    global DEFERRED
    cli = _is_cli()
    if cli and len(sys.argv) > 1 and sys.argv[1] in DATA_COMMANDS:
        return
    try:
        ensure()
    except DataError as e:
        if not cli:
            raise
        DEFERRED = e


# ------------------------------------------------------------------------------------ pack
def _walk(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(p for p in path.rglob("*") if p.is_file() and ".wsbtmp." not in p.name)


def _rel(root: Path, pattern: str) -> str:
    p = Path(pattern)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            raise DataError(f"{pattern}: outside the checkout {root}") from None
    return PurePosixPath(pattern.replace("\\", "/")).as_posix()


def expand(patterns: Iterable[str], root: Path | str | None = None) -> list[str]:
    """``add=`` paths, directories and globs -> the packable files they name. A directory or a
    glob skips the never-packed files; naming one explicitly is an error."""
    root = _root(root)
    out: set[str] = set()
    for raw in patterns:
        pat = _rel(root, raw)
        if _GLOB_CHARS & set(pat):
            hits = [f for m in sorted(root.glob(pat)) for f in _walk(m)]
            explicit = False
        else:
            hits = _walk(root / pat)
            explicit = (root / pat).is_file()
        if not hits:
            raise DataError(f"{raw}: matches no file under {root}")
        for f in hits:
            rel = f.relative_to(root).as_posix()
            if never_packed(rel):
                if explicit:
                    raise DataError(
                        f"{rel}: never packed (Markdown under evals/, adapted/README.md and "
                        ".report.lock stay out of the zips)"
                    )
                continue
            unit_of(rel)  # raises for a path outside evals/<entry>, adapted/<model>/<entry>
            out.add(rel)
    return sorted(out)


def _matches(path: str, pat: str) -> bool:
    pat = pat.rstrip("/")
    return path == pat or path.startswith(pat + "/") or fnmatch.fnmatchcase(path, pat)


def _require_unpacked(sides: dict[str, dict[str, Any]], stamp: dict[str, Any]) -> None:
    behind = [u for u in sorted(sides) if _differs(sides[u], stamp["units"].get(u))]
    if behind:
        more = f" (+{len(behind) - 5} more)" if len(behind) > 5 else ""
        raise DataError(
            f"run `wsbench unpack` first: {len(behind)} unit(s) are not unpacked from the "
            f"current sidecars ({', '.join(behind[:5])}{more}); packing now would read their "
            "missing or stale files as edits."
        )


def _default_release(sides: dict[str, dict[str, Any]]) -> str:
    tags = [s["release"] for s in sides.values() if _RELEASE_RE.match(s.get("release") or "")]
    return max(tags, default=FIRST_RELEASE)


def pack(
    units: Iterable[str] | None = None,
    add: Iterable[str] | None = None,
    rm: Iterable[str] | None = None,
    root: Path | str | None = None,
) -> list[str]:
    """Re-zip every unit whose listed files changed (plain ``pack`` never touches the network).
    ``add`` / ``rm`` edit the lists first; a unit with no sidecar is born by ``add`` (R8).
    Returns the units whose sidecar was written or removed."""
    root = _root(root)
    sides = load_sidecars(root)
    stamp = load_stamp(root)
    _require_unpacked(sides, stamp)
    lists: dict[str, set[str]] = {u: set(s["files"]) for u, s in sides.items()}
    touched: set[str] = set()
    for pat in rm or ():
        pat = _rel(root, pat)
        hits = [(u, p) for u, ps in lists.items() for p in ps if _matches(p, pat)]
        if not hits:
            raise DataError(f"rm={pat}: no listed file matches")
        for u, p in hits:
            lists[u].discard(p)
            touched.add(u)
    for path in expand(add or (), root):
        u = unit_of(path)
        if path not in lists.setdefault(u, set()):
            lists[u].add(path)
            touched.add(u)
    selected = set(_select(units, lists) if units else lists) | touched
    plan: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for unit in sorted(selected):
        shas: dict[str, str] = {}
        for path in sorted(lists[unit]):
            sha = _sha_or_none(root / path)
            if sha is None:
                missing.append(f"{unit}: {path}")
            else:
                shas[path] = sha
        if unit in touched or unit not in sides or shas != sides[unit]["files"]:
            plan[unit] = shas
    if missing:
        raise DataError(
            "listed file(s) missing from the working tree (restore them, or drop them with "
            "`wsbench pack rm=<path>`):\n  " + "\n  ".join(missing)
        )
    if not plan:
        return []
    line = canary(root)
    release = _default_release(sides)

    def build(unit: str) -> tuple[str, dict[str, Any] | None]:
        shas = plan[unit]
        if not shas:
            return unit, None  # every file removed: the unit goes
        sha, size = _build_zip(root, shas, line)
        return unit, {
            "unit": unit,
            "release": sides.get(unit, {}).get("release") or release,
            "zip_sha256": sha,
            "zip_bytes": size,
            "files": shas,
        }

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        built = list(pool.map(build, sorted(plan)))
    for unit, side in built:
        if side is None:
            sidecar_path(root, unit).unlink(missing_ok=True)
            sides.pop(unit, None)
            stamp["units"].pop(unit, None)
            continue
        _atomic_write(sidecar_path(root, unit), _dump(side))
        sides[unit] = side
        stamp["units"][unit] = _entry(side)
    _write_stamp(root, stamp, sides)
    _note(
        f"packed {len(built)} unit(s) into {cache_dir()}; they exist only there until "
        "`wsbench pack publish`"
    )
    return [u for u, _ in built]


def status(units: Iterable[str] | None = None, root: Path | str | None = None) -> dict[str, Any]:
    """Per unit: listed files that differ from the sidecar (``modified``), are gone
    (``missing``), and files beside them no sidecar lists (``unlisted``); plus the entries of
    ``evals/`` and ``adapted/<model>/`` that have no sidecar at all (``unlisted_entries``)."""
    root = _root(root)
    sides = load_sidecars(root)
    out: dict[str, dict[str, list[str]]] = {}
    for unit in _select(units, sides):
        files = sides[unit]["files"]
        rec: dict[str, list[str]] = {"modified": [], "missing": [], "unlisted": []}
        for path, want in sorted(files.items()):
            have = _sha_or_none(root / path)
            if have is None:
                rec["missing"].append(path)
            elif have != want:
                rec["modified"].append(path)
        for p in _walk(root / unit):
            rel = p.relative_to(root).as_posix()
            if rel not in files and not never_packed(rel):
                rec["unlisted"].append(rel)
        if any(rec.values()):
            out[unit] = rec
    entries: list[str] = []
    if not units:
        tops = [root / "evals", *sorted(p for p in (root / "adapted").glob("*") if p.is_dir())]
        for top in tops:
            for p in sorted(top.glob("*")):
                rel = p.relative_to(root).as_posix()
                if rel in sides or never_packed(rel):
                    continue
                inside = [p] if p.is_file() else (f for f in p.rglob("*") if f.is_file())
                if any(not never_packed(f.relative_to(root).as_posix()) for f in inside):
                    entries.append(rel)
    return {"units": out, "unlisted_entries": entries}


def dirty(report: dict[str, Any]) -> bool:
    """The "forgot to pack" condition: any listed file modified or missing."""
    return any(r["modified"] or r["missing"] for r in report["units"].values())


def format_status(report: dict[str, Any], limit: int = 20) -> str:
    lines: list[str] = []
    for kind, mark in (("modified", "M"), ("missing", "!"), ("unlisted", "?")):
        for unit in sorted(report["units"]):
            lines += [f"{mark} {p}" for p in report["units"][unit][kind]]
    entries = report["unlisted_entries"]
    lines += [f"? {e} (no sidecar)" for e in entries[:limit]]
    if len(entries) > limit:
        lines.append(f"? ... and {len(entries) - limit} more entries without a sidecar")
    if dirty(report):
        lines.append("listed files differ from their sidecars: run `wsbench pack`")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ diff
def git(root: Path, args: list[str], stdin: bytes | None = None) -> bytes:
    """Read-only git (the seam ``diff`` uses); ``DataError`` when git fails."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args], input=stdin, capture_output=True, check=False
        )
    except OSError as e:
        raise DataError(f"git: {e}") from e
    if r.returncode:
        raise DataError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def sidecars_at(ref: str, root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """The sidecars as committed at ``ref`` (none before the cutover commit)."""
    root = _root(root)
    listing = git(root, ["ls-tree", "-r", "-z", ref, "--", "data/evals", "data/adapted"])
    out: dict[str, dict[str, Any]] = {}
    for row in listing.decode("utf-8").split("\0"):
        if not row:
            continue
        meta, path = row.split("\t", 1)
        if not path.endswith(".json"):
            continue
        side = json.loads(git(root, ["cat-file", "blob", meta.split()[2]]))
        out[side["unit"]] = side
    return out


def diff(
    ref: str = "HEAD",
    units: Iterable[str] | None = None,
    text: bool = False,
    root: Path | str | None = None,
) -> tuple[list[tuple[str, str]], str]:
    """Files added (A) / removed (D) / changed (M) between the sidecars at ``ref`` and the
    working tree's listed files (a listed file gone from disk is ``!``); with ``text`` a unified
    diff of each changed text file against the version in ``ref``'s zip (fetched when not
    cached). Returns ``(rows, text diff)``."""
    root = _root(root)
    then = sidecars_at(ref, root)
    now = load_sidecars(root)
    keep = set(_select(units, set(then) | set(now)))
    old = {p: (u, s) for u, side in then.items() if u in keep for p, s in side["files"].items()}
    new = {p for u, side in now.items() if u in keep for p in side["files"]}
    rows: list[tuple[str, str]] = []
    changed: list[str] = []
    for path in sorted(set(old) | new):
        if path not in new:
            rows.append(("D", path))
            continue
        have = _sha_or_none(root / path)
        if have is None:
            rows.append(("!", path))
        elif path not in old:
            rows.append(("A", path))
        elif have != old[path][1]:
            rows.append(("M", path))
            changed.append(path)
    chunks: list[str] = []
    if text and changed:
        by_unit: dict[str, list[str]] = {}
        for path in changed:
            by_unit.setdefault(old[path][0], []).append(path)
        for unit, paths in sorted(by_unit.items()):
            side = then[unit]
            blobs = _read_zip(unit, get_zip(unit, side["release"], side["zip_sha256"], root), paths)
            for path in paths:
                try:
                    a = blobs[path].decode("utf-8").splitlines(keepends=True)
                    b = (root / path).read_bytes().decode("utf-8").splitlines(keepends=True)
                except UnicodeDecodeError:
                    chunks.append(f"Binary files a/{path} and b/{path} differ\n")
                    continue
                body = "".join(
                    line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                    for line in difflib.unified_diff(a, b, f"a/{path}", f"b/{path}")
                )
                chunks.append(body)
    return rows, "".join(chunks)


# --------------------------------------------------------------------------------- publish
def gh(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    """Run the GitHub CLI (the seam ``publish`` uses): ``(returncode, stdout, stderr)``."""
    try:
        r = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    except OSError as e:
        raise DataError(f"`gh` is needed to publish and could not be run: {e}") from e
    return r.returncode, r.stdout, r.stderr


def _gh(args: list[str], stdin: str | None = None) -> str:
    code, out, err = gh(args, stdin)
    if code:
        raise DataError(f"gh {' '.join(args[:4])} ...: {err.strip() or out.strip()}")
    return out


def _releases(repo: str) -> list[str]:
    rows = json.loads(
        _gh(["release", "list", "--repo", repo, "--limit", "1000", "--json", "tagName"]) or "[]"
    )
    return sorted(r["tagName"] for r in rows if _RELEASE_RE.match(r.get("tagName") or ""))


def _assets(repo: str, tag: str) -> dict[str, tuple[int, str]]:
    raw = json.loads(_gh(["release", "view", tag, "--repo", repo, "--json", "assets"]))
    return {a["name"]: (int(a["size"]), a.get("state", "")) for a in raw.get("assets", [])}


def release_notes(root: Path) -> str:
    return (
        "WorkspaceBench evaluation data (password-protected zips; the password is in "
        "data/README.md of the repository).\n\n"
        "This is evaluation data. Please do not train on it, and do not republish it "
        "unpacked.\n\n" + canary(root) + "\n"
    )


def _create_release(repo: str, tag: str, root: Path) -> None:
    """Ruling R2: a non-draft release on an ORPHAN commit that holds only ``data/README.md``,
    reachable from the tag alone, so its source archives carry no bank text and no branch
    moves. The commit is made through the API (no local git write)."""

    def api(path: str, body: dict[str, Any]) -> dict[str, Any]:
        args = ["api", f"repos/{repo}/git/{path}", "--method", "POST", "--input", "-"]
        return json.loads(_gh(args, json.dumps(body)))

    readme = (root / "data" / "README.md").read_text(encoding="utf-8")
    blob = api("blobs", {"content": readme, "encoding": "utf-8"})["sha"]
    entry = {"path": "data/README.md", "mode": "100644", "type": "blob", "sha": blob}
    tree = api("trees", {"tree": [entry]})["sha"]
    msg = f"{tag}: WorkspaceBench data release (zips are release assets)"
    commit = api("commits", {"message": msg, "tree": tree, "parents": []})["sha"]
    api("refs", {"ref": f"refs/tags/{tag}", "sha": commit})
    _gh(
        ["release", "create", tag, "--repo", repo, "--target", commit, "--verify-tag",
         "--latest=false", "--title", tag, "--notes", release_notes(root)]
    )  # fmt: skip


def _next_tag(tags: list[str]) -> str:
    if not tags:
        return FIRST_RELEASE
    return f"data-{int(_RELEASE_RE.match(tags[-1]).group(1)) + 1:04d}"  # type: ignore[union-attr]


def publish(repo: str | None = None, root: Path | str | None = None) -> dict[str, list[str]]:
    """Upload every current sidecar's zip the release does not hold yet (rulings R2-R4), then
    download each new asset anonymously and check its sha256. Returns ``{"uploaded": units,
    "rewritten": units}``; a rewritten sidecar (rebuilt zip, or another release tag) must be
    committed."""
    root = _root(root)
    idx = index(root)
    repo = repo or (idx or {}).get("repo")
    if not repo:
        raise DataError(f"{INDEX}: no repo to publish to (pass repo=<owner/name>)")
    sides = load_sidecars(root)
    stamp = load_stamp(root)
    tags = _releases(repo)
    held: dict[str, dict[str, tuple[int, str]]] = {}

    def assets(tag: str) -> dict[str, tuple[int, str]]:
        if tag not in held:
            held[tag] = _assets(repo, tag) if tag in tags else {}
        return held[tag]

    pending: list[str] = []
    for unit in sorted(sides):
        side = sides[unit]
        got = assets(side["release"]).get(f"{side['zip_sha256']}.zip")
        if got != (side["zip_bytes"], "uploaded"):
            pending.append(unit)
    rewritten: set[str] = set()
    for unit in pending:  # every pending zip must be in the cache before anything is uploaded
        side = sides[unit]
        if _cached(side["zip_sha256"]) is not None:
            continue
        try:  # a fork's asset being mirrored upstream (R3)
            get_zip(unit, side["release"], side["zip_sha256"], root)
            continue
        except DataError:
            pass
        shas = {p: _sha_or_none(root / p) for p in side["files"]}
        if shas != side["files"]:
            raise DataError(
                f"{unit}: its zip is in neither the cache nor a release, and the working tree "
                "differs from the sidecar, so it cannot be rebuilt: run `wsbench pack` first"
            )
        sha, size = _build_zip(root, shas, canary(root))  # AES zips are not deterministic
        sides[unit] = side = {**side, "zip_sha256": sha, "zip_bytes": size}
        rewritten.add(unit)
        _note(f"{unit}: zip rebuilt from the tree; its sidecar's zip_sha256 changed (commit it)")
    uploads: dict[str, list[str]] = {}
    for unit in pending:
        side = sides[unit]
        name = f"{side['zip_sha256']}.zip"
        dest = side["release"]
        if dest not in tags or (name not in assets(dest) and len(assets(dest)) >= RELEASE_CAP):
            if tags and len(assets(tags[-1])) < RELEASE_CAP:
                dest = tags[-1]
            else:
                dest = _next_tag(tags)
                _note(f"creating release {dest} on {repo}")
                _create_release(repo, dest, root)
                tags.append(dest)
                held[dest] = {}
        if name in assets(dest):  # same name, wrong size or state: a broken upload
            _gh(["release", "delete-asset", dest, name, "--repo", repo, "--yes"])
        assets(dest)[name] = (side["zip_bytes"], "uploaded")
        uploads.setdefault(dest, []).append(unit)
        if dest != side["release"]:
            sides[unit] = {**side, "release": dest}
            rewritten.add(unit)
            _note(f"{unit}: published to {dest}; its sidecar's release changed (commit it)")
    for unit in sorted(rewritten):
        _atomic_write(sidecar_path(root, unit), _dump(sides[unit]))
        if unit in stamp["units"]:
            stamp["units"][unit] = _entry(sides[unit])
    if rewritten:
        _write_stamp(root, stamp, sides)
    for dest, batch in uploads.items():
        paths = [str(cache_path(sides[u]["zip_sha256"])) for u in batch]
        for i in range(0, len(paths), UPLOAD_BATCH):
            _note(f"uploading {min(i + UPLOAD_BATCH, len(paths))}/{len(paths)} to {dest}")
            _gh(["release", "upload", dest, *paths[i : i + UPLOAD_BATCH], "--repo", repo])

    def verify(unit: str) -> str | None:
        side = sides[unit]
        url = asset_url(repo, side["release"], side["zip_sha256"])
        try:
            blob = fetch(url)  # anonymous, as a fresh clone will download it
        except OSError as e:
            return f"{unit}: {url}: {e}"
        if blob is None:
            return f"{unit}: {url}: not found (a draft release is not anonymously readable)"
        got = hashlib.sha256(blob).hexdigest()
        return None if got == side["zip_sha256"] else f"{unit}: {url}: sha256 {got}"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        bad = [b for b in pool.map(verify, pending) if b]
    if bad:
        raise DataError("uploaded, but the anonymous download check failed:\n  " + "\n  ".join(bad))
    _note(f"published {len(pending)} zip(s) to {repo}; {len(sides) - len(pending)} already held")
    return {"uploaded": pending, "rewritten": sorted(rewritten)}
