"""Summary mode (plan 0021 Part A) on brew_intermediates: one digest per (item, unit) in one
collapse pass, one ranked multi-select call per unit, the rank rule and its floors, the one
retry of an invalid ``ranked``, item states by the emit unit, and the no-cut render."""

import itertools
import json
import re
from pathlib import Path

import pytest
from summary_fakes import FakeLLM, first_lines_digest, summary_args

from wsbench import digest, registry
from wsbench.banks import load_bank
from wsbench.evals.brew_intermediates import judge
from wsbench.evals.brew_intermediates import prompts as bp
from wsbench.llm import schema_block

REPO = Path(__file__).resolve().parents[1]
BANK = REPO / "evals/brew_intermediates/items.json"
_H, ITEMS = load_bank(BANK)
A, B = ITEMS[0], ITEMS[1]
LAYERS = (20, 24)


@pytest.fixture(autouse=True)
def _registered():
    from wsbench.evals.brew_intermediates import SPEC

    registry.FAMILIES.setdefault(SPEC.name, SPEC)


def _offs(it: dict) -> list[str]:
    keep = (it["intermediates"][0], it["start"], it["answer"])
    return sorted(c for c in it["options_adjacent"][0] if c not in keep)


def _pos(it: dict, regions: tuple[str, ...]) -> list[int]:
    return sorted(int(p) for p, r in it["regions"].items() if r in regions)


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _rows(it: dict, *, first: str, second: str, all_regions: bool = False) -> list[dict]:
    """Emission cells name ``first`` at L20 and ``second`` at L24; stir cells name the start;
    start/question cells (``all_regions``) name the answer."""
    rows = []
    for layer in LAYERS:
        colour = first if layer == LAYERS[0] else second
        for p in _pos(it, judge.EMIT):
            text = f"at {p} the potion turns {colour}"
            rows.append({"id": it["id"], "layer": layer, "pos": p, "samples": [text]})
        for p in _pos(it, judge.STIR):
            text = f"stir {p} keeps it {it['start']}"
            rows.append({"id": it["id"], "layer": layer, "pos": p, "samples": [text]})
        if all_regions:
            for p in _pos(it, ("start", "question")):
                text = f"line {p} asks about {it['answer']}"
                rows.append({"id": it["id"], "layer": layer, "pos": p, "samples": [text]})
    return rows


def _readouts(tmp_path: Path, all_regions: bool = False) -> Path:
    a = _rows(A, first=A["intermediates"][0], second=_offs(A)[0], all_regions=all_regions)
    b = _rows(B, first=_offs(B)[0], second=B["intermediates"][0], all_regions=all_regions)
    return _write(tmp_path / "r.jsonl", a + b)


def _all_lines(user: str) -> dict:
    return first_lines_digest(user, 50)


def _candidates(user: str) -> list[str]:
    return re.search(r"^Candidate colours: (.*)$", user, flags=re.M).group(1).split(", ")


def _readout(user: str) -> str:
    return user.split("<readout>\n", 1)[1].split("\n</readout>", 1)[0]


def ranking_judge(system: str, user: str, schema: dict) -> dict:
    """Name every candidate the digest names; rank by the first line it appears on."""
    assert schema["name"] == "brew_summary"
    lines = _readout(user).splitlines()
    first: dict[str, int] = {}
    for k, ln in enumerate(lines):
        for c in _candidates(user):
            if re.search(rf"\b{c}\b", ln) and c not in first:
                first[c] = k
    ranked = sorted(first, key=first.__getitem__)
    return {
        "named": sorted(ranked),
        "primary": ranked[0] if ranked else "none",
        "basis": "explicit" if ranked else "none",
        "ranked": ranked,
    }


def _args(tmp_path: Path, readouts: Path, **kw):
    return summary_args(
        readouts, tmp_path / "out", bp.PROMPT_VERSION, **{"items": [A["id"], B["id"]], **kw}
    )


# ---------------------------------------------------------------- pure pieces


def test_native_schema_is_mains():
    """Plan 0021 rev 4 #3: the native contract does not move (a verbatim copy of origin/main's
    ``SCHEMA``); summary mode uses its own ``SCHEMA_SUMMARY``."""
    main_schema = schema_block(
        "brew",
        {
            "named": {"type": "array", "items": {"type": "string", "enum": bp.COLOURS}},
            "primary": {"type": "string", "enum": [*bp.COLOURS, "none"]},
            "basis": {"type": "string", "enum": ["explicit", "implied", "none"]},
        },
        ["named", "primary", "basis"],
    )
    assert json.dumps(bp.SCHEMA, sort_keys=False) == json.dumps(main_schema, sort_keys=False)
    assert bp.SCHEMA_SUMMARY["name"] != bp.SCHEMA["name"]
    assert "ranked" in json.dumps(bp.SCHEMA_SUMMARY) and "ranked" not in json.dumps(bp.SCHEMA)
    assert bp.render_user("x", ["red"]) == bp.USER.replace("{readout}", "x").replace(
        "{options}", "red"
    )


