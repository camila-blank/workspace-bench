"""association bank v2 (plan 0039): retired items, the 8-way MC judge and its deterministic gate,
the summarize-suffix read sites, the summary digest of every cell, lite reads, prompt-only."""

import dataclasses
import hashlib
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from conftest import write_jsonl, write_sidecar
from summary_fakes import FakeLLM, summary_args

from wsbench import digest, mc, readplan, reads, registry
from wsbench.baselines import text_inversion as ti
from wsbench.evals.association import SPEC
from wsbench.evals.association import judge as aj
from wsbench.evals.association import prompts as ap

REPO = Path(__file__).resolve().parents[1]
V1 = REPO / "evals/association/items.json"
V1_SHA = "e8b1307bd24b41c01c9d7eb27031d87adaa34a717d854ca6d5a153d6b099764d"
RETIRED = {"nasa", "cia", "history-h", "love-h", "death-h", "game-h", "de-konig"}
GRID = readplan.GRID
POS = list(range(24, 43))  # 19 contiguous read sites (b3-as-birthday's real ones)
FILL = "- a short story is being summarized\n- one sentence"


@pytest.fixture(autouse=True)
def _registered(real_families):
    return real_families


def items() -> dict[str, dict]:
    return {it["id"]: it for it in aj.load_items()}


def write_cells(
    path: Path,
    hits: dict[tuple[str, int, int], str | list[str]],
    ids: list[str],
    *,
    tokens: bool = False,
    layers: list[int] = GRID,
    positions: list[int] = POS,
    drop: set[tuple[str, int, int]] = frozenset(),
    sidecar: dict | None = None,
) -> Path:
    rows = []
    for iid in ids:
        for layer in layers:
            for p in positions:
                if (iid, layer, p) in drop:
                    continue
                v = hits.get((iid, layer, p))
                if tokens:
                    toks = v if isinstance(v, list) else ["story", "summary", "sentence"]
                    rows.append({"id": iid, "layer": layer, "pos": p, "tokens": toks})
                else:
                    rows.append({"id": iid, "layer": layer, "pos": p, "samples": [v or FILL]})
    write_jsonl(path, rows)
    if sidecar is not None:
        write_sidecar(path, **sidecar)
    return path


def keyword_digest(keys: list[str]):
    """A fake digest: every bundle line holding a key (folded), else the first line."""

    def run(user: str) -> dict:
        lines = [ln[2:] for ln in user.splitlines() if ln.startswith("- ")]
        hit = [ln for ln in lines if any(k.casefold() in ln.casefold() for k in keys)]
        chosen = hit or lines[:1]
        return {
            "items": [
                {"statement": f"S{i}", "where": "", "evidence": ln} for i, ln in enumerate(chosen)
            ]
        }

    return run


def listing_of(user: str) -> list[str]:
    tail = user.rsplit("Which concept do the readouts name?\n", 1)[1]
    return [m.group(1) for m in re.finditer(r"^  \d+\. (.*)$", tail, flags=re.M)]


def answer(user: str, option: str, quote: str) -> dict:
    return {"choice": listing_of(user).index(option) + 1, "quote": quote}


_MP: list = []


def _mp():
    return _MP[-1]


@pytest.fixture(autouse=True)
def _monkeypatch_handle(monkeypatch):
    _MP.append(monkeypatch)
    yield
    _MP.pop()


def judge_run(tmp_path, path, ids, judge, keys, *, items_arg=True, **kw):
    fake = FakeLLM.install(_mp(), judge=judge, digest=keyword_digest(keys))
    args = summary_args(
        path,
        tmp_path / "out",
        SPEC.judge.prompt_version,
        items=ids if items_arg else None,
        allow_missing=kw.pop("allow_missing", False),
        **kw,
    )
    return SPEC.run(args), fake


def native_run(tmp_path, path, ids, judge, *, layers=None):
    fake = FakeLLM.install(_mp(), judge=judge)
    args = summary_args(
        path,
        tmp_path / "nat",
        SPEC.judge.prompt_version,
        items=ids,
        layers=layers,
        extra={"aggregate": "native"},
    )
    return SPEC.run(args), fake


def mc_users(fake, name="readout_mc") -> list[str]:
    return [u for n, _s, u in fake.calls if n == name]


# ---------------------------------------------------------------- C1: bank v2


