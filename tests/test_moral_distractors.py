"""scripts/moral_distractors.py: the mechanical half of the v2 option authoring (plan 0046 §2.4)."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/moral_distractors.py"
_spec = importlib.util.spec_from_file_location("moral_distractors", SCRIPT)
md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(md)  # type: ignore[union-attr]

GOLD = "Paying ransom funds and incentivizes further criminal activity and future kidnappings"


def test_mechanical_filters():
    assert (
        md._mech_ok("A human life outweighs any sum of money, so paying is a moral duty", GOLD)
        == ""
    )
    assert md._mech_ok("Too short", GOLD) == "length"
    assert md._mech_ok(GOLD + " (acknowledged but judged outweighed)", GOLD) == "concession"
    near = "Paying ransom funds and incentivizes further criminal activity and more kidnappings"
    assert md._mech_ok(near, GOLD) == "jaccard_gold"


def _entry(klass: str) -> dict:
    reasons = [
        {"text": "Y reason", "supports": "yes", "frequency": 3},
        {"text": "N reason", "supports": "no", "frequency": 9},
        {"text": "Y2 reason", "supports": "yes", "frequency": 5},
    ]
    lf = ["Y reason", "N reason"] if klass == "deliberative" else ["N reason"]
    draws = [{"answer": "Yes", "draws": ["y"] * 10}, {"answer": "No", "draws": ["n"] * 10}]
    return {
        "v1": {"id": "x", "question": "Q?", "answer_format": "yes_no"},
        "v2": {"reason_class": klass, "reasons": reasons, "look_for_reasons": lf,
               "commit_direction": "no"},
        "rekey": {"reason_draws": draws},
    }  # fmt: skip


def test_committed_list_takes_qwens_own_opposite_reason():
    (lst,) = md.lists_of(_entry("committed"))
    assert lst["side"] == "committed" and lst["gold"] == "N reason" and lst["gold_side"] == "no"
    assert lst["qwen_opp"] == ["Y2 reason"]  # the most frequent opposite-side reason
    assert lst["need"] == {"opp": 2, "same": 2} and lst["draws"] == ["n"] * 10


def test_deliberative_lists_take_the_other_sides_gold():
    yes, no = md.lists_of(_entry("deliberative"))
    assert (yes["side"], yes["gold"], yes["qwen_opp"]) == ("yes", "Y reason", ["N reason"])
    assert (no["side"], no["gold"], no["qwen_opp"]) == ("no", "N reason", ["Y reason"])
    assert yes["draws"] == ["y"] * 10 and no["draws"] == ["n"] * 10


def test_register_pass_output_is_what_freezes():
    chosen = [{"text": "a, so b", "kind": "same", "source": "opus"}]
    restyled = [{"text": "a then b", "kind": "same", "source": "opus", "restyled_from": "a, so b"}]
    assert md._final({"chosen": chosen}) == chosen
    assert md._final({"chosen": chosen, "restyled": restyled}) == restyled
    assert "{lo}" in md.RESTYLE_SYSTEM and "STYLE REFERENCE" in md.RESTYLE_USER
