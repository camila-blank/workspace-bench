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
        "reader": "logit_lens",
        "reads_checked": "sidecar",
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


def _source(tmp_path: Path, **extra: object) -> Path:
    p = tmp_path / f"run_config_{len(extra)}_{abs(hash(str(extra)))}.json"
    p.write_text(json.dumps({"protocol": ll.PROTOCOL, "model": "Qwen/Qwen3.6-27B", **extra}))
    return p


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
    src = _source(tmp_path, bench={"x": "y"}, junk=1)
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
    a = _run(tmp_path, "a", _result("association", _spv("association")))
    ll.freeze(a, None, dst, source=_source(tmp_path, producer="p1", measured="d1"))
    b = _run(tmp_path, "b", _result("multihop", _spv("multihop")))
    got = ll.freeze(b, None, dst, source=_source(tmp_path, measured="d2"))
    assert set(ll.floors(dst)) == {"association", "multihop"}
    # provenance is the latest record's only, never a merge with the previous run's
    assert got["_source"]["measured"] == "d2" and "producer" not in got["_source"]


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
        ({"reader": "jlens-public"}, "logit-lens floor needs"),
        ({"reader": None}, "logit-lens floor needs"),
        ({"reads_checked": "no_sidecar"}, "logit-lens floor needs"),
    ],
)
def test_freeze_refuses(tmp_path, real_families, override, msg):
    r = _result("association", _spv("association"))
    r.config.update(override)
    with pytest.raises(ValueError, match=msg):
        ll.freeze(_run(tmp_path, "bad", r), None, tmp_path / "ll.json", source=_source(tmp_path))


def test_freeze_refuses_bad_source_and_bank_root(tmp_path, real_families, monkeypatch):
    run = _run(tmp_path, "ok", _result("association", _spv("association")))
    with pytest.raises(ValueError, match="protocol"):
        ll.freeze(run, None, tmp_path / "ll.json", source=_source(tmp_path, protocol="po-opus-v1"))
    monkeypatch.setenv("WSB_BANK_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="registered banks only"):
        ll.freeze(run, None, tmp_path / "ll.json", source=_source(tmp_path))


def test_freeze_checks_readouts_sidecar(tmp_path, real_families, monkeypatch):
    from wsbench import reads

    monkeypatch.setattr(reads, "plan_sha", lambda family: "main-sha")
    run = _run(tmp_path, "ok", _result("wrong_intermediate", _spv("wrong_intermediate")))
    ro = tmp_path / "readouts"
    ro.mkdir()
    meta = ro / "wrong_bridge.jsonl.meta.json"  # the legacy file name is accepted
    meta.write_text(json.dumps({"reader": "logitlens-L60", "plan_sha": "main-sha"}))
    got = ll.freeze(run, None, tmp_path / "ll.json", source=_source(tmp_path), readouts=ro)
    assert got["wrong_intermediate"]["plan_sha"] == "main-sha"
    meta.write_text(json.dumps({"reader": "logitlens-L60", "plan_sha": "old-sha"}))
    with pytest.raises(ValueError, match="plan_sha"):
        ll.freeze(run, None, tmp_path / "b.json", source=_source(tmp_path), readouts=ro)
    meta.write_text(json.dumps({"reader": "jlens-public", "plan_sha": "main-sha"}))
    with pytest.raises(ValueError, match="declares reader"):
        ll.freeze(run, None, tmp_path / "c.json", source=_source(tmp_path), readouts=ro)
    meta.unlink()
    with pytest.raises(ValueError, match="no wrong_intermediate"):
        ll.freeze(run, None, tmp_path / "d.json", source=_source(tmp_path), readouts=ro)


def test_cell_text_marks_incomplete():
    e = {"rate": 0.29, "complete": False, "counts": {"n_unjudged_cells": 1}}
    assert ll.cell_text(e) == "0.290 (incomplete: 1 unjudged)"
    assert ll.cell_text({"rate": 0.5, "complete": True}) == "0.500"


def test_freeze_refuses_ati_without_scope_or_entry(tmp_path, real_families):
    spv = _spv("association")
    full = _run(tmp_path, "full", _result("association", spv))
    src = _source(tmp_path)
    with pytest.raises(ValueError, match="anti_text_inversion"):
        a = _run(tmp_path, "ati", _result("association", spv))
        ll.freeze(full, a, tmp_path / "a.json", source=src)
    ati = _run(tmp_path, "ati2", _result("multihop", _spv("multihop"), scope=ATI_SCOPE))
    with pytest.raises(ValueError, match="no full-bank entry"):
        ll.freeze(full, ati, tmp_path / "b.json", source=src)


def test_report_column_matches_summary_rows(tmp_path, monkeypatch, real_families):
    spv = _spv("association")
    dst = tmp_path / "ll.json"
    ll.freeze(
        _run(tmp_path, "full", _result("association", spv, value=0.25)),
        None,
        dst,
        source=_source(tmp_path),
    )
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
    from wsbench import reads

    monkeypatch.setattr(reads, "plan_sha", lambda family: "s")
    full = _run(tmp_path, "full", _result("association", _spv("association")))
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "association.jsonl.meta.json").write_text(
        json.dumps({"reader": "logit_lens", "plan_sha": "s"})
    )
    dst = tmp_path / "ll.json"
    args = ["freeze", "kind=logit_lens", f"src={full}", f"dst={dst}", f"readouts={ro}"]
    assert main(args) == 2  # source= is required
    assert "needs src=" in capsys.readouterr().err
    assert main([*args, f"source={_source(tmp_path)}"]) == 0
    assert "association" in ll.floors(dst)
    monkeypatch.setenv("WSB_BANK_ROOT", str(tmp_path))
    assert main([*args, f"source={_source(tmp_path)}"]) == 2
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
        assert len(e["plan_sha"]) == 64, f


def test_report_hides_column_when_no_row_draws_it(tmp_path, monkeypatch, real_families):
    spv = _spv("association")
    dst = tmp_path / "ll.json"
    ll.freeze(_run(tmp_path, "f", _result("association", spv, value=0.25)), None, dst,
              source=_source(tmp_path))  # fmt: skip
    monkeypatch.setattr(ll, "FROZEN", dst)
    match = _run(tmp_path, "match", _result("association", spv, value=0.5))
    assert main(["report", f"dir={match}"]) == 0
    assert f"| {ll.COLUMN} |" in (match / "summary.md").read_text()
    row = _result("association", spv, value=0.5)
    row.config["summarizer"] = {**SUMM, "max_items": 20}
    other = _run(tmp_path, "other", row)
    assert main(["report", f"dir={other}"]) == 0
    assert ll.COLUMN not in (other / "summary.md").read_text()
