"""Former family keys (``registry.LEGACY_FAMILY_NAMES``) keep working: on the command line, as
readout file names under ``run``, in stored results.json files and as ATI subset keys. Offline:
the run legs use directed_modulation_obscure (regex scorer, never calls a model)."""

import json
import shutil
from pathlib import Path

import pytest

from wsbench import llm, registry
from wsbench.baselines import text_inversion as ti
from wsbench.cli import main
from wsbench.evals.directed_modulation_obscure import SPEC as DMO
from wsbench.results import read_results

REPO = Path(__file__).resolve().parents[1]
# The toy items (trickster_figure) were retired from the live bank by the ATI panel refill (plan
# 0019); these tests run on a byte copy of the pre-refill bank and example readouts. The live
# bank's structure is checked by tests/test_refilled_banks.py.
FIXTURE_BANK = REPO / "tests/fixtures/directed_modulation_obscure_pre_refill_items.json"
EXAMPLE = REPO / "tests/fixtures/directed_modulation_obscure_pre_refill_readouts.jsonl"
TOY_IDS = "middle_eastern_spice_blend,trickster_figure"


@pytest.fixture
def _pre_refill_bank(monkeypatch):
    # only the legs that read TOY_IDS / EXAMPLE use the pre-refill bank; everything else
    # (e.g. the frozen-file check) sees the live bank
    monkeypatch.setenv("WSB_BANK_FILE_DIRECTED_MODULATION_OBSCURE", str(FIXTURE_BANK))


RENAMES = {
    "introspection_inject": "introspection",
    "cutoff_confab": "knowledge_cutoff",
    "dm_category_default": "directed_modulation_obscure",
}


@pytest.fixture(autouse=True)
def _no_calls(monkeypatch):
    registry.FAMILIES.setdefault(DMO.name, DMO)

    def no_call(*a, **k):
        raise AssertionError("a legacy-name test must never call a model")

    monkeypatch.setattr(llm, "stream_json", no_call)
    monkeypatch.setattr(llm, "stream_json_async", no_call)


def test_map_and_canonical(real_families):
    assert registry.LEGACY_FAMILY_NAMES == RENAMES
    for old, new in RENAMES.items():
        assert registry.canonical(old) == new and registry.canonical(new) == new
        assert new in registry.FAMILIES and old not in registry.FAMILIES
        assert registry.legacy_names(new) == [old]
        assert not (REPO / "evals" / old).exists() and (REPO / "evals" / new).is_dir()
    assert registry.canonical("multihop") == "multihop" and registry.legacy_names("multihop") == []


# ---------------------------------------------------------------- (a) old names on the CLI


@pytest.mark.usefixtures("_pre_refill_bank")
def test_judge_accepts_old_family_name(tmp_path, capsys):
    out = tmp_path / "j"
    rc = main(
        [
            "judge",
            "opts=aggregate=native",
            "family=dm_category_default",
            f"readouts={EXAMPLE}",
            f"out={out}",
            f"items={TOY_IDS}",
        ]
    )
    assert rc == 0
    assert "renamed to 'directed_modulation_obscure'" in capsys.readouterr().err
    res = read_results(out)
    assert res.family == "directed_modulation_obscure" and res.value == 0.5


@pytest.mark.usefixtures("_pre_refill_bank")
def test_run_accepts_old_family_name(tmp_path):
    root = tmp_path / "readouts"
    root.mkdir()
    shutil.copy(EXAMPLE, root / "directed_modulation_obscure.jsonl")
    out = tmp_path / "run"
    rc = main(
        [
            "run",
            "opts=aggregate=native",
            "families=dm_category_default",
            f"readouts_root={root}",
            f"out={out}",
            f"items={TOY_IDS}",
        ]
    )
    assert rc == 0
    assert read_results(out / "directed_modulation_obscure").value == 0.5
    statuses = json.loads((out / "run.json").read_text())["families"]
    assert statuses["directed_modulation_obscure"]["status"] == "ok"


def test_plan_accepts_old_family_name(tmp_path, real_families):
    out = tmp_path / "plan"
    assert main(["plan", "families=dm_category_default", f"out={out}", "limit=2"]) == 0
    assert [p.name for p in out.iterdir()] == ["directed_modulation_obscure.jsonl"]


