"""Plan 0060 D10: association, conjunctive_association and role_bound_association are gated in
the render they are READ in (``readplan`` ``chat_summarize``): no system turn, the scene + the
summarize suffix as the one user turn; the leak screen is the subject's own summary, and a
question leg is a follow-up after that summary whose render keeps the read render as an exact
prefix. Fake Qwen-like and Gemma-like templates; no model, no network."""

import pytest

from wsbench.adapt import families as fams
from wsbench.adapt.gate import Ask, Runner, gate_record, grade_leg, run_gate
from wsbench.adapt.template import followup_tail
from wsbench.capable.questions import NAME_REFERENT
from wsbench.produce.render import render as produce_render
from wsbench.readplan import SUMMARIZE_SUFFIX, ReadSpec


class QwenTok:
    """ChatML with Qwen3's no-think generation prompt; a PAST assistant turn renders without the
    empty think block (as Qwen3's template does), so a naive re-render is not a prefix."""

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=True, **kw):
        out = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in msgs)
        if add_generation_prompt:
            out += "<|im_start|>assistant\n"
            if kw.get("enable_thinking") is False:
                out += "<think>\n\n</think>\n\n"
        return self._ids(out) if tokenize else out

    def _ids(self, text):  # one id per character: enough for produce.render's prefix check
        return [ord(c) for c in text]

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": self._ids(text)}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(map(chr, ids))

    def convert_ids_to_tokens(self, ids):
        return [chr(i) for i in ids]


class GemmaTok(QwenTok):
    """Gemma: <bos>, user/model turns, no system role of its own, no think block."""

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=True, **kw):
        role = {"user": "user", "assistant": "model"}
        out = "<bos>" + "".join(
            f"<start_of_turn>{role[m['role']]}\n{m['content']}<end_of_turn>\n" for m in msgs
        )
        if add_generation_prompt:
            out += "<start_of_turn>model\n"
        return self._ids(out) if tokenize else out


class FakeRunner(Runner):
    """``Runner.render`` with a fake template; answers come from ``answer(prompt)`` and every
    rendered prompt is logged."""

    def __init__(self, tok, answer):
        self.tok, self.answer, self.seed, self.log = tok, answer, 0, []

    def sample(self, asks, draws, temperature, top_p=1.0):
        out = []
        for a in asks:
            p = self.render(a)
            self.log.append(("sample", a.leg, p))
            out.append((self.answer(p, a), [self.answer(p, a)] * draws))
        return out

    def generate(self, prompts, temperature, max_tokens, seed, top_p=1.0, stop_token_ids=None):
        assert temperature == 0.0
        self.log.extend(("generate", None, p) for p in prompts)
        return [{"text": self.answer(p, None), "ids": []} for p in prompts]


SCENE = "Sparks flew from the anvil as he shaped the horseshoe."
ASSOC = {"name": "a1", "prompt": SCENE, "target": "blacksmith", "aliases": ["smith"]}
ROLE = {
    "id": "r1",
    "stimulus": "Dylan bolted with the cash; Marcus, badge on, chased him.",
    "asked_question": "In one sentence, state who did what to whom in this scene.",
    "gold_label": "the police officer chased the thief",
    "names": {"a": "Marcus", "b": "Dylan"},
    "role_a": "police officer",
    "role_b": "thief",
}
CONJ = {"id": "c1", "stimulus": "The casseroles filled the freezer.", "gold_label": "grief"}
SUMMARY = "A man works at a forge."

READ = {  # the read render (produce.render, chat_summarize) of SCENE, per template
    "qwen": f"<|im_start|>user\n{SCENE}{SUMMARIZE_SUFFIX}<|im_end|>\n"
    "<|im_start|>assistant\n<think>\n\n</think>\n\n",
    "gemma": f"<bos><start_of_turn>user\n{SCENE}{SUMMARIZE_SUFFIX}<end_of_turn>\n"
    "<start_of_turn>model\n",
}
CLOSE_ASK = {  # what follows the reply: the template's close of it, the follow-up, the gen prompt
    "qwen": "<|im_end|>\n<|im_start|>user\n{q}<|im_end|>\n<|im_start|>assistant\n"
    "<think>\n\n</think>\n\n",
    "gemma": "<end_of_turn>\n<start_of_turn>user\n{q}<end_of_turn>\n<start_of_turn>model\n",
}
TOKS = {"qwen": QwenTok, "gemma": GemmaTok}


