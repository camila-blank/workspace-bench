"""Plan 0084 top-ups: a twin an earlier audit found defective never re-enters a re-paired set; emits
``{pair_id: reason}`` for every current pair holding such a twin (applied with ``drop_pairs.py``).

    python carry_forward.py <prior audit_drops.json> <build out_dir> <out drops.json>
"""

import json
import sys
from pathlib import Path


def flagged(prior: dict[str, str]) -> dict[str, str]:
    """Item key -> reason. The reason's wording names the defective twin: an N answer (the
    default), an I question / I twin, or both (the same event in both twins, both answers
    readable)."""
    out: dict[str, str] = {}
    for pid, reason in prior.items():
        i_key, n_key = pid.split("~")
        r = reason.lower()
        both = "same event" in r or "likewise readable" in r or "also readable" in r
        on_i = both or "i question" in r or "i twin" in r
        on_n = both or not on_i
        why = "prior audit (carried forward): " + reason.split(": ", 1)[-1]
        if on_i:
            out[i_key] = why
        if on_n:
            out[n_key] = why
    return out


def drops(prior: dict[str, str], pairs: list[dict]) -> dict[str, str]:
    """The current pairs that hold a flagged twin, with the prior reason."""
    bad = flagged(prior)
    out = {}
    for p in pairs:
        for k in (p["I"], p["N"]):
            if k in bad:
                out[p["pair_id"]] = bad[k]
    return out


def main() -> None:
    prior = json.loads(Path(sys.argv[1]).read_text())
    pairs = json.loads((Path(sys.argv[2]) / "pairs.json").read_text())
    out = drops(prior, pairs)
    Path(sys.argv[3]).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"[carry_forward] {len(flagged(prior))} flagged items; {len(out)} of {len(pairs)} hit")


if __name__ == "__main__":
    main()