def test_v2_retires_underdetermined_items():
    raw = json.loads((REPO / "evals/association/items_v2.json").read_text(encoding="utf-8"))
    ids = {it["name"] for it in raw["items"]}
    assert raw["version"] == 2 and raw["render"] == "chat_summarize"
    assert len(ids) == 93 and not ids & RETIRED
    assert set(raw["retired"]) == RETIRED and set(raw["retired_rule"]) == {"a", "b", "c"}
    assert all(raw["retired"][i][0] in "abc" for i in RETIRED)
    assert registry.bank_path("association").name == "items_v2.json"
    assert hashlib.sha256(V1.read_bytes()).hexdigest() == V1_SHA  # v1 byte-identical
    sh = items()["bf-sherlock"]
    assert sh["mc"]["gold"] == "Sherlock Holmes" and sh["intermediates"] == ["Sherlock Holmes"]
    assert "detective" not in [mc.fold(a) for a in sh["mc"]["aliases"]]


def test_ati_entry_has_no_retired_ids():
    """The frozen v1 entry still lists retired ids, so subset_entry must refuse it until the run
    phase re-gates (gpu R8); a re-gated entry must hold none of them."""
    entry = json.loads(ti.FROZEN.read_text(encoding="utf-8"))["association"]
    if entry.get("bank_sha") != ti.bank_fingerprint("association"):
        assert set(entry["ids"]) & RETIRED  # the stale v1 entry
        with pytest.raises(SystemExit, match="re-gate"):
            ti.subset_entry("association")
    else:
        assert not set(entry["ids"]) & RETIRED


# ---------------------------------------------------------------- C3: options


def test_v2_options_shape():
    for iid, it in items().items():
        m = it["mc"]
        shown, gold_pos = aj.build_options(it)
        subst = shown[:-1]
        assert len(subst) == 8 and len({mc.fold(o) for o in subst}) == 8, iid
        assert subst[gold_pos - 1] == m["gold"] == it["intermediates"][0], iid
        forms = aj.gold_forms(it)
        for d in m["distractors"]:
            assert not mc.form_in(d, forms), (iid, d)  # rule (ii)
            assert not any(mc.form_in(f, [d]) for f in forms), (iid, d)
            assert not mc.is_cannot(d)
            if aj._LATIN.search(m["gold"]) and m["gold"].isupper() and " " not in m["gold"]:
                assert d.isupper(), (iid, d)  # rule (iv): an acronym for an acronym
            assert len(d.split()) <= len(m["gold"].split()) + 2, (iid, d)
        assert m["category"] and m["aliases"] is not None


def test_build_options_seeded_cannot_last():
    it = items()["b3-as-birthday"]
    a, ga = aj.build_options(it)
    b, gb = aj.build_options(it)
    assert a == b and ga == gb and a[-1] == mc.CANNOT
    m = it["mc"]
    want = mc.options_with_cannot(
        mc.seeded_shuffle([m["gold"], *m["distractors"]], f"{ap.ASSOC_SEED}:b3-as-birthday")
    )
    assert a == want
    assert {"Christmas", "Halloween", "Thanksgiving", "Easter"} <= set(a)


def test_adapted_bank_without_mc_fails_loudly(tmp_path, monkeypatch, capsys):
    raw = json.loads(V1.read_text(encoding="utf-8"))
    f = tmp_path / "items.json"
    f.write_text(json.dumps(raw))
    monkeypatch.setenv("WSB_BANK_FILE_ASSOCIATION", str(f))
    with pytest.raises(SystemExit):
        aj.load_items()
    assert "re-adapt: no mc options" in capsys.readouterr().err


# ---------------------------------------------------------------- C3: the gate (summary mode)

BD = "b3-as-birthday"
LAST = POS[-1]


def test_gold_pick_with_verified_quote_passes(tmp_path):
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): "- a birthday cake"}, [BD])
    r, fake = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "birthday", "a birthday cake"), ["cake"]
    )
    row = r.rows[0]
    assert row["pass"] is True and row["outcome"] == "gold_listed_form"
    assert row["credit"] == "listed_form" and r.value == 1.0 and r.chance == 0.125
    assert fake.n("quote_mc") == 0


def test_distractor_pick_fails(tmp_path):
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): "- Christmas morning"}, [BD])
    r, _f = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "Christmas", "Christmas"), ["Christmas"]
    )
    assert r.rows[0]["pass"] is False and r.rows[0]["outcome"] == "distractor"
    assert r.extras["n_distractor"] == 1 and r.value == 0.0


