# ruff: noqa: C408, E501  (the template definitions are copied verbatim in GW scrape.py's shape)
"""wrong_intermediate re-scrape rounds r3 (plan 0077 §3) and r4 (plan 0077 §3b) through GW
scripts/wrong_bridge/scrape.py (imported read-only); keeps only candidates new to every
``--exclude`` dir (by id and by gate-log name). Usage:
``--round r4 --out <dir> --exclude <r1>,<r2a>,<r2b>,<r3> [--prefill <dirs>] [--fill-missing]
[--gw <dir>] [--names-only]``.

``--prefill`` seeds the entity cache from earlier rounds' ``entities.json`` (the first dir wins,
round-1 precedence as ``merge_pools``); ``--fill-missing`` fetches the remaining QIDs with
Wikidata ``wbgetentities`` (``en|mul`` labels, claims trimmed to the props the scrape reads,
``sitelinks_n``) instead of QLever's entity fetch, as r3 did by hand (``r3_meta.json``)."""

import argparse
import hashlib
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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

# r4 (plan 0077 §3b (4)): every template at sitelinks >= 2; the parents and namesake stay higher
R4_MIN_SL = dict.fromkeys(R3_MIN_SL, 2) | {"person_father": 4, "person_mother": 8, "namesake": 4}
MIN_SL = {"r3": R3_MIN_SL, "r4": R4_MIN_SL}
PLAN = {"r3": "0077 §3 (wrong_intermediate r3)", "r4": "0077 §3b (wrong_intermediate r4)"}
# hop-1 / fact props QLever's ITEM_PROPS lacks: lyricist, librettist (r2 templates), position held
# and country of origin (§3b's kind facts)
EXTRA_ITEM_PROPS = ("P676", "P87", "P39", "P495")
MIN_FREE_GB = 3  # stop before a checkpoint write would leave the disk under this


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
    qlever.fetch = checkpointed(lambda sub: real_fetch(sub, chunk=len(sub)), out, 3000)


def checkpointed(fetch_chunk, out: Path, chunk: int, tag: str = ""):
    """``fetch(ids)`` that fetches ``ids`` in sorted chunks through ``fetch_chunk`` and keeps each
    chunk's result under ``<out>/entities.partial/<tag><sha>.json`` (a restart reads it back)."""
    part = out / "entities.partial"

    def fetch(ids: list[str], chunk: int = chunk) -> dict:
        part.mkdir(parents=True, exist_ok=True)
        ids = sorted(set(ids))
        got: dict = {}
        for n in range(0, len(ids), chunk):
            sub = ids[n : n + chunk]
            key = hashlib.sha256(" ".join(sub).encode()).hexdigest()[:16]
            f = part / f"{tag}{key}.json"
            if f.exists():
                got.update(json.loads(f.read_text()))
                continue
            free = shutil.disk_usage(part).free / 1e9
            if free < MIN_FREE_GB:
                raise SystemExit(
                    f"only {free:.1f} GB free under {part}: stopping (checkpoints kept)"
                )
            res = fetch_chunk(sub)
            f.write_text(json.dumps(res))
            got.update(res)
            print(
                f"[entities{'/' + tag.strip('-') if tag else ''}] {n + len(sub)}/{len(ids)}",
                flush=True,
            )
        return got

    return fetch


def load_prefill(dirs: list[Path]) -> dict:
    """The union of ``<d>/entities.json`` over ``dirs``, the FIRST dir winning (round-1
    precedence, as ``merge_pools``). Each file is read once and released before the next."""
    ents: dict = {}
    for d in dirs:
        with (d / "entities.json").open(encoding="utf-8") as fh:
            part = json.load(fh)
        n0 = len(ents)
        for q, e in part.items():
            ents.setdefault(q, e)
        print(
            f"[prefill] {d}: {len(part)} entities, {len(ents) - n0} new -> {len(ents)}", flush=True
        )
        del part
    return ents


