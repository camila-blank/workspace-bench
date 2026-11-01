"""Plan 0059 digest options: route-aware digest reasoning and fallback, ``summ_context`` and
``summ_cache_only`` (replay a seeded digest cache with no call)."""

from pathlib import Path

import pytest
from conftest import native_args

from wsbench import digest, llm, runner
from wsbench.cache import Cache
from wsbench.judge_config import JudgeConfig, resolve
from wsbench.llm import Spend
from wsbench.readouts import Cell
from wsbench.registry import JudgeArgs

FAM = "test_sweep_family"
OR_MODEL = "deepseek/deepseek-v4-flash"


def _args(tmp_path: Path, **extra: str) -> JudgeArgs:
    return native_args(
        readouts=tmp_path / "r.jsonl",
        out=tmp_path / "out",
        judge=resolve(JudgeConfig(prompt_version="t")),
        layers=None,
        items=None,
        limit=0,
        allow_missing=False,
        concurrency=1,
        rpm=1.0,
        dry_run=False,
        extra={"aggregate": "summary", **extra},
    )


# ---------------------------------------------------------------- settings (change 1, 2)


def test_claude_digest_reasoning_and_fingerprint_unchanged(tmp_path):
    s = digest.settings(_args(tmp_path))
    assert s.reasoning == {"anthropic_effort": "default"}
    assert digest.settings(_args(tmp_path, summ_effort="low")).reasoning == {
        "anthropic_effort": "low"
    }
    assert "context" not in s.to_json()  # the default context keeps to_json (and fps) as is


def test_openrouter_digest_reasoning(tmp_path):
    s = digest.settings(_args(tmp_path, summ_model=OR_MODEL))
    assert s.reasoning is None and s.fallback == "none"
    for level in ("low", "medium", "high"):
        s = digest.settings(_args(tmp_path, summ_model=OR_MODEL, summ_effort=level))
        assert s.reasoning == {"effort": level}
    assert not digest.pinned(_args(tmp_path, summ_model=OR_MODEL))


def test_explicit_fallback_with_non_claude_digest_exits_2(tmp_path):
    for fb in ("default", "default+claude-sonnet-5"):
        with pytest.raises(SystemExit) as e:
            digest.settings(_args(tmp_path, summ_model=OR_MODEL, summ_fallback=fb))
        assert e.value.code == 2
        with pytest.raises(SystemExit):
            runner.parse_opts([f"summ_model={OR_MODEL}", f"summ_fallback={fb}"])
    s = digest.settings(_args(tmp_path, summ_model=OR_MODEL, summ_fallback="none"))
    assert s.fallback == "none"


def test_family_default_fallback_becomes_none_for_non_claude(tmp_path):
    fam = "jailbreak_recognition"
    assert "+" in digest.FAMILY_DEFAULTS[fam]["summ_fallback"]
    a = _args(tmp_path, summ_model=OR_MODEL)
    assert digest.settings(a, fam).fallback == "none"
    assert digest.config(a, "prose", family=fam)["summarizer"]["fallback"] == "none"
    # a Claude digest keeps the family's refusal rescue
    assert digest.settings(_args(tmp_path), fam).fallback == "default+claude-sonnet-5"


def test_summ_context(tmp_path):
    s = digest.settings(_args(tmp_path, summ_context="200000"))
    assert s.context == 200_000 and s.to_json()["context"] == 200_000
    assert digest.settings(_args(tmp_path, summ_context=str(digest.MIN_CONTEXT))).context
    for bad in ("127999", "0", "x"):
        with pytest.raises(SystemExit) as e:
            digest.settings(_args(tmp_path, summ_context=bad))
        assert e.value.code == 2
    assert {"summ_context", "summ_cache_only"} <= digest.KNOWN_KEYS


# ---------------------------------------------------------------- fakes


@pytest.fixture
def fam(monkeypatch):
    monkeypatch.setitem(
        digest.FAMILY_DEFAULTS,
        FAM,
        {"summ_overflow": "hierarchical", "summ_max": "5", "summ_layers": "joint"},
    )