def test_rank_rule_and_identity():
    gold, offs = "green", ["purple", "red"]
    r = judge.rank_rule(["green", "blue", "red"], gold, offs)
    assert r["pass"] and r["null"] == 0.0 and r["baseline"] == pytest.approx(1 / 3)
    # the start/answer colour ranked first is ignored
    assert judge.rank_rule(["blue", "green", "red"], gold, offs)["pass"]
    r = judge.rank_rule(["red", "green"], gold, offs)
    assert not r["pass"] and r["null"] == 0.5 and r["count_rule"] is True
    assert judge.rank_rule([], gold, offs) == {
        "pass": False,
        "null": 0.0,
        "baseline": 0.0,
        "count_rule": False,
    }
    # an un-named off colour never blocks the gold; the count rule then agrees
    r = judge.rank_rule(["green"], gold, offs)
    assert r["pass"] and r["count_rule"]
    # pass + K*null == [any of gold + offs named], over every ranking of every subset
    pool = [gold, *offs, "blue", "black"]
    for n in range(len(pool) + 1):
        for sub in itertools.permutations(pool, n):
            r = judge.rank_rule(list(sub), gold, offs)
            anyn = any(c in sub for c in [gold, *offs])
            assert r["pass"] + len(offs) * r["null"] == pytest.approx(float(anyn))
            assert r["baseline"] == pytest.approx(anyn / 3)


def test_ranked_validation_filters_to_options():
    opts = ["red", "black", "green", "purple", "blue"]
    ok = {"named": ["green", "red", "teal"], "ranked": ["teal", "red", "green"]}
    assert judge.ranked_valid(ok, opts)  # teal is off-list on both sides
    assert not judge.ranked_valid({"named": ["green", "red"], "ranked": ["red"]}, opts)
    assert not judge.ranked_valid({"named": ["green"], "ranked": ["green", "green"]}, opts)
    assert not judge.ranked_valid({"named": ["green"], "ranked": "green"}, opts)
    v = judge.summary_verdict(
        {**ok, "primary": "teal", "basis": "explicit"}, opts, "green", ["purple", "red"]
    )
    assert v["named"] == ["green", "red"] and v["ranked"] == ["red", "green"]
    assert v["pass"] is False and v["primary"] == "none" and v["null"] == 0.5


# ---------------------------------------------------------------- runs


def test_units_rule_floors_and_stamps(tmp_path, monkeypatch):
    path = _readouts(tmp_path)
    fake = FakeLLM.install(monkeypatch, ranking_judge, digest=_all_lines)
    r = judge.run(_args(tmp_path, path))
    assert fake.n("digest") == 4  # emit + stir per item, one collapse pass
    assert fake.n("brew_summary") == 4 and fake.n("brew") == 0
    digest_users = [u for n, _s, u in fake.calls if n == "digest"]
    assert all("stir" not in u for u in digest_users if "potion turns" in u)  # never mixed
    assert all(s == bp.SYSTEM for n, s, _u in fake.calls if n == "brew_summary")
    rows = {x["id"]: x for x in r.rows}
    ra, rb = rows[A["id"]], rows[B["id"]]
    assert ra["pass"] is True and ra["emit"]["state"] == "judged"
    assert ra["emit"]["ranked"][0] == A["intermediates"][0] and ra["emit"]["n_cells"] == 6
    assert rb["pass"] is False and rb["emit"]["null"] == 0.5
    assert rb["emit"]["count_rule"] is True  # gold and one off named: K*1 > 1, despite the rank
    assert ra["stir"]["named"] == [A["start"]] and ra["stir"]["pass"] is False
    assert {"summary_emit", "summary_stir"} <= set(ra) and "summary_compute" not in ra
    assert "summary" not in ra and ra["summary_emit"]["summary_state"] == "summarized"
    assert r.value == 0.5
    assert r.extras["null"] == pytest.approx(0.25) and r.extras["baseline"] == pytest.approx(1 / 3)
    assert r.extras["stir_pass_rate"] == 0.0 and r.extras["stir_null"] == 0.0
    assert r.extras["primary_gold_rate"] == 0.5 and r.extras["summary_count_rule_rate"] == 1.0
    assert "cell_gold_rate" not in r.extras and r.extras["summary"]["n_items_summarized"] == 4
    assert r.config["aggregate"] == "summary" and r.config["source_kind"] == "prose"
    pv = digest.prompt_version(bp.PROMPT_VERSION, _args(tmp_path, path), family=judge.NAME)
    assert r.config["prompt_version"] == pv
    assert pv.startswith(bp.PROMPT_VERSION + "+digest-v2-blind-extractive+")
    assert r.config["summarizer"] == digest.config(_args(tmp_path, path), "prose")["summarizer"]
    # the judge reads the summary-mode turn with the digest note
    user = next(u for n, _s, u in fake.calls if n == "brew_summary")
    assert digest.SUMMARY_NOTE in user and "4. ranked:" in user
    # n_expected / n_missing are the native, cell-level accounting
    FakeLLM.install(monkeypatch, lambda s, u, schema: {"named": [], "primary": "none"})
    native = judge.run(_args(tmp_path / "n", path, extra={"aggregate": "native"}))
    for k in ("n_expected_cells", "n_missing_cells"):
        assert r.counts[k] == native.counts[k]
    assert r.counts["n_expected_cells"] == 2 * 2 * 9


