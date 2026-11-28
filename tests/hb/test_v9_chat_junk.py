"""Plan 0121 §2.2 (chat / story pipeline): ``--summarizer-prompt interp-v2`` is threaded through
``Models`` (recorded only when not interp-v1, guarded, replayed), and a junk summary is a
settled answer that says nothing: today's empty record plus the tokens and
``stages.summary = "junk"``; never failed, never re-asked, no map call, no claim."""

import json

import pytest

from wsbench.hb import cli, final, summarizer
from wsbench.hb import consistency_v2 as cons
from wsbench.hb import index as ix
from wsbench.hb.cache import fingerprint
from wsbench.hb.evals.wsbench_chat import bank
from wsbench.hb.evals.wsbench_chat import run as wrun
from wsbench.hb.llm import CallResult, JudgeConfigError, Usage

from .conftest import TOY_ITEMS, write_jsonl
from .test_claimmap_v28 import Map28Fake

V28 = "claimmap-v2.8"
M8 = wrun.Models(map_prompt=V28)  # a v8-style root: the default summariser, interp-v1
M9 = wrun.Models(map_prompt=V28, summ_prompt="interp-v2")
NOT_WORDS = ["##", "]]", "\\u"]  # the bag the fake summariser answers junk for
TEXT = "A friendly note about Paris and the teacher."


class V2Fake(Map28Fake):
    """Answers interp-v2 summary calls (junk for the ``NOT_WORDS`` bag); interp-v1 routing is
    the shared fake's, unchanged."""

    async def call(self, *, model, system, user, schema):
        if system == summarizer.INTERP2_SYSTEM:
            assert schema is summarizer.INTERP2_SCHEMA
            self.calls.append(("summary2", model, system, user))
            if "##" in user:
                # junk = true is junk whatever the text
                return CallResult({"junk": True, "interpretation": "nothing"}, Usage(calls=1))
            return CallResult({"junk": False, "interpretation": TEXT}, Usage(calls=1, usd=0.01))
        return await super().call(model=model, system=system, user=user, schema=schema)


def _tokens(tmp_path, layers=(44,), junk=((44, 0),)):
    """Token readouts on the first item's first two sites; ``junk`` = (layer, site) bags that
    hold no word."""
    item = next(it for it in bank.load_items() if it["id"] == TOY_ITEMS[0])
    rows = []
    for la in layers:
        for n, s in enumerate(item["sites"][:2]):
            toks = NOT_WORDS if (la, n) in junk else ["ĠParis", "Ġteacher", "Ġnote"]
            rows.append(
                {
                    "id": item["id"],
                    "layer": la,
                    "pos": int(s["pos"]),
                    "tokens": toks,
                    "scores": [3.0, 2.0, 1.0],
                }
            )
    return write_jsonl(tmp_path / "t.jsonl", rows)


def _run(out, readouts, models, fake):
    return wrun.run_arms(
        [("jl", readouts)],
        out,
        models,
        layers=None,
        limit=None,
        rpm=1e6,
        concurrency=2,
        allow_missing=True,
        llm=fake,
        stages=["map"],
    )


def _cells(d, name="cells.jsonl"):
    return [json.loads(x) for x in (d / name).read_text().splitlines()]


def _cache(d):
    return [json.loads(x) for x in (d / "cache.jsonl").open()]


# ---------------------------------------------------------------- Models / records


def test_summ_prompt_is_recorded_only_when_not_interp_v1():
    assert wrun.Models().summ_prompt == "interp-v1" == summarizer.V1
    assert "summ_prompt" not in wrun.Models().to_dict()
    assert "summ_prompt" not in M8.to_dict()
    assert M9.to_dict()["summ_prompt"] == "interp-v2"
    assert list(M9.to_dict())[-1] == "summ_prompt"  # appended: the v8 keys keep their order


def test_cli_flag():
    p = cli.build_parser()
    assert cli._models(p.parse_args(["run"])).summ_prompt == "interp-v1"
    a = p.parse_args(["run", "--summarizer-prompt", "interp-v2"])
    assert cli._models(a).summ_prompt == "interp-v2"
    with pytest.raises(SystemExit):
        p.parse_args(["run", "--summarizer-prompt", "interp-v3"])
    # commands without the flag (report, consistency-v2, ...) keep interp-v1
    assert cli._models(p.parse_args(["consistency-v2", "--arms", "a:44"])).summ_prompt == (
        "interp-v1"
    )


