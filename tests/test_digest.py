"""digest (summary mode): bundle, cap, decoder, evidence gate, renders, item states, cache."""

from pathlib import Path

import pytest

from wsbench import digest
from wsbench.cache import Cache
from wsbench.judge_config import JudgeConfig, resolve
from wsbench.llm import Spend
from wsbench.readouts import Cell
from wsbench.registry import JudgeArgs


def _args(tmp_path: Path, **extra: str) -> JudgeArgs:
    return JudgeArgs(
        readouts=tmp_path / "r.jsonl",
        out=tmp_path / "out",
        judge=resolve(JudgeConfig(prompt_version="t")),
        layers=None,
        items=None,
        limit=0,
        allow_missing=False,
        concurrency=1,
        rpm=1.0,
        dry_run=False,
        extra={"aggregate": "summary", **extra},
    )


def _prose(i: str, layer: int, pos: int, *samples: str, token: str | None = None) -> Cell:
    return Cell(i, layer, pos, tuple(samples), None, None, token)


def _toks(i: str, layer: int, pos: int, *tokens: str) -> Cell:
    return Cell(i, layer, pos, None, tuple(tokens), None, None)


# ---------------------------------------------------------------- bundle


def test_bundle_order_headers_and_no_token_text():
    cells = [
        _prose("a", 44, 3, "second\nline", token="SECRET"),
        _prose("a", 20, 7, "first"),
        _prose("a", 20, 2, "zeroth", "  "),
    ]
    text, lines, truncated = digest.bundle(cells, 10_000)
    assert text == "[L20 pos 2]\n- zeroth\n[L20 pos 7]\n- first\n[L44 pos 3]\n- second line"
    assert "SECRET" not in text and not truncated
    assert [ln.text for ln in lines] == ["zeroth", "first", "second line"]


MOJIBAKE = "".join(digest._bytes_to_unicode()[b] for b in "哪一个".encode())


def test_decode_bpe():
    assert digest.decode_bpe(MOJIBAKE) == "哪一个"
    assert digest.decode_bpe("Ġathletic") == " athletic"
    assert digest.decode_bpe("▁word") == " word"
    assert digest.decode_bpe("café") == "café"  # already decoded text is kept


def test_token_line_is_decoded_and_scoreless():
    cell = Cell("a", 20, 1, None, ("Ġfence", "Ġpost", MOJIBAKE), (3.0, 2.0, 1.0), None)
    text, lines, _ = digest.bundle([cell], 10_000)
    assert text == "[L20 pos 1]\n- fence | post | 哪一个"
    assert lines[0].pieces == ("fence", "post", "哪一个")


def test_native_token_render_when_decode_off():
    cell = Cell("a", 20, 1, None, ("Ġfence", MOJIBAKE), None, None)
    text, _lines, _ = digest.bundle([cell], 10_000, decode=False)
    assert text == f"[L20 pos 1]\n- fence | {MOJIBAKE}"


def test_est_tokens_is_script_aware():
    assert digest.est_tokens("abcd" * 10) == 10
    assert digest.est_tokens("哪一个") == 3


def test_cap_round_robin_then_stride_keeps_every_layer():
    cells = [_prose("a", L, p, "x" * 40, "y" * 40) for L in (20, 24) for p in range(4)]
    _text, lines, truncated = digest.bundle(cells, 130)
    assert truncated
    assert sum(ln.text.startswith("x") for ln in lines) == 8  # every cell keeps sample 1
    assert any(ln.text.startswith("y") for ln in lines)
    _text, lines, truncated = digest.bundle(cells, 40)
    text = _text
    assert truncated and "[L20 pos 0]" in text and "[L24 pos 0]" in text
    assert len(lines) < 8


# ---------------------------------------------------------------- evidence


def _lines(*cells: Cell) -> list[digest.Line]:
    return digest.bundle(list(cells), 10_000)[1]


def test_evidence_verbatim_whitespace_case_and_across_samples():
    lines = _lines(_prose("a", 20, 1, "The Iron Gate opened", "and then it closed"))
    assert digest.evidence_line("iron   gate", lines).text == "The Iron Gate opened"
    assert digest.evidence_line("opened and then", lines) is None
    assert digest.evidence_line("", lines) is None


