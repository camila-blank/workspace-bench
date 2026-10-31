"""Final r1 (2026-10-31): multilingual families - raw-sample regex within one layer, option pins,
the language cannot-tell rescue, stitched quotes and the ln-2026-10-31c naming forms."""

from wsbench import digest
from wsbench.digest import Line
from wsbench.multitoken import mcrx, regex
from wsbench.multitoken.family import RAW_REGEX_FAMILIES, raw_layer_hits
from wsbench.multitoken.languages import naming_forms
from wsbench.readouts import Cell

PT = naming_forms("pt", ("en", "zh"))
OTHERS = [{"o": ["Spanish", "español"]}, {"o": ["Italian"]}]
OPTS = ["Portuguese", "Spanish", "Italian", "cannot tell"]


def _named(t, lang):
    return bool(regex.hit_forms([t], naming_forms(lang, ("en", "zh"))))


def test_naming_forms_r1():
    for t, lang in [
        ("formal Indonesian in an academic style", "id"),
        ("Finnish political or news prose", "fi"),
        ("A Korean textbook or Wikipedia-style register", "ko"),
        ("в русском варианте это слово", "ru"),
        ("русский текст звучит так", "ru"),
    ]:
        assert _named(t, lang), t


def test_naming_forms_never_name_these():
    # campaign-lead decision: no arbitrary modifier gaps, closed ru suffixes
    for t, lang in [
        ("Arabic became the official language", "ar"),
        ("Arabic is the official language of the country", "ar"),
        ("the Spanish king signed letters", "es"),
        ("Russian literature and prose", "ru"),
        ("Russian Prose Poetry style", "ru"),
        ("taught in Portuguese in schools", "pt"),
        ("translated from Russian in 1990", "ru"),
        ("русский переводчик", "ru"),
        ("русская текстильная промышленность", "ru"),
        ("the Polish government", "pl"),
    ]:
        assert not _named(t, lang), t


def test_cannot_tell_is_never_credited():
    # review of #201: the deterministic rescue is gone; the judge's question covers it
    lines = [Line("Em português: o número seis"), Line("six")]
    v = mcrx.role_verdict({"choice": "D", "quote": ""}, 0, OPTS, lines, PT, others=OTHERS)
    assert not v["correct"] and v["kind"] == "cannot"


def test_failed_quote_needs_its_own_line():
    lines = [Line("Em português: o número é seis, six"), Line("the six and the five")]
    # a fabricated quote is never rescued by an unrelated line
    v = mcrx.role_verdict(
        {"choice": "A", "quote": "totally invented"}, 0, OPTS, lines, PT, others=OTHERS
    )
    assert not v["correct"]
    # junk parts ('the ... and') never stitch
    v = mcrx.role_verdict(
        {"choice": "A", "quote": "the ... and"}, 0, OPTS, lines, PT, others=OTHERS
    )
    assert not v["correct"]
    # a quote stitched from real parts of ONE line that names the language verifies through it
    q = "Em português: o número ... é seis, six"
    v = mcrx.role_verdict({"choice": "A", "quote": q}, 0, OPTS, lines, PT, others=OTHERS)
    assert v["correct"] and v.get("quote_near")
    # stitched from a line that names no language: no credit, whatever another line says
    q = "the six ... and the five"
    v = mcrx.role_verdict({"choice": "A", "quote": q}, 0, OPTS, lines, PT, others=OTHERS)
    assert not v["correct"]


def test_raw_regex_needs_every_unit_in_one_layer_with_support():
    units = [
        regex.ScoredUnit("concept_native", True, {"en": ["dentist"]}),
        regex.ScoredUnit("language", True, {"fi": ["re:(?i)\\bin Finnish\\b"]}),
    ]
    split = [
        Cell("i", 20, 1, ("written in Finnish, a profession",), None, None, None),
        Cell("i", 44, 1, ("dentist",), None, None, None),
    ]
    by = raw_layer_hits(split, units)
    assert not any(regex.layer_passes(h, units) for h in by.values())  # units at two layers
    one = [
        Cell("i", 20, 1, ("written in Finnish",), None, None, None),
        Cell("i", 20, 2, ("the answer is dentist, in Finnish",), None, None, None),
    ]
    assert regex.layer_passes(raw_layer_hits(one, units)[20], units)
    # an incidental language naming needs two samples of the layer ("written in X" states it)
    lone = [
        Cell("i", 20, 1, ("in Finnish the word sounds odd",), None, None, None),
        Cell("i", 20, 2, ("the answer is dentist",), None, None, None),
    ]
    assert not regex.layer_passes(raw_layer_hits(lone, units)[20], units)
    weak = [
        Cell("i", 20, 1, ("written in Finnish",), None, None, None),
        Cell("i", 20, 2, ("a visit to the dentist was planned",), None, None, None),
    ]
    assert not regex.layer_passes(raw_layer_hits(weak, units)[20], units)  # one mention only
    assert {"multilingual_typo", "multilingual_multihop", "multilingual_mt"} <= RAW_REGEX_FAMILIES


def test_pin_lines_is_capped_and_symmetric():
    cells = [Cell("i", 20, p, (t,), None, None, None) for p, t in enumerate(
        ["in Russian, монарх", "in Russian, монарх", "Spanish: monarca", "noise", "Italian x"]
    )]  # fmt: skip
    col = digest.Collapse()
    col.cells["i"] = Cell("i", digest.SUMMARY_LAYER, 0, ("",), None, None, None)
    col.grounded["i"] = []
    s = digest.settings(digest._probe({}), "multilingual_multihop")
    preds = [lambda ln, w=w: w in ln.text for w in ("Russian", "Spanish", "Italian")]
    assert digest.pin_lines(col, "i", cells, preds, s, 1) == 3
    texts = [ln.text for ln in col.evidence["i"]]
    assert texts[0] == "in Russian, монарх" and "Spanish: monarca" in texts


def test_text_language_statement_needs_a_text_subject():
    from wsbench.multitoken.family import _states_text_language

    u = regex.ScoredUnit("language", True, {"ru": ["re:x"]})
    assert _states_text_language("this passage is written in Russian", u)
    assert _states_text_language("Language: Russian", u)
    for t in (
        "a novel written in Russian",
        "the text was not written in Russian",
        "originally the letter was written in Russian",
        "the question was translated, the text is in Russian",
    ):
        assert not _states_text_language(t, u), t


def test_language_pins_use_english_naming_forms_only():
    forms = mcrx._option_naming_forms("Russian", ("en", "zh"))
    assert list(forms) == ["en"] and forms["en"]
    assert list(mcrx._option_naming_forms("Pashto", ("en", "zh"))) == ["en"]
