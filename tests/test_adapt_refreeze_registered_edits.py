"""Plan 0072 (critique of §3 Phase 1): a re-freeze carries today's registered judge-side lists
onto originals whose gate rows predate them: multihop_mt's subject-rebuilt originals take the
registered ``mc`` block wherever the gold is the same; basic_readout entity / processing
originals ship the registered ``mc``; a native-langs rebuild names the registered item it was
built from, so a stale rebuild refuses at freeze and ``native_langs stage=upgrade`` rebuilds
originals from today's registered item."""

import copy
import json

import pytest

from wsbench.adapt import ml_native, run, store
from wsbench.adapt import native_langs as nl
from wsbench.adapt.store import original_bank

MODEL = "subj"


@pytest.fixture
def tmp_model(tmp_path, monkeypatch, real_families):
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    monkeypatch.setattr(store, "code_stamp", lambda: {})
    return tmp_path / MODEL


class _CharTok:
    def encode(self, text, add_special_tokens=False):
        return list(text)


def _reg(family, pred=lambda it: True):
    _h, items = original_bank(family)
    return copy.deepcopy(next(it for it in items if pred(it)))


def test_carry_registered_mc_same_gold_only():
    reg = _reg("multihop_mt")
    roles = [r for r, b in reg["mc"].items() if isinstance(b, dict)]
    stale = copy.deepcopy(reg)
    stale["mc"][roles[0]]["distractors"] = ["Old A", "Old B", "Old C", "Old D"]
    if len(roles) > 1:  # the subject's own gold on another role: its block is kept
        stale["mc"][roles[1]]["gold"] = "Subject Gold"
        stale["mc"][roles[1]]["distractors"] = ["S1", "S2"]
    n = run.carry_registered_mc(stale, reg)
    assert n == 1
    assert stale["mc"][roles[0]] == reg["mc"][roles[0]]
    if len(roles) > 1:
        assert stale["mc"][roles[1]]["gold"] == "Subject Gold"
    assert run.carry_registered_mc(stale, reg) == 0  # idempotent
    assert run.carry_registered_mc({"mc": None}, reg) == 0


def test_basic_readout_entity_original_ships_the_registered_mc():
    reg = _reg("basic_readout", lambda it: it.get("subfamily") != "implicit" and it.get("mc"))
    row_item = copy.deepcopy(reg)
    row_item["mc"] = {**reg["mc"], "distractors": ["stale"]}  # marked before a registered edit
    row = {"id": store.item_id(reg), "origin": "original", "item": row_item,
           "gate": {"pass": True, "legs": {}}}  # fmt: skip
    got = run.build_original("basic_readout", reg, row)
    assert got["mc"] == reg["mc"]


def test_basic_readout_implicit_keeps_the_subjects_mc():
    reg = _reg("basic_readout", lambda it: it.get("subfamily") == "implicit" and it.get("mc"))
    row_item = copy.deepcopy(reg)
    row_item["mc"] = {**reg["mc"], "subject_built": True}
    row = {"id": store.item_id(reg), "origin": "original", "item": row_item,
           "gate": {"pass": True, "legs": {}}}  # fmt: skip
    got = run.build_original("basic_readout", reg, row)
    assert got["mc"].get("subject_built") is True


def test_stale_native_rebuild_is_unbuilt():
    reg = _reg("multilingual", lambda it: it.get("lang") == "de")
    row = copy.deepcopy(reg)
    row[nl.MARKER] = ["en"]
    assert nl.unbuilt("multilingual", row, reg, ["en"])  # no source record: rebuilt pre-0072
    row[nl.SOURCE_KEY] = nl.source_sha(reg)
    assert not nl.unbuilt("multilingual", row, reg, ["en"])
    newer = copy.deepcopy(reg)
    newer["mc"] = {**newer["mc"], "edited": True}  # a registered list rebuild (#329)
    assert nl.unbuilt("multilingual", row, newer, ["en"])


