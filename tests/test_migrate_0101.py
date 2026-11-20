"""Plan 0101 T15: the migration script on a synthetic mini-tree (no real data): its refusals,
its KEEP files and patterns, the word boundaries of its rules, the `association` family /
category move."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("migrate_0101", REPO / "scripts/migrate_0101.py")
mig = importlib.util.module_from_spec(_spec)
sys.modules["migrate_0101"] = mig
_spec.loader.exec_module(mig)

FAMILIES = ("association", "conjunctive_association", "multihop", "multihop_mt", "typo")
CODE = """\
from wsbench.evals.association import prompts
from wsbench.evals.association.judge import run
from wsbench.evals import multihop_mt
from wsbench.evals import typo as typo_pkg
import wsbench.evals.conjunctive_association.judge as cj

BANK = "evals/association/items_v5.json"
OTHER = "evals/baselines/prompt_only.json"
TABLE = {"multihop": 1, "multihop_mt": 2, "multilingual_multihop": 3, "relational_multihop": 4}
GOLDEN = "multihop_mc_options_v5.json"


def f(family, header, name, line):
    if family.startswith("multilingual"):
        return {"family": "multihop"}
    if header["family"] == "typo" or header.get("family") == "typo":
        return "multilingual-mt"
    if "multihop" in f"{line}":
        return "typo_mt_v2_options"
    x = {}
    x["subfamily"] = "typo"
    return name == "typo", multihop_mt, typo_pkg, cj, prompts, run
"""
KEEP_TEST = """\
from wsbench import registry
from wsbench.evals.multihop_mt import prompts


def test_old_name():
    assert registry.canonical("multihop_mt_v2") == "multihop_mt"
    assert (registry.REPO_ROOT / "evals/multihop_mt").is_dir() and prompts
"""


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A git checkout with five families in the flat layout, a pre-manifest and a clean tree."""
    monkeypatch.setenv("MIGRATE_0101_RUFF", f"{sys.executable} -m ruff")
    root = tmp_path / "repo"
    for fam in FAMILIES:
        (root / "evals" / fam).mkdir(parents=True)
        (root / "evals" / fam / "items.json").write_text(
            json.dumps({"family": fam.replace("_", "-"), "items": [{"id": f"{fam}-1"}]})
        )
        (root / "evals" / fam / "README.md").write_text(f"# `{fam}`\n\nBank: `evals/{fam}/`.\n")
        pkg = root / "src/wsbench/evals" / fam
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(f'NAME = "{fam}"\n')
    (root / "evals/baselines").mkdir()
    (root / "evals/baselines/prompt_only.json").write_text(
        json.dumps({"_source": {"m": 1}, "multihop": {"rate": 0.5}, "multihop_mt": {"rate": 0.25}},
                   indent=1) + "\n"
    )  # fmt: skip
    (root / "src/wsbench/evals/__init__.py").write_text('"""Families."""\n')
    (root / "src/wsbench/mod.py").write_text(CODE)
    (root / "src/wsbench/registry.py").write_text('KEEP = "multihop"\n')
    (root / "tests/golden").mkdir(parents=True)
    (root / "tests/test_rename_multihop_mt.py").write_text(KEEP_TEST)
    (root / "tests/test_multihop_mt.py").write_text('NAME = "multihop_mt"\n')
    (root / "tests/test_multihop.py").write_text('NAME = "multihop"\n')
    (root / "scripts").mkdir()
    (root / "scripts/migrate_0101_residue.txt").write_text("")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    (root / mig.MANIFEST).write_text(json.dumps({"base_sha": head.stdout.strip()}))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "manifest")
    return root


def _classify_residue(root):
    plan = mig.build_plan(root, strict=False)
    rows = "".join(f"leave\t{r}\n" for r in plan.residue())
    (root / mig.RESIDUE_FILE).write_text(rows)
    _git(root, "commit", "-q", "-am", "residue")