def test_native_mode_untouched_by_the_summary_path(tmp_path, monkeypatch):
    path = _readouts(tmp_path)

    def native_judge(system, user, schema):
        assert schema["name"] == "brew" and "digest" not in user
        return {"named": [], "primary": "none", "basis": "none"}

    fake = FakeLLM.install(monkeypatch, native_judge)
    r = judge.run(_args(tmp_path, path, extra={"aggregate": "native"}))
    assert fake.n("digest") == 0 and fake.n("brew") == 2 * 2 * 9
    assert r.config.get("aggregate") != "summary" and "cell_gold_rate" in r.extras
    assert r.config["prompt_version"] == bp.PROMPT_VERSION


def test_regions_all_adds_the_compute_unit(tmp_path, monkeypatch):
    path = _readouts(tmp_path, all_regions=True)
    fake = FakeLLM.install(monkeypatch, ranking_judge, digest=_all_lines)
    r = judge.run(_args(tmp_path, path, extra={"regions": "all"}))
    assert fake.n("digest") == 6 and fake.n("brew_summary") == 6
    row = {x["id"]: x for x in r.rows}[A["id"]]
    assert row["compute"]["named"] == [A["answer"]] and "summary_compute" in row
    assert r.extras["compute_pass_rate"] == 0.0 and r.extras["units"] == ["emit", "stir", "compute"]
    assert r.counts["n_expected_cells"] == 2 * 2 * 24


def test_invalid_ranked_is_retried_once_then_unjudged(tmp_path, monkeypatch):
    path = _readouts(tmp_path)
    seen: dict[str, int] = {}

    def flaky(system, user, schema):
        good = ranking_judge(system, user, schema)
        seen[user] = seen.get(user, 0) + 1
        if seen[user] == 1:  # first answer: ranked misses a named colour
            return {**good, "ranked": good["ranked"][:-1] + good["ranked"][:-1]}
        return good

    fake = FakeLLM.install(monkeypatch, flaky, digest=_all_lines)
    r = judge.run(_args(tmp_path, path))
    assert fake.n("brew_summary") == 8 and r.value == 0.5  # every unit re-asked once
    assert r.extras["n_ranked_invalid"] == 4 and r.extras["n_ranked_invalid_after_retry"] == 0

    def broken(system, user, schema):
        good = ranking_judge(system, user, schema)
        return {**good, "ranked": good["ranked"] * 2}  # duplicates, always

    fake = FakeLLM.install(monkeypatch, broken, digest=_all_lines)
    r = judge.run(_args(tmp_path / "b", path))
    assert fake.n("brew_summary") == 8  # one retry, no more
    assert all(x["pass"] is None and x["emit"]["state"] == "unjudged" for x in r.rows)
    assert r.counts["n_unjudged_cells"] == 2 and r.value is None  # items, by the emit unit
    assert r.extras["stir_pass_rate"] is None


