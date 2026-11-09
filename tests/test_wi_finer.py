"""wrong_intermediate finer kind key (plan 0086): dims and draw, grown pool, facts cache,
finer-list stability, bank re-admission, undrop and the driver (offline; fakes only)."""

import json
import shlex
import sys

import pytest

from wsbench.adapt import run, store
from wsbench.adapt import wrong_intermediate as wb

MODEL = "qwen3.5-4b"  # a registered subject on the Qwen tokenizer (freeze needs a real one)
NOVELIST = "Q6625963"
LABEL = "Q18127"
JAZZ, ROCK, BLUES, FUNK = "Q8341", "Q11399x", "Q9759", "Q164444"


def _claims(**props):
    return {
        p: [{"rank": "normal", "mainsnak": {"datavalue": {"type": "wikibase-entityid",
                                                          "value": {"id": q}}}} for q in qs]
        for p, qs in props.items()
    }  # fmt: skip


def _ent(label, sl=20, **props):
    return {"labels": {"en": {"value": label}}, "aliases": {}, "claims": _claims(**props),
            "sitelinks_n": sl}  # fmt: skip


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    monkeypatch.setattr(wb, "ADAPT_ITEM_CATEGORY", {})
    monkeypatch.setattr(wb, "class_parents", lambda: {})
    monkeypatch.setattr(wb, "registered_options", lambda: {})


def _fine(p136=(), p135=(), birth=None, human=True, inception=None):
    return {
        "P136": list(p136),
        "P135": list(p135),
        "P27": [],
        "P31": ["Q5"] if human else [],
        "birth": birth,
        "death": None,
        "floruit": None,
        "inception": inception,
        "pub": None,
    }


# ============================================================================ dims
def _it(template="novel_author"):
    return {"name": f"{template}-Q8100-birth_year", "template": template,
            "prompt": "Fact: The author of the book Zzyzx was born in the year",
            "intermediates": ["Alda Quorin"], "true_intermediates": ["Bertil Zanvane"],
            "wikidata": {"S": "Q8100", "B": "Q8102", "B_prime": "Q8101"}}  # fmt: skip


def test_finer_dims_shared_optional_painter_stop_and_era():
    it = _it()
    fine = {"Q8101": _fine([JAZZ, BLUES], ["Qmv"], 1920), "Q8102": _fine([JAZZ], [], 1945)}
    req, opt = wb.finer_dims(it, fine)
    assert req == {"P136": {JAZZ}, "era": (1905, 1960)} and opt == {"P135": {"Qmv"}}
    fine["Q8102"]["birth"] = 1990  # far apart: the gold's own window, optional
    req, opt = wb.finer_dims(it, fine)
    assert "era" not in req and opt["era"] == (1905, 1935)
    stop = sorted(wb.FINER_GENRE_STOP)[0]
    fine = {"Q8101": _fine([stop]), "Q8102": _fine([stop])}
    assert wb.finer_dims(it, fine) == ({}, {})
    painter = _it("painting_creator")
    fine = {"Q8101": _fine([JAZZ], ["Qimp"]), "Q8102": _fine([JAZZ], ["Qimp"])}
    assert wb.finer_dims(painter, fine) == ({"P135": {"Qimp"}}, {})


# ============================================================================ the draw
def _setup(cands, gold_fine, contrast_fine):
    """(item, pool, wd, ents, fine): ``cands`` {name: fine facts}; every name a novelist."""
    it = _it()
    pool, wd, ents = {}, {"entities": {}, "enwiki": {}}, {}
    kind = wb.kind_of("Alda Quorin", wb.category_of("novel_author"))
    fine = {"Q8101": gold_fine, "Q8102": contrast_fine}
    for q, n in (("Q8101", "Alda Quorin"), ("Q8102", "Bertil Zanvane")):
        wd["entities"][q] = {"labels": {"en": n}, "aliases": {}}
        ents[q] = _ent(n, P31=["Q5"], P106=[NOVELIST])
    for k, (n, f) in enumerate(cands.items()):
        q = f"Q77{k:03d}"
        pool[n] = {"qid": q, "kinds": {kind}, "templates": {"novel_author"}}
        wd["entities"][q] = {"labels": {"en": n}, "aliases": {}}
        ents[q] = _ent(n, P31=["Q5"], P106=[NOVELIST])
        fine[q] = f
    return it, pool, wd, ents, fine


