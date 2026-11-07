"""plan 0078 tier C (R51-R57), offline: the ported gw generator with a fake Claude, the pinned batch
loader, and the extension epoch's selection, stop and grade."""

import hashlib
import json
import re

import pytest

from wsbench.adapt import mmh_tierc as t


def test_ported_prompts_equal_gw_by_sha256():
    assert t.ported_hashes() == t.PORTED_SHA256  # gw 2027ef208 prompts.py, verbatim
    assert len(t.SIB_CLUSTERS) == 34 and set(t.INVENTED_TYPES) == set(t.INV_TYPE_KEYS)


def test_reply_items_and_screen_rules():
    good = {"type": "dish", "name": "the Amber Loaf", "pre": "p", "b": "b?", "ref_after": "r",
            "ref_first": "r1", "role": "ro"}  # fmt: skip
    js = {"items": [good, {**good, "type": "planet"}, {**good, "pre": " "}, "x"]}
    assert t.reply_items(js) == [good] and t.reply_items(None) == []
    top = [{"country": "Spain", "p": 0.4}, {"country": "Chile", "p": 0.5}]
    assert t.screen_verdict({"real": False, "marker": False, "top5": top}) == (
        None,
        ["Chile", "Spain"],
    )
    assert t.screen_verdict({"real": True, "top5": top})[0] == "real"
    assert t.screen_verdict({"marker": True, "top5": top})[0] == "marker"
    assert t.screen_verdict({"top5": [{"country": "Peru", "p": 0.7}]})[0] == "top1>=0.7"
    assert t.screen_verdict({"top5": []})[0] == "top1>=0.7"
    assert t.screen_verdict(None)[0] == "screen_failed"


def test_name_index_whole_word_containment():
    ix = t.NameIndex(["the Blue Fort", "Mara Soto"])
    assert ix.dup("Blue Fort") and ix.dup("the Blue Fort Bridge") and ix.dup("mara soto")
    assert not ix.dup("the Bluebell Fortress") and not ix.dup("Soto Lane Market Hall")


def _fake(seed_names):
    calls = {"gen": 0}

    def gen(prompt, valid=None):
        calls["gen"] += 1
        c = next(k for k, v in t.SIB_CLUSTERS.items() if ", ".join(v) in prompt)
        n = calls["gen"]
        items = [
            {"type": "event", "name": f"{c} Cup {n} {j}", "pre": "p.", "b": "Where is it?",
             "ref_after": "the country it is in", "ref_first": "the country of it",
             "role": "the country it is in"}
            for j in range(3)
        ]  # fmt: skip
        items.append(dict(items[0], name=seed_names[0]))  # a known entity: dup
        return {"items": items}

    def screen(prompt, valid=None):
        name = re.search(r'"(.+?)"\. Context', prompt).group(1)
        if name.endswith(" 2"):
            return {"real": False, "marker": True, "top5": []}
        return {"real": False, "marker": False, "top5": [{"country": "A", "p": 0.3}]}

    return gen, screen


def test_build_batch_cut_ids_and_drops():
    known = ["the Old Gate"]
    gen, screen = _fake(known)
    res = t.build_batch(3, gen, screen, known, known, n=100, workers=1)
    rows = res["rows"]
    assert len(rows) == 100 and not res["short"]
    assert [r["id"] for r in rows[:2]] == ["c3iv0000", "c3iv0001"]
    assert all(r["tier"] == "C" and r["batch"] == 3 and "name" not in r for r in rows)
    assert all(r["b"].endswith(t.SUFFIX.format(t="country")) for r in rows)
    assert res["drops"]["marker"] > 0 and res["drops"]["dup"] > 0
    # round-robin: the first pass gives every cluster before any second-pass row
    assert len({r["cluster"] for r in rows[:68]}) == 34


def test_empty_clusters_retire_and_short_batch_is_kept():
    asked = []

    def gen(prompt, valid=None):
        asked.append(prompt)
        js = {"items": []}
        assert valid is not None and not valid(js)  # an empty reply never counts as valid
        return None  # the client's verdict after retries: nothing (never cached)

    res = t.build_batch(1, gen, lambda p, valid=None: None, [], [], n=10, workers=1)
    assert res["rows"] == [] and res["short"] and len(res["retired"]) == 34
    assert len(asked) == 34 * t.EMPTY_MAX and res["n_pass"] == t.EMPTY_MAX


