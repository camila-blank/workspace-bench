"""Fresh-eye review of the audit-fix PRs #194/#195/#196 (2026-10-31): the blocking fixes, each
with the too-lenient shape that must still fail."""

from pathlib import Path

from conftest import native_args
from test_audit_fixes_families import GOLD_LINE, OPP_LINE, _committed_mc, _draw, _gate, _pos

from wsbench import digest, runner
from wsbench.digest import Grounded, Line
from wsbench.evals.moral_rationale import judge as mr
from wsbench.results import FamilyResult

# ---------------------------------------------------------------- moral ec-v5 reads v5 markers


def _v5_summary(*entries: tuple[str, int, int]) -> str:
    """A digest-v5 extractive summary as ``digest.render_summary`` writes it: one line per
    (sample, per-claim count, read layers)."""
    gs = [
        Grounded("s", digest.support_text(n, k), text, Line(text), text, count=n)
        for text, n, k in entries
    ]
    return digest.render_summary(gs, "extractive")[0]


def test_moral_support_reads_the_per_claim_count():
    text = _v5_summary((GOLD_LINE, 1, 1), (OPP_LINE, 12, 3))
    assert text.splitlines()[1].startswith("[12 samples, 3 layers] ")
    sup = mr.digest_support(text)
    assert sup("primary duty of care") == 1 and sup("the fewest total deaths") == 4
    # bucketed on support_of's cut points: 1 / 2-3 / 4-9 / 10+
    assert [mr.support_rank(digest.support_text(n)) for n in (1, 2, 3, 4, 9, 10, 40)] == [
        1, 2, 2, 3, 3, 4, 4,
    ]  # fmt: skip
    assert mr.support_rank("first number of 3 of 4 samples") == 0 and mr.support_rank("many") == 4
    sup = mr.digest_support('- s [support: 5 samples] — evidence: "' + GOLD_LINE + '"')
    assert sup("primary duty of care") == 3  # the abstractive render too


def test_moral_v5_gold_seen_once_beside_a_dominant_opposite_fails():
    """The ec-v5 rule "no opposite-side option has stronger support" on a REAL digest-v5 summary:
    before the fix every v5 marker parsed as support 0, so every gold-plus-opposite item tied and
    passed whatever the counts."""
    mc = _committed_mc()
    opp = _pos(mc, "opp")
    both = _draw((mc.gold_pos, "primary duty of care"), (opp, "the fewest total deaths"))
    gate_ = _gate(GOLD_LINE, OPP_LINE)
    weak = mr.digest_support(_v5_summary((GOLD_LINE, 1, 1), (OPP_LINE, 12, 3)))
    v = mr.ml_verdict([both] * 3, mc, gate_, weak)
    assert v["gold_marked"] and not v["correct"] and v["pick"] == "opposite"
    # about equally often (one bucket): a tie, both sides stated, passes as before
    even = mr.digest_support(_v5_summary((GOLD_LINE, 5, 2), (OPP_LINE, 7, 2)))
    assert mr.ml_verdict([both] * 3, mc, gate_, even)["correct"]
    # the gold dominant passes
    strong = mr.digest_support(_v5_summary((GOLD_LINE, 14, 4), (OPP_LINE, 2, 1)))
    assert mr.ml_verdict([both] * 3, mc, gate_, strong)["correct"]


# ---------------------------------------------------------------- digest: one key, one claim


def test_ground_merges_markdown_variants_of_one_key():
    """``merge_grounded`` folds markdown out of a key; ``ground`` must too, or "Paris" and
    "**Paris**" from ONE digest both survive and their capped counts are summed."""
    lines = [Line(f"the capital is **Paris** ({i})") for i in range(3)]
    res = {
        "items": [
            {"statement": "a", "key": "**Paris**", "where": "", "evidence": lines[0].text,
             "count": 3},
            {"statement": "b", "key": "Paris", "where": "", "evidence": lines[1].text,
             "count": 3},
        ]
    }  # fmt: skip
    dups = [0]
    kept, _ = digest.ground(res, lines, 10, dups)
    assert len(kept) == 1 and dups == [1]
    assert digest.merge_grounded(kept)[0].count == 3  # never 6 from 3 samples


# ---------------------------------------------------------------- F3: refusals only -> measured


def _result(*, unjudged: int, refused: int, undecided: int | None, **kw) -> FamilyResult:
    extras = {"summary": {"n_digest_refused": refused, "n_digest_too_long": 0}}
    if undecided is not None:
        extras["n_items_undecided"] = undecided
    return FamilyResult(
        family="user_modeling",
        metric="pass_rate",
        value=0.5,
        ci95=None,
        n_items=100,
        higher_is_better=True,
        chance=None,
        chance_label="",
        complete=False,
        pinned_instrument=kw.get("pinned", True),
        config={"aggregate": kw.get("aggregate", "summary")},
        counts={
            "n_missing_cells": kw.get("missing", 0),
            "n_unjudged_cells": unjudged,
            "n_empty_cells": kw.get("empty", 0),
        },
        extras=extras,
        rows=[],
    )


def _args(**kw):
    return native_args(readouts=Path("x"), out=Path("y"), judge=None, layers=None,
                       items=kw.get("items"), limit=0, allow_missing=False, concurrency=1,
                       rpm=1.0, dry_run=False, extra={"aggregate": "summary"})  # fmt: skip


def test_digest_refusals_alone_leave_a_row_measured():
    """Audit F3: a row whose only shortfall is final digest refusals (undecided, never a fail) is
    measured on its decided items; results.macro then applies the 20% not-measurable rule. Before,
    any refusal made the row incomplete, so the 20% rule could never fire."""
    assert runner.refusals_only(_args(), _result(unjudged=3, refused=3, undecided=3)) == 3
    r = _result(unjudged=30, refused=30, undecided=30)
    runner.finalize_reads(_args(), r)
    assert r.complete and r.extras["complete_with_digest_refusals"] == 30
    from wsbench.results import macro, not_measurable

    assert not_measurable(r)  # 30% undecided: measured, then left out of the macro as such
    m = macro([r], superseded={}, held_out={}, removed={})
    assert m["excluded"] == [
        {"family": "user_modeling", "reason": "not measurable", "why": "30% undecided"}
    ]


def test_other_shortfalls_keep_a_row_incomplete():
    bad = [
        _result(unjudged=4, refused=3, undecided=4),  # a failed judge call on top: rerun
        _result(unjudged=3, refused=3, undecided=None),  # the family scores them (no undecided)
        _result(unjudged=3, refused=3, undecided=2),  # some refusals scored as fails
        _result(unjudged=3, refused=3, undecided=3, missing=1),
        _result(unjudged=3, refused=3, undecided=3, empty=6),
        _result(unjudged=3, refused=3, undecided=3, pinned=False),
        _result(unjudged=3, refused=3, undecided=3, aggregate="native"),
        _result(unjudged=0, refused=0, undecided=0),
    ]
    for r in bad:
        assert runner.refusals_only(_args(), r) == 0
    assert (
        runner.refusals_only(_args(items=["a"]), _result(unjudged=3, refused=3, undecided=3)) == 0
    )
