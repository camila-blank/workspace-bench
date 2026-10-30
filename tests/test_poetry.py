"""poetry (plan 0038): its own blind MC judge with rhyme distractors, deterministic line-ending
gates and the dominance veto, in native and summary mode; bank v2 (Qwen re-gate + Opus-fails
gate); the line_one_tail read site."""

import importlib.util
import json
import re
from pathlib import Path

import pytest
from conftest import native_args, write_jsonl, write_sidecar
from summary_fakes import FakeLLM, summary_args

from wsbench import digest, mc, readplan, reads, registry
from wsbench.banks import item_targets, load_bank
from wsbench.cli import main
from wsbench.evals.poetry import judge as pj
from wsbench.evals.poetry import options as po
from wsbench.evals.poetry import prompts as pp
from wsbench.evals.poetry import score
from wsbench.judge_config import JudgeConfig, resolve
from wsbench.readouts import Cell
from wsbench.results import read_results

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples/readouts/poetry.jsonl"
GATE = REPO / "scripts/poetry/opus_gate.py"
LINE1 = "The captain pointed at the route ahead,"
PROMPT = f"A rhyming couplet:\n{LINE1}\nAnd told his crew to follow where he'd "
ITEM = {
    "name": "couplet-ahead-head",
    "prompt": PROMPT,
    "intermediates": ["led"],
    "eval_render": "plain",
    "readout": {"kind": "line_one_tail"},
    "line_one_end": "ahead",
    "qwen_commits": {"led": 21, "lead": 1},
    "mc": {"candidates": ["said", "dread", "red", "instead", "bed"], "tiers": {}},
}


def _spec():
    from wsbench.evals.poetry import SPEC

    registry.FAMILIES.setdefault(SPEC.name, SPEC)
    return SPEC


@pytest.fixture
def bank(tmp_path, monkeypatch):
    """A one-item v2-shaped bank through WSB_BANK_FILE_POETRY (the real bank's contents move)."""
    _spec()
    p = tmp_path / "items_v2.json"
    p.write_text(json.dumps({"family": "poetry", "version": 2, "items": [dict(ITEM)]}))
    monkeypatch.setenv("WSB_BANK_FILE_POETRY", str(p))
    return p


def _rows(texts: dict) -> list[dict]:
    out = []
    for (layer, pos), v in texts.items():
        row = {"id": "couplet-ahead-head", "layer": layer, "pos": pos}
        if isinstance(v, dict):
            row.update(v)
        else:
            row["samples"] = v
        out.append(row)
    return out


def _letter(user: str, word: str) -> str:
    m = re.search(rf"^([A-F])\. {re.escape(word)}$", user, flags=re.M)
    assert m, (word, user[-400:])
    return m.group(1)


def _picker(word: str, quote: str):
    def judge(system, user, schema):
        if word == "CANNOT":
            return {"choice": _letter(user, pp.CANNOT), "quote": ""}
        return {"choice": _letter(user, word), "quote": quote}

    return judge


def _args(f: Path, out: Path, extra: dict, layers=None):
    return native_args(
        readouts=f,
        out=out,
        judge=resolve(JudgeConfig(prompt_version=pp.PROMPT_VERSION)),
        layers=layers,
        items=None,
        limit=0,
        allow_missing=True,
        concurrency=1,
        rpm=1.0,
        dry_run=False,
        extra=extra,
    )


