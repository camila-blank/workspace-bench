"""Plan 0121 §4, review fixes of the off-topic refusal PR: a record-only folder is a v8 root for
``sjudge.check_root``; hindex-v9 (``build_index_v9``, ``hbench final``) refuses an overrefusal
root without the bank-v2 / interp-v2 stamps; gate ``orj`` says INCOMPLETE when a rate cannot be
gated or labelling is partial, shows its labellers the summariser's decoding of a bag and refuses
labels of another sample; the v9 export counts ``empty`` digests apart from ``unjudged`` ones
and the units voided by a failed summary."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from wsbench.hb import final
from wsbench.hb import index as ix
from wsbench.hb.evals.overrefusal import cli as or_cli
from wsbench.hb.evals.overrefusal import data, sjudge
from wsbench.hb.evals.overrefusal import offtopic_export as ox

from .index_helpers import stamp_or_v9
from .test_final import _main, _manifest
from .test_index_v8 import v8_sources
from .test_v9_index_final import _stamp, _v9
from .test_v9_or_bank import _rec, v9_world  # noqa: F401  (the fixture)
from .test_v9_or_gate import FakeLabeller, _world, orj

V2 = "interp-v2"


@pytest.fixture(autouse=True)
def _two_layer_grid(monkeypatch):
    monkeypatch.setattr(final, "EXPECTED_LAYERS", (20, 24))


# ---------------------------------------------------------------- 1: check_root


@pytest.mark.parametrize(
    "left", ["sj_records_full.jsonl", "sj_records_calib.jsonl", "offtopic_index"]
)
def test_a_folder_with_v8_outputs_and_no_settings_is_a_v8_root(tmp_path: Path, left: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    if left.endswith(".jsonl"):
        (root / left).write_text("{}\n")
    else:
        (root / left).mkdir()
    assert sjudge.v8_outputs(root) == [left]
    for bank, summ in (("v2", V2), ("v2", None), ("v1", V2)):
        with pytest.raises(ValueError, match=r"judged with .*use another out root"):
            sjudge.check_root(root, bank, summ)
    assert not (root / sjudge.SETTINGS_FILE).exists()  # a refused run records nothing
    sjudge.check_root(root, "v1", None)  # the v8 run it is: accepted, and still no file
    assert not (root / sjudge.SETTINGS_FILE).exists()


def test_an_empty_or_new_folder_takes_any_settings(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("not an output")
    assert sjudge.v8_outputs(empty) == [] and sjudge.v8_outputs(tmp_path / "new") == []
    for root in (empty, tmp_path / "new"):
        sjudge.check_root(root, "v2", V2)
        assert json.loads((root / sjudge.SETTINGS_FILE).read_text()) == {
            "bank": "v2",
            "jlens_summarizer": V2,
        }
    # a v9 root with records stays a v9 root (the settings file decides, not the records)
    (empty / "sj_records_full.jsonl").write_text("{}\n")
    (empty / "offtopic_index").mkdir()
    sjudge.check_root(empty, "v2", V2)
    with pytest.raises(ValueError, match="judged with"):
        sjudge.check_root(empty, "v1", None)


# ---------------------------------------------------------------- 2: the v9 index stamps


def _meta(**over) -> dict:
    m = {
        "unit": "refusal_claims",
        "bank": {"name": "v2", "file": "items_v2.json", "sha256": ix.or_bank_sha_v9()},
        "summarizer": {"jlens": V2},
    }
    return {k: v for k, v in {**m, **over}.items() if v is not None}


def test_or_meta_problem_v9() -> None:
    assert ix.or_bank_sha_v9() == ox._sha256(data.bank_path("v2"))
    assert ix.or_meta_problem_v9(_meta()) is None
    assert ix.or_meta_problem_v9(None) == "is missing"
    bad = {
        "records bank None": _meta(bank=None),
        "records bank 'v1'": _meta(bank={"name": "v1", "sha256": ix.or_bank_sha_v9()}),
        "records bank v2 sha256 'ab'": _meta(bank={"name": "v2", "sha256": "ab"}),
        "records summarizer.jlens None": _meta(summarizer=None),
        "records summarizer.jlens 'interp-v1'": _meta(summarizer={"jlens": "interp-v1"}),
        "comparison-only": _meta(comparison_only="J-lens rows are hindex-v8 records"),
        # the flag alone refuses, whatever it holds
        "comparison_only is set": _meta(comparison_only=False),
    }
    for want, meta in bad.items():
        assert want in ix.or_meta_problem_v9(meta), want


def test_build_index_v9_refuses_an_unstamped_overrefusal_root(tmp_path: Path) -> None:
    src = v8_sources(tmp_path)  # the fixture root is a v8 export: no bank, no summariser
    root = src[ix.OVERREFUSAL][0]
    v8 = ix.build_index(src, ix.Methods(), version="v8")
    for build in (
        lambda: ix.build_index(src, ix.Methods(), version="v9"),
        lambda: ix.build_index_v9(src, ix.Methods()),
        lambda: ix.check_overrefusal_v9(src),
    ):
        with pytest.raises(
            ValueError, match="hindex-v9 needs an Off-topic refusal root on bank v2"
        ):
            build()
    stamp_or_v9(root)
    ix.check_overrefusal_v9(src)
    v9 = ix.build_index(src, ix.Methods(), version="v9")
    assert v9["version"] == "hindex-v9" and v9["rows"] == v8["rows"]
    # the joint stamps are checked beside the joint rows; each stamp is needed
    cases = [
        ({"summarizer": {"jlens": None}}, "records summarizer.jlens None, not 'interp-v2'"),
        ({"bank": {"name": "v1", "sha256": "x"}}, "records bank 'v1', not 'v2'"),
        ({"bank": {"name": "v2", "sha256": "x"}}, "records bank v2 sha256 'x', not the registered"),
        ({"comparison_only": "v8 J-lens records"}, "is a comparison-only export"),
    ]
    for name in (ix.SAFETY_META, ix.SAFETY_META_JOINT):
        good = (root / name).read_text()
        for over, why in cases:
            (root / name).write_text(json.dumps({**json.loads(good), **over}))
            with pytest.raises(ValueError, match=re.escape(f"{name} {why}")):
                ix.build_index(src, ix.Methods(), version="v9")
            (root / name).write_text(good)
    (root / ix.SAFETY_META_JOINT).unlink()
    with pytest.raises(ValueError, match=r"index_meta_joint\.json is missing"):
        ix.build_index(src, ix.Methods(), version="v9")
    (root / ix.SAFETY_COUNTS_JOINT).unlink()  # no joint rows: no joint stamps to check
    ix.check_overrefusal_v9(src)


def test_final_v9_checks_the_overrefusal_stamps(tmp_path: Path, capsys) -> None:
    # an unstamped (v8) overrefusal root under a v9 manifest: refused like the other v9 checks
    assert _main(tmp_path / "a", _manifest(tmp_path / "a", mutate=_v9, prep=_stamp)) == 2
    err = capsys.readouterr().err
    assert (
        "hbench final: overrefusal x jlens x L20:" in err
        and "index_meta.json records bank None" in err
    )
    assert "hindex-v9 needs the off-topic refusal root on bank v2" in err

    def stamped(roots: Path) -> None:
        _stamp(roots)
        stamp_or_v9(roots / "or")

    m = _manifest(tmp_path / "b", mutate=_v9, prep=stamped)
    assert _main(tmp_path / "b", m) == 0
    built = json.loads((tmp_path / "b" / "final" / "index.json").read_text())
    assert built["version"] == "hindex-v9"

    def comparison(roots: Path) -> None:
        stamped(roots)
        stamp_or_v9(roots / "or", comparison_only="v8 J-lens records")

    assert _main(tmp_path / "c", _manifest(tmp_path / "c", mutate=_v9, prep=comparison)) == 2
    assert "comparison-only export" in capsys.readouterr().err

    def stale_joint(roots: Path) -> None:
        stamped(roots)
        p = roots / "or" / ix.SAFETY_META_JOINT
        p.write_text(json.dumps({**json.loads(p.read_text()), "summarizer": {"jlens": None}}))

    assert _main(tmp_path / "d", _manifest(tmp_path / "d", mutate=_v9, prep=stale_joint)) == 2
    err = capsys.readouterr().err
    assert "overrefusal x jlens x all:" in err and "records summarizer.jlens None" in err

    def no_joint_meta(roots: Path) -> None:
        stamped(roots)
        (roots / "or" / ix.SAFETY_META_JOINT).unlink()

    assert _main(tmp_path / "e", _manifest(tmp_path / "e", mutate=_v9, prep=no_joint_meta)) == 2
    assert "index_meta_joint.json is missing" in capsys.readouterr().err


def test_final_v8_reads_no_overrefusal_stamp(tmp_path: Path) -> None:
    """The v8 path is unaffected: the same index.json from an unstamped root, a stamped one and
    one stamped as a comparison, but for the tmp paths (`sources`, `manifest`) and the copy of
    the root's own index_meta.json that the index carries (`safety.overrefusal.meta`)."""

    def built(sub: str, prep=None) -> dict:
        assert _main(tmp_path / sub, _manifest(tmp_path / sub, prep=prep)) == 0
        d = json.loads((tmp_path / sub / "final" / "index.json").read_text())
        assert d["version"] == "hindex-v8"
        for fam, unit in (("overrefusal", "refusal_claims"), ("benign_roleplay", "prompts")):
            for m in d["safety"][fam].pop("meta"):  # {root (a tmp path), meta}
                assert m["meta"]["unit"] == unit
        return {k: v for k, v in d.items() if k not in ("sources", "manifest", "git_sha")}

    plain = built("plain")
    assert built("stamped", lambda r: stamp_or_v9(r / "or")) == plain
    assert built("cmp", lambda r: stamp_or_v9(r / "or", comparison_only="x", bank=None)) == plain


