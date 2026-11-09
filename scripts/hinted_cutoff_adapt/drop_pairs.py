"""Plan 0084 R20: apply audited pair drops at the gw level, so the frozen file, the gate and the
bank stay consistent: the pairs leave ``pairs.json`` and ``date_leak.json``'s ``final`` gate is
recomputed on the kept pairs (``ccut.date_leak``, as ``pair`` computes it); the drops and their
reasons are kept under ``date_leak.audit_drops``. Then re-run read_jobs -> freeze -> score ->
finalize (the predictor calls are cached by pair id).

    HC_SUBJECT=<m> HC_GUESS_GAP=<the build's caliper, e.g. 20> \
        python subject_ccut.py drop_pairs out_dir=<dir> drops=<json {pair_id: reason}>

(without HC_GUESS_GAP the recomputed gate silently lacks the guessability rating)
"""

import json
import sys
from pathlib import Path

import ccut_stages as cs
from global_workspace.olens_suite.cf_influence import ccut

SEED = 20261103 + 5  # ccut_stages.Config.seed + 5, the gate's seed in pair
N_BOOT = 10000


def main() -> None:
    a = dict(x.split("=", 1) for x in sys.argv[1:])
    d = Path(a["out_dir"])
    cs.set_variant("v2", str(d))
    drops: dict[str, str] = json.loads(Path(a["drops"]).read_text())
    pairs = json.loads((d / "pairs.json").read_text())
    have = {p["pair_id"] for p in pairs}
    if missing := sorted(set(drops) - have):
        raise SystemExit(f"drop_pairs: not in pairs.json: {missing}")
    keep = [p for p in pairs if p["pair_id"] not in drops]
    bnk, sil, obs = cs._bank(), json.loads((d / "silent.json").read_text()), cs._obscure(d)

    def cand(k: str) -> ccut.Cand:
        it = sil[k]
        c = cs._v2_cand(
            bnk[it["qid"]],
            obs,
            key=k,
            template=it["template"],
            n_prompt=len(it["prompt_token_ids"]),
            n_reply=cs._reply_tokens(it),
        )
        assert c is not None, k
        return c

    cp = [(cand(p["I"]), cand(p["N"])) for p in keep]
    gate = ccut.date_leak(
        cp, ccut.clusters([bnk[x.qid]["event"] for x, _ in cp]), n_boot=N_BOOT, seed=SEED
    )
    leak = json.loads((d / "date_leak.json").read_text())
    leak["final_before_audit_drops"] = leak["final"]
    leak["final"] = gate
    leak["pass"] = leak["date_matched"] = bool(gate["pass"])
    leak["audit_drops"] = {**leak.get("audit_drops", {}), **drops}
    cs._dump(d / "date_leak.json", leak)
    cs._dump(d / "pairs.json", keep)
    print(
        f"[drop_pairs] {len(pairs)} -> {len(keep)} pairs; day {gate['day_auroc']:.3f} cell "
        f"{gate['cell_excess']:+.3f} "
        + " ".join(f"{r} {gate[f'{r}_auroc']:.3f}" for r in ccut.RATINGS)
    )


if __name__ == "__main__":
    main()