def _native(tmp_path: Path, rows, extra=None, layers=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    f = write_jsonl(tmp_path / "poetry.jsonl", rows)
    return _args(f, tmp_path / "out", extra or {}, layers)


def _summary(tmp_path: Path, rows, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    f = write_jsonl(tmp_path / "poetry.jsonl", rows)
    return summary_args(f, tmp_path / "out", pp.PROMPT_VERSION, allow_missing=True, **kw)


# ---------------------------------------------------------------- bank, spec, README


def test_bank_and_targets():
    header, items = load_bank(registry.bank_path("poetry"))
    assert registry.bank_path("poetry") == REPO / "evals/poetry/items_v2.json"
    assert header["family"] == "poetry" and header["version"] == 2
    assert len(items) == header["n_items"]
    first = next(it for it in items if it["name"] == "couplet-ahead-head")
    assert item_targets(first) == ["led"]


def test_registered_spec_and_readme():
    spec = _spec()
    assert spec.group == "basic" and spec.judge.prompt_version == "poetry-mc-2026-10-31"
    assert spec.metric == "pass_rate" and spec.not_in_macro == "" and not spec.targets_judge
    assert spec.min_full_positions == 2 and spec.full_contiguous and spec.task_note
    readme = (REPO / "evals/poetry/README.md").read_text(encoding="utf-8")
    assert pp.SYSTEM in readme and pp.PROMPT_VERSION in readme


def test_dry_run_on_the_toy_file(tmp_path, capsys, monkeypatch):
    _spec()
    monkeypatch.delenv("WSBENCH_JUDGE_MODEL", raising=False)
    out = tmp_path / "out"
    argv = ["judge", "opts=aggregate=native", "family=poetry", f"readouts={EXAMPLE}",
            f"out={out}", "layers=20,36", "dry_run=True"]  # fmt: skip
    assert main(argv) == 0
    r = read_results(out)
    n = len(load_bank(registry.bank_path("poetry"))[1])
    assert r.value is None and r.extras["n_items_without_readouts"] == n - 2
    printed = capsys.readouterr().out
    assert "judge prompt for" in printed and pp.CANNOT in printed


# ---------------------------------------------------------------- P1: the MC judge


def test_mc_prompt_is_blind(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 14): ["- The road ahead\n- Follow where he led"], (20, 16): ["x"]})
    fake = FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    pj.run_family(_native(tmp_path, rows))
    ((_n, system, user),) = fake.calls
    for leak in (PROMPT, LINE1, "route ahead", "follow where he'd", "rhyming couplet:\n"):
        assert leak not in system and leak not in user
    assert len([ln for ln in user.splitlines() if re.fullmatch(r"[A-F]\. led", ln)]) == 1
    assert "target" not in user.lower() and "gold" not in user.lower()


def test_cannot_tell_is_last_option():
    _h, items = load_bank(registry.bank_path("poetry"))
    for it in items:
        opts, g = po.option_set(it)
        assert opts[-1] == pp.CANNOT and mc.is_cannot(opts[-1])
        assert len(opts) == 6 and opts[g] == po.gold_of(it).lower()


def test_cannot_pick_is_never_a_pass(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 16): ["And told his crew to follow where he led\n"]})
    FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    r = pj.run_family(_native(tmp_path, rows))
    assert r.rows[0]["pass"] is False and r.rows[0]["kind"] == "cannot"


def test_token_note_is_spec_rendered(bank, tmp_path, monkeypatch):
    note = mc.token_note(_spec().flat_types, multi_select=False)
    rows = _rows({(20, 16): {"tokens": ["led", "sky", "night"], "scores": [3.0, 2.0, 1.0]}})
    fake = FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    pj.run_family(_native(tmp_path / "a", rows))
    user = fake.calls[-1][2]
    assert user.count(note) == 1 and mc.TOKEN_NOTE not in user
    fake2 = FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    pj.run_family(_summary(tmp_path / "b", rows))
    user = [c for c in fake2.calls if c[0] != "digest"][-1][2]
    assert user.count(note) == 1 and mc.TOKEN_NOTE not in user


# ---------------------------------------------------------------- P2: distractors


def test_options_rhyme_and_exclude_gold():
    from wsbench.adapt.rhyme import rhymes

    _h, items = load_bank(registry.bank_path("poetry"))
    for it in items:
        gold, end = po.gold_of(it).lower(), po.line_one_end(it)
        dis = po.distractors(it)
        assert len(dis) == 4, it["id"]
        for d in dis:
            assert rhymes(d, gold) and rhymes(d, end), (it["id"], d)
            assert d != gold and d != end.lower()
            assert not po.homophone(d, gold) and not po.inflection(d, gold)
            assert d not in po.committed(it), (it["id"], d)
            assert po.candidate_ok(it, d) == ""


def test_option_set_skips_adapted_gold():
    before = po.distractors(ITEM)
    adapted = {**ITEM, "intermediates": ["dread"], "qwen_commits": {"dread": 10}}
    after = po.distractors(adapted)
    assert "dread" in before and "dread" not in after
    assert after == ["said", "red", "instead", "bed"]