def test_item_states_by_the_emit_unit(tmp_path, monkeypatch):
    a = _rows(A, first=A["intermediates"][0], second=A["intermediates"][0])
    for x in a:  # A: every emission cell blank -> empty -> a judged fail, no call
        if x["pos"] in _pos(A, judge.EMIT):
            x["samples"] = [""]
    b = _rows(B, first=B["intermediates"][0], second=B["intermediates"][0])
    path = _write(tmp_path / "r.jsonl", a + b)

    def dig(user):  # B's emit digest fails -> unsummarized -> undecided
        return None if "potion turns" in user else _all_lines(user)

    fake = FakeLLM.install(monkeypatch, ranking_judge, digest=dig)
    r = judge.run(_args(tmp_path, path))
    rows = {x["id"]: x for x in r.rows}
    assert rows[A["id"]]["pass"] is False and rows[A["id"]]["emit"]["state"] == "empty"
    assert rows[B["id"]]["pass"] is None and rows[B["id"]]["emit"]["state"] == "unsummarized"
    assert fake.n("brew_summary") == 2  # the two stir units only
    assert r.counts["n_empty_cells"] == 1 and r.counts["n_unjudged_cells"] == 1
    assert r.value == 0.0 and r.extras["stir_pass_rate"] == 0.0


def test_missing_emission_cell_undecides_and_stir_failure_only_drops_stir(tmp_path, monkeypatch):
    rows = _readouts(tmp_path).read_text().splitlines()
    gone = (A["id"], LAYERS[1], _pos(A, judge.EMIT)[0])
    keep = [x for x in rows if tuple(json.loads(x)[k] for k in ("id", "layer", "pos")) != gone]
    path = _write(tmp_path / "m.jsonl", [json.loads(x) for x in keep])

    def dig(user):  # every stir digest fails
        return None if "stir" in user else _all_lines(user)

    FakeLLM.install(monkeypatch, ranking_judge, digest=dig)
    with pytest.raises(SystemExit):
        judge.run(_args(tmp_path, path, layers=list(LAYERS)))
    r = judge.run(_args(tmp_path, path, layers=list(LAYERS), allow_missing=True))
    by = {x["id"]: x for x in r.rows}
    assert by[A["id"]]["pass"] is None and by[A["id"]]["emit"]["n_missing_cells"] == 1
    assert by[A["id"]]["emit"]["pass"] is True  # clears the rule, still undecided (as native)
    assert by[B["id"]]["pass"] is False and by[B["id"]]["stir"]["state"] == "unsummarized"
    assert r.counts["n_missing_cells"] == 1 and r.counts["n_unjudged_cells"] == 0
    assert r.extras["stir_pass_rate"] is None


def test_colour_screen_runs_on_the_digest(tmp_path, monkeypatch):
    rows = []
    for it in (A, B):
        for layer in LAYERS:
            for p in _pos(it, judge.EMIT + judge.STIR):
                rows.append({"id": it["id"], "layer": layer, "pos": p, "samples": [f"t {p}"]})
    path = _write(tmp_path / "r.jsonl", rows)
    fake = FakeLLM.install(monkeypatch, ranking_judge, digest=_all_lines)
    r = judge.run(_args(tmp_path, path))
    assert fake.n("brew_summary") == 0 and r.extras["n_screened_units"] == 4
    assert all(x["pass"] is False and x["emit"]["state"] == "screened" for x in r.rows)


def test_full_length_digest_is_never_cut(tmp_path, monkeypatch):
    """A full summ_max digest of mlnla2k-length samples (~600 chars each) reaches the judge
    whole: the native 4000-char cut is native-only."""
    rows = []
    gold = A["intermediates"][0]
    for layer in LAYERS:
        for p in _pos(A, judge.EMIT + judge.STIR):
            text = f"sample {layer} {p} the potion is {gold}. " + "the brew settles " * 45
            rows.append({"id": A["id"], "layer": layer, "pos": p, "samples": [text]})
    path = _write(tmp_path / "r.jsonl", rows)
    fake = FakeLLM.install(monkeypatch, ranking_judge, digest=_all_lines)
    r = judge.run(_args(tmp_path, path, items=[A["id"]], extra={"summ_max": "25"}))
    emit = r.rows[0]["summary_emit"]["summary"]
    assert len(emit) > 4000 and len(emit.splitlines()) == 6
    users = [u for n, _s, u in fake.calls if n == "brew_summary"]
    assert any(_readout(u) == emit for u in users) and r.rows[0]["pass"] is True


def test_dry_run_makes_no_calls(tmp_path, monkeypatch, capsys):
    path = _readouts(tmp_path)
    fake = FakeLLM.install(monkeypatch, ranking_judge, digest=_all_lines)
    args = _args(tmp_path, path)
    r = judge.run(type(args)(**{**args.__dict__, "dry_run": True}))
    assert not fake.calls and r.value is None
    assert "digest prompt" in capsys.readouterr().out