def test_marks_do_not_trip_on_the_pristine_spellings():
    for ok in (
        "from wsbench.evals.association.judge import run",
        "from wsbench.evals.association import prompts",
        "wsbench/evals/association/judge.py",
        'BANK = "evals/association/items_v5.json"',
        "evals/typo/items.json and evals/multihop_mt/README.md",
        "multihop_stamp = first_step",
    ):
        assert mig.has_mark(ok) == "", ok
    for bad in (
        '"multihop_st"',
        "typo_st",
        "evals/basic/association/items_v5.json",
        "evals/association/conjunctive_association/items_v4.json",
        "from wsbench.evals.association.user_modeling import judge",
        "wsbench/evals/model_specific/introspection/judge.py",
    ):
        assert mig.has_mark(bad), bad


def test_the_rewrite_refuses_a_file_that_holds_a_mark():
    with pytest.raises(mig.Refused):
        mig.find_spans('NAME = "multihop_st"\n', "src/wsbench/x.py")
    with pytest.raises(mig.Refused):
        mig.find_spans("from wsbench.evals.basic.typo import SPEC\n", "src/wsbench/x.py")
    mig.find_spans(CODE, "src/wsbench/mod.py")  # the pristine spellings do not raise


def test_rules_keep_the_word_boundaries_and_the_keep_patterns():
    spans, _live = mig.find_spans(CODE, "src/wsbench/mod.py")
    new = mig.apply_spans(CODE, spans)
    # R1: imports, the family `association` under `basic`, the category package untouched
    assert "from wsbench.evals.basic.association import prompts" in new
    assert "from wsbench.evals.basic.association.judge import run" in new
    assert "from wsbench.evals.basic import multihop as multihop_mt" in new
    assert "from wsbench.evals.basic import typo_st as typo_pkg" in new
    assert "import wsbench.evals.association.conjunctive_association.judge as cj" in new
    # R2: a family path moves, an unmoved directory does not
    assert 'BANK = "evals/basic/association/items_v5.json"' in new
    assert 'OTHER = "evals/baselines/prompt_only.json"' in new
    # R4: whole key literals swap in ONE pass; longer names stay whole
    assert '{"multihop_st": 1, "multihop": 2, "multilingual_multihop": 3, ' in new
    assert '"relational_multihop": 4}' in new and 'name == "typo_st"' in new
    # KEEP patterns: golden names, spelling logic, header vocabulary, field values, `in <str>`
    for kept in (
        '"multihop_mc_options_v5.json"',
        'family.startswith("multilingual")',
        '{"family": "multihop"}',
        'header["family"] == "typo"',
        'header.get("family") == "typo"',
        '"multilingual-mt"',
        '"multihop" in f"{line}"',
        '"typo_mt_v2_options"',
        'x["subfamily"] = "typo"',
    ):
        assert kept in new, kept
    assert [s.rule for s in spans].count("R4") == 3
    residue = mig.residue_of(CODE, spans, "src/wsbench/mod.py")
    assert any('{"family": "multihop"}' in r for r in residue)
    assert any('x["subfamily"] = "typo"' in r for r in residue)


