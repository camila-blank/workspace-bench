"""wrong_intermediate re-scrape round r4 (plan 0077 §3b (4)): the min_sl table, the r1-r3
exclusion (ids and gate-log names), ``--prefill`` precedence and ``--fill-missing``'s
``wbgetentities`` trim. Offline: GW's scrape / qlever / wd are fakes written to a tmp dir."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/wrong_intermediate_rescrape.py"
GW_MODS = ("qlever", "scrape", "wd")


def _load():
    spec = importlib.util.spec_from_file_location("_t_wi_rescrape", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load()

FAKE_QLEVER = """
ITEM_PROPS = ["P31", "P106", "P19", "P57"]
DATE_PROPS = ["P569", "P577"]
FETCHED = []

def query(q, tries=12):
    return []

def fetch(ids, chunk=3000):
    FETCHED.extend(ids)
    ent = {"claims": {}, "sitelinks_n": 0}
    return {i: {"labels": {"en": {"value": "ql " + i}}, **ent} for i in ids}
"""

FAKE_WD = """
API = "https://www.wikidata.org/w/api.php"
CALLS = []

def _get(url, params):
    import json
    from pathlib import Path
    CALLS.append(params)
    raw = json.loads((Path(__file__).parent / "wb.json").read_text())
    return {"entities": {q: raw[q] for q in params["ids"].split("|") if q in raw}}
"""

FAKE_SCRAPE = """
import json, sys
from pathlib import Path
import qlever

TEMPLATES = {"film_director": dict(where="w", prop="P57", kind="human", min_sl=8, desc="d",
                                   probe="p")}
ROUND2 = set()
MIN_BRIDGE_SL = 15

def main():
    out = Path(sys.argv[sys.argv.index("--out") + 1])
    here = Path(__file__).parent
    ents = qlever.fetch(json.loads((here / "need.json").read_text()))
    (out / "entities.json").write_text(json.dumps(ents))
    (out / "candidates.jsonl").write_text((here / "rows.jsonl").read_text())
