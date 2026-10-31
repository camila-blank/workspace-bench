"""Final r1 (2026-10-31, campaign-lead decision): conjunctive_association keeps the draw-majority
rule, adds an inverted-twin veto and leaves an item with a failed quote re-judge undecided; the
chance label matches the rule."""

from wsbench.evals.conjunctive_association import judge as cj
from wsbench.evals.conjunctive_association import score


def _combine(picks, failed=()):
    g = cj._Gate(tokens=False)
    ds = [
        {"pick": p, "choice": 1, "quote": "q", "quote_ok": True}
        | ({"failed_stage": "quote_check"} if i in failed else {})
        for i, p in enumerate(picks)
    ]
    return g.combine("x", ds)["pick"]


def test_majority_still_needed():
    assert _combine(["gold", "cannot_tell", "cannot_tell"]) == "cannot_tell"
    assert _combine(["gold", "gold", "cannot_tell"]) == "gold"


def test_inverted_draw_vetoes_a_gold_majority():
    assert _combine(["gold", "gold", "inverted"]) == "inverted"


def test_failed_draw_undecided_only_when_it_could_change_the_verdict():
    assert _combine(["gold", "gold", "api_fail"]) == "api_fail"  # inverted would veto it
    assert _combine(["gold", "api_fail", "cannot_tell"]) == "api_fail"
    assert _combine(["cannot_tell", "cannot_tell", "api_fail"]) == "cannot_tell"  # fails anyway
    assert cj.item_pick(["inverted", "gold", "api_fail"]) != "gold"


def test_chance_matches_the_rule():
    p = 1 / 11
    assert abs(score.mean_chance([["x"] * 11], 3) - (3 * p**2 - 2 * p**3)) < 1e-12
    assert score.mean_chance([["x"] * 11], 1) == p
    assert "majority of the 3" in score.chance_label(3)
