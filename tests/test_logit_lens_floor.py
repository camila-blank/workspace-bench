"""Plan 0071: the frozen logit-lens L60 floor (``evals/baselines/logit_lens.json``): freeze checks,
the entry schema, the report column beside a matching summary row, and the committed file."""

import json
from pathlib import Path

import pytest

from wsbench.baselines import logit_lens as ll
from wsbench.baselines import prompt_only as po
from wsbench.cli import floor_columns, main, summary_floor_columns
from wsbench.judge_config import DEFAULT_JUDGE
from wsbench.results import ATI_SCOPE, FamilyResult, markdown_table, write_results

REPO = Path(__file__).resolve().parents[1]
SUMM = {"model": "claude-opus-5-5", "max_items": 10, "concurrency": 256, "timeout": 900.0}


def _spv(family: str) -> str:
    return f"{po.instrument_versions()[family]}+digest-v5-task-extractive+sj-v10"


def _result(family: str, pv: str, *, value: float = 0.3, **cfg_extra: object) -> FamilyResult:
    cfg: dict = {
        "judge_model": DEFAULT_JUDGE,
        "prompt_version": pv,
        "layers_judged": [60],
        "sampling": "tokens10",
        "aggregate": "summary",
        "summarizer": dict(SUMM),
        **cfg_extra,
    }
    rows = [{"id": f"i{k}", "pass": k < 3} for k in range(10)]
    return FamilyResult(
        family=family,
        metric="pass_rate",
        value=value,
        ci95=(value - 0.1, value + 0.1),
        n_items=10,
        higher_is_better=True,
        chance=None,
        chance_label=None,
        complete=True,
        pinned_instrument=True,
        config=cfg,
        counts={
            "n_expected_cells": 10,
            "n_missing_cells": 0,
            "n_unjudged_cells": 0,
            "n_empty_cells": 0,
        },
        extras={"n_items_decided": 10},
        rows=rows,
    )


def _run(tmp_path: Path, name: str, *results: FamilyResult) -> Path:
    d = tmp_path / name
    for r in results:
        write_results(d / r.family, r)
    return d


def test_freeze_entry_schema_ati_and_source(tmp_path, real_families):
    spv = _spv("association")
    full = _run(tmp_path, "full", _result("association", spv))
    ati = _run(
        tmp_path, "ati", _result("association", spv, value=0.1, scope=ATI_SCOPE, items=["i0"])
    )
    src = tmp_path / "run_config.json"
    src.write_text(json.dumps({"model": "Qwen/Qwen3.6-27B", "bench": {"x": "y"}, "junk": 1}))
    dst = tmp_path / "ll.json"
    got = ll.freeze(full, ati, dst, source=src)
    assert next(iter(got)) == "_source"
    s = got["_source"]
    assert (s["protocol"], s["layer"], s["k"], s["method"]) == (ll.PROTOCOL, 60, 10, "logit_lens")
    assert s["model"] == "Qwen/Qwen3.6-27B" and "junk" not in s
    e = got["association"]
    assert e["rate"] == 0.3 and e["protocol"] == ll.PROTOCOL
    assert e["instrument"] == spv and e["layers_judged"] == [60] and e["aggregate"] == "summary"
    assert e["summarizer"] == po.summarizer_key({"summarizer": SUMM})
    assert e["ati"]["rate"] == 0.1 and e["ati"]["instrument"] == spv
    assert json.loads(dst.read_text()) == got
    assert ll.floors(dst) == {"association": e}


def test_freeze_keeps_other_families(tmp_path, real_families):
    dst = tmp_path / "ll.json"
    ll.freeze(_run(tmp_path, "a", _result("association", _spv("association"))), None, dst)
    ll.freeze(_run(tmp_path, "b", _result("multihop", _spv("multihop"))), None, dst)
    assert set(ll.floors(dst)) == {"association", "multihop"}


@pytest.mark.parametrize(
    ("override", "msg"),
    [
        ({"aggregate": None}, "not a summary-mode run"),
        ({"layers_judged": [44]}, r"not \[60\]"),
        ({"layers_judged": [20, 60]}, r"not \[60\]"),
        ({"sampling": "prose1"}, "sampling"),
        ({"limit": 3}, "subset run"),
        ({"items": ["i0"]}, "subset run"),
        ({"scope": ATI_SCOPE}, "full-bank run"),
        ({"prompt_version": "assoc-old+digest-v5"}, "re-judge first"),
    ],
)
def test_freeze_refuses(tmp_path, real_families, override, msg):
    r = _result("association", _spv("association"))
    r.config.update(override)
    with pytest.raises(ValueError, match=msg):
        ll.freeze(_run(tmp_path, "bad", r), None, tmp_path / "ll.json")


def test_freeze_refuses_ati_without_scope_or_entry(tmp_path, real_families):
    spv = _spv("association")
    full = _run(tmp_path, "full", _result("association", spv))
    with pytest.raises(ValueError, match="anti_text_inversion"):
        ll.freeze(full, _run(tmp_path, "ati", _result("association", spv)), tmp_path / "a.json")
    ati = _run(tmp_path, "ati2", _result("multihop", _spv("multihop"), scope=ATI_SCOPE))
    with pytest.raises(ValueError, match="no full-bank entry"):
        ll.freeze(full, ati, tmp_path / "b.json")


def test_report_column_matches_summary_rows(tmp_path, monkeypatch, real_families):
    spv = _spv("association")
    dst = tmp_path / "ll.json"
    ll.freeze(_run(tmp_path, "full", _result("association", spv, value=0.25)), None, dst)
    monkeypatch.setattr(ll, "FROZEN", dst)
    native = floor_columns()
    assert native[ll.COLUMN] == {}  # summary-mode only: no native cell
    summ = summary_floor_columns()
    entry, text = summ[ll.COLUMN]["association"]
    assert text == "0.250" and ll.floor_applies(entry, _result("association", spv).config)
    row = _result("association", spv, value=0.5)
    other = _result("association", spv, value=0.5)
    other.config["summarizer"] = {**SUMM, "max_items": 20}  # another digest setting
    md = markdown_table([row], None, floors=native, summary_floors=summ)
    assert f"| {ll.COLUMN} |" in md.splitlines()[0] and "0.250" in md
    md2 = markdown_table([other], None, floors=native, summary_floors=summ)
    assert "0.250" not in md2


def test_freeze_cli(tmp_path, real_families, monkeypatch, capsys):
    full = _run(tmp_path, "full", _result("association", _spv("association")))
    dst = tmp_path / "ll.json"
    assert main(["freeze", "kind=logit_lens", f"src={full}", f"dst={dst}"]) == 0
    assert "association" in ll.floors(dst)
    monkeypatch.setenv("WSB_BANK_ROOT", str(tmp_path))
    assert main(["freeze", "kind=logit_lens", f"src={full}", f"dst={dst}"]) == 2
    assert "registered banks only" in capsys.readouterr().err


def test_committed_floor_file():
    got = json.loads(ll.FROZEN.read_text(encoding="utf-8"))
    s = got["_source"]
    assert (s["protocol"], s["layer"], s["k"]) == (ll.PROTOCOL, 60, 10)
    fams = {f for f in got if not f.startswith("_")}
    assert len(fams) == 29 and "jlens_concept_pr" not in fams
    for f in fams:
        e = got[f]
        assert e["layers_judged"] == [60] and e["aggregate"] == "summary", f
        assert e["protocol"] == ll.PROTOCOL and e["rate"] is not None, f