def trim_entity(e: dict | None, props: list[str]) -> dict:
    """A ``wbgetentities`` (``languages=en|mul``) entity in the shape ``qlever.fetch`` writes:
    the ``en`` label (``mul`` when there is none), ``en`` + ``mul`` aliases, claims of ``props``
    only (each statement cut to its rank and main-snak value: all the scrape and wsbench read),
    ``sitelinks_n`` instead of the sitelink list. A missing entity gets QLever's empty record."""
    e = e or {}
    labels = e.get("labels") or {}
    lab = labels.get("en") or labels.get("mul")
    al = e.get("aliases") or {}
    claims: dict = {}
    for p in props:
        for c in (e.get("claims") or {}).get(p, []):
            dv = (c.get("mainsnak") or {}).get("datavalue")
            if dv is None:
                continue
            claims.setdefault(p, []).append(
                {"rank": c.get("rank", "normal"), "mainsnak": {"datavalue": dv}}
            )
    return {
        "labels": {"en": {"value": lab["value"]}} if lab else {},
        "aliases": {"en": [{"value": a["value"]} for a in al.get("en", []) + al.get("mul", [])]},
        "claims": claims,
        "sitelinks": {},
        "sitelinks_n": len(e.get("sitelinks") or {}),
    }


def wb_fetcher(get, props: list[str], workers: int = 2):
    """``fetch_chunk(ids)`` over ``wbgetentities`` (50 ids per call, ``workers`` in flight);
    ``get(params) -> dict`` is GW ``wd._get`` bound to the API (it raises after its own retries:
    retried here without giving up, waits capped at 10 min; a persistent API error splits the
    call into single ids)."""

    def one(sub: list[str]) -> dict:
        params = {"action": "wbgetentities", "ids": "|".join(sub), "languages": "en|mul",
                  "props": "labels|aliases|claims|sitelinks", "redirects": "no", "format": "json"}  # fmt: skip
        for k in range(1000):
            try:
                d = get(params)
            except RuntimeError:
                d = None
            if d is not None and "error" not in d:
                return d.get("entities") or {}
            if d is not None and k >= 2:  # an API error that persists: one id at a time, and
                if len(sub) == 1:  # an id the API keeps refusing gets the empty record
                    print(f"[wb] {sub[0]}: {d['error']} -> empty record", flush=True)
                    return {}
                return {q: e for x in sub for q, e in one([x]).items()}
            time.sleep(min(600, 30 * (k + 1)))
        raise RuntimeError("wbgetentities giving up after 1000 rounds")

    def fetch_chunk(ids: list[str]) -> dict:
        subs = [ids[i : i + 50] for i in range(0, len(ids), 50)]
        raw: dict = {}
        with ThreadPoolExecutor(workers) as ex:
            for part in ex.map(one, subs):
                raw.update(part)
        return {q: trim_entity(raw.get(q), props) for q in ids}

    return fetch_chunk