def _read_render_text(tok, text):
    spec = ReadSpec("association", "a1", "chat_summarize", {"kind": "all"}, [], text=text,
                    suffix=SUMMARIZE_SUFFIX)  # fmt: skip
    return tok.decode(produce_render(spec, tok).ids)


@pytest.mark.parametrize("name", ["qwen", "gemma"])
def test_association_followup_render_extends_the_read_render(name):
    tok = TOKS[name]()
    assert _read_render_text(tok, SCENE) == READ[name]  # the fixture IS the lens's render
    runner = FakeRunner(tok, lambda p, a: SUMMARY if a is None else "Blacksmith")
    (ask,) = fams.get("association").asks(ASSOC)
    assert (
        ask.leg == "surface"
        and ask.followup
        and not any(m["role"] == "system" for m in ask.messages)
    )  # the read has no system turn, so neither does the gate
    rec = run_gate(runner, [ask], draws=10, threshold=0.8)["a1"]
    q = NAME_REFERENT.replace("This text", "The story you just summarized")
    assert q == fams.REFERENT_FOLLOWUP and q.startswith("The story you just summarized describes")
    # the reply is generated greedily on the exact read render, then the follow-up is asked
    assert runner.log[0] == ("generate", None, READ[name])
    assert runner.log[1] == (
        "sample",
        "surface",
        READ[name] + SUMMARY + CLOSE_ASK[name].format(q=q),
    )
    assert rec["pass"] and rec["legs"]["surface"]["reply"] == SUMMARY


def test_qwen_followup_keeps_the_read_prefix_a_naive_rerender_would_drop():
    tok = QwenTok()
    msgs = fams.read_turn(ASSOC, "prompt")
    conv = [*msgs, {"role": "assistant", "content": SUMMARY}, {"role": "user", "content": "Q"}]
    naive = tok.apply_chat_template(conv, enable_thinking=False)
    assert not naive.startswith(READ["qwen"])  # the past turn lost its think block
    ask = fams.get("association").asks(ASSOC)[0]
    from dataclasses import replace

    got = FakeRunner(tok, None).render(replace(ask, reply=SUMMARY))
    assert got.startswith(READ["qwen"])
    assert followup_tail(tok, msgs, ask.followup).startswith("<|im_end|>\n<|im_start|>user\n")


def test_followup_needs_its_reply():
    ask = fams.get("association").asks(ASSOC)[0]
    with pytest.raises(ValueError, match="needs its reply"):
        FakeRunner(QwenTok(), None).render(ask)


@pytest.mark.parametrize("name", ["qwen", "gemma"])
def test_role_bound_asks_after_the_summary_the_leak_screen_judged(name):
    tok = TOKS[name]()
    replies = {"leak": "Marcus chased Dylan down the street.", "surface": "Marcus chased Dylan."}
    runner = FakeRunner(tok, lambda p, a: replies[a.leg])
    asks = fams.get("role_bound_association").asks(ROLE)
    assert [a.leg for a in asks] == ["surface", "leak"]
    leak = asks[1]
    assert leak.messages == ({"role": "user", "content": ROLE["stimulus"] + SUMMARIZE_SUFFIX},)
    assert leak.prompt is None and leak.max_tokens == fams.SUMMARY_TOKENS

    def llm(ask, texts):  # "does it state the gold binding"
        return [ask.leg == "surface" or "officer" in t for t in texts]

    rec = run_gate(runner, asks, draws=10, threshold=0.8, llm=llm)["r1"]
    read = _read_render_text(tok, ROLE["stimulus"])
    # one pass on the read render (the leak screen), then the follow-up after ITS greedy summary;
    # no separate generation
    assert [k for k, _l, _p in runner.log] == ["sample", "sample"]
    assert runner.log[0] == ("sample", "leak", read)
    want = read + replies["leak"] + CLOSE_ASK[name].format(q=ROLE["asked_question"])
    assert runner.log[1] == ("sample", "surface", want)
    assert rec["pass"]
    assert rec["legs"]["surface"]["reply"] == replies["leak"]
    assert "reply" not in rec["legs"]["leak"]  # only follow-up legs carry one