def test_cannot_tell_fails(tmp_path):
    path = write_cells(tmp_path / "r.jsonl", {}, [BD])
    r, _f = judge_run(tmp_path, path, [BD], lambda s, u, sc: answer(u, mc.CANNOT, ""), ["x"])
    assert r.rows[0]["pass"] is False and r.rows[0]["outcome"] == "cannot_tell"


def test_holiday_flat_list_is_cannot_tell(tmp_path):
    toks = ["christmas", "birthday", "halloween", "thanksgiving", "easter"]
    path = write_cells(tmp_path / "r.jsonl", {(BD, 40, LAST): toks}, [BD], tokens=True)
    r, fake = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, mc.CANNOT, ""), ["christmas"]
    )
    (system,) = {s for n, s, _u in fake.calls if n == "readout_mc"}
    (user,) = mc_users(fake)
    assert "A single list or line that holds two or more of the listed options" in system
    assert "singles out\nnone" not in system and "ranked first" not in system
    note = mc.token_note(SPEC.flat_types, multi_select=SPEC.multi_select)
    assert note in user and "ranked first among" not in user
    assert "two or more of the options singles out none of those options" in note
    assert r.rows[0]["pass"] is False and r.rows[0]["outcome"] == "cannot_tell"


def test_lone_option_in_same_type_list_is_singled_out(tmp_path):
    toks = ["birthday", "party", "cake", "celebration", "candles"]
    path = write_cells(tmp_path / "r.jsonl", {(BD, 40, LAST): toks}, [BD], tokens=True)
    r, _f = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "birthday", "birthday"), ["candles"]
    )
    assert r.rows[0]["pass"] is True and r.rows[0]["outcome"] == "gold_listed_form"
    assert r.extras["n_flat_list_voided"] == 0


def test_token_line_with_two_options_voids_gold_pick(tmp_path):
    toks = ["christmas", "birthday", "cake"]
    path = write_cells(tmp_path / "r.jsonl", {(BD, 40, LAST): toks}, [BD], tokens=True)
    r, _f = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "birthday", "birthday"), ["christmas"]
    )
    assert r.config["sampling"] == "tokens10"
    assert r.rows[0]["outcome"] == "gold_flat_list" and r.rows[0]["pass"] is False
    assert r.extras["n_flat_list_voided"] == 1
    # the same line as a prose sample is a judge decision, not voided by step 2b
    lines = [digest.Line("christmas | birthday | cake")]
    others = ["Christmas", "Halloween"]
    assert aj.flat_list_void("birthday", lines, others, "tokens10")
    assert not aj.flat_list_void("birthday", lines, others, "prose1")
    # a quote that also sits in a clean list is not voided
    clean = [*lines, digest.Line("birthday | party | cake")]
    assert not aj.flat_list_void("birthday", clean, others, "tokens10")


def test_system_tie_break_counts_only_singling_lines():
    for system in (ap.SYSTEM_SUMMARY, ap.SYSTEM_NATIVE):
        assert "lines that single out nothing never count" in system
        assert "singles out the option you picked" in system
        assert "carried by clearly more lines" not in system


def test_unverified_quote_fails(tmp_path):
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): "- a birthday cake"}, [BD])
    r, fake = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "birthday", "a birthday party"), ["cake"]
    )
    row = r.rows[0]
    assert row["outcome"] == "gold_unverified" and row["pass"] is False and row["retried"]
    assert fake.n("readout_mc") == 2  # the first call + the one retry


def test_bad_quote_retried_once(tmp_path):
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): "- a birthday cake"}, [BD])

    def judge(s, u, sc):
        good = ap.RETRY_LINE in u
        return answer(u, "birthday", "a birthday cake" if good else "the birthday song")

    r, fake = judge_run(tmp_path, path, [BD], judge, ["cake"])
    assert r.rows[0]["pass"] is True and r.rows[0]["retried"] is True
    assert fake.n("readout_mc") == 2 and r.extras["n_retried"] == 1
    keys = [json.loads(x)["key"] for x in (tmp_path / "out/cells.jsonl").read_text().splitlines()]
    assert f"{BD}|L-1|retry1" in keys


def test_cjk_quote_needs_listed_form(tmp_path):
    path = write_cells(
        tmp_path / "r.jsonl", {(BD, 44, LAST): "- 生日快乐", (BD, 48, LAST): "- 庆祝活动"}, [BD]
    )
    r, fake = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "birthday", "生日"), ["生日"]
    )
    assert r.rows[0]["outcome"] == "gold_listed_form" and fake.n("quote_mc") == 0
    r, fake = judge_run(
        tmp_path / "b", path, [BD], lambda s, u, sc: answer(u, "birthday", "庆祝"), ["庆祝"]
    )
    assert r.rows[0]["outcome"] == "gold_unlisted_script" and r.rows[0]["pass"] is False
    assert fake.n("quote_mc") == 0