def test_avoid_list_is_the_tail_plus_this_clusters_raw_names():
    gen, screen = _fake(["the Old Gate"])
    seen = []

    def g(prompt, valid=None):
        seen.append(prompt)
        return gen(prompt, valid)

    tail = [f"N{i}" for i in range(200)]
    t.build_batch(1, g, screen, ["the Old Gate"], tail, n=5, workers=1)
    first = seen[0]
    assert "N80; " in first and "N79;" not in first  # the last 120 earlier names only


def test_load_batches_refuses_and_merges(tmp_path):
    rows = [{"id": "c1iv0000", "tier": "C", "batch": 1, "cat": "invented"}]
    p = t.batch_path(tmp_path, 1)
    p.write_text(json.dumps({"header": {}, "invented": rows}))
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    assert t.load_batches(tmp_path, {1: sha}) == rows
    with pytest.raises(SystemExit, match="sha256"):
        t.load_batches(tmp_path, {1: "0" * 64})
    with pytest.raises(SystemExit, match="missing"):
        t.load_batches(tmp_path, {2: sha})
    p.write_text(json.dumps({"header": {}, "invented": [dict(rows[0], tier="B")]}))
    with pytest.raises(SystemExit, match="bad row"):
        t.load_batches(tmp_path, {1: hashlib.sha256(p.read_bytes()).hexdigest()})


# ---------------------------------------------------------------- the extension epoch (R53-R56)
from test_adapt_model_multihop import MODEL, _write_baseline  # noqa: E402

from wsbench.adapt import mmh, store  # noqa: E402


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    return tmp_path / MODEL


def _row(i, bridge, ent, tier="B"):
    opts = [bridge, "Alpha", "Beta", "Gamma", "Delta"]
    return {
        "id": i,
        "origin": "refill",
        "gate": {"pass": True},
        "tier": tier,  # as `build` writes the record
        "item": {
            "id": i,
            "cat": "invented",
            "bridge": bridge,
            "entity": ent,
            "mc": {"order": opts, "gold": bridge},
            "pool_tier": tier,
        },
    }


def _setup(n_base=20, n_tc=10, lk_extra=None):
    base = [_row(f"b{k}", f"B{k % 10}", f"the base {k} hall") for k in range(n_base)]
    tc = [_row(f"c1iv{k:04d}", f"T{k}", f"the new {k} lake", "C") for k in range(n_tc)]
    old = [_row("r9iv0001", "Z", "the old lake", "B")]  # tier B, never shipped
    rows = base + tc + old
    draws = {r["id"]: {"blind": [0], "described": [0]} for r in rows}
    lk = {
        "round": 1,
        "draws": draws,
        "confirms": [],
        "final": {
            "ids": [r["id"] for r in base],
            "leakers": [],
            "round": 1,
            "mc_hash": {r["id"]: mmh.mc_hash(r["item"]) for r in base},
        },
        **(lk_extra or {}),
    }
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    store.write_json(
        store.model_dir(MODEL) / f"{mmh.FAMILY}.json",
        {"header": {}, "items": [r["item"] for r in base]},
    )
    return base, tc, old, rows


def _open(base, sum_eligible=10):
    store.write_json(
        mmh.work_dir(MODEL) / "tierc.json",
        {"batches": 1, "evaluated_batches": 1, "stop": "(i)", "sum_eligible": sum_eligible},
    )
    return mmh.extend(MODEL)


def _freeze(items):
    store.write_json(store.model_dir(MODEL) / f"{mmh.FAMILY}.json", {"header": {}, "items": items})


def test_epoch_selection_pins_base_adds_tier_c_only(ws):
    base, tc, _old, rows = _setup()
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    lk["draws"]["c1iv0003"] = {"blind": [4], "described": [1]}  # a screen leaker
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    tc[5]["item"]["entity"] = "the base 3 hall annex"  # a near-duplicate of a base entity
    _open(base)
    ids = [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]
    assert set(ids) >= {r["id"] for r in base}
    assert "r9iv0001" not in ids and "c1iv0003" not in ids and "c1iv0005" not in ids
    assert {i for i in ids if i.startswith("c1")} == {
        f"c1iv{k:04d}" for k in (0, 1, 2, 4, 6, 7, 8, 9)
    }


