"""Summary mode (plan 0021 Part B) on buggy_code: the native read cell -> one digest -> Stage A on
the digest -> the unchanged Stage B; item states mirror native."""

import json
from pathlib import Path

import pytest
from summary_fakes import FakeLLM, digest_lines, summary_args

from wsbench import digest
from wsbench.evals.buggy_code import judge
from wsbench.evals.buggy_code import prompts as bp

REPO = Path(__file__).resolve().parents[1]
EX = REPO / "examples/readouts/buggy_code.jsonl"
BUGGY, BUGGY2, CLEAN = "py-quantity-refund-credit", "py-age-underflow", "py-clean-clamped-discount"
IDS = [BUGGY, BUGGY2, CLEAN]
SUFFIX = f"+{digest.DIGEST_PROMPT_VERSION}-blind-extractive+{digest.SUMMARY_JUDGE_VERSION}"


def _native(path: Path, out: Path, **kw):
    return summary_args(path, out, bp.PROMPT_VERSION, extra={"aggregate": "native"}, **kw)


def _summary(path: Path, out: Path, **kw):
    return summary_args(path, out, bp.PROMPT_VERSION, **kw)


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return path


def _judge(system: str, user: str, schema: dict) -> dict:
    if schema["name"] == "inferred_bug":
        assert system == bp.INFER_SYSTEM
        if "Output:" in user or "assert" in user:
            return {"claim": "bug", "inferred": "the total goes negative", "quote": "< 0"}
        return {"claim": "none", "inferred": "", "quote": ""}
    assert schema["name"] == "closeness" and system == bp.GRADE_SYSTEM
    return {"score": 8, "why": "matches"}


def test_one_digest_and_one_inference_per_item(tmp_path, monkeypatch):
    FakeLLM.install(monkeypatch, _judge)
    native = judge.run(_native(EX, tmp_path / "n", items=IDS))
    fake = FakeLLM.install(monkeypatch, _judge)
    r = judge.run(_summary(EX, tmp_path / "s", items=IDS))
    assert fake.n("digest") == 3 and fake.n("inferred_bug") == 3
    assert r.config["aggregate"] == "summary"
    assert r.config["prompt_version"] == bp.PROMPT_VERSION + SUFFIX
    assert r.config["prompt_version"] == digest.prompt_version(
        bp.PROMPT_VERSION, _summary(EX, tmp_path, items=IDS), family="buggy_code"
    )
    assert r.config["summarizer"]["model"] == digest.DEFAULT_MODEL
    assert r.config["source_kind"] == "prose" and r.config["kind"] == "prose"
    assert r.config["layers_judged"] == native.config["layers_judged"] == [60]
    assert r.metric == native.metric == "score" and r.chance == native.chance
    assert r.pinned_instrument == native.pinned_instrument  # default digest: judge pin decides
    assert set(r.counts) == set(native.counts)
    for k in ("n_expected_cells", "n_missing_cells", "n_unjudged_cells", "n_empty_cells"):
        assert r.counts[k] == native.counts[k], k
    assert r.extras["summary"]["count_units"]["unjudged/empty"] == "items"
    rows = {x["id"]: x for x in r.rows}
    assert all(x["layer"] == digest.SUMMARY_LAYER and x["read_layer"] == 60 for x in r.rows)
    assert rows[BUGGY]["summary_state"] == "summarized" and rows[BUGGY]["score"] == 8
    assert rows[BUGGY2]["claim"] == "none" and rows[BUGGY2]["score"] == bp.SILENT_SCORE["buggy"]
    assert rows[CLEAN]["claim"] == "bug" and rows[CLEAN]["score"] == 8
    assert r.value == pytest.approx((8 + 1 + 8) / 3)
    # Stage A reads the digest under the note, never the native k-samples header
    infer = [u for n, _s, u in fake.calls if n == "inferred_bug"]
    assert all(u.startswith(digest.SUMMARY_NOTE + "\n\nreadout digest:\n") for u in infer)
    assert all("readout samples (k=" not in u and "STATEMENT-" not in u for u in infer)
    assert all(digest_lines(u) for u in infer)
    # Stage B is unchanged: the same user message as native for the same inference
    grades = [u for n, _s, u in fake.calls if n == "closeness"]
    assert len(grades) == 2
    by_id = {it["id"]: it for it in judge.load_bank(judge.bank_path("buggy_code"))[1]}
    assert bp.render_grade(by_id[BUGGY], "bug", "the total goes negative") in grades


def test_digest_reads_only_the_native_max_pos_cell(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "r.jsonl",
        [
            {"id": BUGGY, "layer": 60, "pos": 10, "samples": ["EARLY position sample"]},
            {"id": BUGGY, "layer": 60, "pos": 42, "samples": ["# Output: EOF anchor sample"]},
            {"id": BUGGY, "layer": 56, "pos": 50, "samples": ["OTHER layer sample"]},
        ],
    )
    fake = FakeLLM.install(monkeypatch, _judge)
    r = judge.run(_summary(path, tmp_path / "s", items=[BUGGY]))
    (bundle,) = [u for n, _s, u in fake.calls if n == "digest"]
    assert "EOF anchor sample" in bundle
    assert "EARLY" not in bundle and "OTHER" not in bundle
    assert r.extras["n_rows_not_at_read_cell"] == 2
    assert r.rows[0]["summary_evidence"] == ["# Output: EOF anchor sample"]