def test_options_deterministic():
    assert po.option_set(ITEM) == po.option_set(dict(ITEM))
    _h, items = load_bank(registry.bank_path("poetry"))
    assert [po.option_set(i) for i in items] == [po.option_set(i) for i in items]


def test_candidate_filters():
    assert po.homophone("lead", "led") and po.inflection("stars", "star")
    assert po.inflection("making", "make") and not po.inflection("said", "led")
    assert po.candidate_ok(ITEM, "lead") and po.candidate_ok(ITEM, "led")
    assert po.candidate_ok(ITEM, "sky") and po.candidate_ok(ITEM, "ahead")
    assert po.candidate_ok(ITEM, "said") == ""


# ---------------------------------------------------------------- P3: the Opus-fails gate


def _gate_module():
    spec = importlib.util.spec_from_file_location("opus_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bank_v2_opus_gate_recorded():
    from wsbench.adapt.gate import fold

    header, items = load_bank(registry.bank_path("poetry"))
    args = native_args(readouts=Path("x"), out=Path("y"), judge=None, layers=None, items=None,
                       limit=0, allow_missing=False, concurrency=1, rpm=1.0, dry_run=False,
                       extra={"aggregate": "summary"})  # fmt: skip
    want = reads.strip_reads(digest.prompt_version(pp.PROMPT_VERSION, args, "poetry"))
    for it in items:
        rec = it["prompt_only_opus"]
        assert rec["draws"] == 3 and rec["picked_gold"] == 0, it["id"]
        assert rec["instrument"] == want, it["id"]
        assert fold(po.gold_of(it)) in {fold(w) for w in it["qwen_commits"]}
    assert header["opus_gate"]["n_candidates"] >= header["n_items"]


def test_opus_gate_uses_shared_protocol(tmp_path):
    from wsbench.baselines import prompt_only_opus as pop

    g = _gate_module()
    prompts = [{"family": "poetry", "id": "couplet-ahead-head", "prefix": f"A rhyming couplet:\n"
                f"{LINE1}\n", "token": "\n", "pos": 16, "n_tokens": 27}]  # fmt: skip
    write_jsonl(tmp_path / "prompts.jsonl", prompts)
    (tmp_path / "answers").mkdir()
    for d in range(3):
        write_jsonl(tmp_path / "answers" / f"gate{d}.jsonl", [
            {"family": "poetry", "id": "couplet-ahead-head", "draw": d, "answer": "said",
             "error": "", "tag": f"gate{d}"}])  # fmt: skip
    g.write_readouts(tmp_path, "gate")
    for d in range(3):
        f = tmp_path / "readouts" / f"gate{d}" / "poetry.jsonl"
        (row,) = [json.loads(x) for x in f.read_text().splitlines()]
        assert row["pos"] == 16 and row["layer"] == pop.PROMPT_ONLY_LAYER == 44
        meta = json.loads((f.parent / "poetry.jsonl.meta.json").read_text())
        assert meta["reader"].startswith("prompt_only:") and meta["sampling"] == "prose1"
        assert meta["protocol"] == pop.PROTOCOL and meta["draw"] == f"gate{d}"
    assert g.FLOOR_TAG not in g.tags("gate")  # never the po-opus-v1 floor draw (draw0)
    with pytest.raises(ValueError):
        g.tags("draw")


def test_opus_gate_rejudged_on_pv_change(bank, tmp_path, monkeypatch):
    """R4c: cached gate readouts are re-judged (never re-asked), ``instrument`` rewritten, and an
    item now picked is REPORTED, never dropped (``--dry-run`` leaves the bank file alone)."""
    from wsbench.baselines import prompt_only_opus as pop

    g = _gate_module()
    raw = json.loads(bank.read_text())
    raw["items"][0]["prompt_only_opus"] = {"draws": 3, "picked_gold": 0, "instrument": "old-pv"}
    bank.write_text(json.dumps(raw))
    d0 = tmp_path / "gate"
    d0.mkdir()
    prompts = [{"family": "poetry", "id": "couplet-ahead-head", "prefix": "x\n", "token": "\n",
                "pos": 16, "n_tokens": 27}]  # fmt: skip
    write_jsonl(d0 / "prompts.jsonl", prompts)
    (d0 / "answers").mkdir()
    for d in range(3):
        write_jsonl(d0 / "answers" / f"gate{d}.jsonl", [
            {"family": "poetry", "id": "couplet-ahead-head", "draw": d, "error": "",
             "answer": "A natural line: 'And told his crew to follow where he led.'"}])  # fmt: skip
    g.write_readouts(d0, "gate")

    def no_ask():
        raise AssertionError("rejudge must never ask")

    monkeypatch.setattr(pop, "client", no_ask)
    FakeLLM.install(monkeypatch, judge=_picker("led", "follow where he led"))
    rep = g.rejudge(d0, bank, dry_run=True)
    assert [x["id"] for x in rep["now_picked"]] == ["couplet-ahead-head"]
    assert rep["instrument"].startswith(pp.PROMPT_VERSION) and rep["n_items"] == 1
    assert json.loads(bank.read_text())["items"][0]["prompt_only_opus"]["instrument"] == "old-pv"
    g.rejudge(d0, bank, dry_run=False)
    after = json.loads(bank.read_text())["items"]
    assert len(after) == 1 and after[0]["prompt_only_opus"]["picked_gold"] == 3
    assert after[0]["prompt_only_opus"]["instrument"] == rep["instrument"]


# ---------------------------------------------------------------- P4: the gates


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("The stars shine bright above his head", "shine"),
        ("The stars shone down in skies of blue", "down"),
        ("But lost his way, his heart was sore", "way"),
        ("Each step he takes, a monster's curse he sought", "took"),
        ("He screamed in his bed, a terrible, burning song.", "bed"),
        ("Follow my lead", "led"),
        ("The kite flew high", "fly"),
        ("His powerful roar would shake the shore.", "roar"),
        ("Then swept the dust from floor to door", "floor"),
    ],
)
def test_gate_rejects_incidental_mentions(text, word):
    assert not score.prose_ok(word, text, [])


