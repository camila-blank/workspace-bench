"""wrong_intermediate round r4 (plan 0077 §3b): the kind-matched draw, caps that ignore audited
drops, the three-way rebuild route and the grown-only Wikidata tables (offline; fakes only)."""

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

from wsbench.adapt import run, store
from wsbench.adapt import wrong_intermediate as wb

MODEL = "qwen3.5-4b"  # a registered subject on the Qwen tokenizer (freeze needs a real one)
PHILOSOPHER, WRITER, NOVELIST, ESSAYIST = "Q4964182", "Q36180", "Q6625963", "Q11774202"
RAPPER, PRODUCER = "Q2252262", "Q183945"
LABEL = "Q18127"  # record label


def _claims(**props):
    return {
        p: [{"rank": "normal", "mainsnak": {"datavalue": {"type": "wikibase-entityid",
                                                          "value": {"id": q}}}} for q in qs]
        for p, qs in props.items()
    }  # fmt: skip


def _ent(label, **props):
    return {"labels": {"en": {"value": label}}, "aliases": {}, "claims": _claims(**props),
            "sitelinks_n": 20}  # fmt: skip


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    monkeypatch.setattr(wb, "ADAPT_ITEM_CATEGORY", {})
    monkeypatch.setattr(wb, "class_parents", lambda: {})
    monkeypatch.setattr(wb, "registered_options", lambda: {})


# ============================================================================ (1) the draw
GOLD, CONTRAST = "Alda Quorin", "Bertil Zanvane"


def _direct(gold, contrast, cands, template="novel_author", other=None, kind=None):
    """(item, pool, wd, ents) for a direct build_mc call: ``cands`` {name: claims-or-None} share
    the item's template, ``other`` {name: claims} carry another template of the same pool kind.
    A None value is a factless candidate (no entity)."""
    kind = kind or wb.kind_of(GOLD, wb.category_of(template))
    it = {
        "name": f"{template}-Q8100-birth_year", "template": template,
        "prompt": "Fact: The maker of the thing Zzyzx was founded in the year",
        "intermediates": [GOLD], "true_intermediates": [CONTRAST],
        "wikidata": {"S": "Q8100", "B": "Q8102", "B_prime": "Q8101"},
    }  # fmt: skip
    pool, wd, ents = {}, {"entities": {}, "enwiki": {}}, {}
    wd["entities"]["Q8101"] = {"labels": {"en": GOLD}, "aliases": {}}
    wd["entities"]["Q8102"] = {"labels": {"en": CONTRAST}, "aliases": {}}
    if gold is not None:
        ents["Q8101"] = _ent(GOLD, **gold)
    if contrast is not None:
        ents["Q8102"] = _ent(CONTRAST, **contrast)
    rows = [(n, c, template) for n, c in cands.items()]
    rows += [(n, c, "aircraft_manufacturer") for n, c in (other or {}).items()]
    for k, (n, c, tpl) in enumerate(rows):
        q = f"Q77{k:03d}"
        pool[n] = {"qid": q, "kinds": {kind}, "templates": {tpl}}
        wd["entities"][q] = {"labels": {"en": n}, "aliases": {}}
        if c is not None:
            ents[q] = _ent(n, **c)
    return it, pool, wd, ents


def _people(prefix, n, **claims):
    return {f"{prefix} {chr(65 + i)}{prefix.lower()}son": {"P31": ["Q5"], **claims}
            for i in range(n)}  # fmt: skip


def test_philosopher_among_novelists_is_not_kind_matched():
    pair = {"P31": ["Q5"], "P106": [PHILOSOPHER, WRITER]}
    phil = _people("Phil", 4, P106=[PHILOSOPHER])
    novel = _people("Nov", 4, P106=[WRITER, NOVELIST])
    it, pool, wd, ents = _direct(pair, pair, {**novel, **phil})
    mc = wb.build_mc(it, pool, wd, ents=ents, reg={})
    assert isinstance(mc, dict) and set(mc["distractors"]) == set(phil)
    assert "kind-matched" in mc["pool"] and PHILOSOPHER in mc["pool"]
    it, pool, wd, ents = _direct(pair, pair, {**novel, **dict(list(phil.items())[:3])})
    assert wb.build_mc(it, pool, wd, ents=ents, reg={}) == wb.NO_KIND_LIST  # writer: generic


