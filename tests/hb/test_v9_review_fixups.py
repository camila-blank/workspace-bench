"""Plan 0121, review fix-ups of PR a: a v8 manifest refuses an interp-v2 root; ``check_v9`` does
not fail open on an unknown arm kind; the junk share is junk / summarised; the J-lens
``am_situation`` rate is reported both ways in a v9 index; no v5c span side stage in an interp-v2
root; a v9 index has its own note, display names and plot label; the v1-only scripts refuse an
interp-v2 root. v8 outputs are unchanged."""

import importlib.util
import json
from pathlib import Path

import pytest

from wsbench.hb import cli, final
from wsbench.hb import index as ix
from wsbench.hb.evals.agentic_misalignment import situation
from wsbench.hb.evals.wsbench_chat import run as wrun

from .test_final import CLAIM_DIRS, _main, _manifest
from .test_index_v8 import ITEMS, LAYERS, _w, v8_sources
from .test_v9_chat_junk import M8, M9, V2Fake, _cells, _run, _tokens
from .test_v9_index_final import _stamp, _v9

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _two_layer_grid(monkeypatch):
    monkeypatch.setattr(final, "EXPECTED_LAYERS", (20, 24))


@pytest.fixture(autouse=True)
def _or_roots_carry_the_v9_stamps(monkeypatch):
    """Plan 0121 §4 (review fix 2): a hindex-v9 build refuses an overrefusal root without the
    bank-v2 / interp-v2 stamps, so in this module the fixture roots are stamped as `export_v9`
    stamps them (`index_helpers.or_v9_stamped`; a v8 build reads none of the stamps). The
    refusals themselves are tested in `test_v9_or_review_fixes.py`."""
    import sys

    from . import test_final
    from .index_helpers import or_v9_stamped

    stamped = or_v9_stamped(test_final.v8_sources)
    monkeypatch.setattr(test_final, "v8_sources", stamped)
    monkeypatch.setattr(sys.modules[__name__], "v8_sources", stamped)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 1a: v8 refuses interp-v2 roots


@pytest.mark.parametrize("sub", sorted(CLAIM_DIRS.values()))
@pytest.mark.parametrize("name", ["arm.json", "joint.json"])
def test_v8_manifest_refuses_a_root_recorded_on_another_summariser(tmp_path, capsys, sub, name):
    def prep(roots: Path) -> None:
        p = roots / sub / "jlens" / name
        # (the AM fixture has no joint.json: a new one records the verifier, as a real one does)
        rec = json.loads(p.read_text()) if p.is_file() else {"excuse_verifier": "cverify-v1"}
        p.write_text(json.dumps({**rec, "summ_prompt": "interp-v2"}))

    assert _main(tmp_path, _manifest(tmp_path, prep=prep)) == 2
    err = capsys.readouterr().err
    assert f"{sub}/jlens/{name} records summ_prompt 'interp-v2'; hindex-v8 needs 'interp-v1'" in err


def test_v8_manifest_accepts_a_missing_or_interp_v1_record(tmp_path):
    def prep(roots: Path) -> None:  # an explicit interp-v1 record is the same as none
        a = roots / "story" / "jlens" / "arm.json"
        a.write_text(json.dumps({**json.loads(a.read_text()), "summ_prompt": "interp-v1"}))

    assert _main(tmp_path, _manifest(tmp_path, prep=prep)) == 0
    assert _main(tmp_path / "plain", _manifest(tmp_path / "plain")) == 0

    # the am_situation entries are covered too (a summarised family that is not a claim map)
    def sit_only(m: dict) -> None:
        m["entries"] = [e for e in m["entries"] if e["family"] == ix.AM_SITUATION]

    def stamp_am(roots: Path) -> None:
        a = roots / "am" / "jlens" / "arm.json"
        a.write_text(json.dumps({**json.loads(a.read_text()), "summ_prompt": "interp-v2"}))

    m = final.load_manifest(_manifest(tmp_path / "s", mutate=sit_only, prep=stamp_am))
    with pytest.raises(final.ManifestError, match=r"am_situation x jlens x L20.*hindex-v8 needs"):
        final.check_v8_summariser(m, tmp_path / "s")
    # the frozen manifest of record names no root with a summ_prompt (not checked here: its
    # roots are not in the repository; G1 rebuilds it)
    assert final.V8_SUMMARIZER_PROMPT == "interp-v1"