# ---------------------------------------------------------------- 3: the gate's verdict


def _checks(*vs: str) -> list[tuple[str, str, str]]:
    return [("name", "value", v) for v in vs]


def test_overall_verdict_is_one_distinct_word() -> None:
    too_few = f"TOO FEW (n < {orj.MIN_ROWS_TO_GATE})"
    assert orj.overall(_checks("PASS", "PASS", "PASS"), False, False) == "PASS"
    assert orj.overall(_checks("PASS", "PASS", too_few), False, False) == "INCOMPLETE"
    assert orj.overall(_checks("PASS", "PASS", "PASS"), False, True) == "INCOMPLETE"
    assert orj.overall(_checks(too_few, too_few, too_few), False, True) == "INCOMPLETE"
    # a failed gated rate is a FAIL whatever else is missing
    assert orj.overall(_checks("FAIL", "PASS", too_few), False, True) == "FAIL"
    assert orj.overall(_checks("PASS", "FAIL", "PASS"), False, False) == "FAIL"
    # labellers below the agreement floor: inconclusive, never a pass
    assert orj.overall(_checks("PASS", "PASS", "PASS"), True, False).startswith("INCONCLUSIVE")
    for v in ("PASS", "FAIL", "INCOMPLETE"):
        assert v == getattr(orj, v)


