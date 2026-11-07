"""Vendor HallucinationBench's index into ``evals/hallucinationbench/index.json`` (plan 0040).

    uv run --no-sync python scripts/vendor_hb_index.py \\
        SRC=<hallucination-bench>/outputs/index/claude-opus-5-5/index.json HB_SHA=<sha> [DST=…]

The only writer of the vendored file. It refuses (exit 2) an index whose ``git_sha`` is not
``HB_SHA`` (a 7+ hex prefix of it is accepted as the argument; the full sha is stored), or whose
``version`` is not ``hbindex.VERSION`` (``hindex-v8``, plan 0080). It keeps only the methods
named in ``evals/hallucinationbench/methods.json`` and, per per-layer row, ``method``, ``layer``,
``full`` / ``matched`` and each family's ``{v, ci, n, n_items}``. A joint row (HB layer
``"all"``, hindex-v8's 11-layer unit) goes to ``joint`` instead: ``method``, ``full_joint`` and
each family's ``{v, ci, n, n_items}``; it is never a per-layer row. The excluded-cell report
(``manifest.persistent_failures``, HB plan 0029 Amendment 4) is kept as per-entry counts. The
dev-box ``sources`` paths and the per-reader ``safety`` meta are dropped. A refresh is a new
commit with a new ``hb_sha``.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wsbench import hbindex  # noqa: E402

HB_REPO = "github.com/camila-blank/hallucination-bench"
FAMILY_KEYS = ("v", "ci", "n", "n_items")
JOINT_LAYER = "all"  # hindex-v8's joint 11-layer unit


def vendor(src: Path, hb_sha: str, methods_path: Path | None = None) -> dict[str, Any]:
    """The vendored payload; ``ValueError`` on a sha or version mismatch."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", hb_sha):
        raise ValueError(f"HB_SHA={hb_sha!r}: expected 7-40 lowercase hex")
    idx = json.loads(src.read_text(encoding="utf-8"))
    sha = str(idx.get("git_sha") or "")
    if not sha or not sha.startswith(hb_sha):
        raise ValueError(f"{src}: git_sha {sha or None} is not HB_SHA {hb_sha}")
    if idx.get("version") != hbindex.VERSION:
        raise ValueError(f"{src}: version {idx.get('version')!r} is not {hbindex.VERSION}")
    mapping = hbindex.methods(methods_path)
    keep = sorted(set(mapping["methods"].values()))
    present = {r.get("method") for r in idx.get("rows", [])}
    if missing := [m for m in keep if m not in present]:
        raise ValueError(f"{src}: methods.json names HB methods absent from the index: {missing}")
    rows, joint = [], []
    for r in idx["rows"]:
        if r.get("method") not in keep:
            continue
        fams = {
            fam: {k: cell[k] for k in FAMILY_KEYS if k in cell}
            for fam, cell in (r.get("families") or {}).items()
        }
        if r.get("layer") == JOINT_LAYER:
            j: dict[str, Any] = {"method": r["method"]}
            if isinstance(r.get("full_joint"), dict):
                j["full_joint"] = {"v": r["full_joint"]["v"], "ci": r["full_joint"]["ci"]}
            j["families"] = fams
            joint.append(j)
            continue
        row: dict[str, Any] = {"method": r["method"], "layer": int(r["layer"])}
        for key in ("full", "matched"):
            if isinstance(r.get(key), dict):
                row[key] = {"v": r[key]["v"], "ci": r[key]["ci"]}
        row["families"] = fams
        rows.append(row)
    rows.sort(key=lambda r: (r["method"], r["layer"]))
    joint.sort(key=lambda r: r["method"])
    notes = {
        k: v
        for k, v in (idx.get("notes") or {}).items()
        if k == hbindex.VERSION or k.split(".")[0] in keep
    }
    return {
        "hb_repo": HB_REPO,
        "hb_sha": sha,
        "version": idx["version"],
        "weights": idx.get("weights"),
        "families": list((idx.get("weights") or {}).get("family", {})),
        "notes": notes,
        "rows": rows,
        "joint": joint,
        "excluded": excluded(idx, keep),
    }


def excluded(idx: dict[str, Any], keep: list[str]) -> list[dict[str, Any]]:
    """HB plan 0029 Amendment 4: the cells `hbench final` excluded as persistent judge failures,
    per (family, method, unit), for the kept methods (empty when the index has no report)."""
    pf = (idx.get("manifest") or {}).get("persistent_failures") or {}
    keys = ("family", "method", "unit", "n_cells", "n_excluded", "share", "by_reason")
    return [
        {k: e[k] for k in keys if k in e} for e in pf.get("entries", []) if e.get("method") in keep
    ]


def main(argv: list[str]) -> int:
    kv = dict(a.split("=", 1) for a in argv if "=" in a)
    if not {"SRC", "HB_SHA"} <= set(kv) or len(kv) != len(argv):
        print(__doc__, file=sys.stderr)
        return 2
    dst = Path(kv.get("DST", str(hbindex.INDEX)))
    try:
        payload = vendor(Path(kv["SRC"]), kv["HB_SHA"])
    except (ValueError, OSError) as e:
        print(f"vendor_hb_index: {e}", file=sys.stderr)
        return 2
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {dst}: {len(payload['rows'])} rows, {payload['version']} @ {payload['hb_sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