# ---------------------------------------------------------------- 1b: no fail-open on the kind


@pytest.mark.parametrize("kind", [None, "token", "bags", ""])
def test_v9_refuses_an_arm_of_unknown_kind(tmp_path, capsys, kind):
    def prep(roots: Path) -> None:
        _stamp(roots)
        a = roots / "story" / "jlens" / "arm.json"
        rec = json.loads(a.read_text())
        if kind is None:
            del rec["kind"]
        else:
            rec["kind"] = kind
        a.write_text(json.dumps(rec))

    assert _main(tmp_path, _manifest(tmp_path, mutate=_v9, prep=prep)) == 2
    err = capsys.readouterr().err
    assert f"story/jlens/arm.json records kind {kind!r} (neither 'tokens' nor 'prose')" in err


# ---------------------------------------------------------------- 1c: junk / summarised


def test_junk_share_is_over_the_summarised_cells(tmp_path):
    def chat(i, layer, summary):
        return {"id": i, "layer": layer, "status": "x", "stages": {"summary": summary, "map": None}}

    rows = [chat("a", 20, "ok"), chat("b", 20, "ok"), chat("c", 20, "junk"), chat("d", 20, None)]
    rows += [chat("e", 20, "failed"), chat("f", 20, "pending"), chat("a", 24, "ok")]
    _w(tmp_path / "c.jsonl", rows)
    assert ix.junk_summaries(tmp_path / "c.jsonl", ix.MAIN) == {
        20: {"n_cells": 6, "n_summarised": 3, "n_junk": 1},
        24: {"n_cells": 1, "n_summarised": 1, "n_junk": 0},
    }

    def am(i, status):
        return {"id": i, "layer": 20, "status": "ok", "summary_status": status, "bag": {"x": 1}}

    _w(
        tmp_path / "a.jsonl",
        [am("a", None)] * 7 + [am("b", "ok"), am("c", "junk"), am("d", "junk")],
    )
    got = ix.junk_summaries(tmp_path / "a.jsonl", ix.AM_CLAIMS)
    assert got == {20: {"n_cells": 10, "n_summarised": 3, "n_junk": 2}}
    # a nested or quoted look-alike is not a summarised cell
    odd = {**chat("z", 20, None), "map": {"summary": "ok"}, "samples": ['"summary": "junk"']}
    _w(tmp_path / "o.jsonl", [odd])
    assert ix.junk_summaries(tmp_path / "o.jsonl", ix.MAIN) == {
        20: {"n_cells": 1, "n_summarised": 0, "n_junk": 0}
    }
    md = ix._md_junk_summaries({"junk_summaries": {"am_claims": {"jlens": {"20": got[20]}}}})
    assert "| junk summaries | summarised cells | % of summarised | cells |" in md
    assert "| am_claims | jlens | 20 | 2 | 3 | 66.7 | 10 |" in md  # 2 / 3, not 2 / 10


# ---------------------------------------------------------------- 1d: am_situation both ways


def _sit(i, layer, pos, **kw):
    return {"id": i, "layer": layer, "pos": pos, **kw}


def test_item_rows_junk_as_judged():
    recs = [
        _sit("a", 20, 0, status="ok", counted=True),
        _sit("a", 20, 1, status="empty", junk=True),
        _sit("a", 20, 2, status="empty"),  # an empty cell is never judged
        _sit("b", 20, 0, status="empty", junk=True),
        _sit("b", 20, 1, status="failed"),
    ]
    assert situation.is_junk(recs[1]) and not situation.is_junk(recs[2])
    assert {k: v.tolist() for k, v in situation.item_rows(recs)[20].items()} == {
        "a": [1, 1],
        "b": [0, 0],
    }  # the default (hindex-v8, and the v9 headline): junk dropped
    both = situation.item_rows(recs, junk_as_judged=True)[20]
    assert {k: v.tolist() for k, v in both.items()} == {"a": [2, 1], "b": [1, 0]}