def test_epoch_cap_trims_tier_c_only_and_changed_base_is_refused(ws):
    base, tc, _old, rows = _setup()
    for r in tc:
        r["item"]["bridge"] = "B0"  # base holds 2 x B0; the cap at 30 items is 4
    out = mmh.capped_pinned(base, tc)
    assert all(r in out for r in base) and sum(r["item"]["bridge"] == "B0" for r in out) <= 4
    _open(base)
    base[0]["item"]["mc"] = {**base[0]["item"]["mc"], "forms": {"x": ["y"]}}
    with pytest.raises(SystemExit, match="mc changed"):
        mmh.select_rows(MODEL, rows, 283)


def _union(base, tc):
    items = [r["item"] for r in base + tc]
    _freeze(items)
    return [r["id"] for r in base + tc]


def test_confirm_ext_pass_path_kimi_like_state_never_touches_lucky_screen(ws, tmp_path):
    failed = {"round": 1, "pass": False, "ids": ["x"], "src": "/nowhere/c1"}
    base, tc, _old, rows = _setup(lk_extra={"round": 2, "confirms": [failed]})
    _open(base)
    sel = mmh.select_rows(MODEL, rows, 283)  # epoch round 1 is never pooled
    assert len(sel) == len(base) + len(tc)
    ids = _union(base, tc)
    before = (mmh.work_dir(MODEL) / "lucky_screen.json").read_bytes()
    hits = {i: {"blind": 0, "described": 0} for i in ids}
    hits = {**hits, "c1iv0000": {"blind": 3, "described": 2}, "b1": {"blind": 3, "described": 2}}
    _write_baseline(tmp_path / "e1", hits)
    rec = mmh.confirm_ext(MODEL, str(tmp_path / "e1"), "confirm")
    assert rec["pass"] and rec["tierc_leakers"] == ["c1iv0000"] and "b1" in rec["leakers"]
    assert (mmh.work_dir(MODEL) / "lucky_screen.json").read_bytes() == before
    sel = [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]
    assert "c1iv0000" not in sel and "b1" in sel  # tier C leakers only, no back-fill
    with pytest.raises(SystemExit, match="next epoch measurement"):
        mmh.confirm_ext(MODEL, str(tmp_path / "e1x"), "remeasure")
    ids2 = [i for i in ids if i != "c1iv0000"]
    _freeze([r["item"] for r in base + tc if r["id"] in ids2])
    zero = {i: {"blind": 0, "described": 0} for i in ids2}
    _write_baseline(tmp_path / "f1", zero)
    assert mmh.confirm_ext(MODEL, str(tmp_path / "f1"), "floor")["pass"]
    assert (mmh.work_dir(MODEL) / "lucky_screen.json").read_bytes() == before
    _write_baseline(tmp_path / "r1", zero)
    assert mmh.confirm_ext(MODEL, str(tmp_path / "r1"), "remeasure")["pass"]
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    assert lk["final"]["extension"] and set(lk["final"]["ids"]) == set(ids2)
    assert lk["final"]["leakers"] == [] and set(lk["final"]["base"]) == {r["id"] for r in base}
    assert lk["confirms"][-1]["retest_of_final"] and lk["confirms"][-1]["round"] == 2
    assert not lk.get("blocked") and mmh._ext(MODEL)["state"] == "closed: passed"


def test_tier_c_subset_must_meet_the_bar_on_its_own(ws, tmp_path):
    base, tc, _old, _rows = _setup(n_base=95, n_tc=10)
    _open(base)
    ids = _union(base, tc)
    hits = {i: {"blind": 0, "described": 0} for i in ids}
    hits |= {r["id"]: {"blind": 2, "described": 2} for r in tc}  # tier C at .4 vs .25
    _write_baseline(tmp_path / "e1", hits)
    rec = mmh.confirm_ext(MODEL, str(tmp_path / "e1"), "confirm")
    assert rec["union_pass"] and not rec["tierc_pass"] and not rec["pass"]


