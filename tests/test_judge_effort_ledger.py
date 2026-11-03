"""Plan 0059: ``judge_effort=``, the per-family spend ledger, token counts in results, and the
runner's digest preflight under ``summ_cache_only``."""

import json
import time
from pathlib import Path

import pytest

from wsbench import ledger, llm, mcjudge, registry
from wsbench.cli import main
from wsbench.judge_config import DEFAULT_REASONING, JudgeConfig, resolve
from wsbench.llm import Spend
from wsbench.registry import EvalSpec, JudgeArgs, register
from wsbench.results import FamilyResult

OR_MODEL = "deepseek/deepseek-v4-flash"


def _result(args: JudgeArgs, family: str, **config: object) -> FamilyResult:
    return FamilyResult(
        family=family,
        metric="pass_rate",
        value=0.5,
        ci95=(0.0, 1.0),
        n_items=1,
        higher_is_better=True,
        chance=None,
        chance_label=None,
        complete=True,
        pinned_instrument=args.judge.pinned,
        config={"judge_model": args.judge.model, "reasoning": args.judge.reasoning, **config},
        counts=mcjudge.base_counts(
            n_expected=1, n_unjudged=0, n_empty=0, skipped_rows=0, spend=Spend()
        ),
    )


def _spec(name: str, run, model: str = OR_MODEL, reasoning: dict | None = None) -> EvalSpec:
    return register(
        EvalSpec(
            name=name,
            title=name,
            group="logic",
            bank=Path(f"evals/{name}/items.json"),
            judge=JudgeConfig(model=model, reasoning=reasoning),
            metric="pass_rate",
            higher_is_better=True,
            run=run,
        )
    )


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.delenv("WSBENCH_JUDGE_MODEL", raising=False)
    monkeypatch.setattr(llm, "_make_client", lambda r, k: pytest.fail("no client in tests"))
    seen: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(llm, "preflight", lambda model, reasoning: seen.append((model, reasoning)))
    registry.FAMILIES.clear()
    return seen


