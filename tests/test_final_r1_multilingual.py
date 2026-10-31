"""Final r1 (2026-10-31): multilingual families - raw-sample regex, option pins, the language
cannot-tell rescue, the failed-quote singling credit and the ln-2026-10-31c naming forms."""

from wsbench import digest
from wsbench.digest import Line
from wsbench.multitoken import mcrx, regex
from wsbench.multitoken.family import RAW_REGEX_FAMILIES, raw_regex_samples
from wsbench.multitoken.languages import naming_forms
from wsbench.readouts import Cell

PT = naming_forms("pt", ("en", "zh"))
OTHERS = [{"o": ["Spanish", "español"]}, {"o": ["Italian"]}]
OPTS = ["Portuguese", "Spanish", "Italian", "cannot tell"]


def _named(t, lang):
    return bool(regex.hit_forms([t], naming_forms(lang, ("en", "zh"))))


def test_naming_forms_r1():
    assert _named("formal Indonesian in an academic style", "id")
    assert _named("Finnish political or news prose", "fi")
    assert _named("A Korean textbook or Wikipedia-style register", "ko")
    assert _named("в русском варианте это слово", "ru")
    assert _named("русский текст звучит так", "ru")
    assert not _named("Arabic is the official language of the country", "ar")
    assert not _named("Russian Prose Poetry", "ru")
    assert not _named("the Polish government", "pl")


def test_cannot_tell_rescue_needs_an_uncontested_naming_line():
    lines = [Line("Em português: o número seis"), Line("six")]
    res = {"choice": "D", "quote": ""}
    v = mcrx.role_verdict(res, 0, OPTS, lines, PT, others=OTHERS, rescue_cannot=True)
    assert v["correct"] and v["form_via"] == "cannot_rescue"
    # another option named anywhere: no rescue
    v = mcrx.role_verdict(
        res, 0, OPTS, [*lines, Line("maybe Spanish")], PT, others=OTHERS, rescue_cannot=True
    )
    assert not v["correct"] and v["kind"] == "cannot"
    # only the content role's cannot-tell is never rescued
    v = mcrx.role_verdict(res, 0, OPTS, lines, PT, others=OTHERS)
    assert not v["correct"]


def test_topic_language_line_never_rescues():
    lines = [Line("the Portuguese language market keeps growing")]
    assert regex.hit_forms(["the Portuguese language market"], PT)  # a naming form match ...
    res = {"choice": "D", "quote": ""}
    v = mcrx.role_verdict(res, 0, OPTS, lines, PT, others=OTHERS, rescue_cannot=True)
    assert not v["correct"]  # ... but "language" is masked for the rescue


def test_gold_pick_with_stitched_quote_credited_by_a_singling_line():
    lines = [Line("Em português: o número seis"), Line("six")]
    res = {"choice": "A", "quote": "Em português ... seis é"}
    v = mcrx.role_verdict(res, 0, OPTS, lines, PT, others=OTHERS)
    assert not v["quote_ok"] and v["correct"] and v["form_via"] == "line"
    # a distractor pick with a failed quote is never credited
    v = mcrx.role_verdict({"choice": "B", "quote": "x y"}, 0, OPTS, lines, PT, others=OTHERS)
    assert not v["correct"]


def test_raw_regex_samples_pool_layers():
    cells = [
        Cell("i", 20, 1, ("in Finnish, hammaslääkäri",), None, None, None),
        Cell("i", 44, 1, ("dentist",), None, None, None),
    ]
    got = raw_regex_samples(cells, [])
    assert "dentist" in got and any("Finnish" in g for g in got)
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
    n = digest.pin_lines(col, "i", cells, preds, s, 1)
    assert n == 3
    texts = [ln.text for ln in col.evidence["i"]]
    assert texts[0] == "in Russian, монарх" and "Spanish: monarca" in texts
    assert col.summaries["i"].startswith("[2 samples]") and "in Russian" in col.summaries["i"]