def test_failures_go_to_round_two_then_abandon_never_block(ws, tmp_path):
    failed = {"round": 1, "pass": False, "ids": ["x"], "src": "/nowhere/c1"}
    base, tc, _old, rows = _setup(lk_extra={"round": 2, "confirms": [failed]})
    final0 = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())["final"]
    _open(base)
    ids = _union(base, tc)
    bad = {i: {"blind": 0, "described": 0} for i in ids}
    bad |= {r["id"]: {"blind": 5, "described": 5} for r in tc[:6]}  # pooled out next round
    _write_baseline(tmp_path / "e1", bad)
    assert not mmh.confirm_ext(MODEL, str(tmp_path / "e1"), "confirm")["pass"]
    assert mmh._ext(MODEL)["step"] == "round2"
    with pytest.raises(SystemExit, match="extension epoch is open"):
        mmh.confirm(MODEL, str(tmp_path / "e1b"))
    mmh.extend(MODEL, 2)
    sel = [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]
    assert all(r["id"] in sel for r in base) and "c1iv0000" not in sel  # pooled out
    _freeze([r["item"] for r in base + tc if r["id"] in sel])
    bad2 = {i: {"blind": 0, "described": 0} for i in sel}
    bad2 |= {i: {"blind": 4, "described": 4} for i in sel if i.startswith("c1")}
    _write_baseline(tmp_path / "e2", bad2)
    assert not mmh.confirm_ext(MODEL, str(tmp_path / "e2"), "confirm")["pass"]
    ext = mmh._ext(MODEL)
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    assert ext["state"] == "closed: abandoned" and not lk.get("blocked")
    assert lk["final"] == final0 and len(lk["confirms"]) == 1
    assert [r["id"] for r in mmh.select_rows(MODEL, rows, 283)] == [r["id"] for r in base]


def test_tier_c_rows_never_enter_outside_an_epoch(ws):
    _base, _tc, _old, rows = _setup()
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    lk.pop("final")
    lk["round"] = 2
    lk["confirms"] = [{"round": 1, "pass": False, "ids": ["x"], "src": "/n"}]
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    sel = [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]
    assert sel and not any(i.startswith("c1") for i in sel)


def test_extension_final_trim_never_removes_a_base_item(ws):
    base, tc, _old, rows = _setup(n_base=20, n_tc=2)
    for r in base[:4] + tc:
        r["item"]["bridge"] = "US"  # 6 of 22 > the cap of 3
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    allr = base + tc
    lk["final"] = {
        "ids": [r["id"] for r in allr],
        "leakers": [],
        "mc_hash": {r["id"]: mmh.mc_hash(r["item"]) for r in allr},
        "extension": True,
        "base": [r["id"] for r in base],
    }
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    sel = [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]
    assert all(r["id"] in sel for r in base) and not any(r["id"] in sel for r in tc)


def test_grade_partial_only_when_tier_c_is_exhausted(ws, monkeypatch):
    monkeypatch.setattr(mmh, "pool_rows", lambda limit=0, model=None: [])
    store.write_json(mmh.work_dir(MODEL) / "gen1.json", {})
    assert mmh.grade(MODEL, 70)[0] == "refill_pending"
    store.write_json(
        mmh.work_dir(MODEL) / "tierc.json",
        {"stop": "(ii) x", "sum_eligible": 0, "batches": 1, "evaluated_batches": 1},
    )
    st, why, _ = mmh.grade(MODEL, 70)
    assert st == "partial" and "no eligible item" in why
    store.write_json(
        mmh.work_dir(MODEL) / "tierc.json",
        {"stop": "(i) x", "sum_eligible": 9, "batches": 1, "evaluated_batches": 1},
    )
    assert mmh.grade(MODEL, 70)[0] == "refill_pending"
    store.write_json(mmh.work_dir(MODEL) / "extend.json", {"state": "closed: abandoned"})
    assert mmh.grade(MODEL, 70)[0] == "partial" and mmh.grade(MODEL, 120)[0] == "full"