def _names(prefix, n):
    return [f"{prefix} {chr(65 + i)}{prefix.lower()}ova" for i in range(n)]


def test_jazz_pair_among_rock_draws_only_jazz_and_is_build_mc_on_the_restricted_pool():
    cands = {**{n: _fine([JAZZ], birth=1930) for n in _names("Jaz", 4)},
             **{n: _fine([ROCK], birth=1930) for n in _names("Roc", 6)}}  # fmt: skip
    it, pool, wd, ents, fine = _setup(cands, _fine([JAZZ], birth=1925), _fine([JAZZ], birth=1935))
    mc = wb.build_finer_mc(it, pool, wd, "t", None, ents, {}, fine, {JAZZ: "jazz"})
    assert set(mc["distractors"]) == set(_names("Jaz", 4))
    assert "kind-matched" in mc["pool"] and mc["pool"].endswith(
        "; finer (j1/1): genre jazz; era 1910-1950"
    )
    direct = wb.build_mc(it, {n: pool[n] for n in _names("Jaz", 4)}, wd, "t", None, ents=ents,
                         reg={})  # fmt: skip
    assert {k: v for k, v in direct.items() if k != "pool"} == {
        k: v for k, v in mc.items() if k != "pool"
    }
    assert wb.is_finer(mc) and wb.finer_belongs(it, mc)


def test_rarest_first_widening_and_era_window():
    # gold genres {BLUES (3 names), FUNK (4 names)}; contrast shares none -> optional, widened
    cands = {
        **{n: _fine([BLUES], birth=1940) for n in _names("Blu", 3)},
        **{n: _fine([FUNK], birth=1940) for n in _names("Fun", 3)},
        **{n: _fine([FUNK], birth=1990) for n in _names("Old", 1)},
    }  # out of the era
    it, pool, wd, ents, fine = _setup(
        cands, _fine([BLUES, FUNK], birth=1940), _fine([], birth=2000)
    )
    mc = wb.build_finer_mc(it, pool, wd, "", None, ents, {}, fine)
    assert "j2/2" in mc["pool"] and not set(mc["distractors"]) & set(_names("Old", 1))
    assert len(set(mc["distractors"]) & set(_names("Blu", 3))) >= 1


def test_sentinels():
    cands = {n: _fine([ROCK], birth=1930) for n in _names("Roc", 6)}
    it, pool, wd, ents, fine = _setup(cands, _fine([JAZZ], birth=1930), _fine([JAZZ], birth=1930))
    assert wb.build_finer_mc(it, pool, wd, "", None, ents, {}, fine) == wb.NO_FINER_LIST
    fine["Q8101"] = {"missing": True}
    assert wb.build_finer_mc(it, pool, wd, "", None, ents, {}, fine) == wb.NO_FINER_KEY
    del ents["Q8101"]
    assert wb.build_finer_mc(it, pool, wd, "", None, ents, {}, fine) == wb.NO_KIND_KEY


# ============================================================================ grown pool
def test_grow_pool(tmp_path):
    d = tmp_path / "r9"
    d.mkdir()
    rows = [["S1", "Q1", 3], ["S2", "Q2", 3], ["S3", "Q3", 3], ["S4", "Q4", 3], ["S5", "Q5x", 3],
            ["S6", "Q6", 3], ["S7", "Q7", 3]]  # fmt: skip
    (d / "pairs_album_performer.json").write_text(json.dumps(rows))
    (d / "pairs_namesake.json").write_text(json.dumps([["S9", "Q9", 3]]))
    ents = {"Q1": _ent("Grown One", P31=["Q5"]),  # added: musician
            "Q2": _ent("Low Links", sl=3, P31=["Q5"]),  # under the floor
            "Q3": _ent("Known Name", P31=["Q5"]),  # a pool name already
            "Q4": _ent("Twin", P31=["Q5"]), "Q5x": _ent("Twin", P31=["Q5"]),  # ambiguous
            "Q6": _ent("A Band", P31=["Q215380"]),  # a group: band
            "Q7": _ent("An Orchestra", P31=["Q42998"]),  # org: never added
            "Q9": _ent("Mixed Template", P31=["Q5"])}  # fmt: skip
    pool = {
        "Known Name": {"qid": "Q333", "kinds": {"musician"}, "templates": {"x"}},
        "Same Qid": {"qid": "Q1", "kinds": {"musician"}, "templates": {"x"}},
    }
    out, kinds = wb.grow_pool(pool, ents, [d], {})
    assert set(out) - set(pool) == {"A Band"}  # Q1's QID is in the pool already
    assert out["A Band"]["kinds"] == {"band"} and kinds["A Band"] == "band"
    del pool["Same Qid"]
    out, kinds = wb.grow_pool(pool, ents, [d], {})
    assert set(out) - set(pool) == {"A Band", "Grown One"}
    assert out["Grown One"] == {"qid": "Q1", "kinds": {"musician"},
                                "templates": {"album_performer"}}  # fmt: skip
    assert pool["Known Name"]["kinds"] == {"musician"} and len(pool) == 1  # not mutated