def test_listed_alias_passes_without_rejudge(tmp_path):
    nb = "neighbors"
    path = write_cells(tmp_path / "r.jsonl", {(nb, 44, LAST): "- the neighbours waved"}, [nb])
    r, fake = judge_run(
        tmp_path, path, [nb], lambda s, u, sc: answer(u, "neighbors", "neighbours"), ["waved"]
    )
    assert r.rows[0]["outcome"] == "gold_listed_form" and fake.n("quote_mc") == 0


def test_spelling_variant_rule_present(tmp_path):
    assert '"neighbour" names "neighbors"' in ap.SYSTEM_SUMMARY
    it = items()["neighbors"]
    assert mc.form_in("the neighbours", aj.gold_forms(it))


def test_description_credit_needs_quote_only_rejudge(tmp_path):
    q = "the day she was born, candles on a cake"
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): f"- {q}"}, [BD])

    def judge(gold_again):
        def f(s, u, sc):
            if sc["name"] == "quote_mc":
                assert u.startswith("READOUT (one span quoted from the readouts):\n" + q)
                return answer(u, "birthday" if gold_again else mc.CANNOT, q)
            return answer(u, "birthday", q)

        return f

    r, fake = judge_run(tmp_path, path, [BD], judge(True), ["candles"])
    assert fake.n("quote_mc") == 1 and r.rows[0]["outcome"] == "gold_description"
    assert r.rows[0]["pass"] is True and r.extras["n_description_credit"] == 1
    assert r.extras["description_credit_quotes"] == [q]
    r, fake = judge_run(tmp_path / "b", path, [BD], judge(False), ["candles"])
    assert r.rows[0]["outcome"] == "gold_desc_rejected" and r.rows[0]["pass"] is False


def test_rejudge_api_fail_is_undecided(tmp_path):
    q = "candles on a cake"
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): f"- {q}"}, [BD])

    def judge(s, u, sc):
        return None if sc["name"] == "quote_mc" else answer(u, "birthday", q)

    r, _f = judge_run(tmp_path, path, [BD], judge, ["candles"])
    row = r.rows[0]
    assert row["pick"] == "api_fail" and row["failed_stage"] == "quote_check"
    assert row["pass"] is None and r.complete is False


def test_rows_carry_credit_field(tmp_path):
    ids = [BD, "neighbors", "bf-sherlock", "winter"]
    hits = {
        (BD, 44, LAST): "- a birthday cake",
        ("neighbors", 44, LAST): "- the people next door she watched for years",
        ("bf-sherlock", 44, LAST): "- Poirot twirls his moustache",
        ("winter", 44, LAST): "- dark at four",
    }
    path = write_cells(tmp_path / "r.jsonl", hits, ids)

    def judge(s, u, sc):
        if sc["name"] == "quote_mc":
            return answer(u, "neighbors", "the people next door")
        if "birthday cake" in u:
            return answer(u, "birthday", "birthday cake")
        if "next door" in u:
            return answer(u, "neighbors", "the people next door")
        if "Poirot" in u:
            return answer(u, "Hercule Poirot", "Poirot")
        return answer(u, mc.CANNOT, "")

    r, _f = judge_run(tmp_path, path, ids, judge, ["cake", "next door", "Poirot", "dark"])
    credit = {row["id"]: row["credit"] for row in r.rows}
    assert credit == {
        BD: "listed_form",
        "neighbors": "description",
        "bf-sherlock": "none",
        "winter": "none",
    }


def test_quote_across_bullet_mark_passes(tmp_path, monkeypatch):
    path = write_cells(
        tmp_path / "r.jsonl", {(BD, 44, LAST): "- a family party\n- birthday candles"}, [BD]
    )
    called = []
    monkeypatch.setattr(digest, "statement_lines", lambda *a, **k: called.append(1) or [])
    r, _f = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "birthday", "party birthday"), ["party"]
    )
    assert r.rows[0]["pass"] is True and not called


# ---------------------------------------------------------------- C2: every cell, 19 positions


