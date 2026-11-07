"""Re-derive the frozen ATI file under the panel rule (``ti.panel_verdict``, plan 0071) from each
entry's stored per-reader ``status``: no reader calls. ``--check`` exits 1 if the file differs.

    uv run python scripts/ati_rederive.py [--check] [--frozen PATH]
"""

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from wsbench.baselines import text_inversion as ti

DEFAULT_ORDER = ["qwen", "claude", "astra", "argon"]
# entries re-derived: the current gate, and the pre-plan-0071 label (read only for its statuses)
LEGACY_SOURCE = "panel-v1"
SOURCE_GATES = (LEGACY_SOURCE, ti.GATE_VERSION)


def _entries(data: dict[str, Any]) -> list[str]:
    """The panel entries; a construction-gated or plan-0014 entry is passed through unchanged."""
    return [
        f
        for f, e in data.items()
        if not f.startswith("_") and isinstance(e, dict) and e.get("gate") in SOURCE_GATES
    ]


def _width(entry: dict[str, Any]) -> int:
    return len(entry.get("status_order") or DEFAULT_ORDER)


def _derive(fam: str, entry: dict[str, Any]) -> dict[str, Any]:
    """ids / counts / exclusions of ``entry`` under ``ti.panel_verdict``, as ``_gate_panel``."""
    status = entry.get("status")
    if not isinstance(status, dict):
        raise SystemExit(f"{fam}: entry has no per-item status; cannot re-derive")
    width = _width(entry)
    for i, st in status.items():
        if len(st) != width:
            raise SystemExit(f"{fam}: status of {i} has width {len(st)}, the panel has {width}")
    verdict = {i: ti.panel_verdict(st[0], list(st[1:])) for i, st in status.items()}
    ati_all = [i for i in status if verdict[i] == "ati"]
    excl = {i: ti.ATI_EXCLUDE_ALL[fam] for i in ati_all if fam in ti.ATI_EXCLUDE_ALL}
    excl.update(ti.ATI_EXCLUDE.get(fam, {}))
    ati = [i for i in ati_all if i not in excl]
    inv = [i for i in status if verdict[i] == "invertible"]
    out: dict[str, Any] = {
        "ids": ati,
        "n_ati": len(ati),
        "n_invertible": len(inv),
        "n_unverified": len(status) - len(ati_all) - len(inv),
    }
    if fam in ti.ATI_MIN_RULE:
        out["ati_exclude"] = {i: excl[i] for i in ati_all if i in excl}
        out["ati_reportable"] = len(ati) > ti.MIN_ATI_ITEMS
    return out


def rederive(data: dict[str, Any]) -> dict[str, Any]:
    """The file re-derived under ``ti.panel_verdict`` (idempotent on a re-derived file)."""
    out: dict[str, Any] = {}
    panel = set(_entries(data))
    # a reason the code also states is refreshed from ``ti.NOT_GATED`` (what ``gate()`` merges
    # under the file's text), so a stale reason in the file cannot outlive its code edit
    not_gated = {k: ti.NOT_GATED.get(k, v) for k, v in dict(data.get("_not_gated", {})).items()}
    for fam, e in data.items():
        if fam not in panel:  # metadata, or a construction / plan-0014 entry: kept as is
            out[fam] = copy.deepcopy(e)
            continue
        new = _derive(fam, e)
        n = len(e["status"])
        if n and new["n_unverified"] / n > ti.MAX_REFUSED_SHARE:
            why = f"unverified: {new['n_unverified']}/{n} items undecided under the panel (> 20%)"
            if fam in ti.CRAFTED:
                raise SystemExit(f"{fam} (crafted ATI family): {why}; stop and report")
            not_gated[fam] = why
            continue
        if not set(new["ids"]) <= set(e["ids"]):
            raise SystemExit(f"{fam}: the re-derived ids are not a subset of the old ones")
        entry: dict[str, Any] = {}
        for k, v in e.items():
            if k == "gate":
                entry["gate"] = ti.GATE_VERSION
                prior = LEGACY_SOURCE if e["gate"] == LEGACY_SOURCE else e.get("rederived_from")
                if prior:  # gate() itself never writes rederived_from
                    entry["rederived_from"] = prior
            elif k == "rederived_from":
                continue
            else:
                entry[k] = new.get(k, v)
        out[fam] = entry
    if "_not_gated" in out or not_gated:
        out["_not_gated"] = not_gated
    src = out.get("_source")
    if isinstance(src, dict):
        rule = ti.panel_source(ti.DRAWS)["rule"]
        out["_source"] = {
            k: (ti.GATE_VERSION if k == "gate" else rule if k == "rule" else v)
            for k, v in src.items()
        }
    return out


def dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frozen", type=Path, default=ti.FROZEN)
    ap.add_argument("--check", action="store_true", help="exit 1 if the file differs")
    args = ap.parse_args(argv)
    text = args.frozen.read_text(encoding="utf-8")
    data = json.loads(text)
    new = dump(rederive(data))
    if args.check:
        if new != text:
            print(f"{args.frozen}: differs from its re-derivation; run scripts/ati_rederive.py")
            return 1
        print(f"{args.frozen}: matches its re-derivation")
        return 0
    args.frozen.write_text(new, encoding="utf-8")
    after = json.loads(new)
    for fam in _entries(data):
        now = len(after[fam]["ids"]) if fam in after else "not gated"
        print(f"{fam}: {len(data[fam]['ids'])} -> {now}")
    print(f"wrote {args.frozen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