# ============================================================================ facts cache
@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    return tmp_path / MODEL


def _time(y):
    return [{"mainsnak": {"datavalue": {"value": {"time": f"+{y}-01-01T00:00:00Z"}}}}]


def test_fine_facts_and_labels_cache(tmp_store):
    calls = []

    def fetch(params):
        calls.append(params["ids"])
        ents = {}
        for q in params["ids"].split("|"):
            if q == "Qnone":
                ents[q] = {"missing": ""}
            elif params["props"] == "labels":
                ents[q] = {"labels": {"en": {"value": f"lab-{q}"}}}
            else:
                ents[q] = {"claims": {**_claims(P136=[JAZZ], P31=["Q5"]), "P569": _time(1931)}}
        return {"entities": ents}

    f = wb.fine_facts(MODEL, ["Qa", "Qnone"], fetch=fetch)
    assert (
        f["Qa"]["P136"] == [JAZZ] and f["Qa"]["birth"] == 1931 and f["Qnone"] == {"missing": True}
    )
    assert wb.fine_year(f["Qa"]) == 1931
    wb.fine_facts(MODEL, ["Qa", "Qb"], fetch=fetch)
    assert calls == ["Qa|Qnone", "Qb"]
    assert wb.fine_labels(MODEL, ["Qg"], fetch=fetch) == {"Qg": "lab-Qg"}
    wb.fine_labels(MODEL, ["Qg"], fetch=fetch)
    assert calls[-1] == "Qg" and len(calls) == 3
    assert "Qb" in wb.fine_facts(MODEL, [], fetch=fetch)  # persisted


# ============================================================================ stability
def _finer(it, ds, forms=None):
    g, c = it["intermediates"][0], it["true_intermediates"][0]
    order = [g, c, *ds]
    return {
        "gold": g,
        "contrast": c,
        "distractors": list(ds),
        "order": order,
        "forms": {n: [n] for n in order} | (forms or {}),
        "pool": f"{wb.MC_POOL}; distractors: kind-matched (Q1); finer (j1/1): era 1-2",
    }


def test_finer_belongs_and_mc_reusable():
    it = _it()
    ds = ["Outside Wan", "Outside Two", "Outside Tri", "Outside For"]
    assert wb.mc_reusable(it, _finer(it, ds), {}, {"entities": {}}, None)  # not in the pool
    assert not wb.finer_belongs(it, {**_finer(it, ds), "gold": "Someone"})
    assert not wb.finer_belongs(it, _finer(it, ds[:3]))
    assert not wb.finer_belongs(it, _finer(it, [*ds[:3], "Bertil Zanvane"]))
    in_prompt = _finer(it, [*ds[:3], "Zzyzx Press"], {"Zzyzx Press": ["Zzyzx"]})
    assert not wb.finer_belongs(it, in_prompt)  # a form in the prompt
    shared = _finer(it, ds, {"Outside Wan": ["Quorin"], "Alda Quorin": ["Quorin"]})
    assert not wb.finer_belongs(it, shared)  # a form shared with the gold
    plain = {**_finer(it, ds), "pool": f"{wb.MC_POOL}; distractors: kind-matched (Q1)"}
    assert not wb.mc_reusable(it, plain, {}, {"entities": {}}, None)  # today's rule: not in pool