@pytest.mark.parametrize(
    ("text", "word", "others"),
    [
        ("But she collapsed under the weight of her case\n", "case", []),
        ("So he could comb his tangled hair.", "hair", []),
        ("The world, she vowed, would never read her name.", "name", []),
        ("Rhyme: King / Ring", "ring", ["sing", "wing"]),
        ('A plausible line would be "And golden light announced the coming day."', "day", []),
    ],
)
def test_gate_accepts_line_endings(text, word, others):
    assert score.prose_ok(word, text, others)


def test_named_plan_names_no_other_option():
    t = 'Likely options are "black", "back", "track" or "attack" to rhyme with crack.'
    assert not score.named_plan("black", t, ["back", "stack"])
    assert score.named_plan("black", "The line will end with black.", ["back"])
    assert not score.named_plan("black", "The sky was black and grey", ["back"])  # no plan cue


def test_token_rule():
    toks = ["Earth", "celestial", "planets", "orbit", "stars", "And"]
    assert not score.token_ok("star", toks, ["far", "car"], "scar")


def test_token_rule_ignores_rank():
    assert not score.token_ok("star", ["star", "far", "car", "bar", "jar", "x"], [], "scar")
    assert not score.token_ok("star", ["star", "far", "night"], ["far"], "scar")


def test_token_lone_option_is_singled_out():
    base = ["star", "night", "sky", "moon", "dark"]
    assert score.token_ok("star", base, ["jar", "bar"], "scar")
    assert not score.token_ok("star", [*base, "far", "car"], ["jar", "bar"], "scar")


def test_dominance_veto(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 16): ["- To follow where he led\n- in words that he said"]})
    FakeLLM.install(monkeypatch, judge=_picker("led", "follow where he led"))
    r = pj.run_family(_native(tmp_path, rows, extra={"sampling": "bullet4"}))
    assert r.rows[0]["kind"] == "vetoed_tie" and r.rows[0]["pass"] is False


def test_prose1_generation_counts_once():
    text = "- Then follow where he led\n- the words the captain said\n- And on they went, he led"
    words = ["led", "said", "dread", "red", "instead"]
    one = [Cell("couplet-ahead-head", 20, 16, (text,), None, None)]
    n1 = score.counts(words, one, "ahead")
    assert n1["led"] == 1 == n1["said"]  # prose1: one generation = one sample
    four = [Cell("couplet-ahead-head", 20, 16, tuple(text.split("\n")), None, None)]
    n4 = score.counts(words, four, "ahead")
    assert n4["led"] == 2 and n4["said"] == 1  # bullet4: the bullets are separate samples


