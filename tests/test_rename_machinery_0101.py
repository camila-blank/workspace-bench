"""Plan 0101 commit 2: the registry's pair machinery on a SYNTHETIC renamed pair (the real
tables are empty until the rename): ``poetry`` plays the family that took the bare key,
``typo`` the one that held it before."""

import json

import pytest

from wsbench import registry, split
from wsbench.readouts import refuse_partner_file
from wsbench.registry import RenamedKey, Reused

LIVE, OTHER = "poetry", "typo"
LIVE_ID, OTHER_ID = "b3-po-bell-spell", "typo-February"


@pytest.fixture
def pair(monkeypatch, real_families):
    entry = Reused(LIVE, OTHER, ("poetry-",), ("bank-",), "live-header", "other-header", "then")
    monkeypatch.setitem(registry.REUSED_FAMILY_KEYS, LIVE, entry)
    registry._family_ids.cache_clear()
    assert LIVE_ID in registry.family_ids(LIVE) and OTHER_ID in registry.family_ids(OTHER)
    yield entry
    registry._family_ids.cache_clear()


def test_tables_are_empty_before_the_rename():
    assert registry.FROZEN_KEY_NAMES == {} and registry.PRE_0101_KEYS == {}
    assert all(r.other_stamps is None for r in registry.REUSED_FAMILY_KEYS.values())
    assert registry.partner("multihop_mt") is None and registry.shared_key("typo") is None
    assert registry.owner("typo", ids=["typo-February"]) is None
    assert registry.bank_pins("multihop_mt") == [
        "WSB_BANK_FILE_MULTIHOP_MT",
        "WSB_BANK_FILE_MULTIHOP_MT_V2",
    ]


def test_frozen_name_is_what_the_raw_key_sites_embed(monkeypatch):
    before = [split.is_dev("typo", str(i)) for i in range(200)]
    monkeypatch.setitem(registry.FROZEN_KEY_NAMES, "typo_renamed", "typo")
    assert registry.frozen_name("typo_renamed") == "typo" and registry.frozen_name("x") == "x"
    assert [split.is_dev("typo_renamed", str(i)) for i in range(200)] == before


def test_rekey_pre_0101(monkeypatch):
    monkeypatch.setattr(registry, "PRE_0101_KEYS", {"a": "a_st", "a_mt": "a"})
    assert registry.rekey_pre_0101({"a": 1, "a_mt": 2, "z": 3}) == {"a_st": 1, "a": 2, "z": 3}
    assert list(registry.rekey_pre_0101({"z": 0, "a_mt": 2})) == ["z", "a"]
    assert registry.from_pre_0101("a") == "a_st" and registry.from_pre_0101("wrong_bridge") == (
        "wrong_intermediate"
    )
    with pytest.raises(ValueError, match="two keys"):
        registry.rekey_pre_0101({"a_mt": 1, "a": 2, "a_st": 3})


def test_pair_names(pair):
    assert registry.partner(LIVE) == OTHER and registry.partner(OTHER) == LIVE
    assert registry.shared_key(LIVE) == registry.shared_key(OTHER) == LIVE
    assert registry.lookup_names(OTHER) == [OTHER, LIVE]
    assert registry.lookup_names(LIVE) == [LIVE]
    assert registry.bank_pins(OTHER) == ["WSB_BANK_FILE_TYPO", "WSB_BANK_FILE_POETRY"]
    assert registry.lookup_names("multihop_mt") == ["multihop_mt_v2", "multihop_mt"]  # retained


def test_stored_family_by_stamp(pair):
    assert registry.stored_family(LIVE, "poetry-free-2026") == LIVE
    assert registry.stored_family(LIVE, "bank-2026-10-30b+digest-v5") == OTHER
    assert registry.stored_family(LIVE, "") == LIVE and registry.stored_family(LIVE, None) == LIVE
    with pytest.raises(RenamedKey):
        registry.stored_family(LIVE, "unknown-stamp")
    # the 2026-11-04 key keeps its rule: every non-live stamp, empty included, is the removed one
    assert registry.stored_family("multihop_mt", "mh2-mc-2026-11-01") == "multihop_mt"
    assert registry.stored_family("multihop_mt", "") == "multihop_mt_regex"
    assert registry.stored_family("multihop_mt_v2", "mh2-mc-2026-11-01") == "multihop_mt"
    assert registry.stored_family("wrong_bridge", "x") == "wrong_intermediate"


def test_owner_by_each_kind_of_evidence(pair):
    assert registry.owner(LIVE) is None
    assert registry.owner(LIVE, ids=[LIVE_ID]) == LIVE
    assert registry.owner(LIVE, ids=[OTHER_ID]) == OTHER
    assert registry.owner(LIVE, ids=["in-neither-bank"]) is None
    assert registry.owner(LIVE, header="other-header") == OTHER
    assert registry.owner(LIVE, stamp="bank-x") == OTHER
    assert registry.owner(LIVE, plan_sha=("b", "a", "b")) == OTHER
    assert registry.owner(LIVE, plan_sha=("c", "a", "b")) is None
    with pytest.raises(RenamedKey):
        registry.owner(LIVE, ids=[LIVE_ID, OTHER_ID])
    with pytest.raises(RenamedKey):
        registry.owner(LIVE, ids=[LIVE_ID], header="other-header")
    with pytest.raises(RenamedKey):
        registry.owner(LIVE, stamp="unknown-stamp")


