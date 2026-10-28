"""Native vs summary mode (plan 0018 D3), per family, for one method: paired rates, a paired
bootstrap CI of the difference, retention, and the catastrophe flag.

    python scripts/summary_mode_compare.py native=DIR summary=DIR method=LABEL \
        [exclude=dev_ids.json] [out=PREFIX]

``DIR`` is a ``wsbench run`` out dir (``<family>/results.json``). ``exclude`` is a JSON
``{family: [ids]}`` (the D2 dev slice), dropped from the headline and reported separately."""

import json
import random
import sys
from pathlib import Path
from typing import Any

from wsbench import digest
from wsbench.family import span_in

MIN_N = 20  # results.ATI_MACRO_MIN_ITEMS: smaller pairs are shown, never flagged
N_BOOT = 2000

# (native prompt_version base, summary base before "+digest") pairs that differ by design
BASE_PAIRS: dict[str, dict[str, str]] = {
    # summary mode judges the digest with the single-cell prompt; native cells=all is batched
    "arithmetic_intermediates": {"arith-free-batched-2026-09-23": "arith-free-2026-09-23"},
}
MT_FAMILIES = {
    "basic_readout_mt",
    "multihop_mt",
    "multihop_mt_v2",
    "multilingual_mt",
    "multilingual_multihop",
    "multilingual_typo",
    "typo_mt",
}
# config keys that must match between the two runs (everything but the summary stamp)
SAME_KEYS = ("layers", "scope", "cells", "items", "limit")