def test_layer_override_and_single_layer_guard(tmp_path, monkeypatch):
    fake = FakeLLM.install(monkeypatch, _judge)
    path = _write(
        tmp_path / "r.jsonl",
        [
            {"id": BUGGY, "layer": 60, "pos": 3, "samples": ["L60 sample"]},
            {"id": BUGGY, "layer": 44, "pos": 3, "samples": ["# Output: L44 sample"]},
        ],
    )
    r = judge.run(_summary(path, tmp_path / "s", items=[BUGGY], layers=[44]))
    (bundle,) = [u for n, _s, u in fake.calls if n == "digest"]
    assert "L44 sample" in bundle and "L60" not in bundle
    assert r.rows[0]["read_layer"] == 44 and r.config["layers_judged"] == [44]
    with pytest.raises(SystemExit):
        judge.run(_summary(path, tmp_path / "s2", items=[BUGGY], layers=[44, 60]))


def test_empty_digest_scores_silent(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "r.jsonl",
        [
            {"id": BUGGY, "layer": 60, "pos": 1, "samples": ["  ", ""]},  # blank cell
            {"id": CLEAN, "layer": 60, "pos": 1, "samples": ["some code echo"]},
        ],
    )
    fake = FakeLLM.install(monkeypatch, _judge, digest=lambda u: {"items": []})
    r = judge.run(_summary(path, tmp_path / "s", items=[BUGGY, CLEAN]))
    assert fake.n("digest") == 1  # the blank cell makes no digest call
    assert fake.n("inferred_bug") == 0 and fake.n("closeness") == 0
    rows = {x["id"]: x for x in r.rows}
    for i, src in ((BUGGY, "buggy"), (CLEAN, "clean")):
        assert rows[i]["judged"] and rows[i]["empty"] and rows[i]["claim"] == "none"
        assert rows[i]["score"] == bp.SILENT_SCORE[src]
        assert rows[i]["summary_state"] == "empty"
    assert r.counts["n_empty_cells"] == 2 and r.counts["n_unjudged_cells"] == 0
    assert r.value == pytest.approx(3.0)


def test_unsummarized_item_is_unjudged(tmp_path, monkeypatch):
    def failing(user: str) -> dict | None:
        if "-53.51" in user:
            return None
        return {"items": [{"statement": "s", "where": "", "evidence": "not in any sample"}]}

    fake = FakeLLM.install(monkeypatch, _judge, digest=failing)
    r = judge.run(_summary(EX, tmp_path / "s", items=[BUGGY, BUGGY2]))
    # BUGGY: failed call; BUGGY2: every digest item fails the evidence check
    assert fake.n("inferred_bug") == 0
    rows = {x["id"]: x for x in r.rows}
    for i in (BUGGY, BUGGY2):
        assert not rows[i]["judged"] and rows[i]["score"] is None
        assert rows[i]["summary_state"] == "unsummarized" and rows[i]["missing"] is False
    assert r.counts["n_unjudged_cells"] == 2 and r.counts["n_empty_cells"] == 0
    assert r.extras["n_buggy_judged"] == 0 and r.ci95 is None
    assert r.complete is False
    assert r.extras["summary"]["n_items_unsummarized"] == 2


def test_failed_stage_is_unjudged_and_missing_keeps_native_path(tmp_path, monkeypatch):
    def flaky(system: str, user: str, schema: dict) -> dict | None:
        return None if schema["name"] == "closeness" else _judge(system, user, schema)

    FakeLLM.install(monkeypatch, flaky)
    missing_id = "py-clean-clamp-max"
    r = judge.run(
        _summary(EX, tmp_path / "s", items=[BUGGY, BUGGY2, missing_id], allow_missing=True)
    )
    rows = {x["id"]: x for x in r.rows}
    assert not rows[BUGGY]["judged"] and rows[BUGGY]["score"] is None  # grade failed
    assert rows[BUGGY2]["judged"] and rows[BUGGY2]["score"] == bp.SILENT_SCORE["buggy"]
    assert rows[missing_id]["missing"] and "summary_state" not in rows[missing_id]
    assert r.counts["n_missing_cells"] == 1 and r.counts["n_unjudged_cells"] == 1
    assert r.value == pytest.approx(1.0)
    with pytest.raises(SystemExit):
        judge.run(_summary(EX, tmp_path / "s2", items=[BUGGY, missing_id]))


def test_pinned_follows_digest(tmp_path, monkeypatch):
    FakeLLM.install(monkeypatch, _judge)
    a = _summary(EX, tmp_path / "s", items=[BUGGY])
    r = judge.run(a)
    assert r.pinned_instrument == digest.pinned(a, family="buggy_code")
    b = _summary(EX, tmp_path / "s2", items=[BUGGY], extra={"summ_effort": "low"})
    r2 = judge.run(b)
    assert r2.pinned_instrument is False and r2.config["summarizer"]["reasoning"] == {
        "anthropic_effort": "low"
    }


def test_native_prompts_and_version_unchanged():
    assert bp.PROMPT_VERSION == "buggy-score-2026-09-23"
    assert bp.INFER_USER == "readout samples (k={k}):\n{samples}"
    assert bp.render_infer(["a"]) == "readout samples (k=1):\n[0] a"
    assert bp.render_infer_summary("NOTE", "[one] x") == "NOTE\n\nreadout digest:\n[one] x"
    for k in ("INFER_USER_SUMMARY", "DIGEST_TASK_NOTE", "SUMMARY_NOTE", "SUMMARY_NOTE_TOKENS"):
        assert k in bp.PROMPTS
    # the task note never names the bug, the source or the answer
    assert not any(w in bp.DIGEST_TASK_NOTE.lower() for w in ("bug", "clean", "correct"))
    assert "buggy_code" not in digest.NATIVE_ONLY