def test_bullet4_split_passes_the_veto(bank, tmp_path, monkeypatch):
    text = "- Then follow where he led\n- the words the captain said\n- And on they went, he led"
    rows = _rows({(20, 16): [text]})
    FakeLLM.install(monkeypatch, judge=_picker("led", "follow where he led"))
    r1 = pj.run_family(_native(tmp_path / "a", rows, extra={"sampling": "prose1"}))
    r4 = pj.run_family(_native(tmp_path / "b", rows, extra={"sampling": "bullet4"}))
    assert r1.rows[0]["kind"] == "vetoed_tie" and r4.rows[0]["kind"] == "correct"


def test_quote_goes_through_quote_ok(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 16): ["And told his crew to follow where he led\n"]})
    FakeLLM.install(monkeypatch, judge=_picker("led", "follow where he led"))
    ok = pj.run_family(_native(tmp_path / "a", rows))
    assert ok.rows[0]["kind"] == "correct" and ok.rows[0]["pass"] is True
    monkeypatch.setattr(digest, "quote_ok", lambda q, lines: False)
    r = pj.run_family(_native(tmp_path / "b", rows))
    assert r.rows[0]["kind"] == "quote_failed" and r.rows[0]["pass"] is False


def test_translation_and_inflection_rejected(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 16): ["他们跟着他走, 他领导了\n"]})
    FakeLLM.install(monkeypatch, judge=_picker("led", "领导"))
    assert pj.run_family(_native(tmp_path / "a", rows)).rows[0]["kind"] == "wrong_form"
    rows = _rows({(20, 16): ["And told his crew to follow where he leads\n"]})
    FakeLLM.install(monkeypatch, judge=_picker("led", "where he leads"))
    assert pj.run_family(_native(tmp_path / "b", rows)).rows[0]["kind"] == "wrong_form"
    assert not score.exact_form("希望", "hope") and not score.exact_form("made", "make")


# ---------------------------------------------------------------- P5: the summarizer


def test_summary_is_default_and_gets_all_cells(bank, tmp_path, monkeypatch):
    rows = _rows({(L, p): [f"sample L{L} p{p}"] for L in readplan.GRID for p in (14, 15, 16)})
    fake = FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    f = write_jsonl(tmp_path / "poetry.jsonl", rows)
    args = _args(f, tmp_path / "out", {})
    args.extra.pop("aggregate")  # the library default
    r = pj.run_family(args)
    (dig,) = [c for c in fake.calls if c[0] == "digest"]
    assert len(re.findall(r"^\[L\d+ pos \d+\]$", dig[2], flags=re.M)) == 33
    assert r.config["aggregate"] == "summary"
    assert pp.TASK_NOTE in dig[1]


def _digest_of(*evidence: str):
    def dig(user):
        return {"items": [{"statement": "s", "where": "", "evidence": e} for e in evidence]}

    return dig


def test_summary_gate_uses_raw_newlines(bank, tmp_path, monkeypatch):
    # the bundle line flattens "...he led\nand the crew..." to one line; the raw sample keeps \n
    rows = _rows({(20, 16): ["To follow where he led\nand the crew sailed on"]})
    FakeLLM.install(monkeypatch, judge=_picker("led", "follow where he led"),
                    digest=_digest_of("To follow where he led and the crew sailed on"))  # fmt: skip
    r = pj.run_family(_summary(tmp_path, rows))
    assert r.rows[0]["kind"] == "correct" and r.rows[0]["pass"] is True


def test_summary_raw_map_uses_native_lines():
    raw = "- The road ahead\n- Follow my lead"
    cells = [Cell("couplet-ahead-head", 20, 16, (raw,), None, None)]
    (ln,) = digest.native_lines(cells)
    assert ln.text == f"The road ahead{digest.BULLET_MARK}Follow my lead"
    assert score.line_final("lead", raw)  # a bullet end
    ((_nl, got),) = score.evidence_samples("Follow my lead", [ln], cells)
    assert got == raw
    orphan = digest.Line("Follow my lead", None, "[L24 pos 16]", 24, 16)
    assert score.evidence_samples("Follow my lead", [orphan], cells) == []


