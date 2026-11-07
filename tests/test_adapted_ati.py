"""Adapted-set ATI subsets (plan 0079 §6), offline: the all-readers-fail verdict, ``gate_adapted``
on the panel layout with the set's subject in the Qwen slot, the per-set ATI file and the
root-aware ``subset_entry`` (bank_sha guard on adapted entries; no root = the registered file)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wsbench import readplan, registry
from wsbench.baselines import text_inversion as ti

F, T, N = False, True, None


@pytest.mark.parametrize(
    ("subject", "frontier", "want"),
    [
        ("failed", ["failed", "failed", "failed"], "ati"),  # nobody reads it
        ("failed", ["solved", "failed", "failed"], "invertible"),  # one frontier reader does
        ("solved", ["failed", "failed", "failed"], "invertible"),  # the subject itself does
        ("failed", ["failed", "undecided", "failed"], "unverified"),
        ("undecided", ["failed", "failed", "failed"], "unverified"),
        ("undecided", ["solved", "failed", "failed"], "invertible"),
    ],
)
def test_all_fail_verdict(subject, frontier, want):
    assert ti.all_fail_verdict(subject, frontier) == want


def _write(out: Path, answerer: str, fam: str, verdicts: dict[str, list]) -> None:
    for d in range(3):
        af = ti.answers_file(out, answerer, d)
        af.parent.mkdir(parents=True, exist_ok=True)
        with af.open("a") as fh:
            for i in verdicts:
                r = {"family": fam, "id": i, "draw": d, "answer": "text", "error": ""}
                fh.write(json.dumps(r) + "\n")
        rows = [{"id": i, "pass": v[d]} for i, v in verdicts.items()]
        decided = [r["pass"] for r in rows if r["pass"] is not None]
        value = sum(decided) / len(decided) if decided else None
        p = out / "judged" / answerer / f"draw{d}" / fam
        p.mkdir(parents=True, exist_ok=True)
        (p / "results.json").write_text(
            json.dumps(
                {
                    "family": fam,
                    "config": {"judge_model": "claude-opus-5-5", "layers_judged": [16]},
                    "numbers": {"metric": "pass_rate", "value": value},
                    "rows": rows,
                }
            )
        )


@pytest.fixture
def adapted(tmp_path, monkeypatch):
    root = tmp_path / "adapted" / "qwen3.5-4b"
    root.mkdir(parents=True)
    (root / "report.json").write_text(
        json.dumps({"model": "qwen3.5-4b", "hf_id": "Qwen/Qwen3.5-4B"})
    )
    monkeypatch.setenv("WSB_BANK_ROOT", str(root))
    ids = {"fam": list("abcde")}
    monkeypatch.setattr(ti, "adapted_gated_families", lambda: ["fam"])
    monkeypatch.setattr(ti, "gated_families", lambda: ["fam"])
    monkeypatch.setattr(readplan, "plan", lambda f: [SimpleNamespace(id=i) for i in ids[f]])
    monkeypatch.setattr(
        registry, "get", lambda f: SimpleNamespace(judge=SimpleNamespace(prompt_version="v1"))
    )
    monkeypatch.setattr(registry, "load_all", lambda: None)
    monkeypatch.setattr(ti, "bank_fingerprint", lambda f: f"sha-{f}")
    out = tmp_path / "ti"
    # a: all fail -> ATI; b: astra solves once -> out; c: the subject solves -> out;
    # d: opus undecided on a draw -> unverified (out); e: all fail -> ATI
    _write(out, "subject", "fam", {"a": [F] * 3, "b": [F] * 3, "c": [F, T, F], "d": [F] * 3,
                                   "e": [F] * 3})  # fmt: skip
    _write(out, "opus", "fam", {"a": [F] * 3, "b": [F] * 3, "c": [F] * 3, "d": [F, N, F],
                                "e": [F] * 3})  # fmt: skip
    _write(out, "astra", "fam", {i: [F] * 3 for i in "acde"} | {"b": [F, T, F]})
    _write(out, "argon", "fam", {i: [F] * 3 for i in "abcde"})
    (out / "prompts.jsonl").write_text(
        "".join(json.dumps({"family": "fam", "id": i, "layer": 16}) + "\n" for i in "abcde")
    )
    return root, out


def test_gate_adapted_keeps_only_items_every_reader_fails(adapted):
    root, out = adapted
    e = ti.gate_adapted(out)["fam"]
    assert e["ids"] == ["a", "e"]
    assert (e["n_ati"], e["n_invertible"], e["n_unverified"]) == (2, 2, 1)
    assert e["gate"] == ti.ADAPTED_GATE_VERSION and e["bank_sha"] == "sha-fam"
    assert e["panel"]["subject"] == "Qwen/Qwen3.5-4B" and "qwen" not in e["panel"]
    assert e["status"]["c"] == ["solved", "failed", "failed", "failed"]
    assert e["pass_at_3"]["subject"] == 0.2 and e["instrument"] == "v1"
    written = json.loads((root / ti.ADAPTED_FILE).read_text())
    assert written["_source"]["gate"] == ti.ADAPTED_GATE_VERSION
    assert written["_source"]["set"] == "qwen3.5-4b"
    assert written["_not_gated"]["typo_mt"] == ti.NOT_GATED["typo_mt"]
    assert written["fam"]["ids"] == ["a", "e"]


def test_subset_entry_reads_the_set_file_and_guards_its_bank(adapted, monkeypatch):
    root, out = adapted
    ti.gate_adapted(out)
    assert ti.frozen_path() == root / ti.ADAPTED_FILE
    assert ti.subset("fam") == ["a", "e"]
    monkeypatch.setattr(ti, "bank_fingerprint", lambda f: "changed")
    with pytest.raises(SystemExit, match="bank_sha mismatch"):
        ti.subset_entry("fam")


def test_gate_adapted_refuses_not_gated_and_needs_a_root(adapted, monkeypatch):
    _, out = adapted
    with pytest.raises(ValueError, match="not gateable"):
        ti.gate_adapted(out, families=["typo_mt"])
    monkeypatch.delenv("WSB_BANK_ROOT")
    with pytest.raises(SystemExit, match="WSB_BANK_ROOT"):
        ti.gate_adapted(out)


def test_gate_adapted_families_merge_into_the_set_file(adapted):
    root, out = adapted
    path = root / ti.ADAPTED_FILE
    path.write_text(json.dumps({"other": {"ids": ["x"]}, "_not_gated": {"fam": "old"}}))
    ti.gate_adapted(out, families=["fam"])
    written = json.loads(path.read_text())
    assert written["other"] == {"ids": ["x"]} and written["fam"]["ids"] == ["a", "e"]
    assert "fam" not in written["_not_gated"]


def test_no_root_reads_the_registered_file(monkeypatch):
    monkeypatch.delenv("WSB_BANK_ROOT", raising=False)
    assert ti.frozen_path() == ti.FROZEN
    assert ti.subject_tokenizer() is None


def test_build_under_a_root_uses_the_subject_tokenizer(adapted, monkeypatch):
    root, _ = adapted
    seen = []

    def fake_tok(model):
        seen.append(model)
        raise RuntimeError("stop after the tokenizer choice")

    import wsbench.adapt.author as author

    monkeypatch.setattr(author, "tokenizer", fake_tok)
    with pytest.raises(RuntimeError, match="tokenizer choice"):
        ti.build(root.parent / "out", ["fam"])
    assert seen == ["qwen3.5-4b"]
