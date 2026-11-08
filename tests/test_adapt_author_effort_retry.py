"""author(): a call that stops at max_tokens with no text block (all thinking) is retried once at
adaptive thinking + effort=low (2026-11-08 hotfix, plan 0075 run)."""

import copy
import json
from types import SimpleNamespace

import pytest

from wsbench.adapt import author, store

FAM = "conjunctive_association"
MODEL = "qwen3.5-4b"


@pytest.fixture
def mdir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "model_dir", lambda model, root=None: tmp_path / model)
    monkeypatch.setattr(store, "code_stamp", lambda: {})
    return tmp_path / MODEL


def _obj(cluster="q01") -> dict:
    reg = [it for it in store.original_bank(FAM)[1] if it["cluster"] == cluster]
    return copy.deepcopy(author._cluster_obj(reg))


def _empty():
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="...")],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=32000),
                           stop_reason="max_tokens")  # fmt: skip


def _ok():
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps([_obj("q01")]))],
                           usage=None, stop_reason="end_turn")  # fmt: skip


def _session(monkeypatch, replies, calls):
    from wsbench.adapt import auth

    class Session:
        pushed = 0

        def stream_final(self, **kw):
            calls.append(kw)
            return replies.pop(0)

        def refresh(self, seen):
            return False

    monkeypatch.setattr(auth, "Session", Session)


def test_empty_max_tokens_reply_is_retried_at_low_effort(mdir, monkeypatch, capsys):
    calls: list[dict] = []
    _session(monkeypatch, [_empty(), _ok()], calls)
    assert author.author(MODEL, FAM, "t1", 1, wave=100) == 10
    assert len(calls) == 2
    assert "thinking" not in calls[0] and "output_config" not in calls[0]  # model default first
    assert calls[1]["thinking"] == {"type": "adaptive"}
    assert calls[1]["output_config"] == {"effort": "low"}
    assert calls[1]["messages"] == calls[0]["messages"]  # the same brief
    out = capsys.readouterr().out
    assert "call 0: no text after max_tokens of thinking; low-effort retry" in out


def test_retry_happens_once(mdir, monkeypatch):
    calls: list[dict] = []
    _session(monkeypatch, [_empty(), _empty()], calls)
    # the low-effort reply is empty too: no third call
    assert author.author(MODEL, FAM, "t1", 1, wave=100) == 0
    assert len(calls) == 2


def test_text_reply_at_max_tokens_is_not_retried(mdir, monkeypatch):
    calls: list[dict] = []
    reply = _ok()
    reply.stop_reason = "max_tokens"  # truncated but has text: parsed as before, no retry
    _session(monkeypatch, [reply], calls)
    assert author.author(MODEL, FAM, "t1", 1, wave=100) == 10
    assert len(calls) == 1 and "output_config" not in calls[0]