def test_report_is_incomplete_when_a_rate_has_too_few_rows(tmp_path: Path) -> None:
    args = _world(tmp_path)  # topic_in_bag has 2 rows: below MIN_ROWS_TO_GATE
    asyncio.run(orj.label_async(args, FakeLabeller()))
    orj.cmd_report(args)
    res = json.loads((args.out / "result.json").read_text())
    assert res["verdict"] == "INCOMPLETE" and res["partial"] is False
    assert (res["n_sampled"], res["n_labelled"]) == (24, 24)
    assert res["checks"]["topic_in_bag"] == {
        "hits": 1,
        "n": 2,
        "rate": 0.5,
        "threshold": 0.9,
        "min_rows": 10,
        "too_few": True,
        "verdict": "TOO FEW (n < 10)",
    }
    c = res["checks"]["claims_decline"]
    assert (c["n"], c["too_few"], c["verdict"]) == (12, False, "PASS")
    c = res["checks"]["decline_recall"]
    assert (c["hits"], c["n"], c["too_few"], c["verdict"]) == (12, 16, False, "PASS")
    text = (args.out / "RESULT.md").read_text()
    assert "**Verdict: INCOMPLETE** (too few rows to gate topic_in_bag (n < 10))" in text
    assert "Verdict: PASS" not in text