def test_rapper_producer_all_shared_tier_then_rarest_first():
    pair = {"P31": ["Q5"], "P106": [RAPPER, PRODUCER]}
    both = _people("Both", 4, P106=[RAPPER, PRODUCER])
    rap = _people("Rap", 3, P106=[RAPPER])
    prod = _people("Prod", 3, P106=[PRODUCER])
    it, pool, wd, ents = _direct(pair, pair, {**rap, **prod, **both}, template="song_performer")
    mc = wb.build_mc(it, pool, wd, ents=ents, reg={})
    assert set(mc["distractors"]) == set(both)  # never producers who are not rappers
    assert RAPPER in mc["pool"] and PRODUCER in mc["pool"]
    # 3 all-shared: the rarest single value (producer: 3 + 2 vs rapper 3 + 3) that gives four,
    # all-shared candidates ranked first inside it
    three = dict(list(both.items())[:3])
    prod2 = dict(list(prod.items())[:2])
    it, pool, wd, ents = _direct(pair, pair, {**rap, **prod2, **three}, template="song_performer")
    mc = wb.build_mc(it, pool, wd, ents=ents, reg={})
    assert set(three) < set(mc["distractors"]) and len(set(mc["distractors"]) & set(prod2)) == 1
    assert mc["pool"].endswith(f"kind-matched ({PRODUCER})")


def test_philosopher_essayist_falls_through_to_philosopher():
    pair = {"P31": ["Q5"], "P106": [PHILOSOPHER, ESSAYIST]}
    phil = _people("Phil", 5, P106=[PHILOSOPHER, WRITER])
    it, pool, wd, ents = _direct(pair, pair, phil)
    mc = wb.build_mc(it, pool, wd, ents=ents, reg={})
    assert set(mc["distractors"]) < set(phil)
    assert mc["pool"].endswith(f"kind-matched ({PHILOSOPHER})")


def test_p31_closure_and_p106_exact(monkeypatch):
    monkeypatch.setattr(
        wb,
        "class_parents",
        lambda: {
            "Qindie": [LABEL],
            "Qmajor": [LABEL],
            "Qsublabel": [LABEL],
            "Qsubocc": ["Qrootocc"],
        },
    )
    labels = {
        "Lab Arvon": {"P31": [LABEL]},
        "Lab Brisk": {"P31": [LABEL]},
        "Lab Cedra": {"P31": ["Qsublabel"]},
        "Lab Dunmo": {"P31": ["Qsublabel"]},
    }
    planes = {f"Plane {c}": {"P31": ["Qplane"]} for c in "PQR"}
    it, pool, wd, ents = _direct({"P31": ["Qindie"]}, {"P31": ["Qmajor"]}, {**labels, **planes},
                                 template="album_label")  # fmt: skip
    mc = wb.build_mc(it, pool, wd, ents=ents, reg={})
    assert set(mc["distractors"]) == set(labels)  # rock band reaches musical group, so to speak
    assert LABEL in mc["pool"]
    # P106 is matched exactly: a candidate with only the parent occupation is not eligible
    pair = {"P31": ["Q5"], "P106": ["Qsubocc"]}
    sub = _people("Sub", 3, P106=["Qsubocc"])
    root = _people("Root", 4, P106=["Qrootocc"])
    it, pool, wd, ents = _direct(pair, pair, {**sub, **root})
    assert wb.build_mc(it, pool, wd, ents=ents, reg={}) == wb.NO_KIND_LIST
    sub4 = _people("Sub", 4, P106=["Qsubocc"])
    it, pool, wd, ents = _direct(pair, pair, {**sub4, **root})
    assert set(wb.build_mc(it, pool, wd, ents=ents, reg={})["distractors"]) == set(sub4)


def test_factless_and_other_kind_candidates_never_drawn():
    pair = {"P31": ["Q5"], "P106": [PHILOSOPHER]}
    phil = _people("Phil", 3, P106=[PHILOSOPHER])
    bad = {"Ghost Nofacts": None, "Org Thinkers": {"P31": ["Qthinktank"]}}
    it, pool, wd, ents = _direct(pair, pair, {**phil, **bad})
    assert wb.build_mc(it, pool, wd, ents=ents, reg={}) == wb.NO_KIND_LIST
    # the registered options table supplies facts the pool entity stores lack
    reg = {pool["Ghost Nofacts"]["qid"]: {"P31": ["Q5"], "P106": [PHILOSOPHER], "P17": [],
                                          "P495": []}}  # fmt: skip
    mc = wb.build_mc(it, pool, wd, ents=ents, reg=reg)
    assert set(mc["distractors"]) == {*phil, "Ghost Nofacts"}