class Fake:
    """count_tokens: ``tpc`` x len(user) (or ``None``); stream_json answers the first two bundle
    lines (``script``: substring -> result). ``live=False`` fails on any call."""

    def __init__(self, monkeypatch, tpc: float | None = 0.3):
        self.calls: list[str] = []
        self.counts = 0
        self.tpc = tpc
        self.live = True
        self.script: dict[str, object] = {}
        monkeypatch.setattr(llm, "count_tokens", self.count_tokens)
        monkeypatch.setattr(llm, "stream_json", self.stream_json)

    def count_tokens(self, model, prompts, concurrency=8):
        assert self.live, "count_tokens under summ_cache_only"
        self.counts += 1
        return [None if self.tpc is None else int(len(u) * self.tpc) for _s, u in prompts]

    def stream_json(self, prompts, *, on_result, **kw):
        assert self.live, "a digest call under summ_cache_only"
        for i, (_s, user) in enumerate(prompts):
            self.calls.append(user)
            hit = next((v for k, v in self.script.items() if k in user), None)
            if hit is not None:
                on_result(i, hit)
                continue
            lines = [ln[2:] for ln in user.splitlines() if ln.startswith("- ")]
            items = [{"statement": "s", "key": "", "where": "", "evidence": ln} for ln in lines[:2]]
            on_result(i, {"items": items})
        return Spend()


def _cells(item: str, n_layers: int) -> list[Cell]:
    return [
        Cell(item, 20 + 4 * L, p, (f"word {item} L{L} p{p} " + "x" * 40,), None, None, None)
        for L in range(n_layers)
        for p in range(3)
    ]


def _run(tmp_path, cells_by_item, name=FAM, preflight=None, **extra):
    with Cache(tmp_path / "c.jsonl") as cache:
        return digest.collapse(
            name,
            cells_by_item,
            args=_args(tmp_path, **extra),
            cache=cache,
            spend=Spend(),
            preflight=preflight,
            task_note="",
        )


def _boom() -> None:
    raise AssertionError("digest preflight under summ_cache_only")


def _same(a: digest.Collapse, b: digest.Collapse) -> None:
    assert a.summaries == b.summaries
    assert a.unsummarized == b.unsummarized and a.empty == b.empty


# ---------------------------------------------------------------- summ_cache_only (change 3)


def test_cache_only_hierarchical_full_cache_makes_no_call(tmp_path, fam, monkeypatch):
    monkeypatch.setattr(digest, "COUNT_TOKENS_ABOVE", 0.0)  # count every bundle (ntok rows)
    monkeypatch.setattr(digest, "COLLAPSE_READOUTS", 1)  # every digest of 2 entries re-asks
    monkeypatch.setitem(digest.FAMILY_DEFAULTS[FAM], "summ_token_cap", "1000")
    fake = Fake(monkeypatch, tpc=1.0)  # 6 layers do not fit: chunks + final
    cells = {"a": _cells("a", 6), "b": _cells("b", 1)}
    first = _run(tmp_path, cells)
    assert first.chunks.get("a", 0) >= 2 and first.diag["n_digest_reasked"] >= 1
    assert first.diag["n_digest_calls"] == len(fake.calls) + 2  # + one count per item
    fake.live = False
    again = _run(tmp_path, cells, preflight=_boom, summ_cache_only="1")
    _same(first, again)
    assert again.diag["n_digest_reasked"] == first.diag["n_digest_reasked"]
    assert again.diag["n_digest_calls"] == 0 and again.diag["n_reask_cache_miss"] == 0
    assert again.diag["n_digest_cached_failed"] == 0 and again.diag["n_ntok_estimated"] == 0
    ex = again.extras()
    assert ex["n_digest_calls"] == 0 and ex["summary_unsummarized_ids"] == []
    assert ex["summary_empty_ids"] == [] and "summary_reask_cache_miss_ids" not in ex