def test_summary_digest_gets_every_cell(tmp_path):
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): "- a birthday cake"}, [BD])
    r, fake = judge_run(
        tmp_path, path, [BD], lambda s, u, sc: answer(u, "birthday", "birthday"), ["cake"]
    )
    (bundle,) = [u for n, _s, u in fake.calls if n == "digest"]
    groups = set(re.findall(r"^\[L(\d+) pos (\d+)\]$", bundle, flags=re.M))
    assert groups == {(str(L), str(p)) for L in GRID for p in POS} and len(groups) == 19 * 11
    assert r.extras["n_readouts_in"] == r.extras["n_readouts_bundled"] == 209


def test_full_read_needs_19_positions(tmp_path, capsys):
    side = {"reader": "s3d", "arm": "s3d", "plan_sha": reads.plan_sha("association")}
    path = write_cells(
        tmp_path / "r.jsonl",
        {},
        [BD],
        drop={(BD, 44, POS[0])},
        sidecar={"sampling": "prose1", **side},
    )
    FakeLLM.install(_mp(), judge=lambda s, u, sc: answer(u, mc.CANNOT, ""))
    args = summary_args(path, tmp_path / "o", SPEC.judge.prompt_version, items=[BD])
    with pytest.raises(SystemExit) as ei:
        SPEC.run(args)
    assert ei.value.code == 2 and "full span" in capsys.readouterr().err
    assert SPEC.min_full_positions == 19 and SPEC.full_contiguous


def test_task_note_reaches_digest(tmp_path):
    assert SPEC.task_note == ap.DIGEST_TASK_NOTE and digest.DEFAULT_VARIANT == "task"
    path = write_cells(tmp_path / "r.jsonl", {}, [BD])
    _r, fake = judge_run(tmp_path, path, [BD], lambda s, u, sc: answer(u, mc.CANNOT, ""), ["x"])
    (system,) = [s for n, s, _u in fake.calls if n == "digest"]
    assert SPEC.task_note in system
    golds = [mc.fold(it["mc"]["gold"]) for it in items().values()]
    assert not any(f" {g} " in f" {mc.fold(SPEC.task_note)} " for g in golds)


# ---------------------------------------------------------------- native (opt-out, ATI gate)


def test_native_any_layer_mc(tmp_path):
    hits = {(BD, 20, LAST): "- Christmas lights", (BD, 44, LAST): "- a birthday cake"}
    path = write_cells(tmp_path / "r.jsonl", hits, [BD], layers=[20, 44])

    def judge(s, u, sc):
        assert s == ap.SYSTEM_NATIVE
        if "Christmas lights" in u:
            return answer(u, "Christmas", "Christmas lights")
        return answer(u, "birthday", "birthday cake")

    r, fake = native_run(tmp_path, path, [BD], judge, layers=[20, 44])
    row = r.rows[0]
    assert row["pass"] is True and row["earliest_layer"] == 44
    assert row["layers"]["20"]["outcome"] == "distractor"
    assert row["layers"]["44"]["outcome"] == "gold_listed_form"
    assert r.extras["n_items_gold_any_layer"] == 1
    assert r.extras["n_items_distractor_any_layer"] == 1
    assert r.extras["n_items_distractor_majority"] == 0
    assert r.config["prompt_version"] == ap.PROMPT_VERSION and fake.n("readout_mc") == 2


def test_ati_gate_reads_association_mc_rows(tmp_path):
    from wsbench.results import write_results

    hits = {(BD, 44, LAST): "- a birthday cake", ("winter", 44, LAST): "- summer heat"}
    path = write_cells(tmp_path / "r.jsonl", hits, [BD, "winter"], layers=[44])

    def judge(s, u, sc):
        if "birthday cake" in u:
            return answer(u, "birthday", "birthday cake")
        return answer(u, "summer", "summer heat")

    r, _f = native_run(tmp_path, path, [BD, "winter"], judge, layers=[44])
    write_results(tmp_path / "res", r)
    assert ti.draw_verdicts(tmp_path / "res" / "results.json") == {BD: True, "winter": False}


# ---------------------------------------------------------------- C5: prompt-only