def test_tier_order_same_template_and_country_first():
    g = {"P31": [LABEL], "P17": ["Qc1"]}
    same_tc = {
        "Lab Arvon": {"P31": [LABEL], "P17": ["Qc1"]},
        "Lab Brisk": {"P31": [LABEL], "P17": ["Qc1"]},
    }
    same_t = {
        "Lab Cedra": {"P31": [LABEL], "P17": ["Qc9"]},
        "Lab Dunmo": {"P31": [LABEL], "P17": ["Qc8"]},
    }
    other = {"Lab Elkin": {"P31": [LABEL], "P17": ["Qc1"]},
             "Lab Fyrro": {"P31": [LABEL], "P17": ["Qc7"]},
             "Lab Gaunt": {"P31": [LABEL], "P17": ["Qc6"]}}  # fmt: skip
    it, pool, wd, ents = _direct(g, {"P31": [LABEL]}, {**same_tc, **same_t}, template="album_label",
                                 other=other)  # fmt: skip
    mc = wb.build_mc(it, pool, wd, ents=ents, reg={})
    assert set(same_tc) < set(mc["distractors"]) and len(set(mc["distractors"]) & set(same_t)) == 2
    # one same-template-only name left: tier (3) (same country, other template) fills next
    it, pool, wd, ents = _direct(g, {"P31": [LABEL]}, {**same_tc, "Lab Cedra": same_t["Lab Cedra"]},
                                 template="album_label", other=other)  # fmt: skip
    mc = wb.build_mc(it, pool, wd, ents=ents, reg={})
    assert set(mc["distractors"]) == {*same_tc, "Lab Cedra", "Lab Elkin"}


def test_sentinels_and_none():
    pair = {"P31": ["Q5"], "P106": [PHILOSOPHER]}
    phil = _people("Phil", 4, P106=[PHILOSOPHER])
    it, pool, wd, ents = _direct(None, pair, phil)  # a gold with no facts
    assert wb.build_mc(it, pool, wd, ents=ents, reg={}) == wb.NO_KIND_KEY
    generic = {"P31": ["Q5"], "P106": [WRITER]}  # only a stoplisted occupation
    it, pool, wd, ents = _direct(generic, generic, phil)
    assert wb.build_mc(it, pool, wd, ents=ents, reg={}) == wb.NO_KIND_KEY
    # the contrast gives no key of its own: the gold's own values (a namesake-style mixed pair)
    it, pool, wd, ents = _direct(pair, {"P31": ["Q515"]}, phil)
    assert set(wb.build_mc(it, pool, wd, ents=ents, reg={})["distractors"]) == set(phil)
    it, pool, wd, ents = _direct(pair, pair, phil, template="building_architect", kind="writer")
    assert wb.build_mc(it, pool, wd, ents=ents, reg={}) is None  # no category: still None
    assert wb.NO_KIND_LIST != wb.NO_KIND_KEY


def test_ents_none_draws_as_before():
    pair = {"P31": ["Q5"], "P106": [PHILOSOPHER]}
    novel = _people("Nov", 6, P106=[NOVELIST])
    it, pool, wd, _ents = _direct(pair, pair, novel)
    mc = wb.build_mc(it, pool, wd)
    b = wb.mc_builder()
    assert mc["distractors"] == b.seeded_shuffle(sorted(pool), f"{wb.MC_SEED}:{it['name']}")[:4]
    assert mc["pool"] == f"{wb.MC_POOL}; distractors: {wb.MC_SAME_TEMPLATE}"


# ============================================================================ bank fixtures
@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    return tmp_path / MODEL


def _row(rid, s, bp, template="film_director", strength=8, hop2="birth_year", b="Q9"):
    e = {
        "id": rid, "S": s, "Bp": bp, "B": b, "template": template, "hop2": hop2,
        "vkind": "year", "Bp_answer": 1946, "answer": {"value": 1931}, "Bp_label": f"X{bp}",
        "Bp_names": [f"X{bp}"], "B_label": f"Y{b}", "B_aliases": [],
        "prompt_2hop": f"Fact: The thing {s} was founded in the year",
    }  # fmt: skip
    ev = {"p2_samples_Ap": strength, "p1_samples_Bp": 10, "lp_Ap_minus_A": 1.0}
    return {"id": rid, "e1": e, "evidence": ev, "gate": {"pass": True, "rate": 0.8}}


def _no_fetch(qids, titles):
    return {"entities": {}, "enwiki": {}}


def _write_inputs(tmp_path, gated, ents=None):
    wd = wb.work_dir(MODEL)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "gated.jsonl").write_text("".join(json.dumps(r) + "\n" for r in gated))
    (wd / "adjudicated.jsonl").write_text(
        "".join(json.dumps({"id": r["id"], "clean": True}) + "\n" for r in gated)
    )
    pool = tmp_path / "pool"
    pool.mkdir(exist_ok=True)
    (pool / "candidates.jsonl").write_text("")
    (pool / "entities.json").write_text(json.dumps(ents or {}))
    return str(pool)


