"""Plan 0084 §2: pick the pilot's base candidates from the cutoff probe and write the pilot dir's
``base_cands.json`` (ccut's format), so the ccut v2 stages run from ``screen_gen`` on.

    python pilot_select.py probe=<probe dir> out=<pilot dir> start=YYYY-MM end=YYYY-MM n=150

Base kinds are v2's rule on the probe's 10 no-hint samples (I <= 1 correct, N >= 9, <= 1
truncated). Only base-pairable candidates (the v2 pairable rule without the template term,
``ccut.pairable_qids``) come first, then other base candidates (seeded, about half I) up to
``n``; the guessable-answer exclusion (``ccut.v2_guessable``) is applied up front. The pilot
measures stage survivals, so non-pairable candidates are as informative as pairable ones.
"""

import json
import random
import sys
from pathlib import Path

import ccut_stages as cs
from global_workspace.olens_suite.cf_influence import ccut


def main() -> None:
    a = dict(x.split("=", 1) for x in sys.argv[1:])
    probe, out = Path(a["probe"]), Path(a["out"])
    out.mkdir(parents=True, exist_ok=True)
    cs.set_variant("v2", str(out))
    bnk = cs._bank()
    obs = json.loads((out / "obscure.json").read_text())
    items = json.loads((probe / "probe_items.json").read_text())
    by = {"I": [], "N": []}
    for qid, r in items.items():
        q = bnk.get(qid)
        if q is None or not (a["start"] <= r["month"] <= a["end"]) or ccut.v2_guessable(q):
            continue
        if r["unknown"] > ccut.BASE_MAX_UNKNOWN:
            continue
        kind = (
            "I"
            if r["correct"] <= ccut.BASE_I_MAX_CORRECT
            else "N"
            if r["correct"] >= ccut.V2_BASE_N_MIN_CORRECT
            else None
        )
        if kind and (c := cs._v2_cand(q, obs)) is not None:
            by[kind].append(c)
    pi, pn = ccut.pairable_qids(by["I"], by["N"], ccut.V2Match(), template=False)
    print(f"[pilot] base I {len(by['I'])} N {len(by['N'])}; pairable I {len(pi)} N {len(pn)}")
    n = int(a.get("n", 150))
    rng = random.Random(20261108)
    # every base-pairable candidate first, then the rest of each kind (seeded) up to n, half I
    keep: dict[str, str] = dict.fromkeys(pi, "I") | dict.fromkeys(pn, "N")
    rest = {k: sorted({c.qid for c in by[k]} - set(keep)) for k in ("I", "N")}
    for k in ("I", "N"):
        rng.shuffle(rest[k])
    while len(keep) < n and (rest["I"] or rest["N"]):
        k = (
            "I"
            if (sum(v == "I" for v in keep.values()) <= len(keep) / 2 and rest["I"])
            or not rest["N"]
            else "N"
        )
        keep[rest[k].pop()] = k
    cands = {q: {"kind": k, "base_correct": items[q]["correct"]} for q, k in sorted(keep.items())}
    cs._dump(out / "base_cands.json", cands)
    print(
        f"[pilot] kept {sum(k == 'I' for k in keep.values())} I, "
        f"{sum(k == 'N' for k in keep.values())} N"
    )


if __name__ == "__main__":
    main()
