"""digest hierarchical overflow (plan 0021): real-token fit, chunks + final union, persisted
fallback on refusal / too long, cached counts, dry run without network."""

from pathlib import Path

import pytest

from wsbench import digest, llm
from wsbench.cache import Cache
from wsbench.judge_config import JudgeConfig, resolve
from wsbench.llm import Spend
from wsbench.readouts import Cell
from wsbench.registry import JudgeArgs

FAM = "test_hier_family"


def _args(tmp_path: Path, dry: bool = False, **extra: str) -> JudgeArgs:
    return JudgeArgs(
        readouts=tmp_path / "r.jsonl",
        out=tmp_path / "out",
        judge=resolve(JudgeConfig(prompt_version="t")),
        layers=None,
        items=None,
        limit=0,
        allow_missing=False,
        concurrency=1,
        rpm=1.0,
        dry_run=dry,
        extra={"aggregate": "summary", **extra},
    )


@pytest.fixture
def fam(monkeypatch):
    monkeypatch.setitem(
        digest.FAMILY_DEFAULTS,
        FAM,
        {"summ_overflow": "hierarchical", "summ_token_cap": "1000", "summ_max": "5"},
    )


class Fake:
    """count_tokens returns ``tokens_per_char`` x len(user); stream_json picks the first bundle
    line of each prompt (or a scripted marker by substring)."""

    def __init__(self, monkeypatch, tokens_per_char: float = 1.0):
        self.counts: list[int] = []
        self.calls: list[str] = []
        self.script: dict[str, object] = {}  # substring -> result
        self.exact: dict[str, object] = {}  # whole user prompt -> result
        self.tpc = tokens_per_char
        monkeypatch.setattr(llm, "count_tokens", self.count_tokens)
        monkeypatch.setattr(llm, "stream_json", self.stream_json)

    def count_tokens(self, model, prompts, concurrency=8):
        self.counts.append(len(prompts))
        return [int(len(u) * self.tpc) for _s, u in prompts]

    def stream_json(self, prompts, *, on_result, **kw):
        assert kw.get("mark_refusals") is True and kw.get("attempts") == digest.DIGEST_ATTEMPTS
        for i, (_s, user) in enumerate(prompts):
            self.calls.append(user)
            hit = self.exact.get(user)
            if hit is None:
                hit = next((v for k, v in self.script.items() if k in user), None)
            if hit is not None:
                on_result(i, hit)
                continue
            lines = [ln[2:] for ln in user.splitlines() if ln.startswith("- ")]
            on_result(
                i,
                {
                    "items": [
                        {"statement": "s", "key": "", "where": "", "evidence": ln}
                        for ln in lines[:2]
                    ]
                },
            )
        return Spend()


def _cells(item: str, n_layers: int, text: str = "word") -> list[Cell]:
    return [
        Cell(item, 20 + 4 * L, p, (f"{text} {item} L{L} p{p} " + "x" * 40,), None, None, None)
        for L in range(n_layers)
        for p in range(3)
    ]


def _run(tmp_path, cells_by_item, **kw):
    with Cache(tmp_path / "c.jsonl") as cache:
        return digest.collapse(
            FAM,
            cells_by_item,
            args=_args(tmp_path, **kw),
            cache=cache,
            spend=Spend(),
            preflight=None,
            task_note="",
        )


def test_fits_is_one_call_and_counts_are_cached(tmp_path, fam, monkeypatch):
    fake = Fake(monkeypatch, tokens_per_char=0.5)
    col = _run(tmp_path, {"a": _cells("a", 2)})
    assert len(fake.calls) == 1 and not col.chunks and col.summaries["a"]
    assert col.row("a")["summary_hierarchical"] is False
    _run(tmp_path, {"a": _cells("a", 2)})
    assert fake.counts == [1] and len(fake.calls) == 1  # rerun: count and digest cached


def test_over_budget_chunks_at_layer_boundaries_then_final(tmp_path, fam, monkeypatch):
    fake = Fake(monkeypatch, tokens_per_char=1.0)
    col = _run(tmp_path, {"a": _cells("a", 6)})
    n = col.chunks["a"]
    assert n >= 2 and len(fake.calls) == n + 1  # n chunk calls + 1 final
    final = fake.calls[-1]
    assert "(support:" in final and "[L20 pos 0]" in final
    assert col.row("a")["summary_hierarchical"] is True and col.row("a")["summary_chunks"] == n
    # every chunk is a run of whole layers (the bundle has 3 positions per layer)
    for user in fake.calls[:-1]:
        heads = [ln for ln in user.splitlines() if ln.startswith("[L")]
        assert len(heads) % 3 == 0
    # evidence is verbatim original samples
    assert all("x" * 40 in e.text for e in col.evidence["a"])


def test_refused_single_falls_back_to_chunks_and_is_persisted(tmp_path, fam, monkeypatch):
    fake = Fake(monkeypatch, tokens_per_char=0.3)
    cells = {"a": _cells("a", 4)}
    first_user = digest.render_user(digest._render([digest._group(c) for c in cells["a"]]))
    fake.exact[first_user] = llm.REFUSED_JSON  # only the whole-bundle call is refused
    col = _run(tmp_path, cells)
    assert col.diag["n_digest_refused"] == 1 and col.chunks.get("a", 0) >= 2
    assert col.summaries["a"]
    n_before = len(fake.calls)
    _run(tmp_path, cells)
    rerun_users = fake.calls[n_before:]
    assert first_user not in rerun_users  # the known-refused single call is never re-sent


def test_chunk_outcomes(tmp_path, fam, monkeypatch):
    fake = Fake(monkeypatch, tokens_per_char=1.0)
    fake.script["word"] = {"items": []}  # every chunk reports nothing
    col = _run(tmp_path, {"a": _cells("a", 6)})
    assert "a" in col.empty
    fake2_dir = tmp_path / "b"
    fake2_dir.mkdir()
    fake.script = {
        "word": {"items": [{"statement": "s", "key": "", "where": "", "evidence": "not there"}]}
    }
    col = _run(fake2_dir, {"a": _cells("a", 6)})
    assert "a" in col.unsummarized


def test_dry_run_makes_no_network_call(tmp_path, fam, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network in dry run")

    monkeypatch.setattr(llm, "count_tokens", boom)
    monkeypatch.setattr(llm, "stream_json", boom)
    col = _run(tmp_path, {"a": _cells("a", 6)}, dry=True)
    assert col.unsummarized == {"a"}


def test_family_defaults_reach_settings_and_config(tmp_path, fam):
    a = _args(tmp_path)
    s = digest.settings(a, FAM)
    assert (s.overflow, s.token_cap, s.max_items) == ("hierarchical", 1000, 5)
    assert digest.config(a, "prose", family=FAM)["summarizer"]["overflow"] == "hierarchical"
    assert digest.settings(a).overflow == "stride"  # no family: plan 0020 defaults
    override = _args(tmp_path, summ_max="7")
    assert digest.settings(override, FAM).max_items == 7  # opts beat family defaults