def _fake_mc(fail=()):
    def make_mc(item, opts, wd, topic, kinds=None):
        if item["name"] in fail:
            return None
        return {"gold": item["intermediates"][0], "category": wb.category_of(item)}

    return make_mc


def _bank(tmp_path, gated, fail=(), ents=None):
    pool = _write_inputs(tmp_path, gated, ents)
    rows = wb.bank(MODEL, pool, registered=[], wd={}, r4={}, fetch=_no_fetch,
                   make_mc=_fake_mc(fail))  # fmt: skip
    return {r["id"]: r for r in rows}


def _drop(ids, reason):
    store.update_report(MODEL, lambda r: store.report_slot(r, "drops", wb.FAMILY).update(
        dict.fromkeys(ids, reason)))  # fmt: skip


def _name(r):
    e = r["e1"]
    return f"{e['template']}-{e['S']}-{e['hop2']}"


# ============================================================================ (2) caps
def test_bank_writes_a_dropped_row_and_readmits_the_next_passer(tmp_store, tmp_path):
    gated = [_row(f"c{i}", f"Q1{i}", "QB", strength=10 - i) for i in range(5)]
    by = _bank(tmp_path, gated)
    assert [by[_name(r)]["gate"]["pass"] for r in gated] == [True] * 4 + [False]
    assert by[_name(gated[4])]["gate"]["drop"] == f"cap: {wb.PER_BRIDGE} per wrong intermediate"
    _drop([_name(gated[0])], "audit check 1: two philosophers among novelists")
    by = _bank(tmp_path, gated)
    first = by[_name(gated[0])]
    assert first["gate"]["pass"] is False and first["selected"] is False
    assert first["gate"]["drop"] == "adapt drop: audit check 1: two philosophers among novelists"
    assert by[_name(gated[4])]["gate"]["pass"]  # re-admitted: the kind drop freed the B' slot
    assert store.gated(MODEL, wb.FAMILY)[_name(gated[0])]["gate"]["pass"] is False


@pytest.mark.parametrize("reason", ["lucky-guessing leaker, wi-r4 draw 1 (any-guesser rule)",
                                    "options-only guesser solves it (Any-Guesser rule, #391)",
                                    "Lucky-Guessing leaker r2"])  # fmt: skip
def test_leaker_drop_keeps_its_bprime_slot_against_a_stronger_new_row(tmp_store, tmp_path, reason):
    gated = [_row(f"c{i}", f"Q1{i}", "QB", strength=8 - i) for i in range(4)]
    _bank(tmp_path, gated)
    _drop([_name(gated[3])], reason)  # 3 incumbents + 1 leaker drop
    stronger = _row("new", "Q20", "QB", strength=10)
    by = _bank(tmp_path, [stronger, *gated])
    assert by[_name(stronger)]["gate"]["pass"] is False
    assert by[_name(stronger)]["gate"]["drop"].startswith("cap: 4 per wrong intermediate")
    assert all(by[_name(r)]["gate"]["pass"] for r in gated[:3])


def test_kind_drop_frees_the_bprime_slot_for_a_stronger_new_row(tmp_store, tmp_path):
    gated = [_row(f"c{i}", f"Q1{i}", "QB", strength=8 - i) for i in range(4)]
    _bank(tmp_path, gated)
    _drop([_name(gated[3])], wb.DROP_NO_LIST)
    stronger = _row("new", "Q20", "QB", strength=10)
    by = _bank(tmp_path, [stronger, *gated])
    assert by[_name(stronger)]["gate"]["pass"]


@pytest.mark.parametrize("reason,admitted", [("lucky-guessing leaker, wi-r4 draw 2", False),
                                             ("audit check 1: kind", True)])  # fmt: skip
def test_leaker_drop_keeps_its_s_slot(tmp_store, tmp_path, reason, admitted):
    year = _row("y", "QS", "QB1", hop2="birth_year", strength=9)
    city = _row("c", "QS", "QB2", hop2="birth_city", strength=8)
    by = _bank(tmp_path, [year, city])
    assert by["film_director-QS-birth_year"]["gate"]["pass"]
    assert by["film_director-QS-birth_city"]["gate"]["drop"] == "cap: one item per subject S"
    _drop(["film_director-QS-birth_year"], reason)
    by = _bank(tmp_path, [year, city])
    assert by["film_director-QS-birth_city"]["gate"]["pass"] is admitted


