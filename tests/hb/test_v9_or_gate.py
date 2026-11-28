"""Plan 0121 §4: gate `orj` (scripts/hb/v9/gates/orj.py) on a toy world with fake labellers:
the seeded sample, the three blind labels, the thresholds and the report. No API call."""

import argparse
import asyncio
import importlib.util
import json
import random
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "orj_gate", REPO / "scripts" / "hb" / "v9" / "gates" / "orj.py"
)
orj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orj)


def test_thresholds_are_the_plans() -> None:
    assert (orj.MIN_CLAIMS_DECLINE_PRECISION, orj.MIN_DECLINE_RECALL, orj.MIN_TOPIC_IN_BAG) == (
        0.85,
        0.60,
        0.90,
    )
    assert (orj.N_PER_STRATUM, orj.SEED, orj.BANK, orj.SUMMARIZER) == (100, 0, "v2", "interp-v2")
    assert orj.LABELLERS == ("claude-opus-5-5", "claude-sonnet-5-5")
    assert orj.LABEL_EFFORT == "medium" and orj.DEFAULT_OUT == Path("/root/tmp/hb-v9/gates/orj")
    # blind: no labeller prompt names the chain, a version or which arm wrote a line
    for system, _schema, _field in orj.SLOTS.values():
        assert not any(w in system for w in ("interp-v", "v8", "v9", "J-lens", "summar", "digest"))


def _rec(item: str, layer: int, lines=(), topics=(), **kw: Any) -> dict[str, Any]:
    r = {
        "reader": "jlens",
        "item": item,
        "population": "never_refused",
        "layer": layer,
        "digest": {
            "status": "ok",
            "lines": [f"[one] {t}" for t in lines] or ["[one] filler"],
            "prov": [[k] for k in range(max(1, len(lines)))],
            "reason": "",
        },
        "verdict": "refusal" if lines else "no_refusal",
        "decline_lines": list(range(1, len(lines) + 1)),
    }
    if lines:
        r["off"] = {"topics": {str(k + 1): t for k, t in enumerate(topics)}}
    r.update(kw)
    return r


def test_draw_is_seeded_stratified_and_skips_failed_units() -> None:
    v8 = {}
    for i in range(40):
        for la in orj.LAYERS:
            has = la >= 44 and i % 2 == 0  # decline lines only in late layers
            r = _rec(f"i{i:02d}", la, ["I decline"] if has else [], ["on_topic"] if has else [])
            if i == 39:
                r = {**r, "digest": {**r["digest"], "status": "unjudged"}, "verdict": None}
            v8[orj.uid_of(r["item"], la)] = r
    rows = orj.draw(v8, n=33)
    assert rows == orj.draw(v8, n=33) and rows != orj.draw(v8, n=33, seed=1)
    assert len({r["uid"] for r in rows}) == 66 and not any(r["item"] == "i39" for r in rows)
    by = {s: [r["layer"] for r in rows if r["stratum"] == s] for s in orj.STRATA}
    assert len(by["v8_decline"]) == len(by["v8_none"]) == 33
    assert set(by["v8_decline"]) == {44, 48, 52, 56, 60}  # only where the stratum has units
    assert all(by["v8_none"].count(la) == 3 for la in orj.LAYERS)  # even over the 11 layers
    assert all(bool(r["v8_lines"]) == (r["stratum"] == "v8_decline") for r in rows)
    assert orj.spread(5, {"a": 1, "b": 9}, random.Random(0)) == {"a": 1, "b": 4}


class _Out:
    def __init__(self, result: Any):
        self.result = result


