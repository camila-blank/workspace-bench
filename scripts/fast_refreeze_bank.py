"""Cut a new fast-subset version for families whose REGISTERED bank changed bytes while their
item ids did not (evals/fast/README.md, "Re-freezing"): the new file is the source version with
ONLY the named families' ``bank_sha`` moved to the registered file's sha256. Units, read ids,
positions rules, family order and every other family's entry are the source's, so every other
family stays entry-compatible with the source version (``fast.compatible``) and its cached fast
runs and readouts sidecars remain valid; the named families are compatible with no earlier
version (their instrument changed: re-judge them).

    S=scripts/fast_refreeze_bank.py
    uv run python $S fast-v2 fast-v3 conjunctive_association
    uv run python $S fast-safety-v1 fast-safety-v2 conjunctive_association
    uv run python $S --check fast-v2 fast-v3 conjunctive_association

A frozen file is never edited: the script refuses an existing target (``--check`` compares it
byte for byte instead). It refuses, naming the family, when
- a named family's bank did not change, or an unnamed family's did (name every changed family:
  both subsets need a new version in the same PR);
- a named family's units or read ids are no longer the new bank's (a changed id set needs a
  re-selection on the new bank, not this script): read ids must be plan ids in plan order.

The new file is written in today's family keys (``fast.KEY_ERA`` "0101"). After writing: add
the version to ``fast.VERSIONS`` and ``fast.KEY_ERA``, point ``fast.DEFAULT`` / ``fast.VARIANTS``
at it, and add its pins to ``tests/test_fast_subset.py`` (file sha256, ``REFROZEN``) and its row
to the tables of ``evals/fast/README.md``.
"""

import argparse
import json
import sys
from pathlib import Path

from wsbench import fast, readplan, registry


def target_path(version: str) -> Path:
    return registry.REPO_ROOT / "evals/fast" / (version.replace("-", "_") + ".json")


def build(src: str, dst: str, families: list[str]) -> str:
    registry.load_all()
    doc = json.loads(json.dumps(fast._file(src)))  # the source, read in today's keys; a copy
    named = [registry.canonical(f) for f in families]
    missing = [f for f in named if f not in doc["families"]]
    if missing:
        raise SystemExit(f"{missing}: not in {src}")
    for fam, entry in doc["families"].items():
        changed = entry["bank_sha"] != fast.bank_sha(fam)
        if fam in named and not changed:
            raise SystemExit(f"{fam}: its registered bank is the one {src} froze; nothing to cut")
        if fam not in named and changed:
            raise SystemExit(f"{fam}: its registered bank changed since {src} too; name it")
    for fam in named:
        entry = doc["families"][fam]
        plan_ids = [s.id for s in readplan.plan(fam, layers=None)]
        order = {i: n for n, i in enumerate(plan_ids)}
        read_ids = entry["read_ids"]
        gone = [i for i in read_ids if i not in order]
        if gone:
            raise SystemExit(f"{fam}: read ids not in the new bank's plan, re-select: {gone[:5]}")
        if [order[i] for i in read_ids] != sorted(order[i] for i in read_ids):
            raise SystemExit(f"{fam}: read ids are not in the new plan's order; re-select")
        if "positions" not in entry:  # item-keyed units: each must still be a read id
            units = {u for group in entry["units"].values() for u in group}
            stray = sorted(u for u in units if u not in set(read_ids) and u not in order)
            if stray:
                raise SystemExit(f"{fam}: units not in the new bank, re-select: {stray[:5]}")
        entry["bank_sha"] = fast.bank_sha(fam)
    doc["version"] = dst
    return json.dumps(doc, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare the existing target instead")
    ap.add_argument("src", help="the version to start from, e.g. fast-v2")
    ap.add_argument("dst", help="the new version, e.g. fast-v3")
    ap.add_argument("families", nargs="+", help="every family whose registered bank changed")
    args = ap.parse_args(argv)
    path = target_path(args.dst)
    text = build(args.src, args.dst, args.families)
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            print(f"{path}: not what {args.src} + {args.families} gives", file=sys.stderr)
            return 1
        print(f"{path.name} is up to date")
        return 0
    if path.exists():
        raise SystemExit(f"{path} exists: a frozen file is never edited (pick a new version)")
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