def test_an_unregistered_summ_prompt_is_a_config_error(tmp_path):
    bad = wrun.Models(map_prompt=V28, summ_prompt="interp-v3")
    with pytest.raises(JudgeConfigError, match="unknown summarizer prompt"):
        _run(tmp_path / "out", _tokens(tmp_path), bad, V2Fake())


# ---------------------------------------------------------------- the junk cell


def test_junk_cell_is_the_empty_record_and_is_never_failed_or_reasked(tmp_path):
    t = _tokens(tmp_path)
    fake = V2Fake()
    s = _run(tmp_path / "out", t, M9, fake)["jl"]
    assert fake.n("summary2") == 2 and fake.n("summary") == 0  # no interp-v1 call
    assert fake.n("map") == 1  # the junk cell is never mapped
    d = wrun.arm_dir(tmp_path / "out", M9, "jl")
    junk, ok = _cells(d)
    # A5: today's empty record exactly, plus `tokens` and the marker
    assert junk["status"] == "empty" and junk["samples"] is None and junk["n_samples"] == 0
    assert junk["stages"] == {**dict.fromkeys(wrun.STAGES), "summary": "junk"}
    assert junk["map"] is None and junk["verdict"] is None and junk["spans"] == []
    assert junk["tokens"] == NOT_WORDS
    assert set(junk) - {"tokens"} == set(_empty_record(tmp_path))
    assert ok["status"] != "empty" and ok["samples"] == [TEXT] and ok["stages"]["map"] == "ok"
    # a settled, valid, cached answer: not failed, not pending, exit 0
    row = next(r for r in _cache(d) if r["key"] == f"summ:{junk['key']}")
    assert row["result"] == {"junk": True, "interpretation": "nothing"}
    h = s["health"]
    assert not any(v for k, v in h.items() if k.startswith("n_failed_"))  # the exit-4 rule
    assert h["n_pending_summary"] == 0 and h["n_pending_map"] == 0
    assert h["n_entered_summary"] == 1  # the junk answer is not a summary to judge
    assert h["n_empty_cells"] == 1 and h["n_cells"] == 2
    # the v8 health keys are unchanged (A5: the junk count is derived from the cells)
    from wsbench.hb.evals.wsbench_chat import score

    assert not [k for k in score.HEALTH_COUNTS if "junk" in k]
    assert not [k for k in h if "junk" in k]
    # never re-asked: a second judge run makes no call at all
    n = len(fake.calls)
    _run(tmp_path / "out", t, M9, fake)
    assert len(fake.calls) == n
    # no claims: `hbench final` drops it like an empty cell, the index counts it as junk
    e = {"family": ix.MAIN, "method": "jlens", "unit": "L44"}
    assert final.recount(e, d / "cells.jsonl")[:2] == (1, 0)
    assert ix.junk_summaries(d / "cells.jsonl", ix.MAIN) == {44: {"n_cells": 2, "n_junk": 1}}
    acc = ix.claim_rows(d / "cells.jsonl", ix.MAIN)
    assert len(acc[44]) == 1  # one item row, from the one mapped cell


def _empty_record(tmp_path):
    """The record the pipeline writes today for an empty token cell."""
    item = next(it for it in bank.load_items() if it["id"] == TOY_ITEMS[0])
    p0, p1 = (int(s["pos"]) for s in item["sites"][:2])
    rows = [
        {"id": item["id"], "layer": 44, "pos": p0, "tokens": []},
        {"id": item["id"], "layer": 44, "pos": p1, "tokens": ["ĠParis"], "scores": [1.0]},
    ]
    _run(tmp_path / "e", write_jsonl(tmp_path / "e.jsonl", rows), M8, V2Fake())
    rec, _other = _cells(wrun.arm_dir(tmp_path / "e", M8, "jl"))
    assert rec["status"] == "empty" and "tokens" not in rec
    return rec


def test_invalid_v2_answer_is_failed_not_junk(tmp_path):
    class Blank(V2Fake):
        async def call(self, *, model, system, user, schema):
            if system == summarizer.INTERP2_SYSTEM:
                self.calls.append(("summary2", model, system, user))
                return CallResult({"junk": False, "interpretation": " "}, Usage(calls=1))
            return await super().call(model=model, system=system, user=user, schema=schema)

    fake = Blank()
    s = _run(tmp_path / "out", _tokens(tmp_path, junk=()), M9, fake)["jl"]
    assert fake.n("summary2") == 2  # invalid_retries stays 0 for the summary call
    assert s["health"]["n_failed_summary"] == 2
    cells = _cells(wrun.arm_dir(tmp_path / "out", M9, "jl"))
    assert {c["status"] for c in cells} == {"failed_summary"}
    assert {c["stages"]["summary"] for c in cells} == {"failed"}