def entity_source(qlever, out: Path, prefill: dict, fill_missing, stats: dict) -> None:
    """``qlever.fetch`` (what scrape.py calls for every entity it needs) answers from ``prefill``
    first; the rest goes to ``fill_missing`` (a ``wbgetentities`` fetch_chunk, checkpointed) when
    given, else to QLever's own (``_robust``) fetch. Only the entities the scrape needs reach its
    ``entities.json``."""
    ql_fetch = qlever.fetch
    wb = checkpointed(fill_missing, out, 500, tag="wb-") if fill_missing else None

    def fetch(ids: list[str], chunk: int = 3000) -> dict:
        ids = sorted(set(ids))
        got = {q: prefill[q] for q in ids if q in prefill}
        rest = [q for q in ids if q not in got]
        stats["prefilled"] = stats.get("prefilled", 0) + len(got)
        stats["fetched"] = stats.get("fetched", 0) + len(rest)
        print(
            f"[entities] {len(ids)} needed: {len(got)} prefilled, {len(rest)} to fetch", flush=True
        )
        if rest:
            got.update(wb(rest) if wb else ql_fetch(rest))
        return got

    qlever.fetch = fetch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--round", required=True, choices=sorted(MIN_SL))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--exclude", required=True, help="csv of earlier pool dirs (candidates.jsonl)")
    ap.add_argument("--gw", type=Path, default=GW)
    ap.add_argument("--only", default="", help="csv subset of the round's templates")
    ap.add_argument("--prefill", default="", help="csv of earlier pool dirs (entities.json)")
    ap.add_argument(
        "--fill-missing",
        action="store_true",
        help="fetch the entities --prefill lacks with wbgetentities (not QLever)",
    )
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
    pre_dirs = [Path(d) for d in a.prefill.split(",") if d]
    missing = [
        str(d / "candidates.jsonl") for d in excl_dirs if not (d / "candidates.jsonl").exists()
    ]
    missing += [str(d / "entities.json") for d in pre_dirs if not (d / "entities.json").exists()]
    if missing:  # fail before hours of scraping, not after
        raise SystemExit(f"missing: {', '.join(missing)}")
    sys.path.insert(0, str(a.gw))
    import qlever
    import scrape
    import wd

    tmpl: dict[str, dict] = {}
    for k, sl in MIN_SL[a.round].items():
        if a.only and k not in a.only.split(","):
            continue
        t = dict(NEW[k]) if k in NEW else dict(scrape.TEMPLATES[k])
        t["min_sl"] = sl
        tmpl[k] = t
    scrape.TEMPLATES.clear()
    scrape.TEMPLATES.update(tmpl)
    scrape.ROUND2.clear()
    scrape.ROUND2.update(tmpl)  # every pair through QLever (WDQS times out at low min_sl)
    for p in EXTRA_ITEM_PROPS:  # hop1 props of the new templates (the single-valued check), facts
        if p not in qlever.ITEM_PROPS:
            qlever.ITEM_PROPS.append(p)
    _robust(qlever, a.out)
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    props = list(qlever.ITEM_PROPS) + list(qlever.DATE_PROPS)
    fill = wb_fetcher(lambda p: wd._get(wd.API, p), props) if a.fill_missing else None
    ent_stats: dict = {}
    entity_source(qlever, out, load_prefill(pre_dirs), fill, ent_stats)
    sys.argv = [sys.argv[0], "--out", str(out)]
    scrape.main()
    shutil.rmtree(out / "entities.partial", ignore_errors=True)  # entities.json now holds them
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
    meta = {"plan": PLAN[a.round], "round": a.round,
            "templates": tmpl, "min_bridge_sl": scrape.MIN_BRIDGE_SL,
            "excluded": shas, "n_scraped": n_all, "n_new": kept}  # fmt: skip
    if pre_dirs or a.fill_missing:
        meta["entity_cache"] = {
            "prefill": [str(d / "entities.json") for d in pre_dirs],
            "precedence": "first dir wins (round-1 precedence, as merge_pools)",
            "fill": (
                "wbgetentities (GW wd._get; languages en|mul, mul the en fallback as QLever; "
                "claims trimmed to qlever.ITEM_PROPS + DATE_PROPS; sitelinks_n)"
                if a.fill_missing
                else "QLever (scrape.py's own fetch, checkpointed)"
            ),
            "props": props,
            "n_prefilled": ent_stats.get("prefilled", 0),
            "n_fetched": ent_stats.get("fetched", 0),
        }
    (out / f"{a.round}_meta.json").write_text(json.dumps(meta, indent=1))
    print(f"[{a.round}] {n_all} scraped, {kept} not in the earlier pools -> {out}/candidates.jsonl")
    drop_name_collisions(out, excl_dirs, a.round)
    return 0


if __name__ == "__main__":
    sys.exit(main())