def test_incumbents_keep_their_slots(tmp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "PER_TEMPLATE", 2)
    strong = _row("s", "Q1", "QB1", strength=10)  # no mc at the first bank
    weak = [_row("a", "Q2", "QB2", strength=9), _row("b", "Q3", "QB3", strength=8)]
    by = _bank(tmp_path, [strong, *weak], fail={_name(strong)})
    assert by[_name(strong)]["gate"]["drop"].startswith("no mc")
    assert all(by[_name(r)]["gate"]["pass"] for r in weak)
    by = _bank(tmp_path, [strong, *weak])  # its options build now: re-admitted, stays out
    assert by[_name(strong)]["gate"]["drop"] == "cap: 2 per template"
    assert all(by[_name(r)]["gate"]["pass"] for r in weak)


def test_landed_and_pool_generation_ignore_drops(tmp_store, tmp_path, monkeypatch):
    gated = [_row(f"c{i}", f"Q1{i}", f"QB{i}", strength=9 - i) for i in range(3)]
    _bank(tmp_path, gated)
    assert wb._landed(MODEL) == 3
    r4 = tmp_path / "wrong_bridge_r4"
    r4.mkdir()
    (r4 / "candidates.jsonl").write_text(
        "".join(json.dumps({"id": r["id"], "leak": False}) + "\n" for r in gated)
    )
    monkeypatch.setenv(wb.REFILL_POOL_ENV, str(r4))
    monkeypatch.setattr(wb, "_screened_ids", lambda model: {r["id"] for r in gated})
    assert wb.pool_generation(MODEL)["landed"] == 3
    _drop([_name(gated[0])], "lucky-guessing leaker")
    assert wb._landed(MODEL) == 2 and wb.prior_tally(MODEL) == (3, 2)
    assert wb.pool_generation(MODEL)["landed"] == 2
    assert wb.pool_generation(MODEL)["name"] == "wi-r4"


def test_funded_dir_is_r4_by_name_or_meta(tmp_path, monkeypatch):
    assert wb.FUNDED_DIR_NAME == "wrong_bridge_r4" and wb.GENERATION == "wi-r4"
    assert wb.DEFAULT_POOLS[-1].endswith("/wrong_bridge_r4") and len(wb.DEFAULT_POOLS) == 5
    copy_dir, r3 = tmp_path / "copy", tmp_path / "wrong_bridge_r3"
    for d in (copy_dir, r3):
        d.mkdir()
    (r3 / "r3_meta.json").write_text("{}")
    monkeypatch.setenv(wb.REFILL_POOL_ENV, f"{r3},{copy_dir}")
    assert wb.funded_dir() is None  # r3 is no longer the funded generation
    (copy_dir / "r4_meta.json").write_text("{}")
    assert wb.funded_dir() == copy_dir


# ============================================================================ real build_mc
LBL = ["Arvon Records", "Brisk Records", "Cedra Records", "Dunmo Records", "Elkin Records",
       "Fyrro Records", "Gaunt Records", "Hollo Records", "Istra Records", "Jorvo Records",
       "Kemble Records", "Lunet Records"]  # fmt: skip


def _q(name):
    return f"Q6{LBL.index(name):03d}" if name in LBL else f"Q69{abs(hash(name)) % 1000:03d}"


def _label_row(i, gold, contrast, strength):
    s = f"Q50{i:02d}"
    e = {
        "id": f"cand{i}", "S": s, "Bp": _q(gold), "B": _q(contrast), "template": "album_label",
        "hop2": "founded_year", "vkind": "year", "Bp_answer": 1990, "answer": {"value": 1970},
        "Bp_label": gold, "Bp_names": [gold], "B_label": contrast, "B_aliases": [],
        "prompt_2hop": f"Fact: The record label of the album Zq{i} was founded in the year",
    }  # fmt: skip
    ev = {"p2_samples_Ap": strength, "p1_samples_Bp": 10, "lp_Ap_minus_A": 1.0}
    return {"id": f"cand{i}", "e1": e, "evidence": ev, "gate": {"pass": True, "rate": 0.8}}


def _label_ents(names, kind=LABEL):
    return {_q(n): _ent(n, P31=[kind]) for n in names}


def _real_bank(tmp_path, gated, ents):
    pool = _write_inputs(tmp_path, gated, ents)
    rows = wb.bank(MODEL, pool, registered=[], wd={}, r4={}, fetch=_no_fetch)
    return pool, {r["id"]: r for r in rows}