def test_evidence_token_pieces_never_join():
    lines = _lines(_toks("a", 20, 1, "Ġfence", "Ġpost", "Ġgate"))
    assert digest.evidence_line("fence post", lines) is None
    assert digest.evidence_line("post", lines) is not None
    assert digest.evidence_line("fence | post", lines) is not None
    assert digest.evidence_line("post | fence", lines) is None


def _result(*items: tuple[str, str, str]) -> dict:
    return {"items": [{"statement": s, "where": w, "evidence": e} for s, w, e in items]}


def test_evidence_resolution_prefers_named_group_and_exact_match():
    lines = _lines(
        _prose("a", 20, 1, "France or Spain, hard to say"),
        _prose("a", 50, 1, "the answer is France"),
        _toks("a", 24, 1, "Ġcatalog", "Ġx"),
        _toks("a", 52, 1, "Ġcat", "Ġy"),
    )
    assert digest.evidence_line("France", lines, "[L50 pos 1]").text == "the answer is France"
    assert digest.evidence_line("France", lines).text.startswith("France or Spain")
    assert digest.evidence_line("cat", lines).text == "cat | y"  # whole token beats catalog
    assert digest.evidence_line("answer is france", lines).text == "the answer is France"


def test_support_is_counted_mechanically():
    cells = [_prose("a", L, 1, "boxing match") for L in (20, 24, 28, 32)] + [
        _prose("a", 36, 1, "chess")
    ]
    lines = _lines(*cells)
    assert digest.support_of("boxing", lines) == "several"
    assert digest.support_of("chess", lines) == "one"
    many = _lines(*[_prose("a", 20, p, "boxing") for p in range(12)])
    assert digest.support_of("boxing", many) == "many"


def test_token_note_only_for_token_sources(tmp_path):
    a = _args(tmp_path)
    assert digest.SUMMARY_NOTE_TOKENS not in digest.summary_note(a, "prose")
    assert digest.summary_note(a, "tokens").endswith(digest.SUMMARY_NOTE_TOKENS)


def test_ground_and_renders():
    lines = _lines(_prose("a", 20, 1, "a large orange cat sat", "a dog barked"))
    kept, dropped = digest.ground(
        _result(
            ("a tiger", "many", "large orange cat"),
            ("a cat", "few", "orange cat"),  # same sample: one extractive line
            ("a wolf", "one", "wolf"),  # not in any sample: dropped
        ),
        lines,
        10,
    )
    assert dropped == 1 and len(kept) == 2
    text, evid = digest.render_summary(kept, "extractive")
    assert text == "[one] a large orange cat sat"
    assert "tiger" not in text and [ln.text for ln in evid] == ["a large orange cat sat"]
    text, _ = digest.render_summary(kept, "abstractive")
    assert text.splitlines()[0] == ('- a tiger [support: one] — evidence: "a large orange cat sat"')


# ---------------------------------------------------------------- collapse


@pytest.fixture
def scripted(monkeypatch):
    seen: list[tuple[str, str]] = []
    replies: dict[str, object] = {}

    def fake_stream_json(prompts, *, on_result, **_kw):
        for i, (system, user) in enumerate(prompts):
            seen.append((system, user))
            key = next((k for k in replies if k in user), None)
            on_result(i, replies.get(key) if key else None)
        return Spend()

    monkeypatch.setattr(digest.llm, "stream_json", fake_stream_json)
    return seen, replies


def test_collapse_item_states(tmp_path, scripted):
    seen, replies = scripted
    replies["alpha-sample"] = _result(("alpha", "", "alpha-sample"))
    replies["beta-sample"] = _result(("made up", "", "not there"))  # every item dropped
    replies["gamma-sample"] = {"items": []}  # zero items: empty
    by_item = {
        "a": [_prose("a", 20, 5, "alpha-sample", token="'s")],
        "b": [_prose("b", 20, 1, "beta-sample")],
        "c": [_prose("c", 20, 1, "gamma-sample")],
        "d": [_prose("d", 20, 1, "delta-sample")],  # failed call
        "e": [_prose("e", 20, 1, "   ")],  # blank: empty, no call
    }
    args = _args(tmp_path)
    with Cache(tmp_path / "c.jsonl") as cache:
        col = digest.collapse(
            "fam", by_item, args=args, cache=cache, spend=Spend(), preflight=None, task_note="t"
        )
    assert len(seen) == 4
    assert col.cells["a"].samples == ("[one] alpha-sample",)
    assert (col.cells["a"].layer, col.cells["a"].pos, col.cells["a"].token) == (-1, 5, "'s")
    assert col.unsummarized == {"b", "d"} and col.empty == {"c", "e"}
    for i in "bcde":
        assert col.cells[i].samples == ("",)
    assert digest.gate_texts(col.cells["a"], col) == ["alpha-sample"]
    assert col.diag["n_items_all_dropped"] == 1 and col.row("b")["summary_state"] == "unsummarized"


