"""Plan 0078 tier C (R51-R52): new invented entities by gw's sibling-country route, ported verbatim
from gw ``scripts/feas/model_multihop/{prompts,mmh_stages}.py`` at main ``2027ef208``, and the
sha-pinned batch files ``mmh.load_pool`` appends to the pool."""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

GW_COMMIT = "2027ef208"
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"  # gw llm.OPUS / llm.SONNET
SUFFIX = "If you are not sure, give your best guess. Answer with just the {t}."  # gw mdate.SUFFIX
BATCH_N = 2000  # R52: kept rows per batch
PER_CALL = 20  # gw: min(20, ...) entities per generation call
AVOID_TAIL = 120  # gw: prev_names[-120:]
TOP1_MAX = 0.7  # gw: top-1 p >= 0.7 is dropped
# gw mmh_stages.INV_QUOTA's keys (the sibling route lists every type; the weights are unused there)
INV_TYPE_KEYS = ("landmark", "natural", "company", "person", "institution", "dish", "event")

# ---- verbatim from gw prompts.py (2027ef208); tests pin their sha256 ----
INVENTED_TYPES = {
    "landmark": ("a landmark or historic building (fort, tower, bridge, palace, villa, gate, "
                 "lighthouse, cathedral-like building, monument)", "It is a well-known landmark."),
    "natural": ("a natural feature (lake, hill range, waterfall, cave, valley, gorge, bay)",
                "It is a natural landmark."),
    "company": ("a company or brand (manufacturer, bank, airline, retailer, logistics firm, "
                "food brand)", "It is a well-known company."),
    "person": ("a person (chef, footballer, novelist, architect, singer, politician, scientist, "
               "painter)", ""),
    "institution": ("an institution (university, hospital, museum, library, conservatory, "
                    "research institute)", "It is a well-known institution."),
    "dish": ("a dish or food (stew, pastry, sweet, bread, drink, street food)",
             "It is a traditional dish."),
    "event": ("a festival, recurring event or sports club", "It is a well-known event."),
}  # fmt: skip

INVENTED_SCREEN = """Here is the name of a {etype}: "{name}". Context: "{pre}"

1. Is this a real, specific entity you recognise? (true/false)
2. Without any other information, which countries would you guess it is in / from? Give your \
top 5 countries with probabilities summing to at most 1.
3. Does the name itself contain a country, city, region, demonym, or a word that belongs to \
one country's language only? (true/false)

Return JSON only: {{"real": bool, "top5": [{{"country": ..., "p": ...}}], "marker": bool}}"""

SIB_CLUSTERS = {
    "nordic": ["Norway", "Sweden", "Denmark", "Finland", "Iceland"],
    "south_asia": ["India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal"],
    "anglo_north_america": ["United States", "Canada"],
    "anglo_isles_oceania": ["United Kingdom", "Ireland", "Australia", "New Zealand"],
    "iberia_latam": ["Spain", "Mexico", "Argentina", "Colombia", "Chile", "Peru"],
    "southern_cone": ["Argentina", "Uruguay", "Chile", "Paraguay"],
    "lusophone": ["Portugal", "Brazil", "Angola", "Mozambique"],
    "dach": ["Germany", "Austria", "Switzerland", "Luxembourg"],
    "benelux_france": ["Netherlands", "Belgium", "France", "Luxembourg"],
    "francophone_africa": ["Senegal", "Ivory Coast", "Cameroon", "Mali", "Burkina Faso"],
    "maghreb": ["Morocco", "Algeria", "Tunisia", "Libya"],
    "levant": ["Lebanon", "Syria", "Jordan", "Palestine", "Israel"],
    "gulf": ["Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain"],
    "persianate": ["Iran", "Afghanistan", "Tajikistan"],
    "turkic": ["Turkey", "Azerbaijan", "Kazakhstan", "Uzbekistan", "Turkmenistan"],
    "balkans": ["Serbia", "Croatia", "Bosnia and Herzegovina", "Montenegro", "Slovenia",
                "North Macedonia"],
    "west_slavic": ["Poland", "Czechia", "Slovakia"],
    "east_slavic": ["Russia", "Ukraine", "Belarus"],
    "baltic": ["Estonia", "Latvia", "Lithuania"],
    "greek_cyprus": ["Greece", "Cyprus"],
    "italy_malta": ["Italy", "San Marino", "Malta", "Switzerland"],
    "romance_east": ["Romania", "Moldova"],
    "chinese_world": ["China", "Taiwan", "Singapore", "Hong Kong", "Malaysia"],
    "korea_japan": ["South Korea", "Japan"],
    "malay_world": ["Malaysia", "Indonesia", "Brunei", "Singapore"],
    "mainland_sea": ["Thailand", "Laos", "Cambodia", "Myanmar", "Vietnam"],
    "philippines_pacific": ["Philippines", "Guam", "Fiji", "Papua New Guinea"],
    "east_africa": ["Kenya", "Tanzania", "Uganda", "Rwanda", "Ethiopia"],
    "southern_africa": ["South Africa", "Botswana", "Namibia", "Zimbabwe", "Zambia"],
    "west_africa_anglo": ["Nigeria", "Ghana", "Sierra Leone", "Liberia", "Gambia"],
    "caribbean": ["Jamaica", "Trinidad and Tobago", "Barbados", "Bahamas", "Guyana"],
    "central_america": ["Guatemala", "Honduras", "El Salvador", "Nicaragua", "Costa Rica",
                        "Panama"],
    "andes": ["Peru", "Bolivia", "Ecuador", "Colombia", "Venezuela"],
    "caucasus": ["Georgia", "Armenia", "Azerbaijan"],
}  # fmt: skip