"""


def _row(tmpl, s, b, hop2="birth_year"):
    return {"id": f"{tmpl}.{s}.{b}.{hop2}", "template": tmpl, "S": s, "B": b, "hop2": hop2}


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def gw(tmp_path, monkeypatch):
    d = tmp_path / "gw"
    d.mkdir()
    (d / "qlever.py").write_text(FAKE_QLEVER)
    (d / "wd.py").write_text(FAKE_WD)
    (d / "scrape.py").write_text(FAKE_SCRAPE)
    (d / "wb.json").write_text("{}")
    (d / "need.json").write_text("[]")
    (d / "rows.jsonl").write_text("")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["rescrape"])
    for m in GW_MODS:
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield d
    for m in GW_MODS:
        sys.modules.pop(m, None)


def _earlier(tmp_path):
    """r1 / r2a / r2b / r3 dirs, each with one candidate and one entity."""
    dirs = {}
    for r, (s, b) in {"r1": ("Q10", "Q11"), "r2a": ("Q20", "Q21"), "r2b": ("Q30", "Q31"),
                      "r3": ("Q40", "Q41")}.items():  # fmt: skip
        d = tmp_path / f"wrong_bridge_{r}"
        _jsonl(d / "candidates.jsonl", [_row("film_director", s, b)])
        (d / "entities.json").write_text(json.dumps({s: {"labels": {"en": {"value": r}}}}))
        dirs[r] = d
    return dirs


def _run(gw, out, dirs, *extra):
    return rs.main(
        ["--round", "r4", "--out", str(out), "--gw", str(gw), "--only", "film_director",
         "--exclude", ",".join(str(d) for d in dirs.values()), *extra]
    )  # fmt: skip


def test_r4_min_sl_table():
    assert set(rs.R4_MIN_SL) == set(rs.R3_MIN_SL)
    special = {"person_father": 4, "person_mother": 8, "namesake": 4}
    for k, v in rs.R4_MIN_SL.items():
        assert v == special.get(k, 2), k
    assert "building_architect" not in rs.R4_MIN_SL
    assert rs.MIN_SL["r4"] is rs.R4_MIN_SL and rs.MIN_SL["r3"] is rs.R3_MIN_SL
    assert rs.R3_MIN_SL["person_mother"] == 15  # r3's table is untouched
    assert {"P676", "P87", "P39", "P495"} <= set(rs.EXTRA_ITEM_PROPS)


def test_r4_excludes_r1_to_r3_ids_and_names(tmp_path, gw):
    dirs = _earlier(tmp_path)
    rows = [
        _row("film_director", "Q10", "Q11"),  # r1 id
        _row("film_director", "Q40", "Q41"),  # r3 id
        _row("film_director", "Q20", "Q99"),  # r2a gate-log name, new hop-1 bridge
        _row("film_director", "Q30", "Q98"),  # r2b gate-log name
        _row("film_director", "Q40", "Q97"),  # r3 gate-log name
        _row("film_director", "Q50", "Q51"),  # new
        _row("film_director", "Q40", "Q41", "death_year"),  # new hop2 on an r3 pair: new name
    ]
    _jsonl(gw / "rows.jsonl", rows)
    out = tmp_path / "wrong_bridge_r4"
    assert _run(gw, out, dirs) == 0
    kept = [json.loads(ln)["id"] for ln in (out / "candidates.jsonl").read_text().splitlines()]
    assert kept == ["film_director.Q50.Q51.birth_year", "film_director.Q40.Q41.death_year"]
    meta = json.loads((out / "r4_meta.json").read_text())
    assert meta["plan"] == "0077 §3b (wrong_intermediate r4)"
    assert meta["round"] == "r4"
    assert meta["templates"]["film_director"]["min_sl"] == 2
    assert meta["n_scraped"] == 7
    assert meta["n_name_collisions_dropped"] == 3
    assert meta["n_new"] == 2
    assert set(meta["excluded"]) == {str(d / "candidates.jsonl") for d in dirs.values()}
    assert not (out / "candidates.all.jsonl").exists()


def test_missing_exclude_dir_fails_before_scraping(tmp_path, gw):
    dirs = _earlier(tmp_path)
    dirs["r3"] = tmp_path / "nope"
    with pytest.raises(SystemExit, match="nope"):
        _run(gw, tmp_path / "out", dirs)
    assert "scrape" not in sys.modules


def test_prefill_first_dir_wins(tmp_path):
    a, b = tmp_path / "r1", tmp_path / "r3"
    for d, ents in ((a, {"Q1": {"v": "r1"}}), (b, {"Q1": {"v": "r3"}, "Q2": {"v": "r3"}})):
        d.mkdir()
        (d / "entities.json").write_text(json.dumps(ents))
    assert rs.load_prefill([a, b]) == {"Q1": {"v": "r1"}, "Q2": {"v": "r3"}}
    assert rs.load_prefill([b, a])["Q1"] == {"v": "r3"}


def _claim(v, rank="normal"):
    return {
        "rank": rank,
        "mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": v}}},
        "qualifiers": {"P580": []},
        "references": [{"x": 1}],
    }


def _wb_entity(q):
    date = {"type": "time", "value": {"time": "+1950-00-00T00:00:00Z", "precision": 9}}
    return {
        "id": q,
        "labels": {"mul": {"language": "mul", "value": "mul " + q}},
        "aliases": {"en": [{"value": "en alias"}], "mul": [{"value": "mul alias"}]},
        "claims": {
            "P31": [_claim("Q5")],
            "P39": [_claim("Q11696", "preferred")],
            "P495": [_claim("Q30")],
            "P676": [_claim("Q7")],
            "P999": [_claim("Q8")],  # not a prop the scrape reads
            "P18": [{"rank": "normal", "mainsnak": {"datavalue": {"type": "string"}}}],
            "P569": [{"rank": "normal", "mainsnak": {"datavalue": date}}],
            "P570": [{"rank": "normal", "mainsnak": {"snaktype": "somevalue"}}],
        },
        "sitelinks": {"enwiki": {}, "dewiki": {}, "frwiki": {}},
    }


def test_trim_entity_shape():
    props = ["P31", "P39", "P495", "P569", "P570"]
    t = rs.trim_entity(_wb_entity("Q1"), props)
    assert t["labels"] == {"en": {"value": "mul Q1"}}  # mul is the en fallback
    assert t["aliases"] == {"en": [{"value": "en alias"}, {"value": "mul alias"}]}
    assert set(t["claims"]) == {"P31", "P39", "P495", "P569"}  # P570 has no value
    assert t["claims"]["P39"] == [
        {
            "rank": "preferred",
            "mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q11696"}}},
        }
    ]
    assert t["sitelinks_n"] == 3 and t["sitelinks"] == {}
    en = _wb_entity("Q1") | {"labels": {"en": {"value": "E"}, "mul": {"value": "M"}}}
    assert rs.trim_entity(en, props)["labels"]["en"]["value"] == "E"
    empty = rs.trim_entity(None, props)
    assert empty == {
        "labels": {},
        "aliases": {"en": []},
        "claims": {},
        "sitelinks": {},
        "sitelinks_n": 0,
    }


def test_wb_fetcher_batches_and_trims():
    calls = []

    def get(params):
        calls.append(params)
        ids = params["ids"].split("|")
        return {"entities": {q: _wb_entity(q) for q in ids if q != "Q3"}}

    ids = [f"Q{i}" for i in range(1, 121)]
    got = rs.wb_fetcher(get, ["P31", "P569"])(ids)
    assert [len(c["ids"].split("|")) for c in calls] == [50, 50, 20]
    assert {c["languages"] for c in calls} == {"en|mul"}
    assert set(got) == set(ids)
    assert set(got["Q1"]["claims"]) == {"P31", "P569"}
    assert got["Q3"]["claims"] == {} and got["Q3"]["labels"] == {}  # missing -> empty record


def test_fill_missing_trims_to_item_and_date_props_and_skips_prefilled(tmp_path, gw):
    dirs = _earlier(tmp_path)
    (gw / "need.json").write_text(json.dumps(["Q10", "Q20", "Q40", "Q60", "Q61"]))
    (gw / "wb.json").write_text(json.dumps({q: _wb_entity(q) for q in ("Q60", "Q61")}))
    out = tmp_path / "wrong_bridge_r4"
    pre = ",".join(str(dirs[r]) for r in ("r1", "r2a", "r2b", "r3"))
    assert _run(gw, out, dirs, "--prefill", pre, "--fill-missing") == 0
    import qlever
    import wd

    assert qlever.FETCHED == []  # nothing went to QLever's entity fetch
    assert [c["ids"] for c in wd.CALLS] == ["Q60|Q61"]  # prefilled ids are never fetched
    assert qlever.ITEM_PROPS[-4:] == ["P676", "P87", "P39", "P495"]
    ents = json.loads((out / "entities.json").read_text())
    assert ents["Q10"] == {"labels": {"en": {"value": "r1"}}}  # prefilled verbatim
    assert set(ents) == {"Q10", "Q20", "Q40", "Q60", "Q61"}  # only what the scrape needs
    # ITEM_PROPS (with the four appended) + DATE_PROPS: P999 / P18 / P570-without-value dropped
    assert set(ents["Q60"]["claims"]) == {"P31", "P39", "P495", "P676", "P569"}
    assert ents["Q60"]["sitelinks_n"] == 3
    assert not (out / "entities.partial").exists()  # checkpoints removed once entities.json is
    meta = json.loads((out / "r4_meta.json").read_text())
    ec = meta["entity_cache"]
    assert ec["prefill"] == [str(dirs[r] / "entities.json") for r in ("r1", "r2a", "r2b", "r3")]
    assert ec["n_prefilled"] == 3 and ec["n_fetched"] == 2
    assert ec["props"] == [
        "P31",
        "P106",
        "P19",
        "P57",
        "P676",
        "P87",
        "P39",
        "P495",
        "P569",
        "P577",
    ]
    assert "wbgetentities" in ec["fill"]


def test_prefill_without_fill_missing_uses_qlever(tmp_path, gw):
    dirs = _earlier(tmp_path)
    (gw / "need.json").write_text(json.dumps(["Q10", "Q60"]))
    out = tmp_path / "wrong_bridge_r4"
    assert _run(gw, out, dirs, "--prefill", str(dirs["r1"])) == 0
    import qlever
    import wd

    assert qlever.FETCHED == ["Q60"] and wd.CALLS == []
    ents = json.loads((out / "entities.json").read_text())
    assert ents["Q10"]["labels"]["en"]["value"] == "r1"
    assert ents["Q60"]["labels"]["en"]["value"] == "ql Q60"


def test_checkpoint_is_reused_on_restart(tmp_path):
    calls = []

    def fetch_chunk(ids):
        calls.append(list(ids))
        return {q: {"q": q} for q in ids}

    f = rs.checkpointed(fetch_chunk, tmp_path, 2, tag="wb-")
    assert f(["Q3", "Q1", "Q2"]) == {q: {"q": q} for q in ("Q1", "Q2", "Q3")}
    assert calls == [["Q1", "Q2"], ["Q3"]]
    assert all(p.name.startswith("wb-") for p in (tmp_path / "entities.partial").iterdir())
    f(["Q1", "Q2", "Q3"])
    assert len(calls) == 2