def test_bank_never_ships_a_sentinel(tmp_store, tmp_path):
    # the target X (Arvon/Brisk): only Cedra/Dunmo/Elkin are labels; Fyrro/Gaunt are factless
    gated = [_label_row(0, "Arvon Records", "Brisk Records", 10),
             _label_row(1, "Cedra Records", "Dunmo Records", 9),
             _label_row(2, "Elkin Records", "Fyrro Records", 8),
             _label_row(3, "Gaunt Records", "Hollo Records", 7)]  # fmt: skip
    ents = _label_ents(["Arvon Records", "Brisk Records", "Cedra Records", "Dunmo Records",
                        "Elkin Records"])  # fmt: skip
    _pool, by = _real_bank(tmp_path, gated, ents)
    x = by["album_label-Q5000-founded_year"]
    assert x["gate"]["pass"] is False and x["gate"]["drop"] == "no mc: no kind-matched list"
    assert "mc" not in x["item"]
    g = by["album_label-Q5003-founded_year"]  # a gold with no facts: no key
    assert g["gate"]["pass"] is False and g["gate"]["drop"] == "no mc: no kind key"


def _shipped(model_dir, rows):
    model_dir.mkdir(parents=True, exist_ok=True)
    items = [{**r["item"], "adapt": {"origin": r["origin"]}} for r in rows]
    (model_dir / f"{wb.FAMILY}.json").write_text(json.dumps({"family": wb.FAMILY,
                                                             "items": items}))  # fmt: skip


def _grown(n_extra):
    """Rows 0..: the target X first (strongest), then label pairs; ``n_extra`` pairs."""
    gated = [_label_row(0, "Arvon Records", "Brisk Records", 10)]
    for k in range(n_extra):
        gated.append(_label_row(k + 1, LBL[2 + 2 * k], LBL[3 + 2 * k], 9 - k))
    return gated


def test_reuse_keeps_a_measured_list_and_regraded_bprime_redraws(tmp_store, tmp_path):
    ents = _label_ents(LBL)
    _pool, by = _real_bank(tmp_path, _grown(2), ents)
    x = by["album_label-Q5000-founded_year"]
    assert x["gate"]["pass"] and "kind-matched" in x["item"]["mc"]["pool"]
    first = x["item"]["mc"]
    _pool, by = _real_bank(tmp_path, _grown(5), ents)  # the pool grows: the list stays
    assert by["album_label-Q5000-founded_year"]["item"]["mc"] == first
    # a re-graded B' (same name, another QID) gets a fresh draw (different seed name not needed)
    regraded = _grown(5)
    regraded[0]["e1"]["Bp"] = "Q6999"
    ents["Q6999"] = _ent("Arvon Records", P31=[LABEL])
    calls = []
    real = wb.build_mc

    def spy(it, *a, **k):
        calls.append(it["name"])
        return real(it, *a, **k)

    wb.build_mc = spy
    try:
        _pool, by = _real_bank(tmp_path, regraded, ents)
    finally:
        wb.build_mc = real
    assert "album_label-Q5000-founded_year" in calls


def test_rebuild_emits_nothing_for_a_kind_matched_shipped_item_after_the_pool_grows(
    tmp_store, tmp_path
):
    ents = _label_ents(LBL)
    pool, by = _real_bank(tmp_path, _grown(2), ents)
    _shipped(tmp_store, [r for r in by.values() if r["gate"]["pass"]])
    pool, by = _real_bank(tmp_path, _grown(5), ents)
    assert wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={}) == ({}, [], [])


def test_rebuild_with_no_targets_returns_empty_without_raising(tmp_store, tmp_path, capsys):
    pool = _write_inputs(tmp_path, [])
    assert wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={}) == ({}, [], [])
    failed = {**_row("f", "Q1", "QB"), "gate": {"pass": False}}
    store.record_gates(MODEL, wb.FAMILY, [{"id": "f", "origin": "generated", "item": {},
                                           "gate": {"pass": False, "drop": "gate"}}])  # fmt: skip
    _write_inputs(tmp_path, [failed])
    assert not (wb.work_dir(MODEL) / "wikidata_extra.json").exists()  # never checked
    assert wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={}) == ({}, [], [])
    out = tmp_path / "p.sh"
    assert _script().main([MODEL, "--pool", pool, "--out", str(out)]) == 0
    assert len(out.read_text().splitlines()) == 3  # the header only


def _script():
    sys.path.insert(0, str(store.REPO_ROOT / "scripts"))
    try:
        import wrong_intermediate_rebuild_mc as script
    finally:
        sys.path.pop(0)
    return script


def _patch(iid, fields):
    store.update_report(MODEL, lambda r: r.setdefault("patches", {}).setdefault(
        wb.FAMILY, {}).update({iid: {"set": fields, "note": "plan 0077 §3"}}))  # fmt: skip