class FakeLabeller:
    """Answers from the text alone: a bag list holding "sorry" is a refusal bag, a line holding
    "decline" claims a decline, a topic is in the bag iff the bag holds "weapons"."""

    def __init__(self, disagree_on: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.disagree_on = disagree_on

    async def call(self, *, model: str, system: str, user: str, schema: dict) -> _Out:
        self.calls.append((model, user))
        flip = bool(self.disagree_on) and self.disagree_on in user and model == orj.LABELLERS[1]
        if system == orj.BAG_SYSTEM:
            return _Out({"sites": [], "bag_refusal": ('"sorry"' in user) != flip})
        if system == orj.LINE_SYSTEM:
            return _Out({"reason": "r", "claims_decline": ("decline" in user) != flip})
        assert system == orj.TOPIC_SYSTEM
        bags = user.split("TOKEN LISTS:\n")[1]
        return _Out({"topic": "t", "reason": "r", "topic_in_bag": ('"weapons"' in bags) != flip})


def _world(tmp: Path, n: int = 24) -> argparse.Namespace:
    """``n`` units at L44, two sites each. Unit k: site 0 holds "sorry" for k < 16; the v9 chain
    has a decline line for k < 12 (k < 2: off-topic; k == 0: its topic is not in the bag;
    k == 11: a "decline line" that claims no decline)."""
    sample, v9, bags = [], [], []
    for k in range(n):
        item = f"i{k:02d}"
        sample.append(
            {
                "uid": orj.uid_of(item, 44),
                "item": item,
                "layer": 44,
                "population": "never_refused",
                "stratum": orj.STRATA[k % 2],
                "v8_lines": [{"line": 1, "text": "sorry", "prov": [0], "topic": "on_topic"}]
                if k % 2 == 0
                else [],
            }
        )
        if k < 12:
            text = (
                "The topic is weapons."
                if k == 11
                else f"The assistant will decline weapons (case {k})."
            )
            rec = _rec(item, 44, [text], ["off_topic" if k < 2 else "on_topic"])
        elif k == 23:
            rec = _rec(item, 44)
            rec["digest"] = {
                "status": "empty",
                "lines": [],
                "prov": [],
                "reason": "every site's summary is junk",
            }
            rec["n_junk_sites"] = 2
        else:
            rec = _rec(item, 44)
        v9.append({**rec, "summarizer": "interp-v2"})
        site0 = (["sorry"] if k < 16 else ["bread"]) + ([] if k == 0 else ["weapons"])
        bags.append(
            {"id": item, "layer": 44, "pos": 0, "tokens": site0, "scores": [1.0] * len(site0)}
        )
        bags.append({"id": item, "layer": 44, "pos": 1, "tokens": ["oven"], "scores": [1.0]})
        bags.append({"id": item, "layer": 48, "pos": 0, "tokens": ["sorry"], "scores": [1.0]})
    out = tmp / "gate"
    orj.write_jsonl(out / "sample.jsonl", sample)
    orj.write_jsonl(tmp / "v9.jsonl", v9)
    orj.write_jsonl(tmp / "bags.jsonl", bags)
    return argparse.Namespace(
        out=out,
        v9_records=tmp / "v9.jsonl",
        readouts=tmp / "bags.jsonl",
        limit=None,
        concurrency=4,
        rpm=600.0,
    )


def test_label_and_report_on_a_toy_world(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    args = _world(tmp_path)
    llm = FakeLabeller()
    asyncio.run(orj.label_async(args, llm))
    # per unit one bag call, per v9 decline line one line call, per off-topic line one topic call
    assert len(llm.calls) == 2 * (24 + 12 + 2)
    bag_user = next(
        u for m, u in llm.calls if "i00" not in u and "TOKEN LISTS:\n1." in u and "LINE" not in u
    )
    assert '"oven"' in bag_user and "pos" not in bag_user and "L44" not in bag_user
    topic_users = [u for m, u in llm.calls if "LINE:" in u and "TOKEN LISTS:" in u]
    assert all('"oven"' not in u for u in topic_users)  # only the line's own site's bag
    n = len(llm.calls)
    asyncio.run(orj.label_async(args, llm))
    assert len(llm.calls) == n  # the label cache answers a re-run
    orj.cmd_report(args)
    res = json.loads((args.out / "result.json").read_text())
    assert res["agreed"] == {
        "claims_decline": [11, 12],  # unit 11's line claims no decline
        "decline_recall": [12, 16],  # 16 units with a refusal word, 12 with a v9 decline line
        "topic_in_bag": [1, 2],  # unit 0's topic is not in its bag
    }
    assert res["agreed"] == res["all"]
    assert res["agreement"] == {
        "bag_refusal": [24, 24],
        "claims_decline": [12, 12],
        "topic_in_bag": [2, 2],
    }
    assert res["scores"]["v8"] == {
        "n_units_with_decline": 12,
        "n_decline_lines": 12,
        "n_off_topic": 0,
        "score": 0.0,
    }
    assert res["scores"]["v9"]["n_off_topic"] == 2 and res["scores"]["v9"]["n_decline_lines"] == 12
    assert (
        res["scores"]["v9"]["n_failed_units"] == 0 and res["scores"]["v9"]["n_all_junk_units"] == 1
    )
    text = (args.out / "RESULT.md").read_text()
    assert "claims_decline precision" in text and "91.7% (11/12) | PASS" in text
    assert "75.0% (12/16) | PASS" in text and "TOO FEW (n < 10)" in text
    assert "Verdict: PASS on the rates that can be gated" in text
    sheet = (args.out / "SHEET.md").read_text()
    assert "v9 off-topic line" in sheet and "a v9 decline line does not claim a decline" in sheet
    assert "refusal word in a bag, no v9 decline line" in sheet


def test_report_verdicts(tmp_path: Path) -> None:
    def m(c, r, t):
        return {"claims_decline": c, "decline_recall": r, "topic_in_bag": t}

    v = [x[2] for x in orj.verdicts(m((85, 100), (60, 100), (9, 10)))]
    assert v == ["PASS", "PASS", "PASS"]
    v = [x[2] for x in orj.verdicts(m((84, 100), (59, 100), (8, 10)))]
    assert v == ["FAIL", "FAIL", "FAIL"]
    assert orj.verdicts(m((9, 9), (60, 100), (0, 0)))[0][2].startswith("TOO FEW")
    # a labeller disagreement: out of the agreed rates, counted against the chain in `all`
    args = _world(tmp_path)
    asyncio.run(orj.label_async(args, FakeLabeller(disagree_on="(case 5)")))
    orj.cmd_report(args)
    res = json.loads((args.out / "result.json").read_text())
    assert res["agreement"]["claims_decline"] == [11, 12]
    assert res["agreed"]["claims_decline"] == [10, 11] and res["all"]["claims_decline"] == [10, 12]
    assert "labeller disagreement" in (args.out / "SHEET.md").read_text()


def test_label_needs_the_v9_chain_records(tmp_path: Path) -> None:
    args = _world(tmp_path)
    recs = orj.read_jsonl(args.v9_records)
    orj.write_jsonl(args.v9_records, recs[1:])
    with pytest.raises(SystemExit, match="run the v9 chain first"):
        asyncio.run(orj.label_async(args, FakeLabeller()))
    orj.write_jsonl(
        args.v9_records, [{k: v for k, v in r.items() if k != "summarizer"} for r in recs]
    )
    with pytest.raises(SystemExit, match="not interp-v2 records"):
        asyncio.run(orj.label_async(args, FakeLabeller()))
    # --limit: the first N sampled units only
    orj.write_jsonl(args.v9_records, recs)
    args.limit = 3
    llm = FakeLabeller()
    asyncio.run(orj.label_async(args, llm))
    assert len(orj.read_jsonl(args.out / "labels.jsonl")) == 3
    orj.cmd_report(args)
    assert "PARTIAL: a smoke run, not the gate" in (args.out / "RESULT.md").read_text()