# ============================================================================ bank
def _row(rid, s, bp, template="film_director", strength=8, hop2="birth_year", b="Q9"):
    e = {
        "id": rid, "S": s, "Bp": bp, "B": b, "template": template, "hop2": hop2,
        "vkind": "year", "Bp_answer": 1946, "answer": {"value": 1931}, "Bp_label": f"X{bp}",
        "Bp_names": [f"X{bp}"], "B_label": f"Y{b}", "B_aliases": [],
        "prompt_2hop": f"Fact: The thing {s} was founded in the year",
    }  # fmt: skip
    ev = {"p2_samples_Ap": strength, "p1_samples_Bp": 10, "lp_Ap_minus_A": 1.0}
    return {"id": rid, "e1": e, "evidence": ev, "gate": {"pass": True, "rate": 0.8}}


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


def _fake_mc(item, opts, wd, topic, kinds=None):
    return {"gold": item["intermediates"][0], "category": wb.category_of(item), "pool": "fake"}


def _bank(tmp_path, gated):
    pool = _write_inputs(tmp_path, gated)
    rows = wb.bank(
        MODEL,
        pool,
        registered=[],
        wd={},
        r4={},
        fetch=lambda q, t: {"entities": {}, "enwiki": {}},
        make_mc=_fake_mc,
    )
    return {r["id"]: r for r in rows}


def _patch(iid, fields, note="n"):
    run.patch(MODEL, wb.FAMILY, iid, fields, note)


def test_readmitted_leaker_takes_its_patched_list_and_keeps_its_s_slot(tmp_store, tmp_path):
    x = _row("x", "QS", "QB1", hop2="birth_year", strength=8)  # the recovered leaker
    y = _row("y", "QS", "QB2", hop2="birth_city", strength=10)  # a stronger S-sibling
    x_name, y_name = "film_director-QS-birth_year", "film_director-QS-birth_city"
    by = _bank(tmp_path, [x])
    assert by[x_name]["gate"]["pass"]
    run.drop(MODEL, wb.FAMILY, [x_name], "lucky-guessing leaker, wi-r4 draw 1")
    by = _bank(tmp_path, [y, x])
    assert by[y_name]["gate"]["drop"] == "cap: one item per subject S"  # the reservation
    item = by[x_name]["item"]
    mc = _finer(item, ["Dee One", "Dee Two", "Dee Tri", "Dee For"])
    _patch(x_name, {"mc": mc})
    run.undrop(MODEL, wb.FAMILY, [x_name])
    by = _bank(tmp_path, [y, x])
    assert by[x_name]["gate"]["pass"] and by[x_name]["item"]["mc"] == mc
    assert by[y_name]["gate"]["drop"] == "cap: one item per subject S"
    # now an incumbent: even with the patch gone, its measured finer list is reused
    store.update_report(MODEL, lambda r: r["patches"][wb.FAMILY].pop(x_name))
    by = _bank(tmp_path, [y, x])
    assert by[x_name]["gate"]["pass"] and by[x_name]["item"]["mc"] == mc
    assert by[y_name]["gate"]["drop"] == "cap: one item per subject S"


def test_readmitted_row_cut_by_a_cap_keeps_its_slots(tmp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "PER_TEMPLATE", 1)
    inc = _row("inc", "Q1", "QB0", template="film_director", strength=10)
    x = _row("x", "QS", "QB", template="film_director", strength=9)  # re-admitted, cut by template
    y = _row("y", "Q3", "QB", template="novel_author", strength=10)  # stronger, same B'
    sib = _row("s", "QS", "QB7", template="novel_author", hop2="birth_city", strength=10)
    x_name = "film_director-QS-birth_year"
    by = _bank(tmp_path, [inc, x])
    assert by[x_name]["gate"]["drop"] == "cap: 1 per template"
    _patch(x_name, {"mc": _finer(by[x_name]["item"], ["Dee One", "Dee Two", "Dee Tri", "Dee For"])})
    monkeypatch.setattr(wb, "PER_BRIDGE", 1)
    by = _bank(tmp_path, [y, sib, inc, x])
    assert by[x_name]["gate"]["drop"] == "cap: 1 per template"
    assert by["novel_author-Q3-birth_year"]["gate"]["drop"] == "cap: 1 per wrong intermediate"
    assert by["novel_author-QS-birth_city"]["gate"]["drop"] == "cap: one item per subject S"