def _all_off_topic(args) -> None:
    """Every v9 decline line of the toy world is off-topic: 12 topic rows, 11 in the bag."""
    recs = orj.read_jsonl(args.v9_records)
    for r in recs:
        if r.get("off"):
            r["off"]["topics"] = dict.fromkeys(r["off"]["topics"], "off_topic")
    orj.write_jsonl(args.v9_records, recs)


def test_report_passes_only_when_every_rate_is_gated_on_a_full_sample(tmp_path: Path) -> None:
    args = _world(tmp_path)
    _all_off_topic(args)
    asyncio.run(orj.label_async(args, FakeLabeller()))
    orj.cmd_report(args)
    res = json.loads((args.out / "result.json").read_text())
    assert res["verdict"] == "PASS" and res["partial"] is False
    assert res["agreed"]["topic_in_bag"] == [11, 12]
    assert not any(c["too_few"] for c in res["checks"].values())
    assert {c["verdict"] for c in res["checks"].values()} == {"PASS"}
    assert "**Verdict: PASS**\n" in (args.out / "RESULT.md").read_text()
    # the same rates on a partial labelling are not the gate: INCOMPLETE
    args.limit = 20
    asyncio.run(orj.label_async(args, FakeLabeller()))
    orj.cmd_report(args)
    res = json.loads((args.out / "result.json").read_text())
    assert res["verdict"] == "INCOMPLETE" and res["partial"] is True
    assert (res["n_sampled"], res["n_labelled"]) == (24, 20)
    assert not any(c["too_few"] for c in res["checks"].values())
    assert (
        "**Verdict: INCOMPLETE** (20 of 24 sampled units labelled)"
        in (args.out / "RESULT.md").read_text()
    )


def test_report_fails_on_a_failed_gated_rate(tmp_path: Path) -> None:
    args = _world(tmp_path)
    recs = orj.read_jsonl(args.v9_records)
    for r in recs[:6]:  # six of twelve "decline lines" claim no decline: 5 / 12 < 0.85
        r["digest"]["lines"] = ["[one] The topic is weapons."]
    orj.write_jsonl(args.v9_records, recs)
    asyncio.run(orj.label_async(args, FakeLabeller()))
    orj.cmd_report(args)
    res = json.loads((args.out / "result.json").read_text())
    assert res["checks"]["claims_decline"]["verdict"] == "FAIL"
    assert res["checks"]["topic_in_bag"]["too_few"] is True
    assert res["verdict"] == "FAIL"  # not INCOMPLETE: a gated rate failed


# ---------------------------------------------------------------- 5a: the summariser's decoder


def test_gate_bags_are_decoded_by_the_summarisers_decoder(tmp_path: Path, monkeypatch) -> None:
    from wsbench.hb.readouts import Cell

    raw = ["Ġsorry", "â–ģx", "æĬ±æŃī", "café", *[f"t{k}" for k in range(8)]]  # 12 tokens
    rows = [{"id": "i0", "layer": 44, "pos": 3, "tokens": raw, "scores": [1.0] * 12}]
    orj.write_jsonl(tmp_path / "bags.jsonl", rows)
    got = orj.load_bags(tmp_path / "bags.jsonl", {("i0", 44)})[("i0", 44)][3]
    assert got == sjudge.bag_tokens(raw) and len(got) == sjudge.BAG_K == orj.BAG_K
    assert got[0] == " sorry" and got[2] == "抱歉"
    # ... which is what the summary request of that site holds, token for token
    cell = Cell("i0", 44, 3, None, tuple(raw), tuple([1.0] * 12))
    assert sjudge.bag_text(cell) == " | ".join(f"{t} (1.00)" for t in got)
    # the gate calls sjudge's function (no private copy)
    monkeypatch.setattr(sjudge, "bag_tokens", lambda toks: ["<decoded by sjudge>"])
    assert orj.load_bags(tmp_path / "bags.jsonl", {("i0", 44)})[("i0", 44)][3] == [
        "<decoded by sjudge>"
    ]
    src = (Path(orj.__file__)).read_text()
    assert "wsb_bpe" not in src and "decode_token" not in src


# ---------------------------------------------------------------- 5b: labels of the sample


