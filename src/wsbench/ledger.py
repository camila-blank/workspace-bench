"""The spend ledger (plan 0059): a per-family ``spend.jsonl`` that survives a killed run.

``runner`` runs each family inside :func:`family` (a ``ContextVar`` holding the family's ``dest``
and name); every ``llm.Spend`` built under it registers here (id-keyed: ``Spend`` is an unhashable
dataclass). A daemon thread appends one cumulative snapshot line per active family to
``<dest>/spend.jsonl`` every :data:`INTERVAL` seconds, and at exit; :func:`family` writes a final
one when the family finishes. A reader sums the LAST snapshot of each ``run_id`` (one process =
one ``run_id``), so a SIGKILL loses at most one interval of tally. Spends built outside any
family (the runner's main-thread preflights, ``wsbench judge``) are not recorded.
"""

import atexit
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INTERVAL = 30.0  # seconds between periodic snapshots (tests patch it)
FILENAME = "spend.jsonl"
RUN_ID = f"{os.getpid()}-{time.time():.3f}"
_INT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "calls",
    "retries",
    "errors",
    "refusals",
)
_DICT_FIELDS = ("transient", "failed")


@dataclass
class _Family:
    dest: Path
    name: str
    spends: dict[int, Any] = field(default_factory=dict)  # id(spend) -> Spend
    lock: threading.Lock = field(default_factory=threading.Lock)


_CURRENT: ContextVar[_Family | None] = ContextVar("wsbench_ledger_family", default=None)
_ACTIVE: dict[int, _Family] = {}
_ACTIVE_LOCK = threading.Lock()
_WRITER: threading.Thread | None = None
_WAKE = threading.Event()


def register(spend: Any) -> None:
    """Record ``spend`` under the current family, if any (called by ``Spend.__post_init__``)."""
    fam = _CURRENT.get()
    if fam is not None:
        with fam.lock:
            fam.spends[id(spend)] = spend


def snapshot(fam: _Family) -> dict[str, Any]:
    """The cumulative tally of every Spend registered under ``fam``."""
    with fam.lock:
        spends = list(fam.spends.values())
    snap: dict[str, Any] = {
        "run_id": RUN_ID,
        "time": time.time(),
        "family": fam.name,
        "usd": sum(float(getattr(s, "usd", 0.0) or 0.0) for s in spends),
    }
    for k in _INT_FIELDS:
        snap[k] = sum(int(getattr(s, k, 0) or 0) for s in spends)
    for k in _DICT_FIELDS:
        merged: dict[str, int] = {}
        for s in spends:
            for status, n in dict(getattr(s, k, None) or {}).items():
                merged[status] = merged.get(status, 0) + int(n)
        snap[k] = merged
    return snap


def write(fam: _Family) -> None:
    """Append one snapshot line to ``<dest>/spend.jsonl``."""
    line = json.dumps(snapshot(fam), ensure_ascii=False) + "\n"
    try:
        fam.dest.mkdir(parents=True, exist_ok=True)
        with (fam.dest / FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as e:  # a ledger write must never fail the family
        print(f"  ledger: cannot write {fam.dest / FILENAME}: {e}")


def _flush_all() -> None:
    with _ACTIVE_LOCK:
        fams = list(_ACTIVE.values())
    for fam in fams:
        write(fam)


def _loop() -> None:
    while True:
        _WAKE.wait(INTERVAL)
        _WAKE.clear()
        _flush_all()


def _ensure_writer() -> None:
    global _WRITER
    with _ACTIVE_LOCK:
        if _WRITER is not None and _WRITER.is_alive():
            return
        _WRITER = threading.Thread(target=_loop, name="wsbench-ledger", daemon=True)
        _WRITER.start()


atexit.register(_flush_all)


@contextmanager
def family(dest: Path, name: str) -> Iterator[_Family]:
    """Run the body as family ``name`` writing its ledger to ``dest``; the final snapshot is
    written on exit (also on an exception)."""
    fam = _Family(Path(dest), name)
    token = _CURRENT.set(fam)
    with _ACTIVE_LOCK:
        _ACTIVE[id(fam)] = fam
    _ensure_writer()
    try:
        yield fam
    finally:
        _CURRENT.reset(token)
        with _ACTIVE_LOCK:
            _ACTIVE.pop(id(fam), None)
        write(fam)


def read_total(path: Path) -> dict[str, Any]:
    """Sum of the last snapshot of each ``run_id`` in a ``spend.jsonl`` (malformed lines are
    skipped): the family's total over every run that wrote to it."""
    last: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("run_id"), str):
            last[row["run_id"]] = row
    total: dict[str, Any] = {"n_runs": len(last), "usd": 0.0}
    total.update(dict.fromkeys(_INT_FIELDS, 0))
    total.update({k: {} for k in _DICT_FIELDS})
    for row in last.values():
        total["usd"] += float(row.get("usd") or 0.0)
        for k in _INT_FIELDS:
            total[k] += int(row.get(k) or 0)
        for k in _DICT_FIELDS:
            for status, n in (row.get(k) or {}).items():
                total[k][status] = total[k].get(status, 0) + int(n)
    return total