# ---------------------------------------------------------------- record, guard, replay


def test_root_records_guards_and_replays_the_summariser(tmp_path, capsys):
    t = _tokens(tmp_path)
    out = tmp_path / "out"
    s = _run(out, t, M9, V2Fake())["jl"]
    d = wrun.arm_dir(out, M9, "jl")
    assert json.loads((d / "arm.json").read_text())["summ_prompt"] == "interp-v2"
    assert s["config"]["prompt_versions"]["summary"] == "interp-v2"
    assert s["config"]["models"]["summ_prompt"] == "interp-v2"
    # the guard: this root refuses interp-v1 (and nothing is called or rewritten)
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    fake = V2Fake()
    with pytest.raises(JudgeConfigError, match="judged with --summarizer-prompt interp-v2"):
        _run(out, t, M8, fake)
    assert fake.calls == [] and {p.name: p.read_bytes() for p in d.iterdir()} == before
    # ... as exit 3 from the CLI
    argv = ["run", "--out", str(out), "--arm", f"jl={t}", "--map-prompt", V28]
    argv += ["--excuse-verifier", "off", "--stages", "map", "--allow-missing"]
    assert cli.main(argv) == 3
    assert "judged with --summarizer-prompt interp-v2" in capsys.readouterr().err
    # replay: rebuild / report read the recorded version whatever the caller passes
    cells = (d / "cells.jsonl").read_bytes()
    summary = wrun.rebuild_arm("jl", t, out, M8)
    assert (d / "cells.jsonl").read_bytes() == cells
    assert summary["config"]["prompt_versions"]["summary"] == "interp-v2"
    assert wrun.with_recorded_prompt(d, M8).summ_prompt == "interp-v2"
    assert cli.main([*argv, "--rebuild"]) == 0
    assert (d / "cells.jsonl").read_bytes() == cells


def test_interp_v1_root_refuses_interp_v2(tmp_path, capsys):
    t = _tokens(tmp_path)
    out = tmp_path / "out"
    _run(out, t, M8, V2Fake())
    with pytest.raises(JudgeConfigError, match="judged with --summarizer-prompt interp-v1"):
        _run(out, t, M9, V2Fake())
    argv = ["run", "--out", str(out), "--arm", f"jl={t}", "--map-prompt", V28]
    argv += ["--excuse-verifier", "off", "--summarizer-prompt", "interp-v2"]
    assert cli.main(argv) == 3
    assert "judged with --summarizer-prompt interp-v1" in capsys.readouterr().err
    assert wrun.with_recorded_prompt(wrun.arm_dir(out, M8, "jl"), M9).summ_prompt == "interp-v1"


def test_v8_style_root_is_unchanged_with_the_default_prompt(tmp_path):
    """The default (interp-v1) path: same call, same cache key and fingerprint, no new key in
    arm.json / summary.json / the records."""
    t = _tokens(tmp_path, junk=())
    fake = V2Fake()
    s = _run(tmp_path / "out", t, M8, fake)["jl"]
    assert fake.n("summary") == 2 and fake.n("summary2") == 0
    assert {c[2] for c in fake.calls if c[0] == "summary"} == {summarizer.INTERP_SYSTEM}
    d = wrun.arm_dir(tmp_path / "out", M8, "jl")
    arm = json.loads((d / "arm.json").read_text())
    assert list(arm) == [
        "arm",
        "eval",
        "readouts",
        "kind",
        "judge",
        "summarizer",
        "classifier",
        "map_model",
        "map_anchor",
        "effort",
        "map_prompt",
    ]
    assert s["config"]["prompt_versions"]["summary"] == "interp-v1"
    assert "summ_prompt" not in s["config"]["models"]
    user = summarizer.render_user(
        summarizer.render_bag([" Paris", " teacher", " note"], [3.0, 2.0, 1.0])
    )
    fp = fingerprint("interp-v1", "claude-opus-5-5", "low", summarizer.INTERP_SYSTEM, user)
    rows = [r for r in _cache(d) if r["key"].startswith("summ:")]
    assert len(rows) == 2 and {r["fingerprint"] for r in rows} == {fp}
    assert all(r["result"] == {"interpretation": TEXT} for r in rows)
    cells = _cells(d)
    assert {c["stages"]["summary"] for c in cells} == {"ok"} and "junk" not in json.dumps(cells)
    # a v8 root ignores a caller's interp-v2 on rebuild (the record is replayed)
    before = (d / "cells.jsonl").read_bytes(), (d / "summary.json").read_bytes()
    wrun.rebuild_arm("jl", t, tmp_path / "out", M9)
    assert ((d / "cells.jsonl").read_bytes(), (d / "summary.json").read_bytes()) == before


