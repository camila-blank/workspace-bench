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


def _cannot(lines, raw=(), rivals=()):
    return mcrx.role_verdict(
        {"choice": "D", "quote": ""},
        0,
        OPTS,
        lines,
        PT,
        others=OTHERS,
        rescue_cannot=True,
        raw_texts=raw,
        rivals=rivals,
    )


def test_cannot_tell_rescue_needs_an_uncontested_naming_line():
    lines = [Line("Em português: o número seis"), Line("six")]
    v = _cannot(lines)
    assert v["correct"] and v["form_via"] == "cannot_rescue"
    assert not _cannot([*lines, Line("maybe Spanish")])["correct"]  # a rival option named
    # a rival language named in a RAW sample blocks it too
    assert not _cannot(lines, raw=["the word in Russian is"], rivals=[{"r": ["Russian"]}])[
        "correct"
    ]
    # only the language role is rescued
    v = mcrx.role_verdict({"choice": "D", "quote": ""}, 0, OPTS, lines, PT, others=OTHERS)
    assert not v["correct"]


def test_topic_lines_never_rescue():
    for t in (
        "the Portuguese language market keeps growing",
        "In Portuguese, exams are hard for students",
        "Portuguese: the official language of Brazil",
    ):
        assert not _cannot([Line(t)])["correct"], t


def test_failed_quote_needs_its_own_line():
    lines = [Line("Em português: o número seis"), Line("six")]
    # campaign-lead decision: a fabricated quote is never rescued by an unrelated line
    v = mcrx.role_verdict(
        {"choice": "A", "quote": "totally invented"}, 0, OPTS, lines, PT, others=OTHERS
    )
    assert not v["correct"]
    # a quote stitched from parts of ONE line that names the language verifies through it
    v = mcrx.role_verdict(
        {"choice": "A", "quote": "Em português ... seis"}, 0, OPTS, lines, PT, others=OTHERS
    )
    assert v["correct"] and v.get("quote_near")


def test_raw_regex_needs_every_unit_in_one_layer_with_support():
    units = [
        regex.ScoredUnit("concept_native", True, {"en": ["dentist"]}),
        regex.ScoredUnit("language", True, {"en": ["re:(?i)\\bin Finnish\\b"]}),
    ]
    split = [
        Cell("i", 20, 1, ("written in Finnish, a profession",), None, None, None),
        Cell("i", 44, 1, ("dentist",), None, None, None),
    ]
    by = raw_layer_hits(split, units)
    assert not any(regex.layer_passes(h, units) for h in by.values())  # units at two layers
    one = [
        Cell("i", 20, 1, ("written in Finnish",), None, None, None),
        Cell("i", 20, 2, ("the answer is dentist",), None, None, None),
    ]
    assert regex.layer_passes(raw_layer_hits(one, units)[20], units)
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