def test_cache_only_newest_failed_row_is_a_failed_call(tmp_path, fam, monkeypatch):
    fake = Fake(monkeypatch)
    cells = {"a": _cells("a", 1)}
    fake.script["word"] = None  # stream_json skips on_result -> fake a failed call below
    real = fake.stream_json

    def failing(prompts, *, on_result, **kw):
        for i, _p in enumerate(prompts):
            on_result(i, None)  # a call that failed after its retries (cached as failed)
        return Spend()

    monkeypatch.setattr(llm, "stream_json", failing)
    first = _run(tmp_path, cells)
    assert "a" in first.unsummarized
    monkeypatch.setattr(llm, "stream_json", real)
    fake.live = False
    again = _run(tmp_path, cells, summ_cache_only="1")
    assert "a" in again.unsummarized and again.diag["n_digest_calls"] == 0
    assert again.diag["n_digest_cached_failed"] == 1
    assert again.extras()["summary_unsummarized_ids"] == ["a"]


def test_cache_only_no_level_row_is_level_zero_and_ntok_miss_estimates(tmp_path, fam, monkeypatch):
    monkeypatch.setattr(digest, "COUNT_TOKENS_ABOVE", 0.0)
    fake = Fake(monkeypatch, tpc=None)  # every count fails: est x 1.7, no ntok row (as R4)
    cells = {"a": _cells("a", 2)}
    first = _run(tmp_path, cells)
    assert first.summaries["a"] and not first.chunks
    with Cache(tmp_path / "c.jsonl") as cache:
        keys = set(cache._rows)
    assert not any(k.endswith("|level") or k.startswith("ntok:") for k in keys)
    fake.live = False
    again = _run(tmp_path, cells, summ_cache_only="1")
    _same(first, again)
    assert again.diag["n_ntok_estimated"] == 1 and again.diag["n_digest_calls"] == 0


def test_cache_only_primary_miss_exits_2_before_any_call(tmp_path, fam, monkeypatch, capsys):
    fake = Fake(monkeypatch)
    fake.live = False
    with pytest.raises(SystemExit) as e:
        _run(tmp_path, {"a": _cells("a", 1)}, summ_cache_only="1")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert FAM in err and "digest:a|all" in err and "summ_cache_only" in err


def test_cache_only_reask_miss_keeps_the_first_pass(tmp_path, fam, monkeypatch):
    fake = Fake(monkeypatch)
    cells = {"a": _cells("a", 2)}
    first = _run(tmp_path, cells)  # COLLAPSE_READOUTS = 100: no re-ask in the seed
    assert first.diag["n_digest_reasked"] == 0
    monkeypatch.setattr(digest, "COLLAPSE_READOUTS", 1)  # the replay reaches a re-ask
    fake.live = False
    again = _run(tmp_path, cells, summ_cache_only="1")
    _same(first, again)
    assert again.diag["n_digest_reasked"] == 1 and again.diag["n_reask_cache_miss"] == 1
    assert again.diag["n_digest_reask_better"] == 0 and again.diag["n_digest_calls"] == 0
    assert again.extras()["summary_reask_cache_miss_ids"] == ["a"]


def test_cache_only_stride(tmp_path, monkeypatch):
    fake = Fake(monkeypatch)
    cells = {"a": [Cell("a", 20, 1, ("alpha",), None, None, None)]}
    first = _run(tmp_path, cells, name="basic_readout", summ_overflow="stride")
    assert first.summaries["a"] and first.diag["n_digest_calls"] == 1
    fake.live = False
    again = _run(
        tmp_path,
        cells,
        name="basic_readout",
        preflight=_boom,
        summ_overflow="stride",
        summ_cache_only="1",
    )
    _same(first, again)
    assert again.diag["n_digest_calls"] == 0
    with pytest.raises(SystemExit) as e:  # a miss
        _run(
            tmp_path / "x",
            cells,
            name="basic_readout",
            summ_overflow="stride",
            summ_cache_only="1",
        )
    assert e.value.code == 2