def test_finer_patch_is_checked_on_the_patched_item(tmp_store, tmp_path):
    x = _row("x", "Q2", "QB", strength=9)
    x_name = "film_director-Q2-birth_year"
    by = _bank(tmp_path, [x])
    item = by[x_name]["item"]
    run.drop(MODEL, wb.FAMILY, [x_name], "lucky-guessing leaker")
    mc = _finer(item, ["Dee One", "Dee Two", "Dee Tri", "Dee For"])
    # an earlier patch's prompt names a distractor: freeze would ship it, so bank refuses the list
    _patch(x_name, {"mc": mc, "prompt": "Fact: The Dee One thing was founded in the year"})
    run.undrop(MODEL, wb.FAMILY, [x_name])
    by = _bank(tmp_path, [x])
    assert by[x_name]["item"]["mc"] != mc


def test_readmitted_row_still_takes_caps(tmp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "PER_BRIDGE", 1)
    inc = _row("inc", "Q1", "QB", strength=5)
    x = _row("x", "Q2", "QB", strength=9)
    by = _bank(tmp_path, [inc])
    x_name = "film_director-Q2-birth_year"
    store.record_gates(MODEL, wb.FAMILY, [{"id": x_name, "origin": "generated", "item": {},
                                           "gate": {"pass": False, "drop": "x"}}])  # fmt: skip
    _patch(
        x_name,
        {
            "mc": _finer(
                by["film_director-Q1-birth_year"]["item"]
                | {"intermediates": ["XQB"], "true_intermediates": ["YQ9"]},
                ["A a", "B b", "C c", "D d"],
            )
        },
    )
    by = _bank(tmp_path, [x, inc])
    assert by[x_name]["gate"]["drop"] == "cap: 1 per wrong intermediate"  # the incumbent keeps it


def test_undrop(tmp_store, tmp_path):
    _bank(tmp_path, [_row("a", "Q1", "QB")])
    n = "film_director-Q1-birth_year"
    run.drop(MODEL, wb.FAMILY, [n], "lucky-guessing leaker")
    with pytest.raises(SystemExit):
        run.undrop(MODEL, wb.FAMILY, [n, "nope"])
    assert n in store.read_report(MODEL)["drops"][wb.FAMILY]
    run.undrop(MODEL, wb.FAMILY, [n])
    assert n not in store.read_report(MODEL)["drops"][wb.FAMILY]


# ============================================================================ round trip
LBL = ["Arvon Records", "Brisk Records", "Cedra Records", "Dunmo Records", "Elkin Records",
       "Fyrro Records", "Gaunt Records", "Hollo Records"]  # fmt: skip


def _q(name):
    return f"Q6{LBL.index(name):03d}"


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


def _gated():
    return [_label_row(k, LBL[2 * k], LBL[2 * k + 1], 10 - k) for k in range(4)]


def _real_bank(tmp_path, ents):
    pool = _write_inputs(tmp_path, _gated(), ents)
    rows = wb.bank(
        MODEL, pool, registered=[], wd={}, r4={}, fetch=lambda q, t: {"entities": {}, "enwiki": {}}
    )
    return pool, {r["id"]: r for r in rows}


def test_round_trip_patch_undrop_bank_freeze_rebuild(tmp_store, tmp_path, capsys):
    ents = {_q(n): _ent(n, P31=[LABEL]) for n in LBL}
    x = "album_label-Q5000-founded_year"
    pool, by = _real_bank(tmp_path, ents)
    assert by[x]["gate"]["pass"]
    first = by[x]["item"]["mc"]
    run.drop(MODEL, wb.FAMILY, [x], "lucky-guessing leaker, wimc1")
    pool, by = _real_bank(tmp_path, ents)
    zed = ["Zed One Music", "Zed Two Music", "Zed Tri Music", "Zed For Music"]
    gold_contrast = [first["gold"], first["contrast"]]
    forms = {n: first["forms"][n] for n in gold_contrast} | {n: [n] for n in zed}
    mc = {**first, "distractors": zed, "order": [*gold_contrast, *zed], "forms": forms}
    mc["pool"] += "; finer (j1/1): era 1950-1980"
    _patch(x, {"mc": mc}, "plan 0086")
    run.undrop(MODEL, wb.FAMILY, [x])
    pool, by = _real_bank(tmp_path, ents)
    assert by[x]["gate"]["pass"] and by[x]["item"]["mc"] == mc
    run.freeze(MODEL, [wb.FAMILY])
    shipped = {it["name"]: it for it in json.loads((tmp_store / f"{wb.FAMILY}.json").read_text())[
        "items"]}  # fmt: skip
    assert shipped[x]["mc"] == mc and shipped[x]["adapt"]["patch"] == "plan 0086"
    (wb.work_dir(MODEL) / "wikidata_extra.json").write_text(json.dumps({"entities": {}}))
    assert wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={}) == ({}, [], [])
    # a later patch puts a distractor's name in the prompt: warned, never redrawn
    bad = {**mc, "forms": {**mc["forms"], "Zed For Music": ["Zed For Music", "Zq0"]}}
    _patch(x, {"mc": bad, "prompt": shipped[x]["prompt"]})
    shipped[x]["mc"] = bad
    (tmp_store / f"{wb.FAMILY}.json").write_text(json.dumps({"items": list(shipped.values())}))
    assert wb.rebuild_mc(MODEL, pool=pool, registered=[], wd={}, r4={}) == ({}, [], [])
    assert "WARNING" in capsys.readouterr().out