def test_per_subject_pool_takes_only_its_batches(ws, tmp_path, monkeypatch):
    rows1 = [{"id": "c1iv0000", "tier": "C", "batch": 1, "cat": "invented"}]
    rows2 = [{"id": "c2iv0000", "tier": "C", "batch": 2, "cat": "invented"}]
    pins = {}
    for k, rs in ((1, rows1), (2, rows2)):
        p = t.batch_path(tmp_path, k)
        p.write_text(json.dumps({"header": {}, "invented": rs}))
        pins[k] = hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setattr(t, "TIERC_SHA256", pins)
    monkeypatch.setenv("WSB_POOL_DIR", str(tmp_path))
    monkeypatch.setattr(mmh, "load_pool", lambda: {"invented": [{"id": "a"}], "cutoff": []})
    assert [r["id"] for r in mmh.pool_rows(model=MODEL)] == ["a"]
    mmh.tierc(MODEL, take=1)
    assert [r["id"] for r in mmh.pool_rows(model=MODEL)] == ["a", "c1iv0000"]
    assert [r["id"] for r in mmh.pool_rows(model="other")] == ["a"]
    with pytest.raises(SystemExit, match="already takes"):
        mmh.tierc(MODEL, take=0)


def test_extension_final_retest_always_needs_the_upper_bound(ws, tmp_path):
    base, _tc, _old, _rows = _setup(n_base=110, n_tc=0)
    ids = [r["id"] for r in base]
    hits = {i: {"blind": (5 if k < 25 else 0), "described": 0} for k, i in enumerate(ids)}
    # blind mean .25 + .2 x ... : point rate within the bar, the item-level bound above it
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    lk["final"]["extension"] = True
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    _write_baseline(tmp_path / "rt", hits)
    rec = mmh.confirm(MODEL, str(tmp_path / "rt"))
    r = rec["rates_pre_drop"]["blind"]
    assert r <= rec["chance"] + mmh.BAR and rec["upper90"]["blind"] > rec["chance"] + mmh.BAR
    assert rec["pass"] is False


def _gate_file(rows):
    store.record_gates(MODEL, mmh.FAMILY, rows)


def test_tierc_evaluates_complete_batches_with_the_capped_stop(ws, monkeypatch):
    _base, tc, _old, rows = _setup(n_base=70, n_tc=40)
    for r in tc[:30]:
        r["item"]["bridge"] = "B0"  # base already has 7 x B0: only 8 more fit at 100
    monkeypatch.setattr(
        mmh,
        "tierc_rows",
        lambda model: (
            [{"id": r["id"], "batch": 1} for r in tc]
            + [{"id": f"c1iv9{k:03d}", "batch": 1} for k in range(600)]
        ),
    )
    store.write_json(mmh.work_dir(MODEL) / "tierc.json", {"batches": 1})
    _gate_file(rows)
    with pytest.raises(SystemExit, match="no gate record"):
        mmh.tierc(MODEL)
    _gate_file(
        [{**_row(f"c1iv9{k:03d}", "Q", f"q {k}", "C"), "gate": {"pass": False}} for k in range(600)]
    )
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    lk["draws"].pop("c1iv0001")
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    with pytest.raises(SystemExit, match="not screened"):
        mmh.tierc(MODEL)
    lk["unscreened"] = ["c1iv0001"]
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    st = mmh.tierc(MODEL)
    # 39 eligible, but only 8 B0 + 10 others count at the target: 18 < 2 x 30 -> no stop (i)
    assert st["sum_eligible"] == 39 and st["capped_eligible"] == 18 and st["stop"] is None
    mmh.tierc(MODEL, take=2)
    with pytest.raises(SystemExit, match="at most 6"):
        mmh.tierc(MODEL, take=7)


def test_epoch_with_no_tier_c_closes_and_take_is_refused(ws):
    base, _tc, old, _rows = _setup(n_tc=0)
    _open(base)
    assert mmh.select_rows(MODEL, base + old, 283) == base
    assert mmh._ext(MODEL)["state"] == "closed: no tier C items"
    with pytest.raises(SystemExit, match="one epoch per subject"):
        mmh.tierc(MODEL, take=1)


def test_remeasure_refuses_a_bank_holding_the_floors_tier_c_leakers(ws, tmp_path):
    base, tc, _old, rows = _setup()
    _open(base)
    ids = _union(base, tc)
    zero = {i: {"blind": 0, "described": 0} for i in ids}
    _write_baseline(tmp_path / "e1", zero)
    assert mmh.confirm_ext(MODEL, str(tmp_path / "e1"), "confirm")["pass"]
    _write_baseline(tmp_path / "f1", {**zero, "c1iv0002": {"blind": 3, "described": 2}})
    assert mmh.confirm_ext(MODEL, str(tmp_path / "f1"), "floor")["pass"]
    _write_baseline(tmp_path / "r1", zero)
    with pytest.raises(SystemExit, match="re-freeze first"):
        mmh.confirm_ext(MODEL, str(tmp_path / "r1"), "remeasure")
    assert "c1iv0002" not in [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]