def test_prose_rules_and_their_history_exception():
    md = (
        "Run `wsbench judge family=typo_mt readouts=examples/readouts/typo_mt.jsonl`.\n"
        "The family `multihop` reads one token; **typo** is superseded.\n"
        "It was keyed `multihop_mt_v2` until 2026-11-04, then `multihop_mt`.\n"
        "| typo | a table cell |\n"
        "Set WSB_BANK_FILE_MULTIHOP_MT or WSB_BANK_FILE_TYPO; adapted/x/multihop_mt/y.json.\n"
    )
    spans, _live = mig.find_spans(md, "docs/summarizer.md")
    new = mig.apply_spans(md, spans).split("\n")
    assert new[0] == "Run `wsbench judge family=typo readouts=examples/readouts/typo.jsonl`."
    assert new[1] == "The family `multihop_st` reads one token; **typo_st** is superseded."
    assert new[2] == "It was keyed `multihop_mt_v2` until 2026-11-04, then `multihop_mt`."
    assert new[3] == "| typo | a table cell |"
    assert (
        new[4] == "Set WSB_BANK_FILE_MULTIHOP or WSB_BANK_FILE_TYPO_ST; adapted/x/multihop/y.json."
    )
    residue = mig.residue_of(md, spans, "docs/summarizer.md")
    assert [r.split(":")[1] for r in residue] == ["3", "4"]
    py = '"""``typo`` is a parameter here; ``typo_mt`` is the family."""\n'
    spans, _live = mig.find_spans(py, "src/wsbench/x.py")
    assert mig.apply_spans(py, spans) == py.replace("``typo_mt``", "``typo``")


def test_tables_agree_with_the_registry_and_the_plan():
    from wsbench import registry

    assert set(mig.CATEGORY.values()) == set(registry.CATEGORIES)
    assert set(mig.OLD_DIR) == set(registry._layout()) and len(mig.CATEGORY) == 32
    registry.load_all()
    old_of = {new: old for old, new in mig.OLD_DIR.items()}
    for new, category in mig.CATEGORY.items():
        assert registry.FAMILIES[old_of[new]].group == category, new
    assert mig.KEYS["multihop_mt_v2"] == mig.KEYS["multihop_mt"] == "multihop"
    assert all(old in mig.KEYS or old == new for old, new in mig.OLD_DIR.items())


def test_apply_moves_rewrites_and_checks(tree):
    _classify_residue(tree)
    before = (tree / "evals/multihop/items.json").read_bytes()
    keep = (tree / "src/wsbench/registry.py").read_bytes()
    assert (
        mig.main(["--root", str(tree), "--partial-tree", "apply"]) == 0
    )  # strict tables, lenient tree
    # the pair swapped, `association` moved under `basic` exactly once, the category is new
    assert (tree / "evals/basic/multihop_st/items.json").read_bytes() == before
    assert json.loads((tree / "evals/basic/multihop/items.json").read_text())["family"] == (
        "multihop-mt"
    )
    assert (tree / "evals/basic/association/items.json").is_file()
    assert (tree / "evals/association/conjunctive_association/items.json").is_file()
    assert not (tree / "evals/basic/association/association").exists()
    assert sorted(p.name for p in (tree / "evals").iterdir()) == [
        "association",
        "baselines",
        "basic",
    ]
    assert (tree / "src/wsbench/evals/basic/__init__.py").is_file()
    assert (tree / "tests/test_multihop.py").read_text() == 'NAME = "multihop"\n'
    assert (tree / "tests/test_multihop_st.py").read_text() == 'NAME = "multihop_st"\n'
    # a KEEP file is byte-identical; a KEEP test gets imports and data paths only
    assert (tree / "src/wsbench/registry.py").read_bytes() == keep
    kept = (tree / "tests/test_rename_multihop_mt.py").read_text()
    assert 'registry.canonical("multihop_mt_v2") == "multihop_mt"' in kept
    assert "from wsbench.evals.basic.multihop import prompts" in kept
    assert '"evals/basic/multihop"' in kept
    floors = json.loads((tree / "evals/baselines/prompt_only.json").read_text())
    assert floors == {"_source": {"m": 1}, "multihop_st": {"rate": 0.5}, "multihop": {"rate": 0.25}}
    # the marker and the record live under .git/, the staging dir is gone, nothing is stray
    gd = mig.git_dir(tree)
    assert (gd / mig.MARKER).is_file() and (gd / mig.RECORD).is_file()
    assert not (tree / mig.STAGING).exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tree, capture_output=True, text=True
    ).stdout
    assert "??" not in status
    assert mig.main(["--root", str(tree), "--partial-tree", "check"]) == 0