def test_role_bound_leak_screen_reads_the_summary():
    leak = fams.get("role_bound_association").asks(ROLE)[1]
    llm = lambda _a, ts: ["officer" in t for t in ts]  # noqa: E731
    stated = "The police officer chased the thief."
    assert not grade_leg(leak, stated, ["fine"] * 10, 0.8, llm).passed  # greedy states it
    assert grade_leg(leak, "fine", [stated] * 10, 0.8, llm).passed  # max_rate 1.0: samples may


@pytest.mark.parametrize("name", ["qwen", "gemma"])
def test_conjunctive_leak_screen_on_the_subject_summary(name):
    tok = TOKS[name]()
    (leak,) = fams.get("conjunctive_association").asks(CONJ)
    runner = FakeRunner(tok, lambda p, a: "She keeps the house in order.")
    rec = run_gate(runner, [leak], draws=10, llm=lambda a, ts: ["grief" in t for t in ts])["c1"]
    assert runner.log == [("sample", "leak", _read_render_text(tok, CONJ["stimulus"]))]
    assert rec["pass"]
    # max_rate 0: one sampled summary stating the composed state fails the item
    llm = lambda _a, ts: ["grief" in t for t in ts]  # noqa: E731
    assert not grade_leg(leak, "ok", ["her grief"] + ["ok"] * 9, 0.8, llm).passed


def test_association_pass_rule_is_greedy_and_eight_of_ten():
    from dataclasses import replace

    ask = replace(fams.get("association").asks(ASSOC)[0], reply=SUMMARY)
    assert grade_leg(ask, "Blacksmith", ["blacksmith"] * 8 + ["farrier"] * 2, 0.8).passed
    assert not grade_leg(ask, "Blacksmith", ["blacksmith"] * 7 + ["farrier"] * 3, 0.8).passed
    assert not grade_leg(ask, "Farrier", ["blacksmith"] * 10, 0.8).passed  # greedy must be right
    # the grader sees the whole conversation: scene + summarize, the reply, the follow-up
    q = ask.question()
    assert q.startswith(f"[user] {SCENE}{SUMMARIZE_SUFFIX}") and f"[assistant] {SUMMARY}" in q
    assert q.endswith(fams.REFERENT_FOLLOWUP)


def test_regrade_restores_the_reply_and_refuses_pre_d10_records():
    from wsbench.adapt.run import _regrade_asks, _regrade_row

    runner = FakeRunner(QwenTok(), lambda p, a: SUMMARY if a is None else "smith")
    rec = run_gate(runner, fams.get("association").asks(ASSOC), draws=10)["a1"]
    row = {"id": "a1", "item": ASSOC, "gate": rec, "sampling": {"threshold": 0.8}}
    assert _regrade_asks("association", row)["surface"].reply == SUMMARY
    assert _regrade_row("association", row, None)["gate"]["pass"]
    old_leg = dict(rec["legs"]["surface"], question=f"[system] x\n\n[user] y\n\n{SCENE}")
    old_leg.pop("reply")
    old = dict(row, gate=dict(rec, legs={"surface": old_leg}))
    assert _regrade_asks("association", old) is None
    with pytest.raises(ValueError, match="regate"):
        _regrade_row("association", old, None)


def test_other_families_keep_their_asks_and_record_shape():
    item = {"id": "b", "code": "x = 1", "verified": "ok", "src": "buggy", "cause": "off by one"}
    asks = fams.get("buggy_code").asks(item)
    assert asks[1].prompt == "x = 1" and not asks[1].followup  # still the plain continuation
    leg = grade_leg(Ask("x", "surface", ("Fe",)), "Fe", ["Fe"] * 10, 0.8)
    assert "reply" not in gate_record({"surface": leg})["legs"]["surface"]
    assert not fams.get("multihop").gate_render