def test_summary_rejects_incidental(bank, tmp_path, monkeypatch):
    text = "He screamed in his bed, a terrible, burning song."
    item = {**ITEM, "intermediates": ["bed"], "line_one_end": "head", "qwen_commits": {"bed": 20},
            "mc": {"candidates": ["said", "dread", "red", "led", "instead"]}}  # fmt: skip
    bank.write_text(json.dumps({"family": "poetry", "version": 2, "items": [item]}))
    rows = _rows({(20, 16): [text]})
    FakeLLM.install(monkeypatch, judge=_picker("bed", "He screamed in his bed"),
                    digest=_digest_of(text))  # fmt: skip
    r = pj.run_family(_summary(tmp_path, rows))
    assert r.rows[0]["kind"] == "not_line_final" and r.rows[0]["pass"] is False


def test_summary_flat_rhyme_list_not_credited(bank, tmp_path, monkeypatch):
    item = {**ITEM, "intermediates": ["star"], "line_one_end": "scar", "qwen_commits": {"star": 20},
            "mc": {"candidates": ["jar", "tar", "cigar", "guitar", "bizarre"]}}  # fmt: skip
    bank.write_text(json.dumps({"family": "poetry", "version": 2, "items": [item]}))
    rows = _rows({(20, 16): {"tokens": ["far", "car", "star", "bar"]}})
    FakeLLM.install(monkeypatch, judge=_picker("star", "star"),
                    digest=_digest_of("far | car | star | bar"))  # fmt: skip
    r = pj.run_family(_summary(tmp_path, rows))
    assert r.rows[0]["kind"] == "not_line_final" and r.rows[0]["pass"] is False


def test_summary_stamp_is_computed(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 16): ["x"]})
    FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    args = _summary(tmp_path, rows)
    r = pj.run_family(args)
    want = reads.stamp(digest.prompt_version(pp.PROMPT_VERSION, args, "poetry"), "full")
    assert r.config["prompt_version"] == want


# ---------------------------------------------------------------- P6: strictness


@pytest.mark.parametrize("kind", [k for k in pj.KINDS if k != "correct"])
def test_strict_kinds_fail(kind, bank, tmp_path, monkeypatch):
    texts = {
        "distractor": ("said", "the words he said", ["the words he said\n"]),
        "cannot": ("CANNOT", "", ["x"]),
        "invalid": ("BAD", "", ["x"]),
        "quote_failed": ("led", "not in the readout", ["where he led\n"]),
        "wrong_form": ("led", "where he leads", ["where he leads\n"]),
        "not_line_final": ("led", "he led", ["he led the crew home"]),
        "vetoed_tie": ("led", "where he led", ["- where he led\n- what he said"]),
        "empty": ("led", "", ["  "]),
    }
    word, quote, samples = texts[kind]
    rows = _rows({(20, 16): samples})
    if word == "BAD":
        FakeLLM.install(monkeypatch, judge=lambda s, u, sc: {"choice": "Z", "quote": ""})
    else:
        FakeLLM.install(monkeypatch, judge=_picker(word, quote))
    extra = {"sampling": "bullet4"} if kind == "vetoed_tie" else {}
    r = pj.run_family(_native(tmp_path, rows, extra=extra))
    assert r.rows[0]["kind"] == kind and r.rows[0]["pass"] is False


def test_native_extras_have_layer_pick_columns(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 16): ["where he led\n"], (24, 16): ["what he said\n"]})

    def judge(system, user, schema):
        w = "led" if "where he led" in user else "said"
        return {"choice": _letter(user, w), "quote": f"he {w}"}

    FakeLLM.install(monkeypatch, judge=judge)
    r = pj.run_family(_native(tmp_path, rows))
    assert r.extras["n_items_gold_any_layer"] == 1
    assert r.extras["n_items_distractor_any_layer"] == 1
    assert r.extras["n_items_distractor_majority"] == 0
    assert r.rows[0]["kind"] == "vetoed_tie"  # led 1, said 1 over the item's loaded cells


# ---------------------------------------------------------------- P7: reads, layers, prompt-only