def test_attribute_rows(pair):
    rows = [
        {"family": LIVE, "id": OTHER_ID},
        {"family": LIVE, "id": LIVE_ID},
        {"family": "wrong_bridge", "id": "x"},
    ]
    assert [r["family"] for r in registry.attribute_rows(rows)] == [
        OTHER,
        LIVE,
        "wrong_intermediate",
    ]
    kept = registry.attribute_rows([{"family": "wrong_bridge", "id": "x"}], canonicalize=False)
    assert kept[0]["family"] == "wrong_bridge"
    with pytest.raises(RenamedKey):
        registry.attribute_rows([{"family": LIVE, "id": "in-neither-bank"}])


def _bank(path, header):
    path.write_text(json.dumps({"family": header, "items": []}), encoding="utf-8")
    return path


def test_root_bank_and_bank_path_by_header(pair, tmp_path, monkeypatch):
    old = _bank(tmp_path / f"{LIVE}.json", "other-header")  # a pre-rename set: the bare key
    assert registry.root_bank(tmp_path, OTHER) == old
    assert registry.root_bank(tmp_path, LIVE) is None
    monkeypatch.setenv(registry.BANK_ROOT_ENV, str(tmp_path))
    assert registry.bank_path(OTHER) == old
    with pytest.raises(SystemExit, match=r"has no bank for 'poetry'.*is the bank of 'typo'"):
        registry.bank_path(LIVE)
    new = _bank(tmp_path / f"{OTHER}.json", "other-header")
    _bank(old, "live-header")
    assert registry.root_bank(tmp_path, OTHER) == new and registry.root_bank(tmp_path, LIVE) == old


def test_a_bare_key_pin_needs_a_positive_header(pair, tmp_path, monkeypatch):
    theirs = _bank(tmp_path / "theirs.json", "other-header")
    mine = _bank(tmp_path / "mine.json", "live-header")
    monkeypatch.setenv("WSB_BANK_FILE_POETRY", str(theirs))
    with pytest.raises(RenamedKey):
        registry.bank_path(LIVE)
    with pytest.raises(RenamedKey, match="WSB_BANK_FILE_TYPO"):
        registry.bank_path(OTHER)
    monkeypatch.setenv("WSB_BANK_FILE_POETRY", str(mine))
    assert registry.bank_path(LIVE) == mine
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("WSB_BANK_FILE_POETRY", str(tmp_path / "list.json"))
    with pytest.raises(RenamedKey):
        registry.bank_path(LIVE)
    monkeypatch.delenv("WSB_BANK_FILE_POETRY")
    monkeypatch.setenv("WSB_BANK_FILE_TYPO", str(mine))  # an unambiguous name, the partner's file
    with pytest.raises(RenamedKey):
        registry.bank_path(OTHER)
    monkeypatch.setenv("WSB_BANK_FILE_TYPO", str(theirs))
    assert registry.bank_path(OTHER) == theirs


def _results(d, family, stamp):
    d.mkdir(parents=True)
    doc = {"family": family, "config": {"prompt_version": stamp}}
    (d / "results.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def test_family_dir_is_content_checked_under_the_bare_key(pair, tmp_path):
    old = _results(tmp_path / "a" / LIVE, LIVE, "bank-2026-10-30b")  # written by the other family
    assert registry.dir_owner(old) == OTHER
    assert registry.family_dir(tmp_path / "a", OTHER) == old
    with pytest.raises(RenamedKey, match="holds results of 'typo'"):
        registry.family_dir(tmp_path / "a", LIVE)
    mine = _results(tmp_path / "b" / LIVE, LIVE, "poetry-free-2026")
    assert registry.family_dir(tmp_path / "b", LIVE) == mine
    assert registry.family_dir(tmp_path / "b", OTHER) == tmp_path / "b" / OTHER
    cells = tmp_path / "c" / LIVE
    cells.mkdir(parents=True)
    rows = [{"key": f"digest:{OTHER_ID}|20", "fp": "x"}, {"key": f"{OTHER_ID}|L20", "fp": "y"}]
    (cells / "cells.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    assert (
        registry.dir_owner(cells) == OTHER and registry.family_dir(tmp_path / "c", OTHER) == cells
    )
    empty = tmp_path / "d" / LIVE
    empty.mkdir(parents=True)
    assert registry.family_dir(tmp_path / "d", LIVE) == empty  # unidentified, under its own key


def test_a_partner_readouts_file_is_never_resumed(pair, tmp_path):
    path = tmp_path / f"{LIVE}.jsonl"
    path.write_text(json.dumps({"id": OTHER_ID, "layer": 1, "pos": 0}) + "\n", encoding="utf-8")
    with pytest.raises(RenamedKey, match="holds 'typo' readouts"):
        refuse_partner_file(path, LIVE)
    refuse_partner_file(path, OTHER)
    refuse_partner_file(tmp_path / "absent.jsonl", LIVE)
    refuse_partner_file(path, "moral_rationale")  # a family outside any pair: no check
