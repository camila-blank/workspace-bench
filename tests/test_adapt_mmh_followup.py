"""plan 0078 follow-up (R49): ooc grades leave the macro, the bridge-cap trim drops the highest hit
rates first, and calling codes that share a prefix are not identifying."""

import json

import pytest
from test_adapt_model_multihop import CUT, INV, INV2, MODEL, _all

from wsbench.adapt import mmh, run, store


@pytest.fixture
def tmp_model(tmp_path, monkeypatch):  # test_adapt_model_multihop's fixture
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    pool = {"header": {}, "invented": [INV, INV2], "cutoff": [CUT]}
    monkeypatch.setattr(mmh, "load_pool", lambda: pool)
    return tmp_path / MODEL


def _row(k: int, bridge: str) -> dict:
    return {
        "id": f"i{k}",
        "origin": "refill",
        "gate": {"pass": True},
        "item": {
            "id": f"i{k}",
            "cat": "invented",
            "bridge": bridge,
            "entity": f"the {k:03d} xq{k}",
            "mc": {"order": list("abcde"), "gold": "a"},
        },
    }


def test_capped_trims_the_highest_rate_first():
    rows = [_row(k, "US" if k < 4 else f"C{k}") for k in range(10)]
    rate = {"i0": 0.4, "i1": 0.0, "i2": 0.3, "i3": 0.0}
    out = mmh.capped(rows, 10, lambda r: rate.get(r["id"], 0.0))
    ids = [r["id"] for r in out]
    # lim = 1 at 10 rows: the one US row kept is the lowest-rate one reached (i1 before i3)
    assert [i for i in ids if i in rate] == ["i1"]
    assert ids == sorted(ids, key=lambda i: int(i[1:]))  # order of `kept` unchanged
    # without a rate: ship order (the earliest US row), as before
    assert [r["id"] for r in mmh.capped(rows, 10) if r["id"] in rate] == ["i0"]


def test_capped_rate_never_changes_the_size_or_other_bridges():
    rows = [_row(k, "US" if k % 2 == 0 else f"C{k}") for k in range(40)]
    rate = {r["id"]: (1.0 if r["item"]["bridge"] == "US" and k < 10 else 0.0)
            for k, r in enumerate(rows)}  # fmt: skip
    a = mmh.capped(rows, 20)
    b = mmh.capped(rows, 20, lambda r: rate[r["id"]])
    assert len(a) == len(b)
    other = lambda xs: [r["id"] for r in xs if r["item"]["bridge"] != "US"]  # noqa: E731
    assert other(a) == other(b)
    us = [r for r in b if r["item"]["bridge"] == "US"]
    lim = max(1, int(mmh.BRIDGE_CAP * len(b)))
    assert len(us) <= lim and all(rate[r["id"]] == 0.0 for r in us)


def test_round_one_cap_trim_uses_the_screen_rate(tmp_model):
    rows = [_row(k, "US" if k < 3 else f"C{k}") for k in range(10)]
    draws = {r["id"]: {"blind": [0], "described": [0]} for r in rows}
    draws["i0"] = {"blind": [2], "described": [1]}  # under the leaker rule, but the highest rate
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", {"round": 1, "draws": draws})
    ids = [r["id"] for r in mmh.select_rows(MODEL, rows, 283)]
    assert "i0" not in ids and "i1" in ids and sum(i in ids for i in ("i0", "i1", "i2")) == 1


def test_status_ooc_leaves_the_macro_and_a_later_grade_rejoins(tmp_model, monkeypatch):
    _r, rows = _all()
    draws = {i: {"blind": [0], "described": [0]} for i in rows}
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", {"round": 1, "draws": draws})
    run.freeze(MODEL, [mmh.FAMILY])
    bank = json.loads((tmp_model / f"{mmh.FAMILY}.json").read_text())
    lk = json.loads((mmh.work_dir(MODEL) / "lucky_screen.json").read_text())
    lk["final"] = {
        "ids": sorted(rows),
        "leakers": [],
        "round": 1,
        "mc_hash": {it["id"]: mmh.mc_hash(it) for it in bank["items"]},
    }
    store.write_json(mmh.work_dir(MODEL) / "lucky_screen.json", lk)
    monkeypatch.setattr(mmh, "N_MIN", 2)
    monkeypatch.setattr(mmh, "Y_MIN", 0.9)
    mmh.status(MODEL)
    rep = json.loads((tmp_model / "report.json").read_text())["families"][mmh.FAMILY]
    assert rep["status"] == "ooc" and rep["exclude_from_macro"] is True
    monkeypatch.setattr(mmh, "Y_MIN", 0.0)
    mmh.status(MODEL)
    rep = json.loads((tmp_model / "report.json").read_text())["families"][mmh.FAMILY]
    assert rep["status"] == "refill_pending" and "exclude_from_macro" not in rep


def test_incapable_ooc_path_also_leaves_the_macro(tmp_model, monkeypatch):
    _all()
    monkeypatch.setattr(mmh, "N_MIN", 2)
    monkeypatch.setattr(mmh, "Y_MIN", 0.9)
    assert mmh.status(MODEL)["stop"] == "ooc"
    rep = json.loads((tmp_model / "report.json").read_text())["families"][mmh.FAMILY]
    assert rep["status"] == "ooc" and rep["exclude_from_macro"] is True


def test_same_value_prefix_only_for_calling_codes():
    assert mmh.same_value("calling_code", "1", "1876")
    assert mmh.same_value("calling_code", "1876", "1")
    assert not mmh.same_value("calling_code", "1876", "1868")
    assert not mmh.same_value("calling_code", "44", "1441")
    assert not mmh.same_value("cctld", ".j", ".jm")
    assert not mmh.same_value("calling_code", None, "1")


def test_nanp_foil_makes_the_us_code_hop_unidentifying():
    attrs = {
        "United States": {"calling_code": "+1", "cctld": ".us", "currency": "USD", "iso3": "USA"},
        "Jamaica": {"calling_code": "+1-876", "cctld": ".jm", "currency": "JMD", "iso3": "JAM"},
    }  # fmt: skip
    hops = [h[0] for h in mmh.identifying_hops("z", "United States", ["Jamaica"], attrs)]
    assert "calling_code" not in hops and "cctld" in hops


def test_gate_fails_a_code_inside_a_foils_zone():
    s = {
        "cat": "invented",
        "hop": "calling_code",
        "foils": ["Jamaica"],
        "truth_attr": {"Jamaica": "+1-876"},
        "b_mode": "United States",
        "m": "What is the calling code of the X?",
        "m2": "The X. What is its calling code?",
        "af": ["a0"],
    }
    out = {f"i|calling_code|{p}": {"text": "t", "greedy": "+1", "samples": ["+1"] * 20}
           for p in ("m", "m2", "a")}  # fmt: skip
    out["i|calling_code|af0"] = {"text": "t", "greedy": "+1 876", "samples": []}
    g = mmh._gate_one("i|calling_code", s, out, lambda prompt, valid=None: {})
    assert g["fail2"] == "identifiability"
    out["i|calling_code|af0"]["greedy"] = "+44"
    s["truth_attr"] = {"Jamaica": "+44"}
    assert mmh._gate_one("i|calling_code", s, out, lambda prompt, valid=None: {})["fail2"] is None
