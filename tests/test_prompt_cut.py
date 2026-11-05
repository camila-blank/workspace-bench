"""plan 0069: the prompt-only control never reads a reply (``baselines.prompt_cut``)."""

import json
import sys
import types
from typing import ClassVar

import pytest

from wsbench.baselines import prompt_cut as pc
from wsbench.baselines import prompt_only_opus as po


class CharTok:
    """One token per character, special markers as single tokens; ``style`` picks the template:
    ``qwen`` (end marker, empty think block on the generation prompt), ``glm`` (no end marker,
    no think block) or ``gemma`` (a BOS before the first turn)."""

    SPECIALS: ClassVar[dict[str, list[str]]] = {
        "qwen": ["<|im_start|>", "<|im_end|>", "<think>", "</think>"],
        "glm": ["[gMASK]", "<|user|>", "<|assistant|>", "<|system|>"],
        "gemma": ["<bos>", "<start_of_turn>", "<end_of_turn>"],
    }

    def __init__(self, style: str = "qwen", template: bool = True):
        self.style = style
        self.chat_template = "x" if template else None
        self.specials = self.SPECIALS[style]
        self.vocab: dict[str, int] = {s: i for i, s in enumerate(self.specials)}

    def _id(self, piece: str) -> int:
        return self.vocab.setdefault(piece, len(self.vocab))

    def encode(self, text: str) -> list[int]:
        out, i = [], 0
        while i < len(text):
            for s in self.specials:
                if text.startswith(s, i):
                    out.append(self._id(s))
                    i += len(s)
                    break
            else:
                out.append(self._id(text[i]))
                i += 1
        return out

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": self.encode(text)}

    def decode(self, ids, **_):
        inv = {v: k for k, v in self.vocab.items()}
        return "".join(inv[i] for i in ids)

    def convert_ids_to_tokens(self, ids):
        inv = {v: k for k, v in self.vocab.items()}
        return [inv[i] for i in ids]

    def render(self, messages, add_generation_prompt):
        s = self.style
        if s == "qwen":
            text = ""
            last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
            for i, m in enumerate(messages):
                body = m["content"]
                if m["role"] == "assistant" and i > last_user:
                    body = "<think>\n\n</think>\n\n" + body
                text += f"<|im_start|>{m['role']}\n{body}<|im_end|>\n"
            if add_generation_prompt:
                text += "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        elif s == "glm":
            text = "[gMASK]" + "".join(f"<|{m['role']}|>\n{m['content']}" for m in messages)
            if add_generation_prompt:
                text += "<|assistant|>\n"
        else:
            role = {"user": "user", "assistant": "model", "system": "user"}
            text = "<bos>" + "".join(
                f"<start_of_turn>{role[m['role']]}\n{m['content']}<end_of_turn>\n" for m in messages
            )
            if add_generation_prompt:
                text += "<start_of_turn>model\n"
        return text

    def apply_chat_template(self, messages, tokenize=True, add_generation_prompt=True, **_):
        text = self.render(messages, add_generation_prompt)
        return self.encode(text) if tokenize else text


def chat(tok, messages, gen=True, prefill=""):
    return tok.apply_chat_template(messages, add_generation_prompt=gen) + tok.encode(prefill)


U = [{"role": "user", "content": "What is the capital of France?"}]


@pytest.mark.parametrize("style", ["qwen", "glm", "gemma"])
def test_marks_and_final_turn_prefill_cut_at_generation_prompt(style):
    tok = CharTok(style)
    m = pc.turn_marks(tok)
    ids = chat(tok, U, prefill="My one-word answer is:")
    assert pc.reply_tokens(ids, m, tok) == len("My one-word answer is:")
    out = pc.prompt_only_ids(ids, len(ids) - 1, m, tok)
    assert out == chat(tok, U)  # the prompt through the generation prompt, nothing of the reply
    pc.assert_no_reply(out, m, tok)
    # a cut inside the reply ends at the same place
    assert pc.prompt_only_ids(ids, len(ids) - 5, m, tok) == chat(tok, U)