def _load(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(root.glob("*/results.json")):
        out[p.parent.name] = json.loads(p.read_text())
    return out


def _base(pv: str) -> str:
    return pv.split("+digest-", 1)[0]


def check_pair(fam: str, nat: dict, summ: dict) -> list[str]:
    """Problems that make a pair incomparable (empty = comparable)."""
    probs: list[str] = []
    if summ["config"].get("aggregate") != "summary":
        probs.append(f"summary run is {summ['config'].get('aggregate')!r}")
    if nat["config"].get("aggregate") == "summary":
        probs.append("native run is a summary run")
    npv, spv = nat["config"].get("prompt_version", ""), summ["config"].get("prompt_version", "")
    want = BASE_PAIRS.get(fam, {}).get(npv, npv)
    if fam in MT_FAMILIES and npv.startswith("mt-regex-summarized"):
        want = npv.replace("mt-regex-summarized", "mt-regex")
    if _base(spv) != want:
        probs.append(f"prompt_version {npv!r} vs {spv!r} (expected summary base {want!r})")
    for k in SAME_KEYS:
        if nat["config"].get(k) != summ["config"].get(k):
            probs.append(f"config {k}: {nat['config'].get(k)!r} != {summ['config'].get(k)!r}")
    return probs


def native_pass(fam: str, row: dict) -> bool | None:
    """The native verdict paired with the summary verdict. chain: the any-of-3 rule (summary
    mode's rule), not top-1 (plan rev 5)."""
    if row.get("pass") is None:
        return None
    if fam == "chain_intermediates":
        return bool(row.get("any_of_3"))
    return bool(row["pass"])


def _native_quotes(row: dict) -> list[str]:
    """Verified quotes of a native row's passing cells (basic-style ``layers`` verdicts)."""
    out: list[str] = []
    for v in (row.get("layers") or {}).values():
        if (
            isinstance(v, dict)
            and v.get("expressed")
            and v.get("quote_ok", True)
            and v.get("quote")
        ):
            out.append(str(v["quote"]))
    return out


def retained(nat_row: dict, summ_row: dict) -> bool | None:
    """Is any native hitting quote inside one of the summary's evidence samples (decoded)? None
    when the native row records no quotes (retention not measurable for that family)."""
    quotes = _native_quotes(nat_row)
    if not quotes:
        return None
    evid = [digest.decode_bpe(e) for e in summ_row.get("summary_evidence") or []]
    return any(span_in(digest.decode_bpe(q), e) for q in quotes for e in evid)


def paired_ci(pairs: list[tuple[bool, bool]], seed: int = 0) -> tuple[float, float] | None:
    """95% bootstrap CI of mean(summary) - mean(native) over paired items."""
    if not pairs:
        return None
    rng = random.Random(seed)
    n = len(pairs)
    diffs = []
    for _ in range(N_BOOT):
        s = [pairs[rng.randrange(n)] for _ in range(n)]
        diffs.append(sum(b for _a, b in s) / n - sum(a for a, _b in s) / n)
    diffs.sort()
    return diffs[int(0.025 * N_BOOT)], diffs[int(0.975 * N_BOOT) - 1]


def compare(
    native: dict[str, dict], summary: dict[str, dict], exclude: dict[str, list[str]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fam in sorted(set(native) | set(summary)):
        if fam not in native or fam not in summary:
            out.append({"family": fam, "status": "not defined (one side missing)"})
            continue
        nat, summ = native[fam], summary[fam]
        if summ["config"].get("aggregate") == "native":
            out.append({"family": fam, "status": "native-only family"})
            continue
        probs = check_pair(fam, nat, summ)
        if probs:
            out.append({"family": fam, "status": "incomparable", "problems": probs})
            continue
        nrows = {r.get("id", r.get("unit")): r for r in nat.get("rows", [])}
        srows = {r.get("id", r.get("unit")): r for r in summ.get("rows", [])}
        drop = set(exclude.get(fam, []))
        pairs: list[tuple[bool, bool]] = []
        dev_pairs: list[tuple[bool, bool]] = []
        ret: list[bool] = []
        for i, sr in srows.items():
            nr = nrows.get(i)
            if nr is None or sr.get("pass") is None:
                continue
            npass = native_pass(fam, nr)
            if npass is None:
                continue
            pair = (npass, bool(sr["pass"]))
            (dev_pairs if i in drop else pairs).append(pair)
            if i not in drop and npass:
                r = retained(nr, sr)
                if r is not None:
                    ret.append(r)
        n = len(pairs)
        nat_rate = sum(a for a, _b in pairs) / n if n else None
        summ_rate = sum(b for _a, b in pairs) / n if n else None
        ci = paired_ci(pairs)
        chance = summ.get("numbers", {}).get("chance")
        flags: list[str] = []
        if n >= MIN_N and nat_rate is not None and summ_rate is not None:
            if summ_rate < 0.5 * nat_rate and ci is not None and ci[1] < 0:
                flags.append("summary < 0.5x native (CI excludes 0)")
            if nat_rate - summ_rate > 0.30:
                flags.append("drop > 0.30")
            if chance is not None and summ_rate <= chance:
                flags.append("at or below chance")
        out.append(
            {
                "family": fam,
                "status": "ok" if n >= MIN_N else "n too small to flag",
                "n": n,
                "native": nat_rate,
                "summary": summ_rate,
                "diff_ci95": ci,
                "retention": sum(ret) / len(ret) if ret else None,
                "n_retention": len(ret),
                "flags": flags,
                "n_dev": len(dev_pairs),
                "summary_extras": summ.get("numbers", {}).get("extras", {}).get("summary"),
            }
        )
    return out


def _f(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def markdown(method: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"### {method}",
        "",
        "| family | n | native | summary | Δ 95% CI | retention | flags / status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "n" not in r:
            note = r["status"] + (": " + "; ".join(r["problems"]) if r.get("problems") else "")
            lines.append(f"| {r['family']} | — | — | — | — | — | {note} |")
            continue
        ci = r["diff_ci95"]
        ci_s = "—" if ci is None else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"
        ret = "—" if r["retention"] is None else f"{r['retention']:.2f} (n={r['n_retention']})"
        flag = ", ".join(r["flags"]) or r["status"]
        lines.append(
            f"| {r['family']} | {r['n']} | {_f(r['native'])} | {_f(r['summary'])} | {ci_s} | "
            f"{ret} | {flag} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    kw = dict(a.split("=", 1) for a in argv)
    native, summary = _load(Path(kw["native"])), _load(Path(kw["summary"]))
    exclude = json.loads(Path(kw["exclude"]).read_text()) if kw.get("exclude") else {}
    rows = compare(native, summary, exclude)
    md = markdown(kw.get("method", "method"), rows)
    print(md)
    if kw.get("out"):
        Path(kw["out"] + ".md").write_text(md)
        Path(kw["out"] + ".json").write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
