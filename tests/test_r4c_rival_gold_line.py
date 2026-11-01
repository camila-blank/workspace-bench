"""Round 4c (rv-2026-11-01g): a rival line that also names the gold is no rival line. The judge
is told "a line that also names your pick" is no rival, but code checked only the rival QUOTE,
so a rival quoted from the gold's own best line tied the gold with itself and the round-4b tie
rule (rival.MORE_THAN) contested it (multihop_mt_v2 all-layer bolivar-caracas 392 vs 392,
rand-novel 166 vs 166; all-lite henry-james-pragmatism bridge2 27 vs 27). Both directions: the
same-line shape passes again; a distinct rival line, or a rival quote also found in a separate
gold-free line, still contests. Digest lines are verbatim from the cached final-olens-rl600-x4
all-layer digests."""

from wsbench import digest, rival


def _g(text: str, count: int) -> digest.Grounded:
    ln = digest.Line(text)
    return digest.Grounded("", digest.support_text(count), text, ln, text, "items", "", count)


BOLIVAR = (
    "Spanish rule was Simón Bolívar, while the man who freed South America from Portuguese rule "
    "was José de San Martín. These two liberators are considered the primary architects of "
    "South American independence."
)
RQ = "freed South America from Portuguese rule was José de San Martín"


def _contest(res, grounded, margin=rival.MORE_THAN):
    return rival.contest(
        res,
        grounded,
        single_gold=lambda ln: "Bolívar" in ln.text,
        names_gold=lambda t: "Bolívar" in t,
        margin=margin,
    )


def test_rival_quoted_from_the_gold_line_is_no_rival():
    gs = [_g(BOLIVAR, 392), _g("Other famous liberators include José de San Martín.", 3)]
    for margin in (rival.MORE_THAN, rival.FAR_MORE):
        rv = _contest({"quote": "Spanish rule was Simón Bolívar", "rival_quote": RQ}, gs, margin)
        assert rv["gold_support"] == 392 and rv["rival_support"] == 0
        assert rv["rival_names_gold"] and rv["rival_verified"] and not rv["contested"]


def test_distinct_rival_line_still_contests():
    gs = [
        _g(BOLIVAR, 392),
        _g("The man who freed South America from Portuguese rule was José de San Martín.", 392),
    ]
    rv = _contest({"quote": "Spanish rule was Simón Bolívar", "rival_quote": RQ}, gs)
    assert rv["rival_support"] == 392 and rv["contested"]  # a tie with another line: contested


def test_rival_quote_in_both_kinds_of_line_counts_the_gold_free_one():
    gs = [
        _g(BOLIVAR, 392),
        _g("He who freed South America from Portuguese rule was José de San Martín.", 5),
    ]
    rv = _contest({"quote": "Spanish rule was Simón Bolívar", "rival_quote": RQ}, gs)
    assert rv["rival_support"] == 5 and not rv["contested"] and "rival_names_gold" not in rv


def test_version_bumped_calls_stay_keyed():
    assert rival.RIVAL_VERSION == "rv-2026-11-01g"
    assert rival.call_version("x+rv-2026-11-01g+y") == "x+rv-2026-10-31e+y"