def test_a_second_apply_is_refused(tree):
    _classify_residue(tree)
    assert mig.main(["--root", str(tree), "--partial-tree", "apply"]) == 0
    with pytest.raises(mig.Refused):
        mig.main(["--root", str(tree), "--partial-tree", "apply"])  # the marker
    (mig.git_dir(tree) / mig.MARKER).unlink()
    with pytest.raises(mig.Refused):
        mig.main(
            ["--root", str(tree), "--partial-tree", "apply"]
        )  # the migrated layout (and a dirty tree)
    _git(tree, "commit", "-q", "-m", "migrated")
    with pytest.raises(mig.Refused):
        mig.main(
            ["--root", str(tree), "--partial-tree", "apply"]
        )  # committed: still the migrated layout


def test_apply_refuses_a_dirty_tree_and_an_unclassified_residue(tree):
    (tree / "stray.txt").write_text("x")
    with pytest.raises(mig.Refused):
        mig.main(["--root", str(tree), "--partial-tree", "apply"])
    (tree / "stray.txt").unlink()
    assert (
        mig.main(["--root", str(tree), "--partial-tree", "apply"]) == 1
    )  # the residue file is empty: check fails


def test_check_sees_a_changed_data_byte(tree):
    _classify_residue(tree)
    assert mig.main(["--root", str(tree), "--partial-tree", "apply"]) == 0
    (tree / "evals/basic/typo_st/items.json").write_text("{}")
    assert mig.main(["--root", str(tree), "--partial-tree", "check"]) == 1


def test_plan_writes_the_reviewable_dry_run_and_changes_nothing(tree, tmp_path):
    out = tmp_path / "plan"
    assert mig.main(["--root", str(tree), "--partial-tree", "plan", "--out", str(out)]) == 0
    names = {p.name for p in out.iterdir()}
    assert {"R1.diff", "R4.diff", "keep-proposed.diff", "residue.txt", "moves.txt",
            "tests-values.diff", "json-keys.diff", "counts.json"} <= names  # fmt: skip
    assert (
        'registry.canonical("multihop") == "multihop"' in (out / "keep-proposed.diff").read_text()
    )
    assert "evals/association -> evals/basic/association" in (out / "moves.txt").read_text()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tree, capture_output=True, text=True
    ).stdout
    assert status == ""


def test_set_migrates_an_adapted_set_by_content(tree, tmp_path):
    d = tmp_path / "set"
    (d / "gate").mkdir(parents=True)
    (d / "multihop.json").write_text(json.dumps({"family": "multihop", "items": [{"id": "a"}]}))
    (d / "multihop_mt.json").write_text(
        json.dumps({"family": "multihop-hard", "items": [{"id": "b"}]})
    )
    (d / "gate/multihop.jsonl").write_text(json.dumps({"id": "a"}) + "\n")
    (d / "report.json").write_text(
        json.dumps({"families": {"multihop": {"n": 1}, "multihop_mt": {"n": 1}}}, indent=1) + "\n"
    )
    assert mig.main(["--root", str(tree), "--partial-tree", "set", str(d)]) == 0  # dry run
    assert (d / "multihop_mt.json").is_file()
    assert mig.main(["--root", str(tree), "--partial-tree", "set", str(d), "--apply"]) == 0
    assert json.loads((d / "multihop_st.json").read_text())["family"] == "multihop"
    assert json.loads((d / "multihop.json").read_text())["family"] == "multihop-hard"
    assert json.loads((d / "gate/multihop_st.jsonl").read_text()) == {"id": "a"}
    report = json.loads((d / "report.json").read_text())
    assert list(report["families"]) == ["multihop_st", "multihop"]
    assert report["family_keys"] == "0101"
    with pytest.raises(mig.Refused):
        mig.main(["--root", str(tree), "--partial-tree", "set", str(d), "--apply"])  # the marker