def _sources_with_junk(tmp: Path) -> dict:
    src = v8_sources(tmp)
    p = tmp / "am" / "jlens" / ix.SITUATION
    recs = [json.loads(x) for x in p.read_text().splitlines()]
    for r in recs:  # at L24: two of i1's four cells are junk; elsewhere nothing changes
        if r["layer"] == 24 and r["id"] == "i1" and r["pos"] in (2, 3):
            r.clear()
            r.update(_sit("i1", 24, 9, status="empty", junk=True))
    _w(p, recs)
    return src


def test_v9_reports_the_jlens_am_situation_rate_both_ways(tmp_path):
    src = _sources_with_junk(tmp_path)
    v8 = ix.build_index(src, ix.Methods(), version="v8")
    v9 = ix.build_index(src, ix.Methods(), version="v9")
    assert "am_situation_junk" not in v8
    got = v9["am_situation_junk"]
    assert set(got) == {"jlens"} and set(got["jlens"]) == {"24"}  # only layers with junk
    c = got["jlens"]["24"]
    n = len(ITEMS) * 4
    assert c["n_junk"] == 2
    assert (c["dropped"]["n"], c["dropped"]["n_misrepresents"]) == (n - 2, 1)
    assert (c["junk_counted"]["n"], c["junk_counted"]["n_misrepresents"]) == (n, 1)
    assert c["dropped"]["v"] == 1 / (n - 2) and c["junk_counted"]["v"] == 1 / n
    assert c["junk_counted"]["n_items"] == c["dropped"]["n_items"] == len(ITEMS)
    # the headline value is "dropped", in v9 as in v8
    for built in (v8, v9):
        row = next(r for r in built["rows"] if r["method"] == "jlens" and r["layer"] == 24)
        fam = row["families"][ix.AM_SITUATION]
        assert {k: fam[k] for k in ("v", "ci", "n")} == {
            k: c["dropped"][k] for k in ("v", "ci", "n")
        }
    assert v9["rows"] == v8["rows"] and v9["over_layers"] == v8["over_layers"]
    md = ix.render_md(v9)
    assert "## am_situation with junk summaries (J-lens, interp-v2): both rates" in md
    assert f"| jlens | 24 | 2 | {100 / (n - 2):.1f} [" in md and f"(1/{n}) |" in md
    assert "am_situation with junk" not in ix.render_md(v8)
    # no junk record: the key is there and empty, and index.md has no such section
    clean = ix.build_index(v8_sources(tmp_path / "clean"), ix.Methods(), version="v9")
    assert clean["am_situation_junk"] == {}
    assert "am_situation with junk" not in ix.render_md(clean)
    assert LAYERS  # (the fixture's layers)


# ---------------------------------------------------------------- 1e: no v5c side stage in v9 roots


def test_no_v5c_span_side_stage_in_an_interp_v2_root(tmp_path, monkeypatch):
    monkeypatch.setattr(wrun, "in_v5c_subsample", lambda item_id, seed=0: True)
    t = _tokens(tmp_path, junk=())
    f8, f9 = V2Fake(), V2Fake()
    _run(tmp_path / "v8", t, M8, f8)
    _run(tmp_path / "v9", t, M9, f9)
    assert f8.n("span") == 2 and f8.n("map") == 2  # interp-v1 root: the side stage, as before
    assert f9.n("span") == 0 and f9.n("map") == 2  # interp-v2 root: never
    c8 = _cells(wrun.arm_dir(tmp_path / "v8", M8, "jl"))
    c9 = _cells(wrun.arm_dir(tmp_path / "v9", M9, "jl"))
    assert {c["stages"]["span"] for c in c8} == {"ok"}
    assert {c["stages"]["span"] for c in c9} == {"pending"}  # as a cell outside the subsample
    assert {c["stages"]["map"] for c in c9} == {"ok"}
    assert wrun.v5c_side_stage(M8, ["map"], False, "x") is True
    assert wrun.v5c_side_stage(M9, ["map"], False, "x") is False
    assert wrun.v5c_side_stage(M8, ["map"], True, "x") is False  # --anchor v5c: its own span stage
    assert wrun.v5c_side_stage(M8, ["summary"], False, "x") is False
    # an explicitly selected span stage still runs in an interp-v2 root
    f = V2Fake()
    wrun.run_arms(
        [("jl", t)],
        tmp_path / "v9",
        M9,
        layers=None,
        limit=None,
        rpm=1e6,
        concurrency=2,
        allow_missing=True,
        llm=f,
        stages=["span"],
    )
    assert f.n("span") == 2