def test_prompt_only_rows_take_the_same_instrument(tmp_path):
    its = items()
    ids = list(its)
    po_rows = [
        {
            "id": i,
            "layer": 44,
            "pos": LAST,
            "samples": [f"- the story is about {its[i]['mc']['gold']} (end)"],
        }
        for i in ids
    ]
    po = write_jsonl(tmp_path / "po.jsonl", po_rows)
    write_sidecar(po, sampling="prose1", reader="prompt_only:claude-opus-5-5", arm="po")

    def judge(s, u, sc):
        for i in ids:
            g = its[i]["mc"]["gold"]
            if f"the story is about {g} (end)" in u:
                return answer(u, g, f"the story is about {g} (end)")
        return answer(u, mc.CANNOT, "")

    fake = FakeLLM.install(_mp(), judge=judge, digest=keyword_digest(["story"]))
    r = SPEC.run(summary_args(po, tmp_path / "po", SPEC.judge.prompt_version))
    assert r.complete is True and r.config["reads_checked"] == "prompt_only"
    assert r.value == 1.0 and r.n_items == 93
    po_systems = {s for n, s, _u in fake.calls if n == "readout_mc"}
    lens = write_cells(tmp_path / "lens.jsonl", {(BD, 44, LAST): "- a birthday cake"}, [BD])
    lr, lfake = judge_run(
        tmp_path / "l", lens, [BD], lambda s, u, sc: answer(u, "birthday", "birthday"), ["cake"]
    )
    assert reads.strip_reads(r.config["prompt_version"]) == reads.strip_reads(
        lr.config["prompt_version"]
    )
    assert po_systems == {s for n, s, _u in lfake.calls if n == "readout_mc"}


# ---------------------------------------------------------------- C6: lite


def test_lite_reads_one_site(tmp_path):
    path = write_cells(tmp_path / "r.jsonl", {(BD, 44, LAST): "- a birthday cake"}, [BD])
    r, fake = judge_run(
        tmp_path,
        path,
        [BD],
        lambda s, u, sc: answer(u, "birthday", "birthday"),
        ["cake"],
        extra={"reads": "lite"},
    )
    (bundle,) = [u for n, _s, u in fake.calls if n == "digest"]
    groups = set(re.findall(r"^\[L(\d+) pos (\d+)\]$", bundle, flags=re.M))
    assert groups == {(str(L), str(LAST)) for L in GRID}
    assert r.config["prompt_version"].endswith("+reads-lite")
    assert json.loads(r.config["lite_rule"]) == [
        {"positions": {"kind": "final_token"}, "layers": None}
    ]


def _lite_table_module():
    spec = importlib.util.spec_from_file_location(
        "lite_site_table", REPO / "scripts/lite_site_table.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def test_lite_site_table_uses_support_share(tmp_path):
    """Fixture digests for 2 items x 3 arms; the choice follows the pooled support share and
    ignores pass rates; a 9-point win keeps final_token, an 11-point win switches."""
    lst = _lite_table_module()
    ids = [BD, "winter"]
    end = LAST
    forms = {i: lst.gold_forms("association", items()[i]) for i in ids}
    assert "生日" in forms[BD] and forms[BD][0] == "birthday"

    def arm_rows(k1: int, k3: int, passes: list[bool]) -> list[dict]:
        rows = []
        for iid, ok in zip(ids, passes, strict=True):
            g = items()[iid]["mc"]["gold"]
            ev = [f"{g} here"] * (k1 + k3) + ["nothing"]
            where = [f"[L44 pos {end}]"] * k1 + [f"[L44 pos {end - 2}]"] * k3 + ["[L44 pos 30]"]
            rows.append(
                {"id": iid, "pass": ok, "summary_evidence": ev, "summary_evidence_where": where}
            )
        return rows

    ends = {(i, 44): end for i in ids}

    def choose(k1, k3, passes):
        per = {
            arm: lst.gold_lines_by_k(arm_rows(k1, k3, passes), forms, ends)
            for arm in ("s3d", "jlens", "mlnla")
        }
        return lst.table("association", per)["chosen"]

    # shares: k=1 vs k=3 of 100 lines -> 9-point win keeps final_token, 11-point win switches
    assert choose(455, 545, [True, False]) == {"kind": "final_token"}  # 45.5% vs 54.5%
    assert choose(445, 555, [True, False]) == {"kind": "offset_from_end", "k": 3}  # 44.5 / 55.5
    assert choose(445, 555, [False, True]) == {"kind": "offset_from_end", "k": 3}  # pass permuted
    assert Counter(lst.gold_lines_by_k(arm_rows(1, 0, [True, True]), forms, ends)) == Counter(
        {1: 2}
    )


def test_registered_spec_is_summary_family():
    assert SPEC.name not in digest.NATIVE_ONLY and not SPEC.targets_judge
    assert SPEC.flat_types == () and SPEC.multi_select is False
    assert dataclasses.asdict(SPEC.judge)["prompt_version"] == "assoc-mc-2026-10-30"