def test_text_inversion_accepts_old_family_names(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(ti, "build", lambda out, families: seen.setdefault("f", families))
    rc = main(["text-inversion", "stage=build", f"out={tmp_path}", "families=cutoff_confab,poetry"])
    assert rc == 0 and seen["f"] == ["knowledge_cutoff", "poetry"]


# ---------------------------------------------------------------- (b) legacy readout file name


@pytest.mark.usefixtures("_pre_refill_bank")
def test_run_reads_legacy_readout_file(tmp_path, capsys):
    root = tmp_path / "readouts"
    root.mkdir()
    shutil.copy(EXAMPLE, root / "dm_category_default.jsonl")  # written before the rename
    out = tmp_path / "run"
    rc = main(
        [
            "run",
            "opts=aggregate=native",
            "families=directed_modulation_obscure",
            f"readouts_root={root}",
            f"out={out}",
            f"items={TOY_IDS}",
        ]
    )
    assert rc == 0
    assert "reading legacy dm_category_default.jsonl" in capsys.readouterr().out
    assert read_results(out / "directed_modulation_obscure").value == 0.5


@pytest.mark.usefixtures("_pre_refill_bank")
def test_run_prefers_new_readout_file(tmp_path, capsys):
    root = tmp_path / "readouts"
    root.mkdir()
    shutil.copy(EXAMPLE, root / "directed_modulation_obscure.jsonl")
    (root / "dm_category_default.jsonl").write_text("not json\n")  # never read
    out = tmp_path / "run"
    args = [
        "run",
        "opts=aggregate=native",
        "families=directed_modulation_obscure",
        f"readouts_root={root}",
        f"out={out}",
    ]
    assert main([*args, f"items={TOY_IDS}"]) == 0
    assert "legacy" not in capsys.readouterr().out


# ---------------------------------------------------------------- (c) stored results.json


@pytest.mark.usefixtures("_pre_refill_bank")
def test_report_canonicalizes_old_family_in_results(tmp_path, capsys):
    out = tmp_path / "runs"
    rc = main(["judge", "opts=aggregate=native", "family=directed_modulation_obscure",
               f"readouts={EXAMPLE}", f"out={out / 'dmcd'}", f"items={TOY_IDS}"])  # fmt: skip
    assert rc == 0
    path = out / "dmcd" / "results.json"
    stored = json.loads(path.read_text())
    stored["family"] = "dm_category_default"  # a results.json written before the rename
    path.write_text(json.dumps(stored))
    assert read_results(out / "dmcd").family == "directed_modulation_obscure"
    capsys.readouterr()
    assert main(["report", f"dir={out}", "json=True", "floors=False"]) == 0
    families = json.loads(capsys.readouterr().out)["families"]
    assert [f["family"] for f in families] == ["directed_modulation_obscure"]


# ---------------------------------------------------------------- (d) ATI entry under an old key


def test_subset_entry_reads_old_key(tmp_path):
    entry = {"instrument": DMO.judge.prompt_version, "ids": ["a"], "n_bank": 98}
    dst = tmp_path / "ati.json"
    dst.write_text(json.dumps({"_not_gated": {}, "dm_category_default": entry}))
    assert ti.subset_entry("directed_modulation_obscure", dst) == entry
    assert ti.subset_entry("dm_category_default", dst) == entry
    dst.write_text(json.dumps({"_not_gated": {"dm_category_default": "policy refusal: 90%"}}))
    with pytest.raises(ti.NotInScope, match="policy refusal"):
        ti.subset_entry("directed_modulation_obscure", dst)


def test_frozen_ati_file_uses_new_keys(real_families):
    frozen = json.loads(ti.FROZEN.read_text(encoding="utf-8"))
    for old, new in RENAMES.items():
        assert old not in frozen and new in frozen
        if frozen[new]["instrument"] != registry.get(new).judge.prompt_version:
            # plan 0032: introspection's instrument gained +cannot; re-gated in the run phase
            with pytest.raises(SystemExit, match="re-gate"):
                ti.subset_entry(new)
            continue
        assert ti.subset_entry(new)["ids"] == frozen[new]["ids"]


def test_ttt_is_removed_everywhere(real_families):
    # plan 0017: ttt (formerly ttt_move) was deleted, not superseded
    from wsbench import readplan

    for name in ("ttt", "ttt_move"):
        assert name not in registry.FAMILIES
        assert name not in registry.LEGACY_FAMILY_NAMES
        assert name not in readplan.families()
        assert name not in ti.CONSTRUCTION_GATED and name not in ti.STRICTER_PASS_FIELD
    frozen = json.loads(ti.FROZEN.read_text())
    assert "ttt" not in frozen and "ttt_move" not in frozen


def test_brew_intermediates_is_removed(real_families):
    # plan 0029: brew_intermediates was retired; stale results.json stays out of the macro
    from wsbench import readplan

    assert "brew_intermediates" in registry.REMOVED_FAMILIES
    assert "brew_intermediates" not in registry.FAMILIES
    assert "brew_intermediates" not in readplan.families()
    frozen = json.loads(ti.FROZEN.read_text())
    assert "brew_intermediates" not in frozen


def test_hallucination_is_removed(real_families):
    # plan 0040: hallucination left WorkspaceBench for HallucinationBench (its index is reported
    # by wsbench.hbindex); stale results.json stays out of the macro with reason "removed"
    from wsbench import readplan
    from wsbench.results import FamilyResult, macro

    assert "hallucination" in registry.REMOVED_FAMILIES
    assert "HallucinationBench" in registry.REMOVED_FAMILIES["hallucination"]
    assert "hallucination" not in registry.FAMILIES
    assert "hallucination" not in readplan.families()
    for path in sorted((REPO / "evals" / "baselines").glob("*.json")):
        frozen = json.loads(path.read_text())
        assert "hallucination" not in frozen, path.name
        assert "hallucination" not in frozen.get("_not_gated", {}), path.name
    assert not (REPO / "evals" / "hallucination").exists()
    assert not list((REPO / "adapted").glob("*/hallucination*.json"))
    old = FamilyResult(
        family="hallucination",
        metric="hallucination_rate",
        value=0.3,
        ci95=(0.2, 0.4),
        n_items=149,
        higher_is_better=False,
        chance=None,
        chance_label=None,
        complete=True,
        pinned_instrument=True,
        config={"judge_model": "m", "prompt_version": "v5c"},
        counts={},
    )
    m = macro([old])
    assert m["families"] == []
    assert [(e["family"], e["reason"]) for e in m["excluded"]] == [("hallucination", "removed")]
    assert "HallucinationBench" in m["excluded"][0]["why"]