# ---------------------------------------------------------------- 1f: v9 notes, names, label


def test_v9_note_and_display_names(tmp_path):
    src = v8_sources(tmp_path)
    v8 = ix.build_index(src, ix.Methods(), version="v8")
    v9 = ix.build_index(src, ix.Methods(), version="v9")
    assert set(v8["notes"]) == {"hindex-v8"} and set(v9["notes"]) == {"hindex-v9"}
    assert not any("hindex-v8" in f"{k} {v}" for k, v in v9["notes"].items())
    note = v9["notes"]["hindex-v9"]
    assert note.startswith("plan 0121 (provisional") and "Off-topic refusal" in note
    assert "interp-v2" in note and "am_situation_junk" in note
    # the claim-map sentence is the checked version's, as in the v8 note
    for kw in ({}, {"prompt_version": "claimmap-v2.9", "excuse_verifier": "cverify-v1"}):
        n8, n9 = ix.notes_v8(**kw)["hindex-v8"], ix.notes_v9(**kw)["hindex-v9"]
        claim = ix.CLAIM_MAPS_NOTE_V8[None]
        if kw:
            claim = ix.CLAIM_MAPS_NOTE_V8_VERIFIER[("claimmap-v2.9", "cverify-v1")]
        assert claim in n8 and claim in n9
        assert n9.endswith(n8.split(claim + " ", 1)[1])  # the shared plan-0029 sentences
    with pytest.raises(ValueError, match="no hindex-v8 note for claim-map prompt"):
        ix.notes_v9("claimmap-v9.9")
    # the v8 note is the template it always was
    assert ix.notes_v8()["hindex-v8"] == ix.NOTES_V8_TEMPLATE.format(
        claim_maps=ix.CLAIM_MAPS_NOTE_V8[None]
    )
    assert v9["display_names"] == {"overrefusal": "Off-topic refusal"} and "display_names" not in v8
    md8, md9 = ix.render_md(v8), ix.render_md(v9)
    assert "| overrefusal | benign_roleplay |" in md8 and "Off-topic refusal" not in md8
    assert "| Off-topic refusal | benign_roleplay |" in md9 and "| overrefusal |" not in md9
    assert "Off-topic refusal 1, benign_roleplay 2" in md9.splitlines()[2]
    assert "- hindex-v9: plan 0121" in md9 and "hindex-v8" not in md9
    # family ids in the data are never renamed
    assert set(v9["weights"]["family"]) == set(v8["weights"]["family"])
    assert set(v9["safety"]) == set(v8["safety"]) == {"overrefusal", "benign_roleplay"}


def test_plot_label_names_the_version_built(tmp_path, monkeypatch):
    labels = []

    def spy(index, keep, dest):
        import matplotlib.axes

        orig = matplotlib.axes.Axes.set_ylabel

        def rec(self, label, *a, **kw):
            labels.append(label)
            return orig(self, label, *a, **kw)

        monkeypatch.setattr(matplotlib.axes.Axes, "set_ylabel", rec)
        return real(index, keep, dest)

    real = final.plot_index_per_layer
    monkeypatch.setattr(final, "plot_index_per_layer", spy)
    assert _main(tmp_path / "8", _manifest(tmp_path / "8")) == 0
    assert _main(tmp_path / "9", _manifest(tmp_path / "9", mutate=_v9, prep=_stamp)) == 0
    assert labels[0] == "hindex-v8 (% hallucinated)" and "hindex-v9 (% hallucinated)" in labels