@pytest.mark.parametrize("style", ["qwen", "glm", "gemma"])
def test_no_reply_is_byte_identical(style):
    tok = CharTok(style)
    m = pc.turn_marks(tok)
    ids = chat(tok, U)
    for last in (5, len(ids) - 3, len(ids) - 1):  # a site in the prompt or the generation prompt
        assert pc.prompt_only_ids(ids, last, m, tok) == ids[: last + 1]


@pytest.mark.parametrize("style", ["qwen", "glm", "gemma"])
def test_earlier_assistant_turn_is_dropped(style):
    tok = CharTok(style)
    m = pc.turn_marks(tok)
    msgs = [
        *U,
        {"role": "assistant", "content": "Paris, of course."},
        {"role": "user", "content": "And Spain?"},
    ]
    ids = chat(tok, msgs)
    last = len(ids) - len(m.gen) - 3  # inside the last user turn
    out = pc.prompt_only_ids(ids, last, m, tok)
    text = tok.decode(out)
    assert "Paris" not in text and "France" in text
    assert text.endswith(tok.decode(ids[last - 2 : last + 1]))
    assert pc.reply_tokens(out, m, tok) == 0
    with pytest.raises(AssertionError, match="reply tokens"):
        pc.assert_no_reply(ids[: last + 1], m, tok)


def test_no_template_fails_loudly():
    with pytest.raises(SystemExit, match="no chat template"):
        pc.turn_marks(CharTok("qwen", template=False))


def test_template_that_rewrites_history_fails_loudly():
    tok = CharTok("qwen")
    orig = tok.render
    tok.render = lambda msgs, gen: ("SYS\n" if len(msgs) > 1 else "") + orig(msgs, gen)
    with pytest.raises(SystemExit, match="prefix"):
        pc.turn_marks(tok)


def _fake_transformers(monkeypatch, tok):
    mod = types.ModuleType("transformers")
    mod.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda *_a, **_k: tok)
    monkeypatch.setitem(sys.modules, "transformers", mod)


def test_build_regression_reply_free(tmp_path, monkeypatch):
    """The families whose last read site lies inside a prefilled / dictated reply (basic_readout's
    implicit items, directed_modulation, multi-concept DM) build reply-free prefixes; a family read
    in the prompt only (association) is byte-identical to the old cut."""
    tok = CharTok("qwen")
    _fake_transformers(monkeypatch, tok)
    from wsbench import readplan
    from wsbench.produce.producer import _positions
    from wsbench.produce.render import render

    fams = [
        "basic_readout",
        "directed_modulation",
        "multi_concept_directed_modulation",
        "association",
    ]
    po.build(tmp_path, fams, limit=4)
    rows = [json.loads(x) for x in (tmp_path / "prompts.jsonl").read_text().splitlines()]
    assert {r["prompt_cut"] for r in rows} == {po.PROMPT_CUT}
    m = pc.turn_marks(tok)
    n_cut = 0
    for r in rows:
        ids = tok.encode(r["prefix"])
        assert pc.reply_tokens(ids, m, tok) == 0, r["id"]
        spec = next(s for s in readplan.plan(r["family"]) if s.id == r["id"])
        rr = render(spec, tok)
        last = po.last_site(_positions(spec.positions, rr.tokens), len(rr)) % len(rr)
        old = tok.decode(rr.ids[: last + 1])
        if r["family"] == "association":
            assert r["prefix"] == old
        elif r["prefix"] != old:
            n_cut += 1
            assert r["prefix"].endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert n_cut >= 8  # every DM / MCDM row and the implicit basic_readout rows in scope


def test_stale_prompts_are_refused(tmp_path):
    (tmp_path / "prompts.jsonl").write_text(
        json.dumps({"family": "f", "id": "a", "prefix": "x"}) + "\n"
    )
    with pytest.raises(SystemExit, match="rebuild"):
        po.write_readouts(tmp_path)
