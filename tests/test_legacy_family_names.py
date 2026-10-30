"""Former family keys (``registry.LEGACY_FAMILY_NAMES``) keep working: on the command line, as
readout file names under ``run``, in stored results.json files and as ATI subset keys. Offline:
the run legs use directed_modulation_obscure (regex scorer, never calls a model)."""

import fnmatch
import json
import re
import shutil
import subprocess
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


# plan 0033: brew_intermediates stays retired. Code and docs may not mention it again outside
# dated history; bank items and model responses (data JSON) use "brew" as an ordinary word.
BREW_RE = re.compile(r"brew_|_brew\b|\bbrew\b", re.I)  # not Hebrew / brewed / brewer
BREW_ALLOW = (
    "plans/*",
    "adapted/*/AUDIT*.md",
    "adapted/*/SET_AUDIT.md",
    ".claude/skills/adapt-bank/SKILL_AUDIT*.md",
    ".claude/skills/adapt-bank/SKILL.md",
    ".claude/skills/adapt-bank/references/pitfalls.md",
    "src/wsbench/registry.py",
    "tests/test_legacy_family_names.py",
    "tests/test_readplan.py",
    "tests/test_text_inversion.py",
    "tests/test_results.py",
    "tests/test_cli.py",
    "CLAUDE.md",
    "evals/baselines/README.md",
)
DATA_ROOTS = ("evals/", "adapted/", "examples/")


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode("utf-8").split("\0") if p]


def _is_data(path: str) -> bool:
    if fnmatch.fnmatch(path, "adapted/*/report.json"):
        return False  # its gate.rule string is prose, not data
    return path.startswith(DATA_ROOTS) and path.endswith((".json", ".jsonl"))


def test_brew_has_no_live_references():
    files = _tracked_files()
    assert files, "git ls-files returned nothing"
    bad_paths = [p for p in files if "brew" in p.lower() and not p.startswith("plans/")]
    assert not bad_paths, f"brew_intermediates was retired (plan 0029): {bad_paths}"
    hits = []
    for path in files:
        if _is_data(path) or any(fnmatch.fnmatch(path, g) for g in BREW_ALLOW):
            continue
        try:
            text = (REPO / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or deleted in the working tree
        hits += [
            f"{path}:{i}: {ln.strip()[:120]}"
            for i, ln in enumerate(text.splitlines(), 1)
            if BREW_RE.search(ln)
        ]
    assert not hits, "brew_intermediates was retired (plan 0029); reword or remove:\n" + "\n".join(
        hits
    )


def test_brew_guard_regex():
    for s in ("brew_intermediates", "gen_brew", "BREW_HEAD", "### Brew (colour)", "chain and brew"):
        assert BREW_RE.search(s), s
    for s in ("Hebrew", "brewed", "brewer", "Brewster", "home-brewing"):
        assert not BREW_RE.search(s), s