INVENTED_SIB_GEN = """You are building an evaluation of which country a language model ASSUMES an \
unfamiliar entity belongs to. We want entities where DIFFERENT models would plausibly guess \
DIFFERENT countries, so each entity must sit between several SIBLING countries.

Sibling countries for this batch: {countries}

Invent {n} entities, mixing these types (give each one's type key): {types}

Hard requirements:
- Entirely made up: no real entity of that name should exist or be famous.
- REGIONALLY ANCHORED, NATIONALLY AMBIGUOUS: the name should clearly feel like it belongs to \
this group of countries, but be about EQUALLY plausible in at least three of them (or both, \
for a two-country group). Shared languages, shared naming customs, shared cuisine and shared \
landscapes are the point.
- It must NOT contain a country, city, region, river or demonym, nor a word, spelling or \
naming convention used in only ONE of the sibling countries.
- Avoid entities a reader would place outside this group.
{person_rule}
- Vary the style across the batch; do not reuse a head word more than twice.
- Do not use any of these names: {avoid}

For each entity give:
- type: one of {type_keys}.
- name: as it would appear in text ("the Blue Fort", "Mara Elin Soto").
- pre: one short sentence that introduces the entity WITHOUT any hint of location, e.g. "The \
Blue Fort is a well-known landmark." / "Ilan Mareo is a celebrated chef."
- b: the bridge question: "In which country is the Blue Fort located?" / "Which country is \
Ilan Mareo from?"
- ref_after: how a follow-up question refers to the country after `pre`: "the country it is \
in" / "the country this person is from".
- ref_first: the same reference written out for a single self-contained question: "the \
country where the Blue Fort is located" / "the country Ilan Mareo is from".
- role: "the country the Blue Fort is in" / "the country Ilan Mareo is from".
- likely: the 3 sibling countries you think are most plausible, most plausible first.

Return JSON only: {{"items": [{{"type": ..., "name": ..., "pre": ..., "b": ..., \
"ref_after": ..., "ref_first": ..., "role": ..., "likely": [...]}}]}}"""

SIB_PERSON_RULE = """- PEOPLE: first name and surname must both be common in several of the \
sibling countries (not in just one), so that a reader can place the person in the region but \
not in a single country."""

PORTED_SHA256 = {  # sha256 (first 16 hex) of the ported strings / repr of the tables, as in gw
    "INVENTED_SIB_GEN": "7e9b7242953ab051",
    "SIB_PERSON_RULE": "52761db8e09254a2",
    "INVENTED_SCREEN": "ed29a208973fee9e",
    "SIB_CLUSTERS": "250d1488e0303955",
    "INVENTED_TYPES": "26526a388a7ae9ac",
}
# R52: one entry per generated batch (k -> sha256 of $WSB_POOL_DIR/mmh_tierc_b<k>.json); a batch
# is pinned in the PR that first uses it
TIERC_SHA256: dict[int, str] = {
    1: "dbc769973ed86d20b21ffed97cf61486f154fd6b429c9048fca2f513245ae504",  # 2026-11-07
}
REQUIRED = ("name", "pre", "b", "ref_after", "ref_first", "role")


def sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def ported_hashes() -> dict[str, str]:
    return {
        "INVENTED_SIB_GEN": sha16(INVENTED_SIB_GEN),
        "SIB_PERSON_RULE": sha16(SIB_PERSON_RULE),
        "INVENTED_SCREEN": sha16(INVENTED_SCREEN),
        "SIB_CLUSTERS": sha16(repr(SIB_CLUSTERS)),
        "INVENTED_TYPES": sha16(repr(INVENTED_TYPES)),
    }


def gen_prompt(cluster: str, n: int, avoid: list[str]) -> str:
    """gw ``cands_invented.one_cluster``'s prompt."""
    types = "; ".join(f"{t} = {INVENTED_TYPES[t][0]}" for t in INV_TYPE_KEYS)
    return INVENTED_SIB_GEN.format(
        n=n,
        countries=", ".join(SIB_CLUSTERS[cluster]),
        types=types,
        type_keys=", ".join(INV_TYPE_KEYS),
        person_rule=SIB_PERSON_RULE,
        avoid="; ".join(avoid) or "(none yet)",
    )


def reply_items(js: Any) -> list[dict[str, Any]]:
    """gw: reply items with a known type and every required field a non-empty string."""
    items = (js or {}).get("items") if isinstance(js, dict) else None
    return [
        x
        for x in items or []
        if isinstance(x, dict)
        and x.get("type") in INV_TYPE_KEYS
        and all(isinstance(x.get(k), str) and x[k].strip() for k in REQUIRED)
    ]


def screen_prompt(x: dict[str, Any]) -> str:
    return INVENTED_SCREEN.format(etype=x["type"], name=x["name"], pre=x["pre"])


def screen_verdict(s: Any) -> tuple[str | None, list[str]]:
    """gw's screen drops: (reason or None, top-5 countries by descending p)."""
    if not isinstance(s, dict):
        return "screen_failed", []
    top = sorted(
        [t for t in s.get("top5") or [] if isinstance(t, dict) and t.get("country")],
        key=lambda t: -float(t.get("p") or 0),
    )
    if s.get("real"):
        return "real", []
    if s.get("marker"):
        return "marker", []
    if not top or float(top[0].get("p") or 0) >= TOP1_MAX:
        return "top1>=0.7", []
    return None, [str(t["country"]) for t in top[:5]]


def words(s: str) -> str:
    """A name as ' w1 w2 ... ' (lower case, letters and digits), for whole-word containment."""
    return " " + " ".join(re.findall(r"[^\W_]+", s.lower())) + " "


class NameIndex:
    """Whole-word containment either way against every known entity name (R51 (a))."""

    def __init__(self, names: Iterable[str] = ()):
        self.names: list[str] = []
        self.by_word: dict[str, set[int]] = {}
        for n in names:
            self.add(n)

    def add(self, name: str) -> None:
        w = words(name)
        k = len(self.names)
        self.names.append(w)
        for t in set(w.split()):
            self.by_word.setdefault(t, set()).add(k)

    def dup(self, name: str) -> bool:
        w = words(name)
        toks = w.split()
        if not toks:
            return True
        cand = set.union(*(self.by_word.get(t, set()) for t in toks))
        return any(w in self.names[k] or self.names[k] in w for k in cand)


def pool_row(k: int, idx: int, x: dict[str, Any], top5: list[str]) -> dict[str, Any]:
    """The kept entity as a pool invented row (gw's cands row; ``name`` -> ``entity_name``)."""
    return {
        "id": f"c{k}iv{idx:04d}",
        "cat": "invented",
        "type": x["type"],
        "cluster": x["cluster"],
        "entity_name": x["name"],
        "pre_text": x["pre"].strip(),
        "b": f"{x['b'].strip()} {SUFFIX.format(t='country')}",
        "ref_after": x["ref_after"],
        "ref_first": x["ref_first"],
        "role": x["role"],
        "sonnet_top5": top5,
        "tier": "C",
        "registered": False,
        "batch": k,
    }


