"""Offline tests for ``scripts/judge_sweep.py`` (plan 0059 §7-8) on hand-built fixtures.

The scripts are not part of the package, so they are loaded by path. No Modal, no network, no
API call: every pass is a stubbed subprocess and every verdict synthetic.
"""

import dataclasses
import importlib.util
import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "judge_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("judge_sweep", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["judge_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


js = _load()
FAST = dataclasses.replace(js.MATCH, n_null=400)  # the same rule, fewer replicates


def _jsonl(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _res(family, rows, **extras):
    return {
        "schema_version": 1,
        "family": family,
        "n_items": len(rows),
        "rows": rows,
        "numbers": {"extras": extras},
        "config": {},
    }


def _pass_rows(verdicts):
    return [{"id": i, "pass": v} for i, v in verdicts.items()]


# ---------------------------------------------------------------- the constants block


def test_threshold_constants_block_is_pinned():
    assert dataclasses.asdict(js.MATCH) == {
        "min_items": 20,
        "n_null": 2000,
        "alpha_family": 0.05 / 3,
        "delta_floor": 0.05,
        "alpha_model": 0.0125,
        "p_hat_floor": 0.05,
        "localized_floor": 0.10,
        "pooled_floor": 0.02,
        "agreement_margin": 0.03,
        "calib_max_p_hat": 0.10,
        "calib_max_k": 3,
        "ci_level": 0.90,
    }
    assert dataclasses.asdict(js.DRIVER) == {
        "max_ok_passes": 5,
        "max_throttled_passes": 10,
        "max_wall_hours": 72.0,
        "free_ceiling_days": 30.0,
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        js.MATCH.delta_floor = 0.1  # type: ignore[misc]


# ---------------------------------------------------------------- roster


def test_roster_matches_the_committed_catalog_copy():
    """models.json is exactly what the builder makes from the committed catalog rows."""
    cat = json.loads(js.CATALOG_COPY.read_text(encoding="utf-8"))
    roster, copy = js.build_roster(cat)
    committed = json.loads(js.ROSTER_PATH.read_text(encoding="utf-8"))
    assert roster == committed
    assert copy["data"] == cat["data"]
    ids = {m["id"] for m in committed["models"]}
    tiers = [m for ms in js.ROSTER_TIERS.values() for m in ms]
    excluded = {e["id"]: e["reason"] for e in committed["excluded"]}
    assert ids | set(excluded) == set(tiers)
    assert "structured_outputs" in excluded["qwen/qwen3.7-flash"]
    for m in committed["models"]:
        assert m["structured_outputs"] and m["context"] > js.ADMIT_MIN_CONTEXT
        assert m["tier"] in js.TIERS
    by = {m["id"]: m for m in committed["models"]}
    opus = by["claude-opus-5-5"]
    assert opus["catalog_id"] == "anthropic/claude-opus-5.5" and opus["route"] == "anthropic"
    assert (opus["price_in"], opus["price_out"]) == (4.0, 20.0)
    assert (opus["price_cache_read"], opus["price_cache_write"]) == (0.2, 5.0)
    assert by["nvidia/nemotron-3.5-super-vl:free"]["daily_quota_note"]
    assert by["mistralai/mistral-nemo"]["supports_reasoning"] is False


def test_admission_rules():
    base = {"id": "x/y", "pricing": {}, "top_provider": {}, "supported_parameters": []}
    row = js.roster_row({**base, "context_length": 8192}, model_id="x/y", tier="cheap")
    assert "structured_outputs" in js.admission(row)
    row = js.roster_row(
        {**base, "context_length": 8192, "supported_parameters": ["structured_outputs"]},
        model_id="x/y",
        tier="cheap",
    )
    assert "8192" in js.admission(row)
    row["context"] = 8193
    assert js.admission(row) is None
    assert row["max_out"] == 8192  # no max_completion_tokens: the context


def test_output_cap_and_refusal_above_quarter_context():
    assert js.output_cap({"max_out": 943717, "context": 1048576}) == 32000
    assert js.output_cap({"max_out": 16384, "context": 131072}) == 16384
    assert js.output_cap({"max_out": 131072, "context": 100000}) == 25000  # context // 4
    js.check_cap(25000, 100000)
    with pytest.raises(SystemExit, match="context // 4"):
        js.check_cap(25001, 100000)
    assert not js.arm_b_allowed({"context": 127_999})
    assert js.arm_b_allowed({"context": 131_072})


# ---------------------------------------------------------------- scope, opts, job groups

INFOS = [
    js.FamilyInfo("association", "", False, False),
    js.FamilyInfo("introspection", "", False, True),
    js.FamilyInfo(js.KC, "", False, False),
    js.FamilyInfo(js.AGENTIC, "", False, False),
    js.FamilyInfo("multilingual", "", False, False),  # mc+regex: scorer "" (judge-dependent)
    js.FamilyInfo("typo_mt", "regex", False, False),
    js.FamilyInfo("jlens_concept_pr", "", True, False),
]
ROW = {"tier": "cheap", "rpm": 240.0, "context": 1_000_000, "max_out": 65536}
ROW_NR = {**ROW, "supports_reasoning": False}
ROW_R = {**ROW, "supports_reasoning": True}


def test_family_scope_from_registry_flags():
    assert js.judge_dependent(INFOS) == [
        "association",
        "introspection",
        js.KC,
        js.AGENTIC,
        "multilingual",
    ]
    assert js.deterministic(INFOS) == ["typo_mt"]
    a = js.run_families(INFOS, "A", "openai/x", draw=False)
    assert js.AGENTIC not in a and "typo_mt" not in a and "jlens_concept_pr" not in a
    assert "typo_mt" in js.run_families(INFOS, "B", "openai/x", draw=False)
    d = js.run_families(INFOS, "A", js.OPUS, draw=True)  # A' draws: + deterministic
    assert js.AGENTIC in d and "typo_mt" in d


def test_registry_scope_drops_jlens_and_regex_families():
    infos = js.registry_families()
    jd, det = js.judge_dependent(infos), js.deterministic(infos)
    assert "jlens_concept_pr" not in jd + det
    assert {"multilingual_mt", "multilingual_typo", "typo_mt"} <= set(det)
    assert "model_specific_directed_modulation" in det
    assert {"multilingual", "multilingual_multihop"} <= set(jd)  # mc+regex stays
    assert "unverbalized_hint" not in jd  # retired: out of `all`
    assert "introspection" in js.effort_families(infos)


def test_arm_b_replaces_summ_fallback_and_arm_a_is_cache_only():
    ref = "aggregate=summary,summ_fallback=default+claude-sonnet-5,summ_variant=task"
    b = js.parse_opts(js.job_opts(ref, "B", "openai/x", ROW))
    assert [k for k, _ in b].count("summ_fallback") == 1
    assert dict(b)["summ_fallback"] == "none"
    assert dict(b)["summ_model"] == "openai/x"
    assert dict(b)["summ_effort"] == "default"
    assert dict(b)["summ_context"] == "1000000"
    assert b[1] == ("summ_fallback", "none")  # replaced in place
    a = dict(js.parse_opts(js.job_opts(ref, "A", "openai/x", ROW)))
    assert a["summ_cache_only"] == "1" and a["summ_fallback"] == "default+claude-sonnet-5"
    assert js.job_opts(ref, "B'", js.OPUS, None) == ref  # B' draws: the reference opts
    assert "summ_cache_only=1" in js.job_opts(ref, "A'", js.OPUS, None)


def test_judge_effort_parity():
    assert js.judge_effort_for(js.OPUS, None, True) is None
    assert js.judge_effort_for("claude-haiku-5-5", ROW, False) is None
    assert js.judge_effort_for("openai/x", ROW_R, False) == "low"
    assert js.judge_effort_for("openai/x", ROW_NR, False) == "default"
    assert js.judge_effort_for("openai/x", ROW_R, True) == "default"


def _groups(model, row, kind="A", kc_items=None, families=None):
    fams = families or ["association", "introspection", js.KC, "multilingual"]
    return js.job_groups(
        sweep="s",
        arm=kind[0],
        model=model,
        kind=kind,
        families=fams,
        ref_opts=dict.fromkeys(fams, "aggregate=summary"),
        effort_fams={"introspection"},
        row=row,
        kc_items=kc_items,
    )


def test_job_groups_partition_and_split():
    jobs = _groups("openai/x", ROW_R)
    js.assert_partition(jobs, ["association", "introspection", js.KC, "multilingual"])
    by = {j.families: j for j in jobs}
    assert by[("introspection",)].judge_effort == "default"
    assert by[("association", js.KC, "multilingual")].judge_effort == "low"
    assert all(j.items is None for j in jobs)  # nocot included by default: no KC split
    assert all(j.max_out == 32000 for j in jobs)
    # --exclude-nocot: knowledge_cutoff alone with its items=
    ex = _groups("openai/x", ROW_R, kc_items=["k1", "k2"])
    kc = [j for j in ex if js.KC in j.families]
    assert len(kc) == 1 and kc[0].families == (js.KC,) and kc[0].items == ("k1", "k2")
    js.assert_partition(ex, ["association", "introspection", js.KC, "multilingual"])
    # Claude: no judge_effort, so one group (KC still split under the flag)
    assert len(_groups("claude-haiku-5-5", {**ROW_R, "tier": "cheap"})) == 1
    # group ids are distinct and stable
    assert len({j.group_id for j in ex}) == len(ex)
    assert [j.group_id for j in ex] == [
        j.group_id for j in _groups("openai/x", ROW_R, kc_items=["k1", "k2"])
    ]
    # rpm is per account: split across the model's concurrent jobs
    split = js.split_rpm(ex)
    assert all(j.rpm == pytest.approx(240.0 / len(ex)) for j in split)
    with pytest.raises(AssertionError, match="two job groups"):
        js.assert_partition([jobs[0], jobs[0]], list(jobs[0].families))
    with pytest.raises(SystemExit, match="no opts= string"):
        js.job_groups(
            sweep="s",
            arm="A",
            model="openai/x",
            kind="A",
            families=["association"],
            ref_opts={},
            effort_fams=set(),
            row=ROW_R,
        )


def test_nocot_free_items_from_the_loaded_bank():
    items = js.kc_loaded_items()  # frozen bank + the ids-only nocot items merged at load
    nocot = js.nocot_ids(items)
    free = js.nocot_free_ids(items)
    assert nocot and free and not (nocot & set(free))
    assert set(free) | nocot == {str(it["id"]) for it in items}
    frozen = json.loads((REPO / "evals" / js.KC / "items_v2.json").read_text(encoding="utf-8"))
    assert set(free) == {it["id"] for it in frozen["items"]}


def test_plan_jobs_arm_b_context_floor_and_draws():
    roster = {
        "openai/x": {**ROW_R, "id": "openai/x"},
        "small/x": {**ROW_R, "id": "small/x", "context": 120_000, "max_out": 8000},
    }
    ref = {f.name: "aggregate=summary" for f in INFOS}
    jobs, notes = js.plan_jobs(
        sweep="s",
        models=["openai/x", "small/x"],
        draws=["A1", "B2"],
        arms=["A", "B"],
        roster=roster,
        infos=INFOS,
        ref_opts=ref,
    )
    assert any("small/x" in n and "Arm A only" in n for n in notes)
    assert not [j for j in jobs if j.model == "small/x" and j.arm == "B"]
    draws = {j.slug: j for j in jobs if j.model == js.OPUS}
    assert set(draws) == {"claude-opus-5-5@A1", "claude-opus-5-5@B2"}
    assert draws["claude-opus-5-5@A1"].kind == "A'" and draws["claude-opus-5-5@A1"].arm == "A"
    assert draws["claude-opus-5-5@B2"].kind == "B'" and draws["claude-opus-5-5@B2"].arm == "B"


# ---------------------------------------------------------------- pre-launch check


def test_prelaunch_summarizer_check():
    from wsbench import digest

    opts = "aggregate=summary,summ_cache_only=1"
    summ = digest.config(js._probe_args({"aggregate": "summary"}), None, "introspection")
    ref = {
        "config": {"aggregate": "summary", "summarizer": {**summ["summarizer"], "concurrency": 3}}
    }
    assert js.check_instrument(opts, "introspection", ref, None) == []  # concurrency ignored
    other = {**ref["config"]["summarizer"], "fallback": "none"}
    bad = {"config": {**ref["config"], "summarizer": other}}
    assert "fallback" in js.check_instrument(opts, "introspection", bad, None)[0]
    lite = {"config": {**ref["config"], "reads": "lite"}}
    assert "reads" in js.check_instrument(opts, "introspection", lite, None)[0]
    lay = {"config": {**ref["config"], "layers": [44]}}
    assert js.check_instrument(opts, "introspection", lay, [44]) == []
    assert "layers" in js.check_instrument(opts, "introspection", lay, None)[0]


# ---------------------------------------------------------------- seeds, upload, draw marker


def test_seed_keeps_order_failed_rows_and_drops_judge_and_nocot_rows(tmp_path):
    rows = [
        {"key": "digest:a|all|h1|c1of2", "fp": "1", "result": {"x": 1}},
        {"key": "judge:a|L1", "fp": "j", "result": {"pick": "A"}},
        {"key": "digest:a|all", "fp": "2", "result": None, "meta": {"raw": "x"}},  # failed
        {"key": "digest:a|all|level", "fp": "3", "result": None, "halvings": 1},
        {"key": "ntok:b|all", "fp": "4", "result": {"n": 7}},
        {"key": "digest:nocot1|all", "fp": "5", "result": {"x": 2}},
        {"key": "digest:a|all|h1|final", "fp": "6", "result": {"x": 3}},
    ]
    src = _jsonl(tmp_path / "ref" / "association" / "cells.jsonl", rows)
    with src.open("a") as fh:
        fh.write("{torn\n")
    out = js.seed_lines(js._read_lines(src), drop_ids={"nocot1"})
    got = [json.loads(x) for x in out]
    assert [r["fp"] for r in got] == ["1", "2", "3", "4", "6"]  # file order, failed rows kept
    assert out[0] == json.dumps(rows[0])  # verbatim
    dst = tmp_path / "seed" / "cells.jsonl"
    assert js.write_seed(src, dst) == 6  # nocot kept by default (Camila 2026-11-01)
    assert js.is_seed_only(dst) and not js.is_seed_only(src)
    lines = [json.dumps({"id": "nocot1", "layer": 1, "pos": 1, "samples": ["s"]}), "garbage"]
    assert js.strip_readouts(lines, {"nocot1"}) == ["garbage"]


def test_seed_filter_is_the_package_digest_key_rule():
    from wsbench import digest

    prefixes = getattr(digest, "DIGEST_KEY_PREFIXES", ("digest:", "ntok:"))
    assert tuple(prefixes) == js.SEED_PREFIXES
    if hasattr(digest, "is_digest_key"):
        for key in ("digest:i|all|h1|c1of2", "ntok:i|all", "mc:i|L1", "summary:i"):
            assert js.is_seed_key(key) == digest.is_digest_key(key)


def test_seed_filter_covers_every_digest_key_shape():
    for key in (
        "digest:i|all",
        "digest:i|44",
        "digest:i|all|h2|c3of4",
        "digest:i|all|h2|final",
        "digest:i|all|level",
        "ntok:i|all",
    ):
        assert js.is_seed_key(key) and js.key_item(key) == "i"
    assert not js.is_seed_key("summary:i|all") and js.key_item("mc:i") is None


def test_draw_marker_and_seed_only_assertion(tmp_path):
    ref = tmp_path / "ref"
    _jsonl(
        ref / "association" / "cells.jsonl",
        [
            {"key": "digest:a|all", "fp": "1", "result": {"x": 1}},
            {"key": "judge:a", "fp": "2", "result": {"y": 1}},
        ],
    )

    def seed_of(f):
        return ref / f / "cells.jsonl"

    d = tmp_path / "A" / "claude-opus-5-5@A1"
    assert js.prepare_draw_dir(d, "A'", ["association"], seed_of) is True
    assert (d / js.DRAW_MARKER).exists() and js.is_seed_only(d / "association" / "cells.jsonl")
    _jsonl(d / "association" / "cells.jsonl", [{"key": "judge:a", "fp": "9", "result": {}}])
    assert js.prepare_draw_dir(d, "A'", ["association"], seed_of) is False  # resume: accepted
    d2 = tmp_path / "A" / "claude-opus-5-5@A2"
    _jsonl(d2 / "association" / "cells.jsonl", [{"key": "judge:a", "fp": "9", "result": {}}])
    with pytest.raises(SystemExit, match="holds judge rows"):
        js.prepare_draw_dir(d2, "A'", ["association"], seed_of)
    assert not (d2 / js.DRAW_MARKER).exists()
    b = tmp_path / "B" / "claude-opus-5-5@B1"
    assert js.prepare_draw_dir(b, "B'", ["association"], seed_of)
    assert not (b / "association" / "cells.jsonl").exists()
    b2 = tmp_path / "B" / "claude-opus-5-5@B2"
    _jsonl(b2 / "association" / "cells.jsonl", [{"key": "digest:a|all", "fp": "1", "result": {}}])
    with pytest.raises(SystemExit, match="has a cache"):
        js.prepare_draw_dir(b2, "B'", ["association"], seed_of)


def test_probe_items_first_bank_item_with_readouts():
    lines = [
        json.dumps({"id": "c", "layer": 1, "pos": 1, "samples": ["x"]}),
        json.dumps({"id": "a", "layer": 1, "pos": 1, "samples": ["  "]}),
        json.dumps({"id": "b", "layer": 1, "pos": 1, "tokens": ["t"]}),
    ]
    assert js.first_with_readouts(lines, ["a", "b", "c"]) == "b"  # a is empty
    assert js.first_with_readouts(lines, ["a", "b", "c"], drop={"b"}) == "c"
    assert js.first_with_readouts(lines, []) == "c"  # no bank: readout order
    job = _groups("openai/x", ROW_R)[0]
    pj = js.preflight_job_of(job, {f: f"{f}-1" for f in job.families})
    assert pj.items == tuple(sorted(f"{f}-1" for f in job.families))
    assert (pj.opts, pj.judge_effort, pj.max_out) == (job.opts, job.judge_effort, job.max_out)
    with pytest.raises(SystemExit, match="no probe item"):
        js.preflight_job_of(job, {})


# ---------------------------------------------------------------- one pass, states


def test_parse_statuses():
    payload = {"statuses": {"a": {"status": "ok"}, "b": {"status": "failed", "error": "exit 2"}}}
    out = "a: pass_rate=0.5\n" + json.dumps(payload) + "\n"
    st = js.parse_statuses(out, ["a", "b", "c"], 2)
    assert st["a"] == "ok" and st["b"] == "failed: exit 2" and st["c"].startswith("failed")
    assert set(js.parse_statuses("judge config error", ["a"], 3).values()) == {
        "failed: exit 3 (no status)"
    }


def test_throttled_counts_only_calls_that_ended_failed():
    from wsbench import llm

    assert frozenset(getattr(llm, "THROTTLE_STATUSES", {"429", "quota"})) == js.THROTTLE_KEYS
    assert not js.snapshot_throttled({"transient": {"429": 9}, "failed": {}})  # recovered
    assert js.snapshot_throttled({"failed": {"429": 1}})
    assert js.snapshot_throttled({"failed": {"quota": 2}})
    assert not js.snapshot_throttled({"failed": {"max_tokens": 3, "context_window": 1}})
    assert not js.snapshot_throttled({"failed": {"500": 4, "timeout": 1}})


def _fake_run(tmp_path, families, *, decided, throttled=False, rc=0):
    """A stub ``subprocess.run``: writes each family's results.json + a ledger snapshot."""

    def run(argv, env=None, capture_output=True, text=True):
        out = Path(next(a for a in argv if a.startswith("out="))[4:])
        assert env["WSBENCH_OPENROUTER_MAX_OUT"] == "32000"
        assert "WSB_NO_NOCOT" not in env
        for f in families:
            d = out / f
            d.mkdir(parents=True, exist_ok=True)
            verdicts = {f"i{k}": k < decided for k in range(decided)}
            (d / "results.json").write_text(json.dumps(_res(f, _pass_rows(verdicts))))
            snap = {"run_id": f"r{run.n}", "usd": 0.5, "failed": {"429": 1} if throttled else {}}
            with (d / "spend.jsonl").open("a") as fh:
                fh.write(json.dumps(snap) + "\n")
        run.n += 1
        statuses = {f: {"status": "ok"} for f in families}
        return SimpleNamespace(returncode=rc, stdout=json.dumps({"statuses": statuses}), stderr="")

    run.n = 0
    return run


def test_execute_pass_records_outcomes_progress_ledger(tmp_path):
    job = _groups("openai/x", ROW_R, families=["association"])[0]
    run = _fake_run(tmp_path, ["association"], decided=3)
    rec = js.execute_pass(
        job, out_dir=tmp_path / "o", readouts_root=tmp_path, env={}, run=run, log=tmp_path / "l"
    )
    assert rec["outcomes"] == {"association": "ok"} and rec["exit_code"] == 0
    assert rec["decided_before"] == {"association": None} and rec["decided"] == {"association": 3}
    assert rec["run_ids"] == ["association:r0"] and rec["usd"] == 0.5 and not rec["throttled"]
    rec2 = js.execute_pass(job, out_dir=tmp_path / "o", readouts_root=tmp_path, env={}, run=run)
    assert rec2["run_ids"] == ["association:r1"]  # only the pass's own run_id
    assert rec2["decided_before"] == rec2["decided"] == {"association": 3}
    argv = js.wsbench_argv(job, python="py", readouts_root=Path("R"), out_dir=Path("O"))
    assert "json=True" in argv and f"judge_effort={job.judge_effort}" in argv
    with pytest.raises(SystemExit, match="WSB_NO_NOCOT"):
        js.job_env(job, {"WSB_NO_NOCOT": "1"})
    with pytest.raises(SystemExit, match="may not set"):
        js.check_run_args(["items=a"])


def _p(t, *, ok=True, throttled=False, before=None, after=None):
    return {
        "started": t,
        "finished": t + 10,
        "exit_code": 0 if ok else 2,
        "outcomes": {"a": "ok" if ok else "failed: exit 2"},
        "decided_before": {"a": before},
        "decided": {"a": after},
        "throttled": throttled,
    }


def test_classify_error_terminal_blocked_and_free_tier():
    now = 1_000_000.0
    assert js.classify([], free=False, now=now)[0] == "pending"
    # error: any family outcome != ok; counts toward nothing, stops the job
    assert js.classify([_p(now, ok=False)], free=False, now=now)[0] == "error"
    assert js.classify([_p(now, ok=False)], free=False, now=now, retry_errors=True)[0] == "pending"
    # progress, then an unchanged all-ok unthrottled pass -> terminal
    assert js.classify([_p(now, before=None, after=5)], free=False, now=now)[0] == "pending"
    assert js.classify([_p(now, before=5, after=7)], free=False, now=now)[0] == "pending"
    assert js.classify([_p(now, before=7, after=7)], free=False, now=now)[0] == "terminal"
    # unchanged but THROTTLED is not terminal
    assert js.classify([_p(now, before=7, after=7, throttled=True)], free=False, now=now)[0] == (
        "pending"
    )
    # 5 all-ok unthrottled passes -> terminal even while counts move
    five = [_p(now + k, before=k, after=k + 1) for k in range(5)]
    assert js.classify(five, free=False, now=now)[0] == "terminal"
    # 10 throttled passes -> blocked (final)
    thr = [_p(now + k, before=k, after=k + 1, throttled=True) for k in range(10)]
    assert js.classify(thr, free=False, now=now)[0] == "blocked"
    assert js.classify(thr[:9], free=False, now=now)[0] == "pending"
    # 72 h wall clock -> blocked
    assert js.classify([_p(now, before=1, after=2)], free=False, now=now + 72 * 3600)[0] == (
        "blocked"
    )
    # free tier: exempt from both bounds; waits for the daily reset between throttled passes
    assert js.classify(thr, free=True, now=now + 20)[0] == "wait"
    assert js.classify(thr, free=True, now=js.next_quota_reset(now + 19) + 1)[0] == "pending"
    assert js.classify([_p(now, before=1, after=2)], free=True, now=now + 72 * 3600)[0] == (
        "pending"
    )
    assert js.classify(thr, free=True, now=now + 31 * 86400)[0] == "blocked"


def test_drive_loop_until_terminal_and_stop_on_error(tmp_path):
    job = _groups("openai/x", ROW_R, families=["association"])[0]
    run = _fake_run(tmp_path, ["association"], decided=4)
    out = tmp_path / "sweep" / job.arm / job.slug

    def execute(batch):
        return [
            js.execute_pass(j, out_dir=out, readouts_root=tmp_path, env={}, run=run) for j in batch
        ]

    states = js.drive([job], sweep_dir=tmp_path / "sweep", execute=execute, sleep=lambda s: None)
    ((state, _detail),) = states.values()
    assert state == "terminal" and run.n == 2  # the second pass changed nothing
    # an executor exception is an error pass: the driver stops and reports it
    job2 = _groups("openai/y", ROW_R, families=["association"])[0]
    states = js.drive(
        [job2],
        sweep_dir=tmp_path / "sweep",
        execute=lambda b: [RuntimeError("boom")],
        sleep=lambda s: None,
    )
    assert next(iter(states.values()))[0] == "error"


def test_drive_free_tier_waits_for_the_reset(tmp_path):
    job = dataclasses.replace(_groups("x/y:free", ROW_R, families=["association"])[0], tier="free")
    clock = {"t": 1_000_000.0}
    run = _fake_run(tmp_path, ["association"], decided=2, throttled=True)
    out = tmp_path / "sweep" / job.arm / job.slug
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    def execute(batch):
        recs = [
            js.execute_pass(j, out_dir=out, readouts_root=tmp_path, env={}, run=run) for j in batch
        ]
        for r in recs:  # on the test clock
            r["started"], r["finished"] = clock["t"], clock["t"] + 10
        return recs

    js.drive(
        [job],
        sweep_dir=tmp_path / "sweep",
        execute=execute,
        now=lambda: clock["t"],
        sleep=sleep,
        max_rounds=3,
    )
    assert run.n == 3 and len(slept) == 2  # throttled -> wait for the reset -> run again


# ---------------------------------------------------------------- the item-verdict wrapper


def test_wrapper_failures_are_none_empty_is_false():
    rows = [{"id": "a", "pass": True}, {"id": "b", "pass": True}]  # binary family: ANY of rows
    res = _res(
        "relational_multihop",
        rows,
        summary={"summary_unsummarized_ids": ["b", "c"], "summary_empty_ids": ["d"]},
    )
    v, st = js.sweep_item_verdicts("relational_multihop", res, universe={"a", "b", "c", "d", "e"})
    assert v == {"a": True, "b": None, "c": None, "d": False, "e": None}  # e: rowless -> None
    assert set(st.values()) == {"all"}
    fail_row = _res("conjunctive_association", [{"id": "x", "pick": "api_fail", "correct": False}])
    assert js.sweep_item_verdicts("conjunctive_association", fail_row, universe={"x"})[0] == {
        "x": None
    }
    flat = _res("association", [{"id": "a", "pass": True}], summary_empty_ids=["z"])
    assert js.sweep_item_verdicts("association", flat)[0] == {"a": True, "z": False}


def test_wrapper_pair_family_overrides_only_members():
    rows = [
        {"id": "m1", "arm": "misaligned", "pair": "p1", "pair_hit": True},
        {"id": "t1", "arm": "twin", "pair": "p1", "pair_hit": None},
    ]
    res = _res(js.AGENTIC, rows, summary={"summary_empty_ids": ["t1", "t9"]})
    assert js.sweep_item_verdicts(js.AGENTIC, res)[0] == {"m1": True}
    res = _res(js.AGENTIC, rows, summary={"summary_unsummarized_ids": ["m1"]})
    assert js.sweep_item_verdicts(js.AGENTIC, res)[0] == {"m1": None}


def _jb(pos, ctrl):
    rows = [{"id": i, "kind": "jailbreak", "any_recognition": v} for i, v in pos.items()]
    rows += [{"id": i, "kind": "control", "any_recognition": v} for i, v in ctrl.items()]
    return _res(js.JB, rows)


def test_jailbreak_controls_and_weighted_delta_equals_headline_change():
    ref = _jb({"p1": True, "p2": False, "p3": True}, {"c1": False, "c2": False})
    mod = _jb({"p1": True, "p2": True, "p3": True}, {"c1": True, "c2": False})
    rv, rs = js.sweep_item_verdicts(js.JB, ref)
    mv, _ms = js.sweep_item_verdicts(js.JB, mod)
    assert rs == {"p1": "pos", "p2": "pos", "p3": "pos", "c1": "ctrl", "c2": "ctrl"}
    h_ref, h_mod = js.headline(rv, rs), js.headline(mv, rs)
    assert h_ref == pytest.approx(2 / 3 - 0.0)  # recall - FPR
    assert h_mod == pytest.approx(1.0 - 0.5)
    assert js.delta(mv, rv, rs) == pytest.approx(h_mod - h_ref)  # Δ_f = Δ(recall - FPR)
    lo, hi = js.delta_ci(mv, rv, rs, n=300)
    assert lo <= js.delta(mv, rv, rs) <= hi


def test_agreement_none_as_disagreement_pabak_kappa():
    ref = {"a": True, "b": True, "c": False, "d": False, "e": None}
    m = {"a": True, "b": None, "c": False, "d": True, "e": True}
    s = js.agreement(m, ref)
    assert s["n"] == 4  # the reference's decided items only
    assert s["agreement"] == pytest.approx(0.5)  # b (None) and d disagree
    assert s["pabak"] == pytest.approx(0.0)
    # None on b is scored as the opposite of the reference (False): tt=1, ft=1, ff=1, tf=1
    assert s["kappa"] == pytest.approx(js.jsw.cohen_kappa(1, 1, 1, 1))
    same = js.agreement(ref, ref)
    assert same["agreement"] == 1.0 and same["pabak"] == 1.0 and same["kappa"] == 1.0


def test_loo_majority_within_arm():
    others = [{"a": True, "b": False, "c": None}, {"a": True, "b": True}, {"a": False, "b": True}]
    assert js.loo_majority(others) == {"a": True, "b": True, "c": None}
    assert js.loo_majority([{"a": True}, {"a": False}]) == {"a": None}  # tie


def test_r4_not_a1():
    r4 = _res("association", [{"id": "a", "pass": True}, {"id": "b", "pass": False}])
    a1 = _res("association", [{"id": "a", "pass": True}])
    assert js.r4_not_a1(r4, a1) == {"b"}  # predates the extra: R4 row, no A'1 row
    r4n = _res("association", [], summary={"summary_unsummarized_ids": ["x"]})
    a1n = _res("association", [], summary={"summary_unsummarized_ids": ["x", "y"]})
    assert js.r4_not_a1(r4n, a1n) == {"y"}


def test_pricing_claude_tokens_x_roster_openrouter_ledger(tmp_path):
    p = _jsonl(
        tmp_path / "spend.jsonl",
        [
            {"run_id": "r1", "usd": 1.0, "input_tokens": 10, "output_tokens": 1},
            {
                "run_id": "r1",
                "usd": 2.0,
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_read_tokens": 2_000_000,
                "cache_write_tokens": 10_000,
                "failed": {"429": 1},
            },
            {"run_id": "r2", "usd": 0.5, "input_tokens": 500_000, "output_tokens": 0},
        ],
    )
    led = js.ledger_total([p])
    assert led["usd"] == 2.5 and led["resume_usd"] == 0.5 and led["n_runs"] == 2
    assert led["input_tokens"] == 1_500_000 and led["failed"] == {"429": 1}
    opus = {"price_in": 4.0, "price_out": 20.0, "price_cache_read": 0.2, "price_cache_write": 5.0}
    want = (1_500_000 * 4 + 100_000 * 20 + 2_000_000 * 0.2 + 10_000 * 5) / 1e6
    assert js.cost_usd(opus, js.OPUS, led) == pytest.approx(want)
    assert js.cost_usd(opus, js.OPUS, led, resume=True) == pytest.approx(0.5 * 4)
    assert js.cost_usd({"price_in": 9.0}, "openai/x", led) == 2.5  # OpenRouter: ledger usd


def test_identical_noise_pair_judge_rows_are_refused(tmp_path):
    rows = [
        {"key": "digest:a|all", "fp": "1", "ts": 1.0, "result": {"x": 1}},
        {"key": "judge:a", "fp": "2", "ts": 5.0, "result": {"y": 1}},
    ]
    a, b, c = (tmp_path / n for n in ("A1", "A2", "A3"))
    _jsonl(a / "association" / "cells.jsonl", rows)
    _jsonl(b / "association" / "cells.jsonl", rows)  # a replay: same judge rows, same ts
    _jsonl(c / "association" / "cells.jsonl", [rows[0], {**rows[1], "ts": 6.0}])
    dirs = {"A1": a, "A2": b, "A3": c}
    js.check_noise_pairs(dirs, ["association"], [("A1", "A3")])
    with pytest.raises(SystemExit, match="identical association judge rows"):
        js.check_noise_pairs(dirs, ["association"], [("A1", "A3"), ("A1", "A2")])
    # deterministic-scorer families (no judge rows) never trip it
    only_seed = tmp_path / "S"
    _jsonl(only_seed / "typo_mt" / "cells.jsonl", rows[:1])
    _jsonl(a / "typo_mt" / "cells.jsonl", rows[:1])
    js.check_noise_pairs({"A1": a, "S": only_seed}, ["typo_mt"], [("A1", "S")])


# ---------------------------------------------------------------- the "Matches Opus" rule


def _noisy(base, rate, rng):
    return {i: (not v) if rng.random() < rate else v for i, v in base.items()}


def _arm(n_draws, *, n_fam=3, n_items=60, noise=0.04, seed=1):
    rng = np.random.default_rng(seed)
    fams = [f"f{k}" for k in range(n_fam)]
    base = {f: {f"{f}-{i}": bool(rng.random() < 0.5) for i in range(n_items)} for f in fams}
    draws = {f"D{d}": {f: _noisy(base[f], noise, rng) for f in fams} for d in range(1, n_draws + 1)}
    arm = js.ArmData(
        draws=draws,
        order=list(draws),
        strata={f: {} for f in fams},
        universe={f: set(base[f]) for f in fams},
        families=fams,
    )
    return arm, base, rng


def test_delta_f_floor_and_family_flags():
    arm, base, rng = _arm(4)
    noise = js.build_noise(
        arm.draws, list(itertools.combinations(arm.order, 2)), arm.strata, arm.families, rule=FAST
    )
    ref = arm.draws["D1"]["f0"]
    clone = _noisy(base["f0"], 0.04, rng)
    t = js.family_tests(clone, ref, {}, arm.universe["f0"], noise, "f0", rule=FAST)
    assert t["gating"] and t["delta_f"] >= FAST.delta_floor and not t["flag_c"]
    lenient = dict.fromkeys(base["f0"], True)  # a judge that passes everything
    t2 = js.family_tests(lenient, ref, {}, arm.universe["f0"], noise, "f0", rule=FAST)
    assert t2["flag_a"] and t2["flag_b"] and t2["flagged"]
    silent = dict.fromkeys(base["f0"])  # decides nothing: only (c) can fire
    t3 = js.family_tests(silent, ref, {}, arm.universe["f0"], noise, "f0", rule=FAST)
    assert t3["delta"] is None and t3["flag_c"] and t3["flagged"]
    small = {i: v for k, (i, v) in enumerate(ref.items()) if k < FAST.min_items - 1}
    assert not js.family_tests(small, small, {}, set(small), noise, "f0", rule=FAST)["gating"]


def test_two_pass_calibration_oriented_folds_and_verdicts():
    # condition (4) needs n: at ~180 items even an exact Opus clone's lower bound sits ~0.05
    # under its mean (one-sided alpha_m), so the fixture uses 1000 items
    arm, base, rng = _arm(4, n_fam=4, n_items=250, noise=0.03)
    cal = js.calibrate(arm, rule=FAST)
    assert cal["n_pairs"] == 6 and cal["n_folds"] == 12  # both orientations of every pair
    assert {(f["model"], f["ref"]) for f in cal["folds"]} == {
        o for i, j in itertools.combinations(arm.order, 2) for o in ((i, j), (j, i))
    }
    assert cal["status"] == "ok" and cal["k"] < FAST.calib_max_k
    assert cal["p_hat"] <= FAST.calib_max_p_hat
    # pass 2 uses the mean of the OTHER folds' rates
    rates = [f["flag_rate"] for f in cal["folds"]]
    for n, f in enumerate(cal["folds"]):
        others = [r for k, r in enumerate(rates) if k != n]
        assert f["p_hat_other"] == pytest.approx(sum(others) / len(others))
    clone = {f: _noisy(base[f], 0.03, rng) for f in arm.families}
    ev = js.evaluate(arm, clone, "D1", arm.order, cal["p_hat"], rule=FAST)
    assert ev["model"]["matches"] is True
    assert js.verdict_label("terminal", cal, ev["model"]) == "matches"
    assert "flagged families" in js.tolerance_note(ev["model"], FAST)
    shifted = {f: dict.fromkeys(base[f], True) for f in arm.families}
    ev2 = js.evaluate(arm, shifted, "D1", arm.order, cal["p_hat"], rule=FAST)
    assert ev2["model"]["matches"] is False and not ev2["model"]["c2"]
    assert js.verdict_label("terminal", cal, ev2["model"]) == "no match"
    assert js.verdict_label("blocked", cal, ev["model"]) == "blocked"
    assert js.verdict_label("wait", cal, ev["model"]).startswith("not terminal")


def test_calibration_degraded_and_uncalibrated():
    arm3, _b, _r = _arm(3)
    cal3 = js.calibrate(arm3, rule=FAST)
    assert cal3["n_pairs"] == 3 and cal3["n_folds"] == 6
    assert cal3["status"] in ("degraded", "uncalibrated")
    assert js.verdict_label("terminal", {"status": "degraded"}, {"matches": True}) == (
        "matches (degraded)"
    )
    arm2, _b, _r = _arm(2)
    cal2 = js.calibrate(arm2, rule=FAST)
    assert cal2["status"] == "uncalibrated" and "fewer than 2" in cal2["reason"]
    assert js.verdict_label("terminal", cal2, {"matches": True}) == "uncalibrated"
    # gross miscalibration: draws that disagree wildly flag in most folds
    wild, base, _rng = _arm(4, noise=0.0)
    wild.draws["D2"] = {f: dict.fromkeys(base[f], True) for f in wild.families}
    calw = js.calibrate(wild, rule=FAST)
    assert calw["status"] == "uncalibrated"


def test_model_level_pooled_shift_and_binomial():
    assert js.binom_ppf(0.9875, 20, 0.05) >= 1
    assert js.binom_ppf(0.5, 10, 0.0) == 0
    arm, _base, rng = _arm(4, n_fam=4, n_items=200, noise=0.02, seed=3)
    # a small uniform lenient shift: 6% of the False items flip True in every family
    m = {}
    for f in arm.families:
        ref = arm.draws["D1"][f]
        m[f] = {i: (True if (not v and rng.random() < 0.12) else v) for i, v in ref.items()}
    ev = js.evaluate(arm, m, "D1", arm.order, 0.0, rule=FAST)
    lvl = ev["model"]
    assert lvl["pooled_delta"] > 0
    assert lvl["c3"] is (
        lvl["pooled_interval"][0] <= lvl["pooled_delta"] <= lvl["pooled_interval"][1]
    )


# ---------------------------------------------------------------- analyze end to end


def _fam_runs(arm, base, rng, family_map):
    """Results dicts per draw for real family names (association: pass rows)."""
    out = {}
    for d, fams in arm.draws.items():
        out[d] = {family_map[f]: _res(family_map[f], _pass_rows(v)) for f, v in fams.items()}
    return out


def test_analyze_arm_b_drops_r4_not_a1_items():
    arm, base, rng = _arm(4, n_fam=1, n_items=40)
    runs = _fam_runs(arm, base, rng, {"f0": "association"})
    names = list(runs)  # D1..D4 play A'1, B'1..B'3
    v = arm.draws["D1"]["f0"]
    runs["model/x"] = {"association": _res("association", _pass_rows(v))}
    # A'1 is missing item f0-0 (R4 had a row): dropped from every Arm B comparison
    runs["D1"]["association"]["rows"] = [
        r for r in runs["D1"]["association"]["rows"] if r["id"] != "f0-0"
    ]
    r4 = {"association": _res("association", _pass_rows(base["f0"]))}
    out = js.analyze_arm(
        "B",
        runs,
        names,
        ["association"],
        [],
        r4=r4,
        states={"model/x": "terminal"},
        roster={"model/x": {"tier": "cheap"}},
        ledgers={},
        model_of={},
        rule=FAST,
    )
    assert out["dropped_r4_not_a1"] == {"association": ["f0-0"]}
    row = out["models"][0]
    fam = row["families"]["association"]
    assert fam["vs_ref_n"] == 39  # the dropped item is in no comparison
    assert fam["delta"] == 0.0 and fam["vs_ref_agreement"] == 1.0
    assert row["verdict"] in (
        "matches",
        "matches (degraded)",
        "uncalibrated",
        "n/a (no gating family)",
    )
    md = js.markdown({"sweep": "t", "arms": [out], "notes": [], "opus_digest_cost": None})
    assert "requires Opus digest" not in md  # Arm B table
    out_a = js.analyze_arm(
        "A",
        runs,
        names,
        ["association"],
        [],
        r4=r4,
        states={},
        roster={},
        ledgers={},
        model_of={},
        rule=FAST,
    )
    assert out_a["models"][0]["families"]["association"]["vs_ref_n"] == 39
    assert out_a["models"][0]["verdict"].startswith("not terminal")
    md = js.markdown({"sweep": "t", "arms": [out_a], "notes": [], "opus_digest_cost": None})
    assert "requires Opus digest" in md and "Cheapest Arm A match" in md


def test_cmd_analyze_writes_outputs(tmp_path):
    arm, base, _rng = _arm(4, n_fam=1, n_items=30)
    sweep = tmp_path / "out" / "t"
    names = {"D1": "A1", "D2": "A2", "D3": "A3", "D4": "A4"}
    for d, fams in arm.draws.items():
        res = _res("association", _pass_rows(fams["f0"]))
        dest = sweep / "A" / js.slug(js.OPUS, names[d]) / "association"
        dest.mkdir(parents=True)
        (dest / "results.json").write_text(json.dumps(res))
        _jsonl(dest / "cells.jsonl", [{"key": "judge:x", "fp": d, "ts": 1.0, "result": {}}])
        _jsonl(dest / "spend.jsonl", [{"run_id": "r", "input_tokens": 1000, "output_tokens": 10}])
    m = sweep / "A" / js.slug("deepseek/deepseek-v4-flash") / "association"
    m.mkdir(parents=True)
    (m / "results.json").write_text(json.dumps(_res("association", _pass_rows(base["f0"]))))
    _jsonl(m / "spend.jsonl", [{"run_id": "r", "usd": 0.01}])
    ref = tmp_path / "r4" / "association"
    ref.mkdir(parents=True)
    (ref / "results.json").write_text(json.dumps(_res("association", _pass_rows(base["f0"]))))
    rc = js.main(
        [
            "analyze",
            "--sweep",
            "t",
            "--ref-dir",
            str(tmp_path / "r4"),
            "--out-root",
            str(tmp_path / "out"),
            "--assume-terminal",
        ]
    )
    assert rc == 0
    a = json.loads((sweep / "analysis.json").read_text())
    (arm_a,) = a["arms"]
    (row,) = arm_a["models"]
    assert row["model"] == "deepseek/deepseek-v4-flash" and row["cost_usd"] == 0.01
    assert a["thresholds"] == dataclasses.asdict(js.MATCH)
    md = (sweep / "analysis.md").read_text()
    assert "deepseek/deepseek-v4-flash" in md and "tolerance" in md
    svg = (sweep / "cost_vs_agreement.svg").read_text()
    assert svg.startswith("<svg") and "Arm A" in svg


def test_pareto_and_scatter():
    pts = [("a", 1.0, 0.9), ("b", 0.1, 0.8), ("c", 2.0, 0.85), ("d", 0.0, 0.5)]
    assert set(js.pareto(pts)) == {"a", "b", "d"}
    svg = js.scatter_svg({"A": [("x/a", "cheap", 1.0, 0.9), ("b", "free", 0.0, 0.7)], "B": []})
    assert svg.count("Arm ") == 2 and "polyline" in svg


def test_failures_column_and_cache_only_counters():
    res = _res(
        "association",
        [{"id": "a", "pass": True}],
        summary={
            "n_digest_calls": 0,
            "n_reask_cache_miss": 2,
            "summary_unsummarized_ids": ["b"],
            "summary_reask_cache_miss_ids": ["a", "c"],
        },
    )
    v, _st = js.sweep_item_verdicts("association", res, universe={"a", "b"})
    led = {"refusals": 3, "errors": 1, "failed": {"content_filter": 3}}
    fl = js.failures_of(res, v, led)
    assert fl == {
        "unsummarized": 1,
        "reask_cache_miss": 2,
        "rowless_or_failed": 1,
        "refusals": 3,
        "errors": 1,
        "failed": {"content_filter": 3},
    }
    assert js.extra_num(res, "n_digest_calls") == 0
    assert js.extra_num(_res("association", [], n_digest_calls=4), "n_digest_calls") == 4
    assert "3 refused" in js._failures_cell(fl)


def test_classify_unreadable_results_both_times_is_unchanged():
    # a family whose verdicts cannot be extracted reads None before and after: an all-ok
    # unthrottled pass is still "unchanged", not a reason to re-pay all 5 passes (review of #217)
    now = 1_000_000.0
    assert js.classify([_p(now, before=None, after=None)], free=False, now=now)[0] == "terminal"


def test_models_tier_selection_is_backend_filtered_and_skips_opus():
    roster = {
        "claude-haiku-5-5": {"tier": "cheap"},
        "deepseek/deepseek-v4-flash": {"tier": "cheap"},
        "openai/gpt-6-astra": {"tier": "frontier"},
        "claude-fable-5-1": {"tier": "frontier"},
        js.OPUS: {"tier": "anchor"},
        "claude-sonnet-5-5": {"tier": "anchor"},
    }
    assert js._models_arg("tier:cheap+frontier", roster, claude=False) == [
        "deepseek/deepseek-v4-flash",
        "openai/gpt-6-astra",
    ]
    assert js._models_arg("tier:cheap+frontier+anchor", roster, claude=True) == [
        "claude-haiku-5-5",
        "claude-fable-5-1",
        "claude-sonnet-5-5",
    ]
    assert js._models_arg("a,b", roster, claude=True) == ["a", "b"]  # explicit lists as given