def test_report_refuses_labels_of_another_sample(tmp_path: Path, capsys) -> None:
    args = _world(tmp_path)
    asyncio.run(orj.label_async(args, FakeLabeller()))
    sha = orj.sha256_file(args.out / "sample.jsonl")
    labels = orj.read_jsonl(args.out / "labels.jsonl")
    assert {r["sample_sha256"] for r in labels} == {sha}
    orj.cmd_report(args)
    assert "WARNING" not in capsys.readouterr().out
    # the sample is re-drawn (here: one unit's stratum edited): the labels are stale
    sample = orj.read_jsonl(args.out / "sample.jsonl")
    orj.write_jsonl(args.out / "sample.jsonl", [{**sample[0], "stratum": "x"}, *sample[1:]])
    with pytest.raises(SystemExit, match=r"was not made from the current .*sample\.jsonl"):
        orj.cmd_report(args)
    # labelling again stamps the new sample (every label call answered by the cache)
    llm = FakeLabeller()
    asyncio.run(orj.label_async(args, llm))
    assert llm.calls == []
    orj.cmd_report(args)


def test_report_accepts_legacy_labels_with_a_warning(tmp_path: Path, capsys) -> None:
    args = _world(tmp_path)
    asyncio.run(orj.label_async(args, FakeLabeller()))
    labels = orj.read_jsonl(args.out / "labels.jsonl")
    legacy = [{k: v for k, v in r.items() if k != "sample_sha256"} for r in labels]
    orj.write_jsonl(args.out / "labels.jsonl", legacy)
    capsys.readouterr()
    orj.cmd_report(args)
    out = capsys.readouterr().out
    assert "WARNING" in out and "records no sample sha256" in out
    assert json.loads((args.out / "result.json").read_text())["n_labelled"] == 24
    # legacy labels of a unit the sample does not hold are refused
    orj.write_jsonl(args.out / "labels.jsonl", [{**legacy[0], "uid": "zz:L44"}, *legacy[1:]])
    with pytest.raises(SystemExit, match="units that are not in"):
        orj.cmd_report(args)
    # and so is a file that mixes stamped and unstamped rows
    orj.write_jsonl(args.out / "labels.jsonl", [labels[0], *legacy[1:]])
    with pytest.raises(SystemExit, match="was not made from the current"):
        orj.cmd_report(args)


# ---------------------------------------------------------------- 4: the export's summary


def _digest(status: str, reason: str) -> dict:
    return {"status": status, "lines": [], "prov": [], "reason": reason}