def test_rebuild_no_list_strip_patch_and_drop_flags(tmp_store, tmp_path, monkeypatch):
    # X's list is kind-matched at the first bank; then its options lose their facts
    ents = _label_ents(LBL)
    pool, by = _real_bank(tmp_path, _grown(2), ents)
    _shipped(tmp_store, [r for r in by.values() if r["gate"]["pass"]])
    x = "album_label-Q5000-founded_year"
    old = {**by[x]["item"]["mc"], "pool": wb.MC_POOL, "distractors": ["Old A", "Old B"]}
    _patch(x, {"target": "1990", "mc": old})  # a §3-era patch
    data = json.loads((tmp_store / f"{wb.FAMILY}.json").read_text())
    data["items"][0]["mc"] = old  # shipped as patched
    (tmp_store / f"{wb.FAMILY}.json").write_text(json.dumps(data))
    thin = {q: e for q, e in ents.items() if q in (_q("Arvon Records"), _q("Brisk Records"))}
    (Path(pool) / "entities.json").write_text(json.dumps(thin))
    store.record_gates(MODEL, wb.FAMILY, [{**by[x], "item": {**by[x]["item"], "mc": old}}])
    patches, no_list, poorer = wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={})
    assert no_list == [x] and not poorer and x not in patches
    real = wb.rebuild_mc
    monkeypatch.setattr(wb, "rebuild_mc", lambda m, ids=None, pool=None: real(
        m, ids=ids, pool=pool, registered=[], wd={}, r4={}))  # fmt: skip
    out = tmp_path / "p.sh"
    assert _script().main([MODEL, "--pool", pool, "--out", str(out)]) == 0
    body = out.read_text().splitlines()[3:]
    assert not any(" drop " in f" {ln} " for ln in body)
    (strip,) = [ln for ln in body if f"id={x}" in ln]
    kv = dict(a.split("=", 1) for a in shlex.split(strip)[4:])
    assert json.loads(kv["set"]) == {"target": "1990"} and kv["note"] == wb.STRIP_NOTE
    assert _script().main([MODEL, "--pool", pool, "--drop-no-list", "--out", str(out)]) == 0
    argvs = [shlex.split(ln) for ln in out.read_text().splitlines()[3:]]
    drops = [a for a in argvs if a[3] == "drop"]
    assert len(drops) == 1
    kv = dict(a.split("=", 1) for a in drops[0][4:])
    assert kv["ids"] == x and kv["reason"] == "no kind-matched list (plan 0077 §3b)"
    # --ids takes a target; anything else is refused
    assert real(MODEL, ids=[x], pool=pool, registered=[], wd={}, r4={})[1] == [x]
    with pytest.raises(SystemExit, match="not rebuild targets"):
        real(MODEL, ids=["nope"], pool=pool, registered=[], wd={}, r4={})


def test_rebuild_poorer_only_with_drop_poorer(tmp_store, tmp_path, monkeypatch):
    ents = _label_ents(LBL)
    pool, by = _real_bank(tmp_path, _grown(2), ents)
    x = "album_label-Q5000-founded_year"
    old = {**by[x]["item"]["mc"], "pool": wb.MC_POOL}
    old["distractors"] = [*old["distractors"][:3], "Other Label"]
    old["forms"] = {**old["forms"], "Arvon Records": ["Arvon Records", "Arvon"]}
    store.record_gates(MODEL, wb.FAMILY, [{**by[x], "item": {**by[x]["item"], "mc": old}}])
    _shipped(tmp_store, [{**by[x], "item": {**by[x]["item"], "mc": old}}])
    real = wb.rebuild_mc
    assert real(MODEL, pool=pool, registered=[], wd={}, r4={}) == ({}, [], [x])
    monkeypatch.setattr(wb, "rebuild_mc", lambda m, ids=None, pool=None: real(
        m, ids=ids, pool=pool, registered=[], wd={}, r4={}))  # fmt: skip
    out = tmp_path / "p.sh"
    _script().main([MODEL, "--pool", pool, "--out", str(out)])
    assert len(out.read_text().splitlines()) == 3
    _script().main([MODEL, "--pool", pool, "--drop-poorer", "--out", str(out)])
    (ln,) = out.read_text().splitlines()[3:]
    kv = dict(a.split("=", 1) for a in shlex.split(ln)[4:])
    assert shlex.split(ln)[3] == "drop" and kv["ids"] == x
    assert kv["reason"] == "rebuilt list has poorer forms (plan 0077 §3b)"