def test_collapse_caches_and_fingerprints(tmp_path, scripted):
    seen, replies = scripted
    replies["alpha"] = _result(("alpha", "", "alpha"))
    by_item = {"a": [_prose("a", 20, 1, "alpha")]}
    with Cache(tmp_path / "c.jsonl") as cache:
        for extra in ({}, {}, {"summ_render": "abstractive"}, {"summ_variant": "task"}):
            digest.collapse(
                "fam",
                by_item,
                args=_args(tmp_path, **extra),
                cache=cache,
                spend=Spend(),
                preflight=None,
                task_note="ZEBRA-NOTE",
            )
    assert len(seen) == 2  # cached on repeat and across renders; the variant re-asks
    assert "ZEBRA-NOTE" in seen[1][0] and "ZEBRA-NOTE" not in seen[0][0]
    rows = (tmp_path / "c.jsonl").read_text().splitlines()
    assert all('"digest:a|all"' in r for r in rows)


def test_gate_texts_splits_token_lines(tmp_path, scripted):
    _seen, replies = scripted
    replies["fence"] = _result(("fence", "[L20 pos 1]", "fence"))
    by_item = {"a": [_toks("a", 20, 1, "Ġfence", "Ġpost")]}
    with Cache(tmp_path / "c.jsonl") as cache:
        col = digest.collapse(
            "fam",
            by_item,
            args=_args(tmp_path),
            cache=cache,
            spend=Spend(),
            preflight=None,
            task_note="",
        )
    assert digest.gate_texts(col.cells["a"], col) == ["fence", "post"]
    assert digest.gate_texts(col.cells["a"], col, joined=True) == ["fence", "post", "fence post"]
    assert col.cells["a"].samples == ("[one] fence | post",)


def test_options_validated(tmp_path):
    with pytest.raises(SystemExit) as e:
        digest.summary_mode(_args(tmp_path, aggregate="pooled"))
    assert e.value.code == 2
    for bad in (
        {"summ_variant": "x"},
        {"summ_render": "x"},
        {"summ_effort": "x"},
        {"summ_max": "0"},
    ):
        with pytest.raises(SystemExit):
            digest.validate({"aggregate": "summary", **bad})
    digest.validate({"summ_variant": "x"})  # native: summary option values are not read
    with pytest.raises(SystemExit):
        digest.validate({"aggregate": "summary", "summ_varient": "task"})  # typo key
    a = _args(tmp_path)
    assert digest.prompt_version("pv", a) == "pv+digest-v2-blind-extractive+sj-v4"
    assert digest.pinned(a) and not digest.pinned(_args(tmp_path, summ_effort="low"))


def test_v2_key_support_and_dedupe():
    cells = [_prose("a", L, 1, f"I am thinking of an apple, sample {L}") for L in range(20, 64, 4)]
    cells.append(_prose("a", 64, 1, "aquariums and fish tanks"))
    lines = _lines(*cells)
    res = {
        "items": [
            {"statement": "apple", "key": "apple", "where": "", "evidence": "an apple, sample 20"},
            {
                "statement": "apple again",
                "key": "Apple",
                "where": "",
                "evidence": "apple, sample 24",
            },
            {"statement": "aquariums", "key": "aquariums", "where": "", "evidence": "aquariums"},
            {"statement": "bad key", "key": "zebra", "where": "", "evidence": "fish tanks"},
        ]
    }
    kept, dropped = digest.ground(res, lines, 10)
    assert dropped == 1  # the second "apple" repeats the first item's content
    assert [(g.key, g.support) for g in kept] == [
        ("apple", "many"),  # 11 samples contain the word, not the whole clause
        ("aquariums", "one"),
        ("fish tanks", "one"),  # a key not inside the evidence falls back to the evidence
    ]
    assert digest.support_of("app", lines) == "one"  # whole words only