def test_cache_only_stride_with_refusal_model_exits_up_front(tmp_path):
    opts = ["summ_overflow=stride", "summ_fallback=default+claude-sonnet-5", "summ_cache_only=1"]
    with pytest.raises(SystemExit) as e:
        runner.parse_opts(opts)
    assert e.value.code == 2
    # a family default refusal model too (jailbreak_recognition), at the family's settings
    a = _args(tmp_path, summ_overflow="stride", summ_cache_only="1")
    with pytest.raises(SystemExit):
        digest.settings(a, "jailbreak_recognition")
    # server-side only (no "+<model>"), or hierarchical: allowed
    digest.settings(_args(tmp_path, summ_overflow="stride", summ_fallback="default"))
    digest.settings(_args(tmp_path, summ_cache_only="1"), "jailbreak_recognition")


def test_cache_only_is_outside_every_fingerprint_and_stamped(tmp_path, fam):
    plain, only = _args(tmp_path), _args(tmp_path, summ_cache_only="1")
    s0, s1 = digest.settings(plain, FAM), digest.settings(only, FAM)
    assert s0.to_json() == s1.to_json()
    assert digest._digest_fp(s0, "n", "T") == digest._digest_fp(s1, "n", "T")
    assert digest.prompt_version("pv", plain, FAM) == digest.prompt_version("pv", only, FAM)
    assert digest.pinned(plain, FAM) == digest.pinned(only, FAM)
    assert "summarizer_cache_only" not in digest.config(plain, "prose", FAM)
    assert digest.config(only, "prose", FAM)["summarizer_cache_only"] is True
    with pytest.raises(SystemExit):
        digest.settings(_args(tmp_path, summ_cache_only="yes"))


def test_summ_context_sets_the_hierarchical_budget(tmp_path, fam, monkeypatch, capsys):
    # one ~60k-token bundle: one call at the default 1M context; chunks at summ_context=128k
    # (budget 128 000 - 32 000 - 64 000 = 32 000)
    monkeypatch.setattr(digest, "COUNT_TOKENS_ABOVE", 0.0)
    fake = Fake(monkeypatch, tpc=60.0)
    cells = {"a": _cells("a", 6)}
    col = _run(tmp_path / "big", cells)
    assert not col.chunks and len(fake.calls) == 1
    col = _run(tmp_path / "small", cells, summ_context=str(digest.MIN_CONTEXT))
    assert "budget 32000 tokens" in capsys.readouterr().out
    assert col.chunks.get("a", 0) >= 2


def test_digest_path_writes_only_seed_keys(tmp_path, fam, monkeypatch):
    """Key coverage: every row the digest path writes (single, chunks, final, re-ask, level,
    ntok; stride) is one a cache-only seed keeps (``digest.is_digest_key``)."""
    monkeypatch.setattr(digest, "COUNT_TOKENS_ABOVE", 0.0)
    monkeypatch.setattr(digest, "COLLAPSE_READOUTS", 1)
    monkeypatch.setitem(digest.FAMILY_DEFAULTS[FAM], "summ_token_cap", "1000")
    written: list[str] = []
    real_put = Cache.put

    def spy(self, key, fp, payload):
        written.append(key)
        return real_put(self, key, fp, payload)

    monkeypatch.setattr(Cache, "put", spy)
    fake = Fake(monkeypatch, tpc=1.0)
    cells = {"a": _cells("a", 4)}
    whole = digest.render_user(digest._render([digest._group(c) for c in cells["a"]]))
    fake.script[whole] = llm.REFUSED_JSON  # a level row
    first = _run(tmp_path, {**cells, "b": _cells("b", 6)})
    assert first.chunks and first.diag["n_digest_reasked"] >= 1
    stride = {"c": [Cell("c", 20, 1, ("alpha",), None, None, None)]}
    _run(tmp_path / "s", stride, name="basic_readout", summ_overflow="stride")
    kinds = {
        "level": any(k.endswith("|level") for k in written),
        "ntok": any(k.startswith("ntok:") for k in written),
        "chunk": any("|h" in k and "of" in k for k in written),
        "final": any(k.endswith("|final") for k in written),
        "stride": "digest:c|all" in written,
    }
    assert all(kinds.values()), kinds
    assert written and all(digest.is_digest_key(k) for k in written)
    assert not digest.is_digest_key("ab12|L20|p3")  # a judge-style key