def _readouts(root: Path, *names: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    row = json.dumps({"id": "a", "layer": 36, "pos": 1, "samples": ["x"]}) + "\n"
    for n in names:
        (root / f"{n}.jsonl").write_text(row)


def _run_argv(root: Path, out: Path, *extra: str, families: str = "") -> list[str]:
    # families= by name: ``main`` runs ``registry.load_all`` first, which registers the real
    # families when this module runs alone
    names = families or ",".join(sorted(registry.FAMILIES))
    return [
        "run",
        "opts=aggregate=native",
        f"families={names}",
        f"readouts_root={root}",
        f"out={out}",
        *extra,
    ]


# ---------------------------------------------------------------- judge_effort (change 5)


def test_resolve_effort_replaces_openrouter_reasoning_and_unpins():
    fam = JudgeConfig(model=OR_MODEL, reasoning={"effort": "low"})
    assert resolve(fam) == resolve(fam, effort=None)  # absent: unchanged
    assert resolve(fam).reasoning == {"effort": "low"} and resolve(fam).pinned
    j = resolve(fam, effort="high")
    assert j.reasoning == {"effort": "high"} and j.pinned is False and j.model == OR_MODEL
    assert resolve(fam, effort="default").reasoning is None
    plain = JudgeConfig(model=OR_MODEL)
    assert resolve(plain).reasoning == DEFAULT_REASONING
    assert resolve(plain, effort="minimal").reasoning == {"effort": "minimal"}
    assert resolve(plain, effort="minimal").pinned is False
    # an OpenRouter override of a Claude family: the effort replaces DEFAULT_REASONING
    opus = JudgeConfig(reasoning={"anthropic_effort": "default"})
    assert resolve(opus, flag=OR_MODEL, effort="low").reasoning == {"effort": "low"}


def test_resolve_effort_with_claude_judge_or_bad_level_exits_2():
    with pytest.raises(SystemExit) as e:
        resolve(JudgeConfig(), effort="low")  # the default judge is claude-opus-5-5
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        resolve(JudgeConfig(model=OR_MODEL), effort="max")


def test_run_judge_effort_recorded_only_when_given(offline, tmp_path):
    seen_reasoning: list[dict | None] = []

    def run(args: JudgeArgs) -> FamilyResult:
        seen_reasoning.append(args.judge.reasoning)
        return _result(args, "g1")

    _spec("g1", run)
    root = tmp_path / "r"
    _readouts(root, "g1")
    assert main(_run_argv(root, tmp_path / "a", "judge_effort=low")) == 0
    run_json = json.loads((tmp_path / "a" / "run.json").read_text())
    assert run_json["judge_effort"] == "low" and "judge_effort" not in run_json["judge_overrides"]
    assert run_json["families"]["g1"]["pinned_instrument"] is False
    assert offline == [(OR_MODEL, {"effort": "low"})] and seen_reasoning == [{"effort": "low"}]
    assert main(_run_argv(root, tmp_path / "b")) == 0
    assert "judge_effort" not in json.loads((tmp_path / "b" / "run.json").read_text())
    assert seen_reasoning[-1] == DEFAULT_REASONING


def test_judge_effort_claude_judge_and_bad_value_exit_2(offline, tmp_path):
    _spec("c1", lambda a: _result(a, "c1"), model="claude-opus-5-5")
    root = tmp_path / "r"
    _readouts(root, "c1")
    with pytest.raises(SystemExit) as e:
        main(_run_argv(root, tmp_path / "o", "judge_effort=low"))
    assert e.value.code == 2
    argv = ["judge", "opts=aggregate=native", "family=c1", f"readouts={root / 'c1.jsonl'}"]
    with pytest.raises(SystemExit) as e:  # as a bad opts= on judge: fail() exits 2
        main([*argv, f"out={tmp_path / 'j'}", "judge_effort=low"])
    assert e.value.code == 2
    assert main([*argv, f"out={tmp_path / 'j'}", "judge_effort=extreme"]) == 2
    assert offline == []  # nothing preflighted


def test_baseline_and_capable_reject_judge_effort(capsys):
    assert main(["baseline", "judge_effort=low"]) == 2
    assert main(["capable", "model=x/y", "judge_effort=low"]) == 2
    assert "judge_effort" in capsys.readouterr().err


# ---------------------------------------------------------------- the ledger (change 6)


def _spending_run(name: str, spend_fields: dict, hold: float = 0.0):
    def run(args: JudgeArgs) -> FamilyResult:
        s = Spend()  # built in the worker thread, under the family's ledger context
        for k, v in spend_fields.items():
            setattr(s, k, v)
        if hold:
            path = args.out / ledger.FILENAME
            deadline = time.monotonic() + hold
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.01)
        return _result(args, name)

    return run


def test_ledger_final_snapshot_per_family_from_worker_threads(offline, tmp_path):
    fields = {
        "usd": 0.25,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 7,
        "cache_write_tokens": 3,
        "calls": 4,
        "retries": 1,
        "errors": 1,
        "refusals": 0,
        "transient": {"429": 1},
        "failed": {"500": 1},
    }
    _spec("f1", _spending_run("f1", fields))
    _spec("f2", _spending_run("f2", {"usd": 1.0, "calls": 1}))
    root, out = tmp_path / "r", tmp_path / "o"
    _readouts(root, "f1", "f2")
    assert main(_run_argv(root, out, "family_workers=2")) == 0
    rows = [json.loads(x) for x in (out / "f1" / ledger.FILENAME).read_text().splitlines()]
    last = rows[-1]
    assert last["run_id"] == ledger.RUN_ID and last["family"] == "f1"
    for k, v in fields.items():
        assert last[k] == v, k
    total = ledger.read_total(out / "f2" / ledger.FILENAME)
    assert total["usd"] == 1.0 and total["calls"] == 1 and total["n_runs"] == 1
    assert not (out / ledger.FILENAME).exists()  # per family, never the run root


def test_ledger_writes_periodically(offline, tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "INTERVAL", 0.02)
    ledger._WAKE.set()  # an already-running writer picks up the short interval now
    _spec("p1", _spending_run("p1", {"usd": 0.5}, hold=5.0))
    root, out = tmp_path / "r", tmp_path / "o"
    _readouts(root, "p1")
    assert main(_run_argv(root, out)) == 0
    rows = (out / "p1" / ledger.FILENAME).read_text().splitlines()
    assert len(rows) >= 2  # >= 1 periodic snapshot while running, then the final one
    assert json.loads(rows[-1])["usd"] == 0.5
    # settle the shared writer back into a long wait, so its pending short wake cannot write an
    # extra snapshot into the next test's family
    monkeypatch.setattr(ledger, "INTERVAL", 3600.0)
    ledger._WAKE.set()
    time.sleep(0.1)


