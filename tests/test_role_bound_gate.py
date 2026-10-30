"""role_bound_association's single-statement gate (plan 0041 C1.4, C2.3): the shared quote
verifier and statement splitter, plus the family's agent / patient reference check."""

import pytest

from wsbench import digest
from wsbench.evals.role_bound_association import gate
from wsbench.readouts import Cell

LEX = gate.load_lexicon()
MIA, RUTH = ("Mia", "child"), ("Ruth", "grandparent")


def _prose(*samples: str, layer: int = 44, pos: int = 1) -> Cell:
    return Cell("i", layer, pos, tuple(samples), None, None)


def _tokens(toks: list[str], layer: int = 44, pos: int = 1) -> Cell:
    return Cell("i", layer, pos, None, tuple(toks), None)


def _check(quote: str, cells: list[Cell], agent=MIA, patient=RUTH) -> dict:
    return gate.check(quote, digest.native_lines(cells), cells, agent, patient, LEX)


def test_stitched_quote_rejected():
    # bullet4: two separate generations; a quote spanning them is not inside one sample (a)
    cell = _prose("Mia runs up the stairs", "she hugs Ruth")
    g = _check("Mia runs up the stairs she hugs Ruth", [cell])
    assert g["quote_ok"] is False and g["ok"] is False


def test_cross_bullet_quote_in_one_prose1_generation_rejected():
    gen = "- Mia runs up the stairs to find Grandfather\n- She hugs her mother"
    cell = _prose(gen)
    (line,) = digest.native_lines([cell])
    assert line.text == "Mia runs up the stairs to find Grandfather ⁞ She hugs her mother"
    for q in ("find Grandfather She hugs", "find Grandfather ⁞ She hugs"):
        g = gate.check(q, [line], [cell], ("Mia", "child"), ("Ruth", "grandparent"), LEX)
        assert g["quote_ok"] is True and g["one_statement"] is False and g["ok"] is False, q


def test_cross_bullet_pick_counts_as_unverified_bullet():
    from wsbench.evals.role_bound_association import score

    gen = "- Mia runs up the stairs to find Grandfather\n- She hugs her mother"
    cell = _prose(gen)
    g = _check("find Grandfather She hugs", [cell])
    row = {"correct": [True, True, True], "gate": g, "gate_ok": g["ok"]}
    counts = score.gate_counts([row])
    assert counts["n_unverified_bullet"] == 1 and counts["n_gate_rejected"] == 1
    assert counts["n_quote_unverified"] == 0


def test_quote_inside_one_bullet_passes():
    cell = _prose("- the old house\n- Mia, the granddaughter, hugs Grandma Ruth\n- a kitchen")
    g = _check("Mia, the granddaughter, hugs Grandma Ruth", [cell])
    assert g == {
        "quote_ok": True,
        "one_statement": True,
        "agent_ref": True,
        "patient_ref": True,
        "ok": True,
    }


def test_grandmother_is_not_parent():
    cell = _prose("the teacher blamed the grandmother")
    g = _check("the teacher blamed the grandmother", [cell], ("Margaret", "teacher"),
               ("Dana", "parent"))  # fmt: skip
    assert g["quote_ok"] and g["agent_ref"] and g["patient_ref"] is False and g["ok"] is False


def test_mother_is_not_grandparent():
    cell = _prose("the girl hugged her mother")
    g = _check("the girl hugged her mother", [cell])
    assert g["agent_ref"] is True and g["patient_ref"] is False and g["ok"] is False


@pytest.mark.parametrize(
    "quote,role,want",
    [
        ("the security guard searched her", "guard", False),  # a longer, different role
        ("the prison guard searched her", "guard", True),
        ("her step-mother", "mother", False),
        ("her mother-in-law", "mother", False),
        ("the baby-sitter fed him", "babysitter", True),
        ("the grandparents", "parent", False),
        ("Mom scolded him", "parent", True),
    ],
)
def test_role_ref_word_and_near_rules(quote, role, want):
    assert gate.role_ref(quote, role, LEX) is want


def test_partial_name_prefix4():
    assert gate.name_ref("Marc chased the thief", "Marcus") is True
    assert gate.name_ref("Marco chased the thief", "Marcus") is True
    assert gate.name_ref("Mark chased the thief", "Marcus") is False
    assert gate.name_ref("Mar chased", "Marcus") is False  # under 4 letters
    assert gate.name_ref("Dev fined", "Dev") is True and gate.name_ref("Devon", "Dev") is False


def test_cjk_role_term_needs_listed_form():
    cell = _prose("孩子拥抱了奶奶")
    assert _check("孩子拥抱了奶奶", [cell])["patient_ref"] is True  # 奶奶 is listed
    cell = _prose("孩子拥抱了老人家")
    assert _check("孩子拥抱了老人家", [cell])["patient_ref"] is False  # unlisted form


def test_token_quote_same_list_only():
    a = _tokens(["Mia", "hugs", "Ruth"], pos=1)
    b = _tokens(["grandma", "kitchen"], pos=2)
    assert _check("Mia | hugs | Ruth", [a, b])["ok"] is True
    g = _check("Mia | grandma", [a, b])  # tokens of two lists never combine
    assert g["quote_ok"] is False and g["ok"] is False


def test_empty_or_non_string_quote_fails():
    cell = _prose("Mia hugs Ruth")
    assert _check("", [cell])["ok"] is False
    assert gate.check(None, digest.native_lines([cell]), [cell], MIA, RUTH, LEX)["ok"] is False