def test_arbitrary_layers(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 15): ["a"], (20, 16): ["b"], (63, 15): ["c"], (63, 16): ["d"]})
    fake = FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    r = pj.run_family(_summary(tmp_path, rows, layers=[63]))
    (dig,) = [c[2] for c in fake.calls if c[0] == "digest"]
    assert "[L63 pos 15]" in dig and "[L20" not in dig
    assert r.config["layers_judged"] == [63] and r.rows[0]["pass"] is False


def test_lite_reads_the_line_one_newline(bank, tmp_path, monkeypatch):
    rows = _rows({(20, 14): ["a"], (20, 15): ["b"], (20, 16): ["c"]})
    fake = FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    r = pj.run_family(_summary(tmp_path, rows, extra={"reads": "lite"}))
    (dig,) = [c[2] for c in fake.calls if c[0] == "digest"]
    assert "[L20 pos 16]" in dig and "pos 14" not in dig and "pos 15" not in dig
    assert r.config["prompt_version"].endswith("+reads-lite")


def test_all_layer_producer_file_is_judged(bank, tmp_path, monkeypatch):
    """Late critique fix (1), held for poetry: ``plan_sha`` never hashes layers, so a file read
    at all 64 layers (sidecar plan_sha = the GRID plan's) is judged."""
    rows = _rows({(L, p): ["x"] for L in range(64) for p in (14, 15, 16)})
    f = write_jsonl(tmp_path / "poetry.jsonl", rows)
    write_sidecar(f, layers=list(range(64)), plan_sha=reads.plan_sha("poetry"))
    FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    r = pj.run_family(summary_args(f, tmp_path / "out", pp.PROMPT_VERSION))
    assert r.rows[0]["kind"] == "cannot" and r.config["reads_checked"] == "sidecar"


def test_prompt_only_rows_use_same_instrument(bank, tmp_path, monkeypatch):
    from wsbench.baselines import prompt_only_opus as pop

    lens = _rows({(20, 14): ["a"], (20, 15): ["b"], (20, 16): ["c"]})
    po_rows = [{"id": "couplet-ahead-head", "layer": 44, "pos": 16, "samples": ["opus text"]}]
    FakeLLM.install(monkeypatch, judge=_picker("CANNOT", ""))
    r_lens = pj.run_family(_summary(tmp_path / "a", lens))
    (tmp_path / "b").mkdir()
    f = write_jsonl(tmp_path / "b" / "poetry.jsonl", po_rows)
    write_sidecar(f, reader=pop.READER, layers=[44], protocol=pop.PROTOCOL)
    r_po = pj.run_family(summary_args(f, tmp_path / "b" / "out", pp.PROMPT_VERSION))
    assert reads.strip_reads(r_po.config["prompt_version"]) == reads.strip_reads(
        r_lens.config["prompt_version"]
    )
    assert r_po.config["reads_checked"] == "prompt_only" and r_po.rows[0]["kind"] == "cannot"


def test_poetry_plan_emits_full_and_lite():
    rows = readplan.plan("poetry")
    assert rows and all(r.positions["kind"] == "line_one_tail" for r in rows)
    assert all(r.lite_positions == {"kind": "final_token"} and r.lite_layers is None for r in rows)
    assert all(r.layers == readplan.GRID for r in rows)


def test_readme_counts_match_the_bank():
    _header, items = load_bank(registry.bank_path("poetry"))
    readme = (REPO / "evals/poetry/README.md").read_text(encoding="utf-8")
    assert f"{len(items)} items" in readme


def test_packed_bullet4_keeps_verse_line_breaks():
    """plan 0038: a cached packed x4 cell ("- g1\\n- g2...") splits into 4 generations, each
    keeping its own line breaks, so ``line_final`` still sees where a verse line ends."""
    from wsbench.readouts import normalize

    packed = "- Her voice rang out like a trumpet's call.\nThe students listened\n- b\n- c\n- d"
    (cell,), _d = normalize([Cell("x", 20, 16, (packed,), None, None)], "bullet4")
    assert cell.samples[0] == "Her voice rang out like a trumpet's call.\nThe students listened"
    assert score.line_final("call", cell.samples[0])
    (ln,) = digest.native_lines([Cell("x", 20, 16, (cell.samples[0],), None, None)])
    assert "\n" not in ln.text  # the bundle line is still one line