def test_export_v9_counts_empty_apart_from_unjudged(v9_world, tmp_path: Path) -> None:  # noqa: F811
    w = v9_world
    recs = data.read_jsonl(w["j9"])
    # jlens a@L20: a digest that came back empty (not all-junk): a failed unit, kind `empty`
    recs[0] = {**recs[0], "digest": _digest("empty", "every cell blank"), "verdict": "no_refusal"}
    recs[0] |= {"decline_lines": [], "off": None, "n_junk_sites": 0}
    # jlens a@L24: two failed summaries void ONE unit
    recs[2] = {
        **recs[2],
        "digest": _digest("unjudged", "summary failed (2 sites)"),
        "verdict": None,
        "decline_lines": [],
        "off": None,
        "n_failed_summaries": 2,
    }
    p = tmp_path / "j9_mixed.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    # the rule of the rows is unchanged: both are failed units, lumped as `unjudged`
    assert [ox.record_counts(r)[2] for r in recs] == ["unjudged", None, "unjudged", None]
    assert [ox.failure_kind(r) for r in recs] == ["empty", None, "unjudged", None]
    _rows, fails = ox.index_rows(recs, data.load_items(bank="v2"), ["jlens"])
    assert fails == {"unjudged": 2, "strict_judge_failed": 0, "off_topic_failed": 0}
    jj = data.read_jsonl(w["j9j"])
    jj[1] = {**jj[1], "digest": _digest("empty", ""), "verdict": "no_refusal"}
    pj = tmp_path / "j9j_mixed.jsonl"
    pj.write_text("".join(json.dumps(r) + "\n" for r in jj))
    res = ox.export_v9(
        w["dest"],
        prose_records=w["full"],
        jlens_records=p,
        prose_records_joint=w["joint"],
        jlens_records_joint=pj,
    )
    h = res["per_layer"]
    assert (h["unjudged"], h["empty"], h["strict_judge_failed"], h["off_topic_failed"]) == (
        1,
        1,
        0,
        0,
    )
    assert h["n_failed_cells"] == 2 == h["by_reader"]["jlens"]["n_failed"]
    zero = dict.fromkeys(ox.FAILURE_KINDS, 0)
    assert h["failed_by_reader"] == {
        "jlens": {**zero, "unjudged": 1, "empty": 1},
        "mlnla-rl600-all11": zero,
        "s3d-rl600": zero,
    }
    assert h["jlens_summaries"] == {
        "n_junk_sites": 3,
        "n_failed_summaries": 2,
        "n_all_junk_units": 2,
    }
    assert h["n_units_voided_by_failed_summary"] == 1
    hj = res["joint"]
    assert (hj["unjudged"], hj["empty"], hj["n_failed_cells"]) == (0, 1, 1)
    assert hj["failed_by_reader"]["jlens"] == {**zero, "empty": 1}
    assert hj["n_units_voided_by_failed_summary"] == 0
    # the meta files hold the same blocks, and the rows are plan 0022's (failed = counts 0)
    for name, block in ((ox.ITEM_META, h), (ox.JOINT_META, hj)):
        assert json.loads((w["dest"] / name).read_text())["health"] == block
    got = {
        (r["arm"], r["layer"], r["item"]): (r["n_claims"], r["n_failed_cells"])
        for r in data.read_jsonl(w["dest"] / ox.ITEM_COUNTS)
    }
    assert got[("jlens", 20, "a")] == (0, 1) and got[("jlens", 24, "a")] == (0, 1)
    assert got[("jlens", 20, "c")] == (0, 0)  # all-junk: settled, not `empty`
    assert sorted(got.values()).count((0, 1)) == 2


def test_export_v9_summary_of_a_clean_world(v9_world, capsys) -> None:  # noqa: F811
    w = v9_world
    res = ox.export_v9(w["dest"], prose_records=w["full"], jlens_records=w["j9"])
    h = res["per_layer"]
    assert (h["unjudged"], h["empty"], h["n_units_voided_by_failed_summary"]) == (0, 0, 0)
    assert set(h["failed_by_reader"]) == {"jlens", "mlnla-rl600-all11", "s3d-rl600"}
    # the v8 exports' health blocks have no new key (their meta bytes do not move)
    import inspect

    for fn in (ox.export, ox.export_joint):
        assert "failure_split" not in inspect.getsource(fn)


def test_v9_dest_help_text(capsys) -> None:
    import argparse

    p = argparse.ArgumentParser()
    or_cli.register(p.add_subparsers())
    with pytest.raises(SystemExit):
        p.parse_args(["overrefusal", "offtopic-export", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "the hindex-v9 Off-topic refusal root (family id overrefusal)" in text
    assert "hindex-v9 overrefusal" not in text


def test_gate_missing_label_is_incomplete_not_pass():
    """Review round 3: a label call that failed leaves a None slot; the agreed rates drop it, so
    the verdict must be INCOMPLETE, never PASS on the shrunken denominator."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "orj_missing", Path(__file__).resolve().parents[2] / "scripts/hb/v9/gates/orj.py"
    )
    orj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orj)
    a, b = orj.LABELLERS
    bag_f, line_f = orj.SLOTS["bag"][2], orj.SLOTS["line"][2]
    row = {
        "v9": {"lines": [{"line": 0, "topic": "on_topic"}]},
        "labels": {
            a: {"bag": {bag_f: True}, "line:0": {line_f: True}},
            b: {"bag": {bag_f: True}, "line:0": {line_f: True}},
        },
    }
    assert orj.missing_labels([row]) == 0
    row["labels"][b]["line:0"] = None
    assert orj.missing_labels([row]) == 1
    checks = [("x", "y", orj.PASS)]
    assert orj.overall(checks, False, orj.missing_labels([row]) > 0) == orj.INCOMPLETE
    assert orj.overall(checks, False, False) == orj.PASS