# ---------------------------------------------------------------- joint cells


def test_joint_junk_layer_contributes_nothing_and_all_junk_is_empty(tmp_path):
    # site 0: junk at L24 only; site 1: junk at both layers
    t = _tokens(tmp_path, layers=(20, 24), junk=((24, 0), (20, 1), (24, 1)))
    out = tmp_path / "out"
    fake = V2Fake()
    wrun.run_joint("jl", [("jl", t)], out, M9, layers=(20, 24), llm=fake)
    d = wrun.arm_dir(out, M9, "jl")
    assert json.loads((d / wrun.JOINT_JSON).read_text())["summ_prompt"] == "interp-v2"
    cells = _cells(d, wrun.JOINT_CELLS)
    # the two read sites; every other bank site has no readout
    item = next(it for it in bank.load_items() if it["id"] == TOY_ITEMS[0])
    keys = [wrun.joint_key(item["id"], int(s["pos"])) for s in item["sites"][:2]]
    mixed, all_junk = (next(c for c in cells if c["key"] == k) for k in keys)
    rest = [c for c in cells if c["key"] not in keys]
    assert {c["status"] for c in rest} == {"empty"}
    # a junk layer is "" like an empty layer; it is settled, so the joint map runs
    assert mixed["samples"] == [TEXT, ""] and mixed["n_samples"] == 1
    assert mixed["status"] == "judged" and mixed["stages"] == {"summary": "ok", "map": "ok"}
    # every layer junk: the joint cell is empty (never pending, never failed)
    assert all_junk["status"] == "empty" and all_junk["samples"] is None
    assert all_junk["stages"] == {"summary": "ok", "map": None}
    assert fake.n("map") == 1 and fake.n("summary2") == 4
    n = len(fake.calls)
    wrun.run_joint("jl", [("jl", t)], out, M9, layers=(20, 24), llm=fake)
    assert len(fake.calls) == n  # nothing re-asked
    # the joint rebuild replays joint.json's summariser whatever the caller passes
    before = (d / wrun.JOINT_CELLS).read_bytes()
    import asyncio

    asyncio.run(wrun.rebuild_joint_async("jl", out, M8))
    assert (d / wrun.JOINT_CELLS).read_bytes() == before
    e = {"family": ix.MAIN, "method": "jlens", "unit": ix.JOINT_LABEL}
    assert final.recount(e, d / wrun.JOINT_CELLS)[:2] == (1, 0)
    same = ("key", "id", "pos", "site_kind")
    assert all_junk == {**rest[0], **{k: all_junk[k] for k in same}}
    # a joint run into this root under interp-v1 is refused
    with pytest.raises(JudgeConfigError, match="--summarizer-prompt"):
        wrun.run_joint("jl", [("jl", t)], out, M8, layers=(20, 24), llm=V2Fake())


# ---------------------------------------------------------------- consistency-v2 (A10)


def test_consistency_v2_is_interp_v1_only(tmp_path):
    t = _tokens(tmp_path)
    out = tmp_path / "out"
    _run(out, t, M9, V2Fake())
    root = tmp_path / "readouts"
    (root / "jl").mkdir(parents=True)
    (root / "jl" / "hallucination.jsonl").write_bytes(t.read_bytes())
    with pytest.raises(JudgeConfigError, match="consistency-v2 is interp-v1 only"):
        cons.run("jl:44", readouts_root=root, out_root=out, models=M8, n=1)
    with pytest.raises(JudgeConfigError, match="consistency-v2 is interp-v1 only"):
        cons.run("jl:44", readouts_root=root, out_root=tmp_path / "none", models=M9, n=1)
    argv = ["consistency-v2", "--out", str(out), "--readouts-root", str(root)]
    assert cli.main([*argv, "--arms", "jl:44", "--n", "1"]) == 3