Ask = Callable[..., Any]
EMPTY_MAX = 4  # gw cands_invented: a cluster stops after 4 consecutive empty replies


def build_batch(
    k: int,
    gen: Ask,
    screen: Ask,
    known: list[str],
    avoid_tail: list[str],
    n: int = BATCH_N,
    workers: int = 16,
) -> dict[str, Any]:
    """R51-R52: active clusters round-robin (one call each per pass, in parallel) until the screen
    keeps >= ``n`` rows after the duplicate rule against ``known``; the first ``n`` in (pass,
    cluster, reply) order. ``avoid_tail`` = earlier names in order (the last 120 are used)."""
    idx = NameIndex(known)
    clusters = list(SIB_CLUSTERS)
    got: dict[str, list[str]] = {c: [] for c in clusters}  # gw `got`: raw reply names
    empty = dict.fromkeys(clusters, 0)
    tail = list(avoid_tail[-AVOID_TAIL:])
    kept: list[tuple[dict[str, Any], list[str]]] = []
    raw_names: list[str] = []
    drops: Counter[str] = Counter()

    def valid(js: Any) -> bool:  # an empty reply is never cached (R51)
        return bool(reply_items(js))

    def ask(c: str) -> list[dict[str, Any]]:
        js = gen(gen_prompt(c, PER_CALL, [*tail, *got[c]]), valid)
        return [dict(x, cluster=c) for x in reply_items(js)]

    def scr(x: dict[str, Any]) -> tuple[str | None, list[str]]:
        return screen_verdict(screen(screen_prompt(x), lambda js: isinstance(js, dict)))

    n_pass = 0
    while len(kept) < n and (active := [c for c in clusters if empty[c] < EMPTY_MAX]):
        n_pass += 1
        with ThreadPoolExecutor(workers) as ex:
            replies = list(ex.map(ask, active))
        raw = []
        for c, xs in zip(active, replies, strict=True):
            empty[c] = 0 if xs else empty[c] + 1
            got[c] += [x["name"] for x in xs]
            raw += xs
        raw_names += [x["name"] for x in raw]
        with ThreadPoolExecutor(workers) as ex:
            verdicts = list(ex.map(scr, raw))
        for x, (why, top5) in zip(raw, verdicts, strict=True):
            if why is None and idx.dup(x["name"]):
                why = "dup"
            if why is not None:
                drops[why] += 1
                continue
            idx.add(x["name"])
            kept.append((x, top5))
    rows = [pool_row(k, i, x, t5) for i, (x, t5) in enumerate(kept[:n])]
    return {
        "rows": rows,
        "n_raw": len(raw_names),
        "n_pass": n_pass,
        "drops": dict(drops),
        "raw_names": raw_names,
        "names_past_cut": [x["name"] for x, _ in kept[n:]],
        "retired": sorted(c for c in clusters if empty[c] >= EMPTY_MAX),
        "short": len(rows) < n,
    }


def batch_path(pool_dir: Path, k: int) -> Path:
    return pool_dir / f"mmh_tierc_b{k}.json"


def batch_headers(pool_dir: Path, pins: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """The pinned batches' headers in batch order (``load_batches`` verifies the files)."""
    pins = TIERC_SHA256 if pins is None else pins
    load_batches(pool_dir, pins)
    return [json.loads(batch_path(pool_dir, k).read_bytes())["header"] for k in sorted(pins)]


def load_batches(pool_dir: Path, pins: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """Every pinned batch's rows, in batch order; refuses a missing or mismatched file, a row that
    is not tier C of its batch, or a duplicate id."""
    pins = TIERC_SHA256 if pins is None else pins
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    for k in sorted(pins):
        p = batch_path(pool_dir, k)
        if not p.is_file():
            raise SystemExit(f"mmh tier C: batch {k} is pinned but {p} is missing (WSB_POOL_DIR)")
        data = p.read_bytes()
        if (got := hashlib.sha256(data).hexdigest()) != pins[k]:
            raise SystemExit(f"mmh tier C: {p} sha256 {got} != pinned {pins[k]}")
        for r in json.loads(data)["invented"]:
            if r.get("tier") != "C" or r.get("batch") != k or r["id"] in ids:
                raise SystemExit(f"mmh tier C: bad row {r.get('id')} in batch {k}")
            ids.add(r["id"])
            rows.append(r)
    return rows