def test_cli_help_no_longer_says_v8_is_the_only_version():
    p = cli.build_parser()
    sub = next(a for a in p._actions if getattr(a, "choices", None) and "index" in a.choices)
    text = " ".join(sub.choices["index"].format_help().split())
    listed = " ".join(a.help or "" for a in sub._choices_actions)
    for t in (text, listed):
        assert "only version" not in t and "only choice" not in t
    assert "--version v9" in listed and "hindex-v9" in listed
    assert "{v8,v9}" in text
    doc = (REPO / "src" / "wsbench" / "hb" / "CLAUDE.md").read_text()
    assert "has ONE version" not in doc and "the only choice" not in doc
    assert "accepts only `v8`" not in doc and "hindex-v8 is the only version" not in doc
    assert "junk / summarised" in doc and "Off-topic refusal for hindex-v9" in doc


# ---------------------------------------------------------------- 1g: v1-only scripts


def test_joint_bundle_refuses_an_interp_v2_root(tmp_path):
    jb = _load("joint_bundle", "scripts/hb/main_x3/joint_bundle.py")
    d = tmp_path / "jlens"
    d.mkdir()
    cache = d / "cache.jsonl"
    v1 = {"key": "summ:a__L020__p1", "result": {"interpretation": "a note"}}
    cache.write_text(json.dumps(v1) + "\n")
    assert jb.summaries(cache) == {"a__L020__p1": "a note"}  # an interp-v1 root: as before
    assert jb.summaries(None) == {} and jb.summaries(d / "none.jsonl") == {}
    (d / "arm.json").write_text(json.dumps({"kind": "tokens"}))
    assert jb.summaries(cache) == {"a__L020__p1": "a note"}
    (d / "arm.json").write_text(json.dumps({"kind": "tokens", "summ_prompt": "interp-v2"}))
    with pytest.raises(
        SystemExit, match=r"records summ_prompt 'interp-v2'.*interp-v1 summaries only"
    ):
        jb.summaries(cache)
    # no arm.json beside the cache: an interp-v2 answer is still recognised and refused
    (d / "arm.json").unlink()
    v2 = {"key": "summ:b__L020__p1", "result": {"junk": True, "interpretation": "not a summary"}}
    cache.write_text(json.dumps(v1) + "\n" + json.dumps(v2) + "\n")
    with pytest.raises(SystemExit, match="summ:b__L020__p1 is not an interp-v1 answer"):
        jb.summaries(cache)


def test_am_audit_refuses_an_interp_v2_root(tmp_path):
    am_audit = _load("am_audit_fixups", "scripts/hb/am_audit.py")
    from wsbench.hb.evals.agentic_misalignment import run as amrun

    argv = ["score", "--out", str(tmp_path), "--arms", "jlens,s3d-rl600"]
    ap_args = lambda: __import__("argparse").Namespace(  # noqa: E731
        out=str(tmp_path), judge_model="claude-opus-5-5", effort="low", arms="jlens,s3d-rl600"
    )
    am_audit.check_interp_v1(ap_args())  # no arm.json at all: nothing to refuse
    d = amrun.arm_dir(tmp_path, am_audit.models_of(ap_args()), "jlens")
    d.mkdir(parents=True)
    (d / "arm.json").write_text(json.dumps({"kind": "tokens"}))
    am_audit.check_interp_v1(ap_args())  # a v8 root records nothing
    (d / "arm.json").write_text(json.dumps({"kind": "tokens", "summ_prompt": "interp-v2"}))
    with pytest.raises(SystemExit, match="records summ_prompt 'interp-v2'; the AM audits read"):
        am_audit.check_interp_v1(ap_args())
    with pytest.raises(SystemExit, match="interp-v1 roots only"):
        am_audit.main(argv)  # every subcommand, before it reads anything
