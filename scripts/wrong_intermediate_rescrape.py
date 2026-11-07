# ruff: noqa: C408, E501  (the template definitions are copied verbatim in GW scrape.py's shape)
"""wrong_intermediate re-scrape round r3 (plan 0077 §3) through GW scripts/wrong_bridge/scrape.py
(imported read-only); keeps only candidates new to every ``--exclude`` dir. Usage:
``--round r3 --out <dir> --exclude <r1>,<r2a>,<r2b> [--gw <dir>] [--names-only]``."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

GW = Path("/workspace/gw-wrong-bridge/scripts/wrong_bridge")

# plan 0066 §5 (b): the re-scrape's new templates (r2 defined them in GW's TEMPLATES shape)
NEW = {
    "song_lyricist": dict(
        where="VALUES ?c {wd:Q134556 wd:Q7366} ?s wdt:P31 ?c; wdt:P676 ?b.", prop="P676",
        kind="human", min_sl=8, desc="the lyricist of the song \"{S}\"",
        probe="The lyrics of the song \"{S}\" were written by"),
    "film_producer": dict(
        where="?s wdt:P31 wd:Q11424; wdt:P162 ?b.", prop="P162", kind="human", min_sl=8,
        desc="the producer of the {Y} film {S}", probe="The {Y} film {S} was produced by"),
    "opera_librettist": dict(
        where="?s wdt:P31 wd:Q1344; wdt:P87 ?b.", prop="P87", kind="human", min_sl=8,
        desc="the librettist of the opera {S}", probe="The libretto of the opera {S} was written by"),
    "album_producer": dict(
        where="?s wdt:P31 wd:Q482994; wdt:P162 ?b.", prop="P162", kind="human", min_sl=8,
        desc="the producer of the {Y} album {S}", probe="The {Y} album {S} was produced by"),
    "aircraft_manufacturer": dict(
        where="VALUES ?c {wd:Q15056993 wd:Q15056995} ?s wdt:P31 ?c; wdt:P176 ?b.", prop="P176",
        kind="org", min_sl=8, desc="the manufacturer of the {S}",
        probe="The {S} aircraft was manufactured by"),
}  # fmt: skip

# r3 (plan 0077 §3): every admitted template once more at half its r2 min_sl, floor 3; the two
# mixed-kind templates r2 never lowered get one halving (person_mother 30 -> 15, namesake 15 -> 7)
R3_FLOOR = 3
R3_MIN_SL = {
    "film_director": 3, "novel_author": 3, "album_performer": 3, "painting_creator": 3,
    "composition_composer": 3, "org_founder": 3, "tv_creator": 3, "game_developer": 3,
    "person_father": 8, "song_performer": 3, "film_screenwriter": 3, "character_creator": 3,
    "product_manufacturer": 3, "doctoral_advisor": 3, "song_composer": 3, "album_label": 3,
    "novel_publisher": 3, "tv_network": 3, "song_lyricist": 4, "film_producer": 4,
    "opera_librettist": 4, "album_producer": 4, "aircraft_manufacturer": 4,
    "person_mother": 15, "namesake": 7,
}  # fmt: skip


def ids_of(path: Path) -> set[str]:
    with path.open(encoding="utf-8") as fh:
        return {json.loads(ln)["id"] for ln in fh if ln.strip()}


def name_of(c: dict) -> str:
    """The gate-log key a candidate's bank row gets (``<template>-<S>-<hop2>``)."""
    return f"{c['template']}-{c['S']}-{c['hop2']}"


def drop_name_collisions(out: Path, excl_dirs: list[Path], round_: str) -> int:
    """Leave out candidates whose gate-log name an earlier dir already has (their hop-1 bridge
    changed since that scrape, so the id differs but the bank row would share a key); the count
    goes into the round's meta. Idempotent."""
    names: set[str] = set()
    for d in excl_dirs:
        with (d / "candidates.jsonl").open(encoding="utf-8") as fh:
            names |= {name_of(json.loads(ln)) for ln in fh if ln.strip()}
    path, tmp, n = out / "candidates.jsonl", out / "candidates.tmp.jsonl", 0
    with path.open(encoding="utf-8") as fi, tmp.open("w", encoding="utf-8") as fo:
        for ln in fi:
            if name_of(json.loads(ln)) in names:
                n += 1
            else:
                fo.write(ln)
    tmp.replace(path)
    mp = out / f"{round_}_meta.json"
    meta = json.loads(mp.read_text()) if mp.exists() else {}
    meta["n_name_collisions_dropped"] = meta.get("n_name_collisions_dropped", 0) + n
    meta["n_new"] = sum(1 for ln in path.open(encoding="utf-8") if ln.strip())
    mp.write_text(json.dumps(meta, indent=1))
    print(f"[{round_}] {n} name collisions with the earlier pools dropped; {meta['n_new']} left")
    return n