# ============================================================================ driver
def _script():
    sys.path.insert(0, str(store.REPO_ROOT / "scripts"))
    try:
        import wrong_intermediate_finer as script
    finally:
        sys.path.pop(0)
    return script


def test_driver_build_and_emit(tmp_store, tmp_path, monkeypatch):
    names = LBL + [f"Extra {c} Records" for c in "ABCDEFGH"]
    q = {n: f"Q7{k:03d}" for k, n in enumerate(names)}
    q.update({n: _q(n) for n in LBL})
    ents = {q[n]: _ent(n, P31=[LABEL]) for n in names}
    _real_bank(tmp_path, ents)
    pool = _write_inputs(tmp_path, _gated(), ents)
    d = tmp_path / "pool"
    rows = [[f"S{k}", q[n], 3] for k, n in enumerate(names[8:])]
    (d / "pairs_album_label.json").write_text(json.dumps(rows))
    x, y = "album_label-Q5000-founded_year", "album_label-Q5001-founded_year"
    run.drop(MODEL, wb.FAMILY, [x], "lucky-guessing leaker, wimc1")
    _patch(x, {"target": "1990"})
    fine = {qq: _fine(human=False, inception=1960) for qq in q.values()}
    fine[_q("Cedra Records")] = {"missing": True}  # y's gold: no finer key
    fine["_lab"] = {}
    work = wb.work_dir(MODEL)
    (work / wb.FINE_CACHE).write_text(json.dumps(fine))
    (work / "wikidata_extra.json").write_text(json.dumps({"entities": {}}))
    lab = {qq: n for n, qq in q.items()}

    def fake_labels(model, qids, wd, fetch=None):
        wd["entities"].update({qq: {"labels": {"en": lab[qq]}, "aliases": {}} for qq in qids})

    monkeypatch.setattr(wb, "fetch_labels", fake_labels)
    s = _script()
    out = tmp_path / "built.json"
    assert s.main(["build", MODEL, "--ids", x, y, "--grow", "--pool", pool, "--json",
                   str(out)]) == 0  # fmt: skip
    built = json.loads(out.read_text())
    assert built[x]["status"] == "built" and built[y]["status"] == "no finer key"
    assert built[x]["drop"] == "lucky-guessing leaker, wimc1" and built[x]["level"] == "j1/1"
    assert wb.is_finer(built[x]["item"]["mc"])
    sh = tmp_path / "recover.sh"
    with pytest.raises(SystemExit):
        s.main(["emit", MODEL, "--json", str(out), "--ids", y, "--out", str(sh)])
    assert s.main(["emit", MODEL, "--json", str(out), "--ids", x, "--out", str(sh)]) == 0
    patch_line, undrop_line = sh.read_text().splitlines()[3:]
    kv = dict(a.split("=", 1) for a in shlex.split(patch_line)[4:])
    assert json.loads(kv["set"]) == {"target": "1990", "mc": built[x]["item"]["mc"]}
    assert kv["note"].endswith("re-admitted from drop: lucky-guessing leaker, wimc1")
    assert shlex.split(undrop_line)[4:] == [f"model={MODEL}", f"family={wb.FAMILY}", f"ids={x}"]