def test_base_retest_rule_and_it_clears_on_any_outcome(ws, tmp_path):
    base, tc, _old, rows = _setup()
    _open(base)
    ids = _union(base, tc)
    worst = {i: {"blind": 5, "described": 5} for i in ids}  # even the base is far above the bar
    _write_baseline(tmp_path / "e1", worst)
    assert not mmh.confirm_ext(MODEL, str(tmp_path / "e1"), "confirm")["pass"]
    mmh.extend(MODEL, 2)
    assert mmh.select_rows(MODEL, rows, 283) == base  # nothing meets the bar: abandon
    ext = mmh._ext(MODEL)
    assert ext["state"] == "closed: abandoned" and ext["base_retest"]
    _freeze([r["item"] for r in base])
    with pytest.raises(SystemExit, match="base re-test"):
        mmh.status(MODEL)
    _write_baseline(tmp_path / "bt", {r["id"]: {"blind": 4, "described": 4} for r in base})
    assert not mmh.confirm(MODEL, str(tmp_path / "bt"))["pass"]  # R42 then applies
    ext = mmh._ext(MODEL)
    assert "base_retest" not in ext and ext["base_retest_done"]["pass"] is False


def test_epoch_round_two_counts_only_the_failed_draw_for_the_base(ws, tmp_path, monkeypatch):
    base, tc, _old, rows = _setup()
    _open(base)
    ids = _union(base, tc)
    hits = {i: {"blind": 0, "described": 0} for i in ids}
    hits |= {r["id"]: {"blind": 5, "described": 5} for r in tc[:6]}
    _write_baseline(tmp_path / "e1", hits)
    mmh.confirm_ext(MODEL, str(tmp_path / "e1"), "confirm")
    mmh.extend(MODEL, 2)
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    for r in base:  # old base draws far above chance must not count in the epoch prefix
        lk["draws"][r["id"]] = {"blind": [5, 5, 5], "described": [5, 5, 5]}
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    seen = {}
    orig = mmh.bar_prefix

    def spy(ranked, draws):
        seen.update(draws)
        return orig(ranked, draws)

    monkeypatch.setattr(mmh, "bar_prefix", spy)
    sel = [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]
    assert seen["b0"] == {"blind": [0], "described": [0]}
    assert len(sel) > len(base) and mmh._ext(MODEL)["state"] == "open"


def test_pass_path_dropping_every_tier_c_item_closes_the_epoch(ws):
    # unreachable through the subset bar today (a leaker hits >= 3/5), kept as a guard
    base, tc, _old, rows = _setup(n_tc=2)
    _open(base)
    ext = mmh._ext(MODEL)
    ids = [r["id"] for r in base + tc]
    ext.update(step="floor", passed={"ids": ids, "tierc_leakers": [r["id"] for r in tc]})
    store.write_json(mmh.work_dir(MODEL) / "extend.json", ext)
    assert mmh.select_rows(MODEL, rows, 283) == base
    assert mmh._ext(MODEL)["state"] == "closed: no tier C items"


def test_screen_with_non_numeric_p_fails_cleanly():
    assert t.screen_verdict({"top5": [{"country": "A", "p": "high"}]})[0] == "screen_failed"


def test_dry_clusters_retire_so_spend_is_bounded():
    def gen(prompt, valid=None):
        c = next(k for k, v in t.SIB_CLUSTERS.items() if ", ".join(v) in prompt)
        item = {"type": "dish", "name": f"{c} dish {len(prompt)}", "pre": "p", "b": "b?"}
        return {"items": [{**item, "ref_after": "r", "ref_first": "r1", "role": "ro"}]}

    res = t.build_batch(1, gen, lambda p, valid=None: {"real": True}, [], [], n=10, workers=1)
    assert res["short"] and res["n_pass"] == t.DRY_MAX and len(res["retired"]) == 34