def test_ledger_exit_flush_and_unregistered_spends(tmp_path):
    outside = Spend(usd=9.0)  # no family context: never in a ledger
    with ledger.family(tmp_path / "fam", "fam") as fam:
        s = Spend(usd=0.1)
        ledger._flush_all()  # what the atexit hook does for families still running
        assert id(s) in fam.spends and id(outside) not in fam.spends
    rows = [json.loads(x) for x in (tmp_path / "fam" / ledger.FILENAME).read_text().splitlines()]
    # the exit flush, then the final snapshot (>= 2: a periodic writer wake may add one)
    assert len(rows) >= 2 and all(r["usd"] == 0.1 for r in rows)


def test_ledger_read_total_sums_last_snapshot_per_run_id(tmp_path):
    path = tmp_path / ledger.FILENAME
    lines = [
        {"run_id": "1-a", "usd": 0.1, "calls": 1, "transient": {"429": 1}},
        {"run_id": "1-a", "usd": 0.3, "calls": 3, "transient": {"429": 2}},
        {"run_id": "2-b", "usd": 0.5, "calls": 5, "failed": {"429": 1}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    total = ledger.read_total(path)
    assert total["usd"] == pytest.approx(0.8) and total["calls"] == 8 and total["n_runs"] == 2
    assert total["transient"] == {"429": 2} and total["failed"] == {"429": 1}


def test_ledger_under_layers_each_and_not_for_judge(offline, tmp_path):
    _spec("e1", _spending_run("e1", {"usd": 0.2}))
    root, out = tmp_path / "r", tmp_path / "o"
    _readouts(root, "e1")
    assert main(_run_argv(root, out, "layers=each")) == 0
    assert (out / "e1" / "all" / ledger.FILENAME).exists()
    assert (out / "e1" / "L36" / ledger.FILENAME).exists()
    argv = ["judge", "opts=aggregate=native", "family=e1", f"readouts={root / 'e1.jsonl'}"]
    assert main([*argv, f"out={tmp_path / 'j'}"]) == 0
    assert (tmp_path / "j" / "results.json").exists()
    assert not (tmp_path / "j" / ledger.FILENAME).exists()


# ---------------------------------------------------------------- token counts


def test_base_counts_carry_tokens_and_round_trip():
    c = mcjudge.base_counts(
        n_expected=1,
        n_unjudged=0,
        n_empty=0,
        skipped_rows=0,
        spend=Spend(usd=0.1, input_tokens=30, output_tokens=5),
    )
    assert (c["input_tokens"], c["output_tokens"]) == (30, 5)
    r = FamilyResult(
        family="x",
        metric="pass_rate",
        value=None,
        ci95=None,
        n_items=0,
        higher_is_better=True,
        chance=None,
        chance_label=None,
        complete=True,
        pinned_instrument=True,
        config={},
        counts=c,
    )
    d = r.to_json()
    assert d["counts"]["input_tokens"] == 30
    assert FamilyResult.from_json(json.loads(json.dumps(d))) == r


# ---------------------------------------------------------------- runner, summ_cache_only


def test_runner_skips_the_digest_preflight_under_cache_only(offline, tmp_path):
    def run(args: JudgeArgs) -> FamilyResult:
        return _result(args, "s1", aggregate="summary")

    _spec("s1", run)
    root = tmp_path / "r"
    _readouts(root, "s1")
    base = ["run", "families=s1", f"readouts_root={root}"]
    assert main([*base, f"out={tmp_path / 'a'}"]) == 0
    assert ("claude-opus-5-5", {"anthropic_effort": "default"}) in offline  # the digest model
    offline.clear()
    mcjudge._PREFLIGHTED.clear()
    assert main([*base, f"out={tmp_path / 'b'}", "opts=summ_cache_only=1"]) == 0
    assert offline == [(OR_MODEL, DEFAULT_REASONING)]  # the judge only