def test_upgrade_rebuilds_originals_from_todays_registered_item(tmp_model):
    nl.record(MODEL, ["en"], evidence="fixture")
    reg = _reg("multilingual", lambda it: it.get("lang") == "de")
    old = copy.deepcopy(reg)
    old["mc"] = {**old["mc"], "stale_list": True}  # the gate row holds an older registered copy
    old["gate_variant"] = "kept"
    gate = {"pass": True, "rate": 1.0, "legs": {}}
    store.record_gates(MODEL, "multilingual", [{"id": store.item_id(reg), "origin": "original",
                                               "item": old, "gate": gate}])  # fmt: skip
    ml_native.upgrade(MODEL, ["multilingual"], tok=_CharTok())
    row = store.gated(MODEL, "multilingual")[store.item_id(reg)]
    assert "stale_list" not in row["item"]["mc"]
    assert row["item"]["gate_variant"] == "kept" and row["gate"] == gate
    assert row["item"][nl.SOURCE_KEY] == nl.source_sha(reg)
    assert not nl.unbuilt("multilingual", row["item"], reg, ["en"])
    json.dumps(row)  # serializable
    shipped = nl.overlay_native_units("multilingual", copy.deepcopy(reg), row["item"])
    assert shipped[nl.SOURCE_KEY] == nl.source_sha(reg)  # the shipped item carries the record


def test_note_refill_block_keeps_a_short_originals_only_family_partial(tmp_model):
    """Plan 0072 D6: user_modeling / multihop refill only by profiling the new registered items;
    `note refill=` records that block and a re-freeze keeps the short family `partial`. Every
    family with a refill driver refuses a hand block (it would pass the strict freeze guard)."""
    m4b = "qwen3.5-4b"  # freeze needs a registered subject
    _h, items = original_bank("multihop")
    gate = {"pass": True, "rate": 1.0, "legs": {}}
    store.record_gates(m4b, "multihop", [{"id": store.item_id(it), "origin": "original",
                                          "item": copy.deepcopy(it), "gate": gate}
                                         for it in items[:3]])  # fmt: skip
    run.freeze(m4b, ["multihop"])
    rep = json.loads((tmp_model.parent / m4b / "report.json").read_text())["families"]["multihop"]
    assert rep["status"] == "untried"
    with pytest.raises(SystemExit, match="route"):
        run.note(m4b, "multihop", refill={"stop": "x"})
    block = {"route": "profile registered", "stop": "pool exhausted (no authoring route)"}
    for fam in ("buggy_code", "multihop_mt", "association", "typo_mt", "knowledge_cutoff"):
        with pytest.raises(SystemExit, match="no refill driver"):
            run.note(m4b, fam, refill=block)
    run.note(m4b, "multihop", status="partial", refill=block)
    run.freeze(m4b, ["multihop"], strict=True)
    rep = json.loads((tmp_model.parent / m4b / "report.json").read_text())["families"]["multihop"]
    assert rep["status"] == "partial" and rep["refill"] == block
    assert (
        rep["change"] == "refill via profile registered; stop: pool exhausted (no authoring route)"
    )


def test_carry_matches_a_relabelled_bridge_by_gold():
    reg = _reg(
        "multihop_mt", lambda it: len([b for b in it["mc"].values() if isinstance(b, dict)]) > 1
    )
    roles = [r for r, b in reg["mc"].items() if isinstance(b, dict)]
    sub = copy.deepcopy(reg)
    a, b = roles[0], roles[1]
    sub["mc"][a], sub["mc"][b] = copy.deepcopy(reg["mc"][b]), copy.deepcopy(reg["mc"][a])
    sub["mc"][a]["distractors"] = ["stale"]  # the registered b block, relabelled as a, stale list
    assert run.carry_registered_mc(sub, reg) == 1
    assert sub["mc"][a] == reg["mc"][b] and sub["mc"][b] == reg["mc"][a]


def test_floor_readouts_keep_a_former_key_file(tmp_path, monkeypatch):
    """Plan 0072 0.1 makes multihop_mt visible to the stock floors: generate overwrites the set's
    existing `multihop_mt_v2.jsonl` (no second file) and ADAPT_FLOORS_FAMILIES=multihop_mt
    selects it in the judge loop."""
    from wsbench.adapt import floors
    from wsbench.registry import canonical
    from wsbench.runner import readouts_path

    rd = tmp_path / "readouts"
    rd.mkdir()
    (rd / "multihop_mt_v2.jsonl").write_text("{}\n")
    assert readouts_path(rd, "multihop_mt") == rd / "multihop_mt_v2.jsonl"
    monkeypatch.setenv("ADAPT_FLOORS_FAMILIES", "multihop_mt")
    only = floors._only()
    assert [p.stem for p in rd.glob("*.jsonl") if canonical(p.stem) in only] == ["multihop_mt_v2"]