def _robust(qlever, out: Path) -> None:
    """QLever rate-limits long fetches (HTTP 429): retry without giving up (waits capped at 10
    min) and checkpoint every fetched entity chunk under ``<out>/entities.partial/``, so a crash
    or restart never refetches what it already has (the fetch result is unchanged)."""
    import time

    real_query = qlever.query

    def query(q: str, tries: int = 12) -> list[list[str]]:
        for k in range(1000):
            try:
                return real_query(q, tries=1)
            except RuntimeError:
                time.sleep(min(600, 30 * (k + 1)))
        raise RuntimeError("qlever giving up after 1000 rounds")

    qlever.query = query
    real_fetch = qlever.fetch
    part = out / "entities.partial"
    part.mkdir(parents=True, exist_ok=True)

    def fetch(ids: list[str], chunk: int = 3000) -> dict:
        ids = sorted(set(ids))
        got: dict = {}
        for n in range(0, len(ids), chunk):
            sub = ids[n : n + chunk]
            key = hashlib.sha256(" ".join(sub).encode()).hexdigest()[:16]
            f = part / f"{key}.json"
            if f.exists():
                got.update(json.loads(f.read_text()))
                continue
            res = real_fetch(sub, chunk=chunk)
            f.write_text(json.dumps(res))
            got.update(res)
        return got

    qlever.fetch = fetch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--round", required=True, choices=["r3"])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--exclude", required=True, help="csv of earlier pool dirs (candidates.jsonl)")
    ap.add_argument("--gw", type=Path, default=GW)
    ap.add_argument("--only", default="", help="csv subset of the round's templates")
    ap.add_argument(
        "--names-only",
        action="store_true",
        help="only re-apply the name-collision filter to an existing round dir",
    )
    a = ap.parse_args(argv)
    excl_dirs = [Path(d) for d in a.exclude.split(",") if d]
    if a.names_only:
        drop_name_collisions(a.out, excl_dirs, a.round)
        return 0
    sys.path.insert(0, str(a.gw))
    import qlever
    import scrape

    tmpl: dict[str, dict] = {}
    for k, sl in R3_MIN_SL.items():
        if a.only and k not in a.only.split(","):
            continue
        t = dict(NEW[k]) if k in NEW else dict(scrape.TEMPLATES[k])
        t["min_sl"] = sl
        tmpl[k] = t
    scrape.TEMPLATES.clear()
    scrape.TEMPLATES.update(tmpl)
    scrape.ROUND2.clear()
    scrape.ROUND2.update(tmpl)  # every pair through QLever (WDQS times out at low min_sl)
    for p in ("P676", "P87"):  # hop1 props of the new templates (the single-valued check)
        if p not in qlever.ITEM_PROPS:
            qlever.ITEM_PROPS.append(p)
    _robust(qlever, a.out)
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    sys.argv = [sys.argv[0], "--out", str(out)]
    scrape.main()
    seen: set[str] = set()
    shas = {}
    for d in excl_dirs:
        p = d / "candidates.jsonl"
        seen |= ids_of(p)
        shas[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    allp, kept, n_all = out / "candidates.all.jsonl", 0, 0
    (out / "candidates.jsonl").rename(allp)
    with allp.open(encoding="utf-8") as fi, (out / "candidates.jsonl").open("w") as fo:
        for ln in fi:
            n_all += 1
            if json.loads(ln)["id"] not in seen:
                fo.write(ln)
                kept += 1
    allp.unlink()  # the excluded rows already live in the earlier dirs
    meta = {"plan": "0077 §3 (wrong_intermediate r3)", "round": a.round,
            "templates": tmpl, "min_bridge_sl": scrape.MIN_BRIDGE_SL,
            "excluded": shas, "n_scraped": n_all, "n_new": kept}  # fmt: skip
    (out / f"{a.round}_meta.json").write_text(json.dumps(meta, indent=1))
    print(f"[{a.round}] {n_all} scraped, {kept} not in the earlier pools -> {out}/candidates.jsonl")
    drop_name_collisions(out, excl_dirs, a.round)
    return 0


if __name__ == "__main__":
    sys.exit(main())