@pytest.mark.parametrize("stripped", [False, True])
def test_patched_item_fails_then_passes_and_rebuild_emits_branch_b(tmp_store, tmp_path, stripped):
    """A §3 patch, a sentinel failure, a freeze that drops the item, the pool grows, the item
    passes again: the rebuild copies the gate-log list (branch b), or emits nothing when the
    mc-stripping patch already removed the old list."""
    x = "album_label-Q5000-founded_year"
    ents = _label_ents(LBL)
    pool, by = _real_bank(tmp_path, _grown(2), ents)
    run.freeze(MODEL, [wb.FAMILY])
    old = {**by[x]["item"]["mc"], "pool": wb.MC_POOL, "distractors": ["Old A", "Old B"]}
    _patch(x, {"target": "1990", "mc": old})
    # its gate-log row carries the §3-era list too (banked before §3b: never reused)
    store.record_gates(MODEL, wb.FAMILY, [{**by[x], "item": {**by[x]["item"], "mc": old}}])
    run.freeze(MODEL, [wb.FAMILY])
    shipped = {it["name"]: it for it in json.loads((tmp_store / f"{wb.FAMILY}.json").read_text())[
        "items"]}  # fmt: skip
    assert shipped[x]["mc"] == old
    # the pool shrinks to X's own pair (facts lost): a sentinel; a freeze drops it
    thin = {q: e for q, e in ents.items() if q in (_q("Arvon Records"), _q("Brisk Records"))}
    pool, by = _real_bank(tmp_path, _grown(2), thin)
    assert by[x]["gate"]["drop"] == wb.NO_KIND_LIST
    if stripped:
        patches, no_list, _p = wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={})
        assert no_list == [x]
        (line,) = wb.strip_lines(MODEL, no_list)
        kv = dict(a.split("=", 1) for a in shlex.split(line)[4:])
        run.patch(MODEL, wb.FAMILY, x, json.loads(kv["set"]), kv["note"])
    run.freeze(MODEL, [wb.FAMILY])
    names = [it["name"] for it in json.loads((tmp_store / f"{wb.FAMILY}.json").read_text())[
        "items"]]  # fmt: skip
    assert x not in names
    # the pool grows: X passes again with a fresh kind-matched list
    pool, by = _real_bank(tmp_path, _grown(5), ents)
    assert by[x]["gate"]["pass"] and "kind-matched" in by[x]["item"]["mc"]["pool"]
    patches, no_list, poorer = wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={})
    assert not no_list and not poorer
    if stripped:
        assert patches == {}
    else:
        assert patches == {x: json.loads(json.dumps(by[x]["item"]["mc"]))}


# ============================================================================ tables
def _tables():
    spec = importlib.util.spec_from_file_location(
        "build_wd_tables", Path(__file__).resolve().parents[1] / "scripts" / "build_wd_tables.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeApi:
    def __init__(self, p279):
        self.p279, self.asked = p279, []

    def entities(self, ids):
        self.asked += list(ids)
        return {q: {"claims": _claims(P279=self.p279[q])} for q in ids if q in self.p279}


def test_build_wd_tables_only_grows_and_walks_a_new_chain_in_full(monkeypatch):
    mod = _tables()
    monkeypatch.setattr(wb, "CATEGORY_P31", {})
    monkeypatch.setattr(wb, "OCCUPATION_ROOTS", {})
    # the committed table knows Qknown (its walk stopped there); a new occupation Qnew sits one
    # step below it, so Qknown's ancestors must be walked again from Qnew's depth
    existing = {"Qknown": ["Qa"], "Qold": []}
    api = _FakeApi({"Qnew": ["Qknown"], "Qknown": ["Qa"], "Qa": ["Qb"], "Qb": ["Qc"]})
    fresh = mod.walk(set(), {"Qnew"}, api)
    assert set(fresh) == {"Qnew", "Qknown", "Qa"}  # 3 steps from empty (OCC_STEPS)
    merged = mod.grow(existing, fresh)
    assert set(merged) >= set(existing) | set(fresh)
    assert merged["Qold"] == [] and merged["Qknown"] == ["Qa"]
    missing: set[str] = set()
    assert wb.closure(["Qnew"], merged, wb.OCC_STEPS, missing)["Qb"] == 3 and not missing
    assert mod.grow({"Qx": ["Qold_parent"]}, {"Qx": ["Qnew_parent"]}) == {"Qx": ["Qold_parent"]}


def test_registered_option_qids_resolve_as_option_pool_and_are_vendored():
    _h, reg = store.original_bank(wb.FAMILY)
    regd, wd, r4 = wb._registered_inputs(None, None, None)
    pool = wb.option_pool([], regd, r4, wd)
    assert wb.registered_option_qids(regd, r4, wd) == {e["qid"] for e in pool.values()}
    table = wb._vendored(wb.REGISTERED_OPTIONS)["options"]  # the committed table, unpatched
    assert set(table) >= {e["qid"] for e in pool.values()}
    assert all(set(v) == set(wb.KIND_PROPS) for v in table.values())
    assert len(reg) == 100
